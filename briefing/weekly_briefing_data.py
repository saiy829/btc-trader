"""
briefing/weekly_briefing_data.py — 周报数据聚合模块（Phase 7P）
==============================================================
对外接口：get_weekly_context() -> str（W1-W10 权威数据块，供 weekly prompt 整体注入）

周边界定义（写死约定）：
  WEEK_END   = 本周一 北京时间 08:00（周一当天运行=今晨08:00；
               其他日手动测试=最近一个周一08:00，即统计"上一个完整周"）
  WEEK_START = WEEK_END - 7天
  与本项目日边界 08:00 约定对齐；Binance fapi 日K的 UTC 00:00 边界
  = 北京 08:00，天然对齐，无需任何偏移换算。

查询口径：
  epoch 表（binance_oi/binance_funding/binance_structure/binance_ls_top/
  gate_liquidations/signal_scores）用秒区间 [week_start_ts, week_end_ts)；
  ISO 表（atas_bars/atas_large_trades，格式 yyyy-MM-ddTHH:mm:ss+08:00）
  用带 +08:00 的 ISO 字符串区间；engine_signals.created_at 是北京时间
  "YYYY-MM-DD HH:MM:SS" 文本，用同格式字符串区间。

容错铁律：每个数据块独立 try/except，任一源失败输出"[该块数据不足：原因]"，
绝不让单块失败拖垮整个模块，绝不用编造值填充（宁缺勿假）。

第0步实情记录（2026-07-13 核实）：
  - daily_summary 表不存在；OKX 清算从未落库（binance_liq 0行，
    liq-monitor 只发TG）→ W7 清算统计只用 gate_liquidations
  - 三因子市场状态为实时计算、无历史落库（binance_structure.note 是
    象限描述文本，非三因子标签）→ W4 相应子项标注"该源无历史"
  - 现有代码无 F&G 采集点 → 本模块自行调 alternative.me /fng/?limit=8
  - get_yesterday_volume_profile() 昨日窗口硬编码不可参数化
    → W5 按同一套"typical分桶+POC双向70%扩展"算法自实现（15m粒度）
"""
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

DB_PATH = "/opt/btc-trader/btc_history.db"
BJT     = timezone(timedelta(hours=8))
FAPI    = "https://fapi.binance.com"


# ── 基础工具 ─────────────────────────────────────────────────────────────

def _q(sql, params=()):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _week_bounds():
    now_bj = datetime.now(BJT)
    week_end = (now_bj - timedelta(days=now_bj.weekday())).replace(
        hour=8, minute=0, second=0, microsecond=0)
    week_start = week_end - timedelta(days=7)
    return week_start, week_end


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _fetch_klines(interval, start_bj, end_bj, limit=1500):
    resp = requests.get(
        f"{FAPI}/fapi/v1/klines",
        params={"symbol": "BTCUSDT", "interval": interval,
                "startTime": int(start_bj.timestamp() * 1000),
                "endTime":   int(end_bj.timestamp() * 1000) - 1,
                "limit": limit},
        timeout=15)
    resp.raise_for_status()
    return resp.json()


def _safe(fn, name):
    try:
        return fn()
    except Exception as e:
        return f"[{name}数据不足：{type(e).__name__}: {e}]"


# ── W1 上周行情 ──────────────────────────────────────────────────────────

def _w1(daily, prev_close):
    # daily: 上周7根日K；prev_close: 前周最后一根日K的收盘
    if not daily:
        raise RuntimeError("上周日K为空")
    w_open  = float(daily[0][1])
    w_close = float(daily[-1][4])
    w_high  = max(float(k[2]) for k in daily)
    w_low   = min(float(k[3]) for k in daily)
    rng_pct = (w_high - w_low) / w_open * 100
    chg_pct = (w_close - w_open) / w_open * 100
    lines = ["=== W1 上周行情 ===",
             f"周开 ${w_open:,.0f} | 周高 ${w_high:,.0f} | 周低 ${w_low:,.0f} | 周收 ${w_close:,.0f}",
             f"周涨跌 {chg_pct:+.2f}% | 周振幅 {rng_pct:.2f}%"]
    if prev_close:
        vs_prev = (w_close - prev_close) / prev_close * 100
        lines.append(f"与前周收盘（${prev_close:,.0f}）对比：{vs_prev:+.2f}%")
    lines.append("逐日涨跌（日期为北京时间日界08:00起，涨跌为环比前日收盘）：")
    pc = prev_close
    for k in daily:
        d = datetime.fromtimestamp(k[0] / 1000, BJT).strftime("%m-%d %a")
        close = float(k[4])
        chg = f"{(close - pc) / pc * 100:+.2f}%" if pc else "-"
        lines.append(f"  {d} | 收 ${close:,.0f} | {chg}")
        pc = close
    return "\n".join(lines)


