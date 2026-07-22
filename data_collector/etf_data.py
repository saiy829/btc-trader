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
v6 更新（2026-07-13 任务卡7M-2，稳定视图补全累计口径）：
- 返回dict新增 stable_week_m：截至 stable_date 的本周累计（逐日行区间求和，
  无需state持久化，每次fetch重算）；披露窗口内量化评分的周分量不再混入
  当日阶段值。total_week/total_month 保持实时口径供简报正文展示。
  月累计不参与评分，不加 stable_month_m
v7 更新（2026-07-22 稳定视图无状态化修复）：
- stable_flow_m / stable_date 改为每次从 parsed 逐日行直接推导（确认日=
  非披露窗口时parsed[-1]、披露窗口时parsed[-2]），不再依赖 etf_state.json
  持久化。修复原设计"稳定键只在窗口外写、窗口内只读，一旦state被并发写
  损坏就到12:00才自愈"的单点故障（实测曾致信号引擎在披露窗口内持续跳过
  3.5小时）。正常日推导值与原state存值完全一致，不改变评分行为。
- _save_state 改原子写（pid临时文件+os.replace），消除并发半截写损坏。
- state 现仅承载 last_reported_date（"今日首发"判断），不再是评分单点依赖。
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
    # 2026-07-22 修复：原来直接 open(STATE_FILE,"w") 是非原子写，而本文件被
    # signal_engine/各简报/weekly 多个进程并发调用，一次半截写会让并发的
    # _load_state() 读到无效JSON→返回{}，进而丢键。改为"写pid私有临时文件
    # + os.replace 原子替换"：任何读者要么看到旧的完整文件、要么看到新的
    # 完整文件，永不出现半截状态。pid 后缀确保多进程各写各的临时文件，
    # 不会互相截断（os.replace 在同一文件系统上是原子 rename）。
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = f"{STATE_FILE}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
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

    # 今日首次更新判断（跨 session 状态追踪）。state 现在只承载
    # last_reported_date 这一个用途（"今日首发"标记），稳定视图不再依赖它。
    state = _load_state()
    last_reported = state.get("last_reported_date", "")
    date_iso = latest_date.isoformat()
    newly_published = (date_iso > last_reported) if last_reported else True
    if newly_published:
        state["last_reported_date"] = date_iso
        _save_state(state)   # 原子写（见 _save_state）

    # ── ETF稳定视图（7M/7M-2；2026-07-22 重构为无状态推导）─────────────
    # "最近一个已确认完整交易日" = parsed 里除去"当前仍在披露窗口内的当日"
    # 之后最新的那一行：
    #   is_settling=True  → 当日(parsed[-1])是阶段值，确认日取 parsed[-2]
    #   is_settling=False → 当日本身已确认，确认日取 parsed[-1]
    # parsed 是按日期升序的完整历史逐日行，parsed[-2] 天然就是上一交易日
    # （周末间隔也正确，如周一的上一确认日=上周五）。
    #
    # 【为何改掉原设计】原来稳定值存在 data/etf_state.json，且"只在窗口外写、
    # 窗口内只读"——一旦该文件在窗口内被并发写损坏（非原子写），稳定键蒸发
    # 后要等到12:00窗口外才自愈，期间 ETF 维度→missing→引擎宁缺勿假整轮跳过
    # （2026-07-22 实测：引擎在窗口内静默了3.5小时）。改为每次从 parsed 直接
    # 推导后，稳定值不再依赖任何持久化状态，天然免疫该故障；正常健康日推导值
    # 与原 state 存的值完全一致（都是"上一确认日"），不改变评分行为。
    # 确认日不可得（parsed 只有1行且在窗口内）→ stable_* 为 None，评分侧仍按
    # "数据缺失"处理（简报记0分/引擎跳过本轮，宁缺勿假）。
    if not is_settling:
        _confirmed = parsed[-1]
    elif len(parsed) >= 2:
        _confirmed = parsed[-2]
    else:
        _confirmed = None
    if _confirmed is not None:
        stable_flow_m = _confirmed["flow"].get("Total", 0)
        stable_date   = _confirmed["date"].isoformat()
    else:
        stable_flow_m = None
        stable_date   = None

    week_mon = latest_date - timedelta(days=latest_date.weekday())
    total_week = sum(
        r["flow"].get("Total", 0) for r in parsed if r["date"] >= week_mon
    )
    total_month = sum(
        r["flow"].get("Total", 0) for r in parsed
        if r["date"].year == latest_date.year and r["date"].month == latest_date.month
    )

    # ── 7M-2 稳定周累计：量化评分的周分量也纳入稳定口径 ──
    # 实现选任务卡的优先级①"逐日行按 日期<=stable_date 求和"——parsed 本身
    # 就是逐日行列表，直接区间求和可得，无需②"实时累计-当日阶段值"等价算法。
    # is_settling=False：区间与 total_week 完全相同（week_mon..latest），数值恒等；
    # is_settling=True：只加到 stable_date 为止，当日披露窗口内的阶段值不进入。
    # 周初边界（显式处理）：settling 时 stable_date 可能属于上一周（如周二
    # 早晨，本周尚无已确认交易日），区间 week_mon..stable_date 为空 → 和为0；
    # 逐日区间求和天然不会为负，0 就是"本周稳定口径累计"的正确取值而非异常。
    # stable_date 缺失（首次部署无state）→ None，评分侧按该分量缺数处理。
    # 月累计不参与评分（_score_etf 只用单日+周），不加 stable_month_m。
    if not is_settling:
        stable_week_m = total_week
    elif stable_date:
        try:
            _stable_d = datetime.strptime(stable_date, "%Y-%m-%d").date()
            stable_week_m = sum(
                r["flow"].get("Total", 0) for r in parsed
                if week_mon <= r["date"] <= _stable_d
            )
        except Exception:
            stable_week_m = None
    else:
        stable_week_m = None

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
        "stable_week_m":     stable_week_m,   # 7M-2：截至stable_date的本周累计(百万美元)
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
