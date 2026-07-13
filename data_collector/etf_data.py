"""
BTC 现货 ETF 资金流量数据采集  v4
双源：Farside Investors + SoSoValue（官方API，需 SOSOVALUE_API_KEY）
v3 更新：
- SoSoValue 改用真实已验证的接口域名 openapi.sosovalue.com（此前猜测的域名是错的）
- 双源交叉验证：两源都有数据时比对最新公共日期，差距过大会记录警告
- 优先使用日期更新的源（通常 SoSoValue 发布更快）
- 新增"今日首次更新"状态追踪：用本地状态文件记录已播报过的最新日期，
  跨 早盘/欧盘/美盘 session 自动判断是否为"新到数据"，避免重复强调旧数据，
  也确保数据一旦发布，最近的下一次简报会明确标注"今日首次更新"
v4 更新：
- 放弃按"到账几只ETF"判断完整性（不同数据源品种清单不一致，coinglass 12家、
  SoSoValue 13家、Farside 12家互相对不上，按固定清单数数会经常误判）
- 改为按"当日更新时间窗口"判断：北京时间 04:00（美股收盘）-12:00（各发行商基本披露完）
  期间的当日最新数据标记为 is_settling=True（阶段性数值），12:00后视为已稳定
- 新增字段 is_settling / completeness_note，供简报 prompt 和仪表板直接展示
v5 更新（2026-07-13 任务卡7M，ETF稳定视图）：
- 返回dict新增 stable_flow_m / stable_date：最近一个已确认完整交易日
  （is_settling=False 时确认）的净流量与日期，靠 state 文件跨进程持久化；
  所有量化评分（signal_score/signal_engine）只用这两个字段，披露窗口内的
  阶段性数值只供简报正文展示
- 修正 state 文件整体覆盖写问题（原 newly_published 写入会清掉其他键）
"""
import os
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from utils.helpers import setup_logger

logger = setup_logger()

ETF_DISPLAY = {
    "IBIT": "IBIT(BlackRock)",
    "FBTC": "FBTC(Fidelity)",
    "ARKB": "ARKB(ARK)",
    "BITB": "BITB(Bitwise)",
    "BTCO": "BTCO(Invesco)",
    "HODL": "HODL(VanEck)",
    "GBTC": "GBTC(Grayscale)",
    "BTC":  "BTC(GrayMini)",
    "EZBC": "EZBC(Franklin)",
    "BRRR": "BRRR(Valkyrie)",
}
TOP_ETFS = ["IBIT", "FBTC", "ARKB", "BITB", "GBTC"]

FARSIDE_URLS = [
    "https://farside.co.uk/btc/",
    "https://farside.co.uk/bitcoin-etf-flow-all-data/",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://farside.co.uk/",
}

SKIP_ROW_LABELS = {"total", "average", "maximum", "minimum", "fee"}

STATE_FILE = "/opt/btc-trader/data/etf_state.json"


# ── 状态追踪（用于"今日首次更新"判断）─────────────────────────
def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.warning(f"ETF 状态文件写入失败: {e}")


def _parse_val(text: str) -> float:
    t = text.strip().replace(",", "").replace("$", "").replace("\xa0", "").replace(" ", "")
    if not t or t in ("-", "—", "–", "", "N/A"):
        return 0.0
    negative = (t.startswith("(") and t.endswith(")")) or t.startswith("-")
    t = t.strip("()-").strip()
    try:
        return -float(t) if negative else float(t)
    except Exception:
        return 0.0


def _fmt_cny(val_million: float) -> str:
    """精确中文万/亿格式化，Python预算好，不依赖AI二次心算"""
    val_usd = val_million * 1_000_000
    abs_usd = abs(val_usd)
    sign = "+" if val_usd >= 0 else "-"
    if abs_usd >= 100_000_000:
        return f"{sign}{abs_usd/100_000_000:.2f}亿美元"
    elif abs_usd >= 10_000:
        return f"{sign}{abs_usd/10_000:.0f}万美元"
    else:
        return f"{sign}{abs_usd:.0f}美元"