# ── W2 ETF周报 ───────────────────────────────────────────────────────────

def _w2_etf(week_start, week_end):
    # 第0步实情：fetch_etf_flows() 只返回聚合dict、不含逐日行；逐日表用
    # etf_data 的内部解析函数等价获得（不改 etf_data.py 本身）。
    # Farside 优先（有逐品种可算主力贡献），SoSoValue 兜底（仅Total）。
    from data_collector.etf_data import _try_farside, _try_sosovalue, fetch_etf_flows
    parsed = _try_farside()
    src = "Farside"
    if not parsed:
        parsed = _try_sosovalue()
        src = "SoSoValue（无逐品种，主力贡献不可得）"
    if not parsed:
        raise RuntimeError("双源均不可用")
    ws_d, we_d = week_start.date(), week_end.date()
    rows = [r for r in parsed if ws_d <= r["date"] < we_d]
    lines = ["=== W2 ETF资金流周报（上周美股交易日；周一发布时天然完整已确认，无待结算日）===",
             f"数据源：{src}",
             "逐日（日期 | 净流(百万美元) | 主力贡献）："]
    week_sum = 0.0
    for r in rows:
        flow = r["flow"]
        total = flow.get("Total", 0)
        week_sum += total
        contrib = sorted(((k, v) for k, v in flow.items() if k != "Total" and v),
                         key=lambda x: -abs(x[1]))[:2]
        c_str = " ".join(f"{k}{v:+.0f}M" for k, v in contrib) or "-"
        lines.append(f"  {r['date']} | {total:+.1f}M | {c_str}")
    lines.append(f"上周合计：{week_sum:+.1f}M（{len(rows)}个交易日）")
    try:
        agg = fetch_etf_flows()
        if agg.get("has_data"):
            lines.append(f"月累计：{agg.get('month_str','-')} | 连续状态：已连续 "
                         f"{agg.get('streak_days','-')} 天{agg.get('streak_dir','-')}")
    except Exception as e:
        lines.append(f"[月累计/连续状态获取失败：{e}]")
    return "\n".join(lines)


# ── W3 衍生品周报 ────────────────────────────────────────────────────────

def _w3_deriv(ws_ts, we_ts, daily, prev_daily, cur_px, extras):
    lines = ["=== W3 衍生品周报 ==="]
    oi = _q("SELECT ts, oi_usd FROM binance_oi WHERE ts>=? AND ts<? ORDER BY ts", (ws_ts, we_ts))
    if oi:
        o0, o1 = oi[0]["oi_usd"], oi[-1]["oi_usd"]
        omax = max(r["oi_usd"] for r in oi); omin = min(r["oi_usd"] for r in oi)
        lines.append(f"OI（USD）：周首 {o0/1e9:.2f}B → 周末 {o1/1e9:.2f}B（{(o1-o0)/o0*100:+.2f}%）"
                     f" | 周内最高 {omax/1e9:.2f}B / 最低 {omin/1e9:.2f}B")
    else:
        lines.append("[OI周数据不足]")
    fr = _q("SELECT ts, rate FROM binance_funding WHERE ts>=? AND ts<? ORDER BY ts", (ws_ts, we_ts))
    if fr:
        rates = [r["rate"] for r in fr]
        mean_r = sum(rates) / len(rates)
        # 结算时段口径：rate为5分钟采样快照，按8小时资金费结算窗口分组
        # （epoch//28800，即UTC 00/08/16边界），组内任一采样点
        # |rate|>0.05%（小数0.0005，binance_funding.rate存的是小数）记1个超阈时段
        groups = defaultdict(list)
        for r in fr:
            groups[r["ts"] // 28800].append(abs(r["rate"]))
        hot = sum(1 for g in groups.values() if max(g) > 0.0005)
        lines.append(f"Funding：周均 {mean_r*100:+.4f}% | 周最高 {max(rates)*100:+.4f}% / "
                     f"最低 {min(rates)*100:+.4f}% | |rate|>0.05%的结算时段 {hot}/{len(groups)}个")
    else:
        lines.append("[Funding周数据不足]")
    try:
        from data_collector.multi_funding import collect_multi_funding
        mf = collect_multi_funding()
        if mf and mf.get("exchanges"):
            pairs = " ".join(f"{e['exchange']}:{e.get('rate_str','-')}" for e in mf["exchanges"])
            lines.append(f"当前5所费率对比：{pairs}（均值 {mf.get('avg_rate',0):+.4f}%）")
    except Exception as e:
        lines.append(f"[5所费率对比获取失败：{e}]")
    if cur_px and extras and extras.get("spot_price"):
        basis = cur_px - extras["spot_price"]
        lines.append(f"当前基差（永续-现货）：{basis:+.0f} USD")
    else:
        lines.append("[基差数据不足：现货价不可用]")
    if daily:
        wk_quote = sum(float(k[7]) for k in daily)
        line = f"周成交额（USDT计）：{wk_quote/1e9:.1f}B"
        if prev_daily:
            pv = sum(float(k[7]) for k in prev_daily)
            if pv:
                line += f"（前周 {pv/1e9:.1f}B，{(wk_quote-pv)/pv*100:+.1f}%）"
        lines.append(line)
    return "\n".join(lines)


# ── W4 市场结构周报 ──────────────────────────────────────────────────────

def _w4_structure(ws_ts, we_ts):
    lines = ["=== W4 市场结构周报 ==="]
    rows = _q("SELECT quadrant FROM binance_structure WHERE ts>=? AND ts<? AND quadrant IS NOT NULL",
              (ws_ts, we_ts))
    if rows:
        cnt = defaultdict(int)
        for r in rows:
            cnt[r["quadrant"]] += 1
        total = sum(cnt.values())
        parts = [f"{q}:{n/total*100:.1f}%" for q, n in sorted(cnt.items(), key=lambda x: -x[1])]
        checksum = sum(n / total * 100 for n in cnt.values())
        lines.append(f"象限时间占比（{total}个5分钟样本）：{' '.join(parts)}（合计自校验:{checksum:.1f}%）")
    else:
        lines.append("[象限周数据不足]")
    lines.append("每日主导三因子状态序列：[该源无历史——三因子状态为简报/引擎实时计算，"
                 "未持久化落库；binance_structure.note 是象限描述非三因子标签]")
    ls = _q("SELECT ts, ls_ratio FROM binance_ls_top WHERE ts>=? AND ts<? ORDER BY ts", (ws_ts, we_ts))
    if ls:
        v0, v1 = ls[0]["ls_ratio"], ls[-1]["ls_ratio"]
        vmax = max(r["ls_ratio"] for r in ls); vmin = min(r["ls_ratio"] for r in ls)
        lines.append(f"大户多空比：周首 {v0:.3f} → 周末 {v1:.3f} | 周内极值 {vmin:.3f}~{vmax:.3f}")
    else:
        lines.append("[大户多空比周数据不足]")
    return "\n".join(lines)


# ── W5 周Volume Profile ─────────────────────────────────────────────────

def _volume_profile(klines, bucket_size=50.0):
    # 与 data_collector/binance_data.get_yesterday_volume_profile() 同算法
    # （typical=(H+L+C)/3 分桶 + POC双向扩展70% VA规则）。该函数昨日窗口
    # 硬编码不可参数化（第0步f项核实），按7P卡在此自实现；粒度15m，
    # 7天=672根 < fapi单次1500上限，一次请求可取
    vol_by_price = defaultdict(float)
    for k in klines:
        h, l, c, v = float(k[2]), float(k[3]), float(k[4]), float(k[5])
        bucket = round(((h + l + c) / 3) / bucket_size) * bucket_size
        vol_by_price[bucket] += v
    if not vol_by_price:
        raise RuntimeError("VP分桶为空")
    total_vol = sum(vol_by_price.values())
    sorted_buckets = sorted(vol_by_price.items())
    poc_price, poc_vol = max(vol_by_price.items(), key=lambda x: x[1])
    prices = [p for p, _ in sorted_buckets]
    poc_idx = prices.index(poc_price)
    captured, lo, hi = poc_vol, poc_idx, poc_idx
    target = total_vol * 0.70
    while captured < target and (lo > 0 or hi < len(sorted_buckets) - 1):
        left  = sorted_buckets[lo - 1][1] if lo > 0 else -1
        right = sorted_buckets[hi + 1][1] if hi < len(sorted_buckets) - 1 else -1
        if left >= right:
            lo -= 1; captured += left
        else:
            hi += 1; captured += right
    return poc_price, sorted_buckets[hi][0], sorted_buckets[lo][0]   # poc, vah, val


def _w5_weekly_vp(week_start, week_end):
    k_this = _fetch_klines("15m", week_start, week_end)
    poc, vah, val = _volume_profile(k_this)
    lines = ["=== W5 周Volume Profile（15m×7天，70% VA规则与日VP同算法）===",
             f"本周 POC ${poc:,.0f} | VAH ${vah:,.0f} | VAL ${val:,.0f}"]
    try:
        k_prev = _fetch_klines("15m", week_start - timedelta(days=7), week_start)
        p_poc, p_vah, p_val = _volume_profile(k_prev)
        if poc > p_vah:
            rel = "上移（本周POC高于前周VAH，价值区上迁）"
        elif poc < p_val:
            rel = "下移（本周POC低于前周VAL，价值区下迁）"
        else:
            rel = "在内（本周POC仍处前周VA区间，价值重叠）"
        lines.append(f"前周 POC ${p_poc:,.0f} | VAH ${p_vah:,.0f} | VAL ${p_val:,.0f}")
        lines.append(f"与前周VA关系：{rel}")
    except Exception as e:
        lines.append(f"[前周VP不可得：{e}]")
    return "\n".join(lines)


# ── W6 订单流周报 ────────────────────────────────────────────────────────

def _w6_orderflow(ws_iso, we_iso):
    lines = ["=== W6 订单流周报（AtasBridge/币安永续路）==="]
    # 按"交易日"（北京日界08:00）分组而非日历日：SQLite date() 会把
    # ISO+08:00 时间戳换算成 UTC 日期，而 UTC 日期恰好等于北京08:00日界的
    # 交易日（BJ 08:00 = UTC 00:00），7天区间恰得7行（substr取日历日会得8行）
    rows = _q("""SELECT date(timestamp) AS d, SUM(delta) AS dsum
                 FROM atas_bars
                 WHERE exchange='binance' AND market_type='perp'
                   AND timestamp>=? AND timestamp<?
                 GROUP BY d ORDER BY d""", (ws_iso, we_iso))
    if rows:
        lines.append("每日Delta合计（BTC）：")
        wk = 0.0
        for r in rows:
            v = r["dsum"] or 0
            wk += v
            lines.append(f"  {r['d']} | {v:+,.1f}")
        lines.append(f"周Delta合计：{wk:+,.1f} BTC")
    else:
        lines.append("[atas_bars周数据不足]")
    lt = _q("""SELECT direction, COUNT(*) AS n, SUM(volume) AS vol
               FROM atas_large_trades
               WHERE exchange='binance' AND market_type='perp'
                 AND timestamp>=? AND timestamp<?
               GROUP BY direction""", (ws_iso, we_iso))
    if lt:
        d = {r["direction"]: r for r in lt}
        b = d.get("buy", {"n": 0, "vol": 0}); s = d.get("sell", {"n": 0, "vol": 0})
        net = (b["vol"] or 0) - (s["vol"] or 0)
        lines.append(f"大单（>=20BTC）：买 {b['n']}笔 {(b['vol'] or 0):,.0f}BTC | "
                     f"卖 {s['n']}笔 {(s['vol'] or 0):,.0f}BTC | 净{'买' if net>=0 else '卖'} {abs(net):,.0f}BTC")
        whales = _q("""SELECT timestamp, direction, volume, price
                       FROM atas_large_trades
                       WHERE exchange='binance' AND market_type='perp'
                         AND timestamp>=? AND timestamp<? AND threshold_level='whale'
                       ORDER BY volume DESC LIMIT 5""", (ws_iso, we_iso))
        if whales:
            lines.append("鲸单Top5（时间|方向|量|价）：")
            for w in whales:
                t = w["timestamp"][5:16].replace("T", " ")
                lines.append(f"  {t} | {w['direction']} | {w['volume']:,.0f}BTC | ${w['price']:,.0f}")
        else:
            lines.append("鲸单：周内无whale级事件")
    else:
        lines.append("[atas_large_trades周数据不足]")
    return "\n".join(lines)


# ── W7 清算与风险偏好 ────────────────────────────────────────────────────

def _w7_risk(ws_ts, we_ts, extras):
    lines = ["=== W7 清算与风险偏好 ==="]
    liq = _q("""SELECT direction, COUNT(*) AS n, SUM(usd_value) AS usd
                FROM gate_liquidations WHERE ts>=? AND ts<? GROUP BY direction""",
             (ws_ts, we_ts))
    if liq:
        tot_n = sum(r["n"] for r in liq); tot_usd = sum(r["usd"] or 0 for r in liq)
        parts = " | ".join(f"{r['direction']} {r['n']}笔 ${(r['usd'] or 0)/1e6:.2f}M" for r in liq)
        lines.append(f"Gate清算：共{tot_n}笔 ${tot_usd/1e6:.2f}M（{parts}）")
    else:
        lines.append("Gate清算：周内无记录")
    lines.append("（第0步实情：OKX清算源无历史落库——liq-monitor仅实时TG预警不写库；"
                 "Binance清算WS德国IP被封，无该源）")
    try:
        resp = requests.get("https://api.alternative.me/fng/", params={"limit": 8}, timeout=10)
        data = resp.json()["data"]          # 最新在前
        seq = list(reversed(data))          # 转为时间正序
        vals = [int(x["value"]) for x in seq]
        lines.append(f"F&G恐惧贪婪（近8日序列）：{'->'.join(str(v) for v in vals)}")
        lines.append(f"  周首 {vals[0]} → 最新 {vals[-1]}（{seq[-1]['value_classification']}）"
                     f" | 8日极值 {min(vals)}~{max(vals)}")
    except Exception as e:
        lines.append(f"[F&G获取失败：{e}]")
    fr = _q("SELECT ts, rate FROM binance_funding WHERE ts>=? AND ts<? ORDER BY ts", (ws_ts, we_ts))
    if len(fr) >= 20:
        rates = [r["rate"] for r in fr]
        mean_r = sum(rates) / len(rates)
        var = sum((x - mean_r) ** 2 for x in rates) / len(rates)
        std = var ** 0.5
        # Z口径（注释写明）：全周rate均值与总体std（非滚动窗口）；按8小时
        # 结算窗口分组（epoch//28800），组内任一采样点|Z|>2 记1个极端时段
        if std > 0:
            groups = defaultdict(list)
            for r in fr:
                groups[r["ts"] // 28800].append(abs((r["rate"] - mean_r) / std))
            hot = sum(1 for g in groups.values() if max(g) > 2)
            lines.append(f"Funding周内|Z|>2极端时段：{hot}/{len(groups)}个（全周均值±总体std口径）")
        else:
            lines.append("Funding周内费率零波动（std=0），无极端时段")
    else:
        lines.append("[Funding Z统计数据不足]")
    if extras and extras.get("cb_premium") is not None:
        lines.append(f"当前CB溢价：{extras['cb_premium']:+.0f} USD（{extras.get('cb_signal','-')}）")
    else:
        lines.append("[CB溢价不可用]")
    return "\n".join(lines)


# ── W8 流动性地图 ────────────────────────────────────────────────────────

def _w8_liquidity(daily_ext, cur_px):
    # daily_ext：近15根日K（含上周），相邻高低点比较找摆动极值
    lines = ["=== W8 流动性地图 ==="]
    if not daily_ext or len(daily_ext) < 3 or not cur_px:
        raise RuntimeError("日K或当前价不足")
    swings_hi, swings_lo = [], []
    for i in range(1, len(daily_ext) - 1):
        h_prev, h, h_next = (float(daily_ext[i-1][2]), float(daily_ext[i][2]),
                             float(daily_ext[i+1][2]))
        l_prev, l, l_next = (float(daily_ext[i-1][3]), float(daily_ext[i][3]),
                             float(daily_ext[i+1][3]))
        d = datetime.fromtimestamp(daily_ext[i][0] / 1000, BJT).strftime("%m-%d")
        if h > h_prev and h > h_next:
            swings_hi.append((h, d))
        if l < l_prev and l < l_next:
            swings_lo.append((l, d))
    bsl = sorted([s for s in swings_hi if s[0] > cur_px])[:3]
    ssl = sorted([s for s in swings_lo if s[0] < cur_px], reverse=True)[:3]
    lines.append(f"（基于近15根日K摆动高低点，当前价 ${cur_px:,.0f}）")
    if bsl:
        lines.append("上方BSL簇（摆动高点上方的止损/追涨单聚集区）：")
        lines += [f"  ${p:,.0f}（{d}形成）" for p, d in bsl]
    else:
        lines.append("上方BSL：近15日无当前价上方的摆动高点")
    if ssl:
        lines.append("下方SSL簇（摆动低点下方的止损/抄底单聚集区）：")
        lines += [f"  ${p:,.0f}（{d}形成）" for p, d in ssl]
    else:
        lines.append("下方SSL：近15日无当前价下方的摆动低点")
    try:
        from data_collector.cme_data import get_cme_gap
        cme = get_cme_gap()
        gaps = cme.get("gaps", [])
        if cme.get("all_filled"):
            lines.append("CME历史缺口：3个已全部填补（本维度退休）")
        elif gaps:
            unfilled = [g for g in gaps if not g.get("is_filled")]
            lines.append(f"CME历史缺口：未填 {len(unfilled)}/3 —— " + "；".join(
                f"${g.get('gap_bot',0):,.0f}-${g.get('gap_top',0):,.0f}" for g in unfilled))
        else:
            lines.append("[CME缺口数据为空]")
    except Exception as e:
        lines.append(f"[CME缺口获取失败：{e}]")
    return "\n".join(lines)


# ── W9 上周简报复盘 ──────────────────────────────────────────────────────

def _w9_review(ws_ts, we_ts, daily, prev_close=None):
    lines = ["=== W9 上周简报复盘 ==="]
    scores = _q("SELECT ts, composite FROM signal_scores WHERE ts>=? AND ts<? ORDER BY ts",
                (ws_ts, we_ts))
    day_px = {}
    if daily:
        pc = prev_close   # 传入前周收盘，首日涨跌不再缺失
        for k in daily:
            d = datetime.fromtimestamp(k[0] / 1000, BJT).strftime("%m-%d")
            close = float(k[4])
            day_px[d] = (close - pc) / pc * 100 if pc else None
            pc = close
    by_day = defaultdict(list)
    for r in scores:
        d = datetime.fromtimestamp(r["ts"], BJT).strftime("%m-%d")
        by_day[d].append(r["composite"])
    lines.append("简报综合分 vs 实际（日期 | 日均分 | 日内max|分| | 当日涨跌%）：")
    if daily:
        for k in daily:
            d = datetime.fromtimestamp(k[0] / 1000, BJT).strftime("%m-%d")
            vals = by_day.get(d)
            chg = day_px.get(d)
            chg_s = f"{chg:+.2f}%" if chg is not None else "-"
            if vals:
                lines.append(f"  {d} | {sum(vals)/len(vals):+.1f} | "
                             f"{max(abs(v) for v in vals)} | {chg_s}")
            else:
                lines.append(f"  {d} | - | - | {chg_s}（当日无简报评分记录）")
    else:
        lines.append("  [日K不可用，对照表跳过]")
    ws_str = datetime.fromtimestamp(ws_ts, BJT).strftime("%Y-%m-%d %H:%M:%S")
    we_str = datetime.fromtimestamp(we_ts, BJT).strftime("%Y-%m-%d %H:%M:%S")
    sigs = _q("""SELECT direction, status, entry, stop, outcome_price
                 FROM engine_signals
                 WHERE created_at>=? AND created_at<? AND status!='open'""",
              (ws_str, we_str))
    if sigs:
        lines.append("引擎信号周内终态（方向 | 结果 | R数）：")
        for s in sigs:
            risk = abs((s["entry"] or 0) - (s["stop"] or 0))
            if risk and s["outcome_price"] is not None:
                r_mult = ((s["outcome_price"] - s["entry"]) / risk
                          if s["direction"] == "LONG"
                          else (s["entry"] - s["outcome_price"]) / risk)
                r_str = f"{r_mult:+.2f}R"
            else:
                r_str = "-"
            lines.append(f"  {s['direction']} | {s['status']} | {r_str}")
    else:
        total_term = _q("SELECT COUNT(*) AS n FROM engine_signals WHERE status!='open'")[0]["n"]
        lines.append(f"引擎信号：周内终态0条；引擎样本累积中（全库终态{total_term}/30），"
                     "本节随样本自动充实")
    return "\n".join(lines)


# ── W10 周一开局数据 ─────────────────────────────────────────────────────

def _w10_monday_open():
    lines = ["=== W10 周一开局数据 ==="]
    now_bj = datetime.now(BJT)
    if now_bj.weekday() == 0:
        # 7P起周一开盘窗口统计由周报调用（原morning_monday分支已退役）；
        # 函数本体仍在 ai_analyst/briefing.py，此处延迟import避免模块加载链过重
        from ai_analyst.briefing import _monday_window_stats
        w = _monday_window_stats()
        if w:
            lines += [
                "周一TradFi开盘窗口实测（系统代码计算，权威数值，禁止推算改写）：",
                f"  开 ${w['open']:,.0f} → 收 ${w['close']:,.0f}（{w['chg_pct']:+.2f}%）"
                f" | 高 ${w['high']:,.0f} / 低 ${w['low']:,.0f}（振幅 {w['range_pct']:.2f}%）",
                f"  PDH清扫判定：{w['pdh_status']}",
                f"  PDL清扫判定：{w['pdl_status']}",
            ]
        else:
            lines.append("[周一开盘窗口实测数据获取失败，本块跳过，禁止凭其他数据推测窗口走势]")
    else:
        lines.append("[今日非周一，周一开盘窗口统计不适用（本输出仅供手动测试）]")
    try:
        from data_collector.binance_data import get_todays_ib
        ib = get_todays_ib() or {}
        if ib:
            lines.append(f"今日IB：${ib.get('ib_low',0):,.0f}-${ib.get('ib_high',0):,.0f}"
                         f"（{ib.get('ib_type','-')}） | {ib.get('opening_type','-')}"
                         f" | {ib.get('position','')}")
        else:
            lines.append("[今日IB暂不可得（未到08:00或数据空）]")
    except Exception as e:
        lines.append(f"[今日IB获取失败：{e}]")
    return "\n".join(lines)


# ── 主入口 ───────────────────────────────────────────────────────────────

def get_weekly_context() -> str:
    week_start, week_end = _week_bounds()
    ws_ts, we_ts = int(week_start.timestamp()), int(week_end.timestamp())
    ws_iso, we_iso = _iso(week_start), _iso(week_end)

    # 共享行情数据（失败不阻断：依赖它的块各自降级输出"数据不足"）
    daily = prev_daily = daily_ext = None
    prev_close = cur_px = None
    extras = None
    try:
        k15 = _fetch_klines("1d", week_start - timedelta(days=8), week_end)
        daily_ext = k15
        daily = [k for k in k15 if int(k[0]) >= ws_ts * 1000]
        prev_daily = [k for k in k15
                      if (ws_ts - 7 * 86400) * 1000 <= int(k[0]) < ws_ts * 1000]
        if prev_daily:
            prev_close = float(prev_daily[-1][4])
    except Exception:
        pass
    try:
        r = requests.get(f"{FAPI}/fapi/v1/ticker/price",
                         params={"symbol": "BTCUSDT"}, timeout=8)
        cur_px = float(r.json()["price"])
    except Exception:
        pass
    try:
        from data_collector.binance_data import get_spot_and_extras
        extras = get_spot_and_extras()
    except Exception:
        pass

    blocks = [
        _safe(lambda: _w1(daily, prev_close), "W1 上周行情"),
        _safe(lambda: _w2_etf(week_start, week_end), "W2 ETF周报"),
        _safe(lambda: _w3_deriv(ws_ts, we_ts, daily, prev_daily, cur_px, extras), "W3 衍生品周报"),
        _safe(lambda: _w4_structure(ws_ts, we_ts), "W4 市场结构周报"),
        _safe(lambda: _w5_weekly_vp(week_start, week_end), "W5 周Volume Profile"),
        _safe(lambda: _w6_orderflow(ws_iso, we_iso), "W6 订单流周报"),
        _safe(lambda: _w7_risk(ws_ts, we_ts, extras), "W7 清算与风险偏好"),
        _safe(lambda: _w8_liquidity(daily_ext, cur_px), "W8 流动性地图"),
        _safe(lambda: _w9_review(ws_ts, we_ts, daily, prev_close), "W9 上周简报复盘"),
        _safe(lambda: _w10_monday_open(), "W10 周一开局数据"),
    ]
    header = (f"[WEEKLY 周报权威数据块 | 统计区间：{week_start:%Y-%m-%d %H:%M} ~ "
              f"{week_end:%Y-%m-%d %H:%M}（北京时间，周边界=周一08:00）| "
              "以下全部数值由系统代码计算，AI只解读禁止修改]")
    return header + "\n\n" + "\n\n".join(blocks)


if __name__ == "__main__":
    print(get_weekly_context())