# ── Farside（已验证可用，主源之一）──────────────────────────────
def _parse_farside_table(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    target = None
    for tbl in soup.find_all("table"):
        txt = tbl.get_text()
        if "IBIT" in txt and ("Total" in txt or "total" in txt):
            target = tbl
            break
    if not target:
        return []

    rows = target.find_all("tr")
    if len(rows) < 2:
        return []

    col_cells = rows[0].find_all(["th", "td"])
    col_names = [c.get_text(strip=True) for c in col_cells]

    parsed = []
    for row in rows[1:]:
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        date_txt = cells[0].get_text(strip=True)
        if date_txt.strip().lower() in SKIP_ROW_LABELS:
            continue

        dt = None
        for fmt in ["%d %b %y", "%d %b %Y", "%b %d, %Y",
                    "%Y-%m-%d", "%d/%m/%Y", "%d-%b-%y"]:
            try:
                dt = datetime.strptime(date_txt, fmt)
                break
            except Exception:
                pass
        if not dt:
            continue

        flow = {}
        raw_texts = []
        for name in list(ETF_DISPLAY.keys()):
            for i, col in enumerate(col_names):
                if name.upper() == col.upper().strip() and i < len(cells):
                    raw = cells[i].get_text(strip=True)
                    raw_texts.append(raw)
                    flow[name] = _parse_val(raw)
                    break

        non_empty = [t for t in raw_texts if t not in ("-", "—", "–", "", "N/A")]
        if not non_empty:
            logger.info(f"Farside {date_txt} 尚未发布（占位符行），跳过")
            continue

        total_val = None
        for i, col in enumerate(col_names):
            if col.upper().strip() == "TOTAL" and i < len(cells):
                total_val = _parse_val(cells[i].get_text(strip=True))
                break
        flow["Total"] = total_val if total_val else sum(flow.values())

        parsed.append({"date": dt.date(), "flow": flow})

    return sorted(parsed, key=lambda x: x["date"])


def _try_farside() -> list:
    for url in FARSIDE_URLS:
        try:
            logger.info(f"尝试 Farside URL: {url}")
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 200:
                rows = _parse_farside_table(resp.text)
                if rows:
                    logger.info(f"Farside 成功: {url}, 获取 {len(rows)} 条有效数据")
                    return rows
                else:
                    logger.warning(f"Farside {url} 返回200但未解析到有效数据")
            else:
                logger.warning(f"Farside {url} 返回 {resp.status_code}")
        except Exception as e:
            logger.warning(f"Farside {url} 失败: {e}")
    return []


# ── SoSoValue（官方API，真实域名 openapi.sosovalue.com）────────
def _try_sosovalue() -> list:
    """
    SoSoValue 官方 API（文档已100%确认）：
    https://sosovalue-1.gitbook.io/sosovalue-api-doc/2.-etf/summary-history
    GET https://openapi.sosovalue.com/openapi/v1/etfs/summary-history
    Headers: x-soso-api-key
    Params: symbol=BTC, country_code=US, limit=300
    响应：JSON数组（倒序，最新在前），total_net_inflow 字段为原始美元（非百万），
         需 / 1,000,000 转换为与 Farside 一致的"百万美元"单位口径
    """
    api_key = os.environ.get("SOSOVALUE_API_KEY", "")
    if not api_key:
        logger.info("SoSoValue API Key 未配置，跳过")
        return []

    url = "https://openapi.sosovalue.com/openapi/v1/etfs/summary-history"
    headers = {"x-soso-api-key": api_key}
    params = {"symbol": "BTC", "country_code": "US", "limit": 300}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"SoSoValue 返回 {resp.status_code}: {resp.text[:200]}")
            return []

        raw = resp.json()
        # 实际响应包了一层 {code, message, data}，而非文档示例的裸数组
        if isinstance(raw, dict):
            if raw.get("code") not in (0, "0", None):
                logger.warning(f"SoSoValue 业务码异常: {raw.get('code')} {raw.get('message')}")
                return []
            items = raw.get("data", [])
        else:
            items = raw

        if not isinstance(items, list) or not items:
            logger.warning(f"SoSoValue 返回非预期格式: {str(raw)[:200]}")
            return []

        # ★ 已确认的接口特性 ★
        # summary-history 对同一日期可能返回多条记录（实测发现：单日值 / 本周累计 / 本月累计
        # 均标注同一个date字段，无period/type区分字段）。
        # 规律：单日值的绝对值必然小于周/月累计值，取同日期分组中绝对值最小的一条即为真实单日值。
        from collections import defaultdict
        by_date = defaultdict(list)
        for item in items:
            try:
                dt = datetime.strptime(item["date"], "%Y-%m-%d").date()
                val = float(item["total_net_inflow"])
                by_date[dt].append(val)
            except Exception:
                continue

        parsed = []
        for dt, vals in by_date.items():
            daily_val = min(vals, key=lambda v: abs(v))
            if len(vals) > 1:
                logger.info(
                    f"SoSoValue {dt} 返回{len(vals)}条记录（疑似含周/月累计），"
                    f"按绝对值最小取单日值: {daily_val:,.0f} （候选值: {[round(v) for v in vals]}）"
                )
            parsed.append({"date": dt, "flow": {"Total": daily_val / 1_000_000}})

        if parsed:
            logger.info(f"SoSoValue 成功，获取 {len(parsed)} 条数据（已按日期去重）")
        return sorted(parsed, key=lambda x: x["date"])

    except Exception as e:
        logger.warning(f"SoSoValue 请求异常: {e}")
        return []


def _build_result(parsed: list, source_note: str, cross_validated: bool) -> dict:
    """从解析数据构建结果"""
    if not parsed:
        return {"has_data": False}

    latest       = parsed[-1]
    latest_date  = latest["date"]
    latest_flow  = latest["flow"]
    total_latest = latest_flow.get("Total", 0)

    today_utc = datetime.now(timezone.utc).date()
    days_old = (today_utc - latest_date).days
    freshness = "" if days_old <= 1 else f"，数据滞后{days_old}天，为最新可获得数据"

    # ── 是否仍在当日更新窗口内 ─────────────────────────────────────────
    # 规律：美股收盘 UTC+8 04:00，各发行商陆续披露，UTC+8 12:00前后基本到齐。
    # 04:00-12:00 之间，若这是"最新一期"（freshness为空=数据新鲜），说明还在陆续更新，
    # Total 是阶段性数值；12:00之后或非新鲜数据视为已稳定。
    now_bj_ = datetime.now(timezone(timedelta(hours=8)))
    is_settling = (freshness == "") and (4 <= now_bj_.hour < 12)
    if is_settling:
        completeness_note = (
            "⏳ 当日数据更新中（美股发行商陆续披露，预计北京时间12:00前后到齐），"
            "当前为阶段性数值，后续可能变化"
        )
    else:
        completeness_note = "✅ 当前为已稳定数据"

    # 今日首次更新判断（跨 session 状态追踪）
    # 7M 修正：原来这里 _save_state({"last_reported_date": ...}) 是整体覆盖写，
    # 会清掉 state 里的其他键——改为读-改-写，保留全部键（稳定视图键靠这个活着）
    state = _load_state()
    last_reported = state.get("last_reported_date", "")
    date_iso = latest_date.isoformat()
    newly_published = (date_iso > last_reported) if last_reported else True
    state_dirty = False
    if newly_published:
        state["last_reported_date"] = date_iso
        state_dirty = True

    # ── 7M ETF稳定视图：记录/读取"最近一个已确认完整交易日"的净流量 ──
    # is_settling=False 时当期值就是稳定值，落进 state；is_settling=True
    # （北京04:00-12:00披露窗口内的新鲜数据）不落，改从 state 读上一稳定值。
    # 量化评分（signal_score/signal_engine）只用 stable_* 字段，窗口内的
    # 阶段性数值只给简报正文展示用。首次部署 state 无记录时 stable_* 为
    # None，评分侧按"数据缺失"处理（简报记0分/引擎跳过本轮，宁缺勿假）。
    if not is_settling:
        if state.get("stable_flow_m") != total_latest or state.get("stable_date") != date_iso:
            state["stable_flow_m"] = total_latest
            state["stable_date"]   = date_iso
            state_dirty = True
        stable_flow_m = total_latest
        stable_date   = date_iso
    else:
        stable_flow_m = state.get("stable_flow_m")
        stable_date   = state.get("stable_date")

    if state_dirty:
        _save_state(state)

    week_mon = latest_date - timedelta(days=latest_date.weekday())
    total_week = sum(
        r["flow"].get("Total", 0) for r in parsed if r["date"] >= week_mon
    )
    total_month = sum(
        r["flow"].get("Total", 0) for r in parsed
        if r["date"].year == latest_date.year and r["date"].month == latest_date.month
    )

    streak, streak_dir = 0, ("净流入" if total_latest >= 0 else "净流出")
    for row in reversed(parsed):
        val = row["flow"].get("Total", 0)
        if (val >= 0 and total_latest >= 0) or (val < 0 and total_latest < 0):
            streak += 1
        else:
            break

    top_items = sorted(
        [(k, latest_flow.get(k, 0)) for k in TOP_ETFS if k in latest_flow],
        key=lambda x: abs(x[1]), reverse=True
    )[:3]
    top3_lines = [
        f"    {ETF_DISPLAY.get(k,k)}: {_fmt_cny(v)}"
        for k, v in top_items if v != 0
    ]

    if total_latest > 500:    signal = "重大机构买入（>$500M），强烈利多"
    elif total_latest > 100:  signal = "机构持续配置，多头基本面支撑"
    elif total_latest > 0:    signal = "小幅净流入，中性偏多"
    elif total_latest > -100: signal = "小幅净流出，中性偏空"
    elif total_latest > -500: signal = "机构减仓，做多需谨慎"
    else:                     signal = "重大机构抛售（>$500M），强烈利空"

    logger.info(
        f"ETF | 来源:{source_note} | 日期:{latest_date}{freshness} | "
        f"净流量:{_fmt_cny(total_latest)} | 连续{streak}天{streak_dir} | "
        f"今日首发:{newly_published} | 更新窗口:{is_settling}"
    )

    return {
        "has_data":          True,
        "date":              latest_date.isoformat(),
        "freshness":         freshness,
        "source":            source_note,
        "cross_validated":   cross_validated,
        "newly_published":   newly_published,
        "is_settling":       is_settling,
        "completeness_note": completeness_note,
        "total_yest":        total_latest,
        "total_week":        total_week,
        "total_month":       total_month,
        "stable_flow_m":     stable_flow_m,   # 7M：最近已确认完整交易日净流(百万美元)
        "stable_date":       stable_date,     # 7M：对应日期字符串（可能为None）
        "streak_days":       streak,
        "streak_dir":        streak_dir,
        "signal":            signal,
        "top3_lines":        "\n".join(top3_lines),
        "yest_str":          _fmt_cny(total_latest),
        "week_str":          _fmt_cny(total_week),
        "month_str":         _fmt_cny(total_month),
    }


def fetch_etf_flows() -> dict:
    """主入口：双源获取 + 交叉验证 + 异常防护（差距过大时信任更稳定的Farside）"""
    farside_parsed = _try_farside()
    soso_parsed    = _try_sosovalue()

    cross_validated = False
    source_note = ""
    parsed = []

    if farside_parsed and soso_parsed:
        f_dates = {r["date"]: r["flow"].get("Total", 0) for r in farside_parsed}
        s_dates = {r["date"]: r["flow"].get("Total", 0) for r in soso_parsed}
        common_dates = sorted(set(f_dates) & set(s_dates), reverse=True)

        if common_dates:
            d = common_dates[0]
            f_val, s_val = f_dates[d], s_dates[d]
            diff = abs(f_val - s_val)
            rel_base = max(abs(f_val), abs(s_val), 1)
            rel_diff = diff / rel_base

            if rel_diff < 0.08 or diff < 15:
                # 双源一致，验证通过，取日期更新的源
                cross_validated = True
                logger.info(
                    f"ETF 双源交叉验证通过 ({d}): "
                    f"Farside={f_val:.1f}M  SoSoValue={s_val:.1f}M"
                )
                f_latest = max(f_dates)
                s_latest = max(s_dates)
                if s_latest >= f_latest:
                    parsed, source_note = soso_parsed, "SoSoValue+Farside验证"
                else:
                    parsed, source_note = farside_parsed, "Farside+SoSoValue验证"
            else:
                # ★ 异常防护：差距过大时不信任 SoSoValue，优先用已长期验证稳定的 Farside ★
                logger.warning(
                    f"ETF 双源数据分歧过大 ({d}): "
                    f"Farside={f_val:.1f}M  SoSoValue={s_val:.1f}M  "
                    f"差距{rel_diff*100:.0f}%，判定SoSoValue当日数值异常，自动改用Farside"
                )
                parsed = farside_parsed
                source_note = "Farside（SoSoValue当日数值异常已自动屏蔽）"
        else:
            f_latest = max(f_dates) if f_dates else None
            s_latest = max(s_dates) if s_dates else None
            if s_latest and (not f_latest or s_latest >= f_latest):
                parsed, source_note = soso_parsed, "SoSoValue（无公共日期可交叉验证）"
            else:
                parsed, source_note = farside_parsed, "Farside（无公共日期可交叉验证）"

    elif soso_parsed:
        parsed, source_note = soso_parsed, "SoSoValue（Farside暂不可用）"
    elif farside_parsed:
        parsed, source_note = farside_parsed, "Farside（SoSoValue未配置或暂不可用）"
    else:
        logger.error("ETF 数据全部来源均不可用")
        return {"has_data": False}

    return _build_result(parsed, source_note, cross_validated)
