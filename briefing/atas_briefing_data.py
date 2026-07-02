"""
ATAS v4.0 Briefing Data - includes Footprint analysis
"""
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH       = Path("/opt/btc-trader/btc_history.db")
BJT           = timezone(timedelta(hours=8))
STALE_MINUTES = 30


def _q(sql, params=()):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _analyze_footprint(bars):
    """Aggregate footprint data across all bars, find absorption + HVN"""
    bucket = {}  # price_rounded -> {vol, bid, ask, count}

    for bar in bars:
        fp_raw = bar.get("footprint_json")
        if not fp_raw:
            continue
        try:
            levels = json.loads(fp_raw)
        except Exception:
            continue
        for lv in levels:
            try:
                px = round(float(lv["price"]) / 10) * 10
                if px not in bucket:
                    bucket[px] = {"vol": 0.0, "bid": 0.0, "ask": 0.0, "cnt": 0}
                bucket[px]["vol"] += float(lv.get("volume", 0))
                bucket[px]["bid"] += float(lv.get("bid", 0))
                bucket[px]["ask"] += float(lv.get("ask", 0))
                bucket[px]["cnt"] += 1
            except Exception:
                continue

    if not bucket:
        return None

    # Top 3 HVN
    hvn = sorted(bucket.items(), key=lambda x: x[1]["vol"], reverse=True)[:3]

    # Bid absorption (bid >> ask, institutions absorbing selling = support)
    bid_ab = [(p, d) for p, d in bucket.items()
               if d["ask"] > 0 and d["bid"] / d["ask"] >= 2.0 and d["bid"] >= 5]
    bid_ab.sort(key=lambda x: x[1]["bid"], reverse=True)
    top_bid = bid_ab[:3]

    # Ask absorption (ask >> bid, institutions absorbing buying = resistance)
    ask_ab = [(p, d) for p, d in bucket.items()
               if d["bid"] > 0 and d["ask"] / d["bid"] >= 2.0 and d["ask"] >= 5]
    ask_ab.sort(key=lambda x: x[1]["ask"], reverse=True)
    top_ask = ask_ab[:3]

    return {"hvn": hvn, "bid_absorb": top_bid, "ask_absorb": top_ask}


def get_atas_context(hours: int = 4) -> str:
    # 2026-07-01：四路市场（币安/OKX × 现货/合约）接入同一张表后，简报只用
    # 币安永续这一路（跟AI简报"BTCUSDT永续合约分析师"的定位一致），加 market
    # 过滤，避免拿OKX现货的心跳去判断"币安永续这路ATAS是否离线"。
    last_bar = _q(
        "SELECT created_at FROM atas_bars "
        "WHERE exchange='binance' AND market_type='perp' "
        "ORDER BY id DESC LIMIT 1"
    )
    if not last_bar:
        return ""
    try:
        last_dt = datetime.fromisoformat(last_bar[0]["created_at"])
        if (datetime.now() - last_dt).total_seconds() / 60 > STALE_MINUTES:
            return ""
    except Exception:
        return ""

    now_bjt = datetime.now(BJT)
    cutoff  = (now_bjt - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    bars = _q("""
        SELECT timestamp, delta, cumulative_delta,
               poc_price, max_pos_delta_price, max_neg_delta_price,
               ask_vol, bid_vol, footprint_json
        FROM atas_bars
        WHERE created_at >= ? AND exchange='binance' AND market_type='perp'
        ORDER BY timestamp ASC
    """, (cutoff,))

    if not bars:
        return ""

    # signals 暂不加 exchange/market 过滤：/atas/signal 走ATAS内置Webhook，
    # 还没有可靠的市场标签来源（见main.py注释），过滤会导致查不到任何数据。
    # 等观察到 raw_instrument 实际取值规律后再补规则。
    signals = _q("""
        SELECT indicator_name, price, timestamp FROM atas_signals
        WHERE created_at >= ? AND indicator_name != 'Unknown'
        ORDER BY timestamp ASC
    """, (cutoff,))

    trades = _q("""
        SELECT direction, volume, price, threshold_level, timestamp
        FROM atas_large_trades
        WHERE created_at >= ? AND exchange='binance' AND market_type='perp'
        ORDER BY timestamp ASC
    """, (cutoff,))

    # K-line stats
    total = len(bars)
    pos_n = sum(1 for b in bars if b["delta"] and b["delta"] > 0)
    neg_n = total - pos_n
    cvd_start = bars[0]["cumulative_delta"] or 0
    cvd_end   = bars[-1]["cumulative_delta"] or 0
    cvd_chg   = cvd_end - cvd_start
    cvd_dir   = "buy side" if cvd_chg > 0 else "sell side"

    poc_list     = [b["poc_price"] for b in bars if b["poc_price"]]
    dominant_poc = Counter(poc_list).most_common(1)[0][0] if poc_list else None
    latest_poc   = bars[-1]["poc_price"] if bars else None
    latest_delta = bars[-1]["delta"] if bars else None

    # Footprint
    fp = _analyze_footprint(bars)

    # Large trades
    buy_t   = [t for t in trades if t["direction"] == "buy"]
    sell_t  = [t for t in trades if t["direction"] == "sell"]
    buy_vol = sum(t["volume"] for t in buy_t)
    sell_vol= sum(t["volume"] for t in sell_t)
    net_vol = buy_vol - sell_vol
    whales  = [t for t in trades if t["threshold_level"] == "whale"]

    # Absorption signals
    abs_sigs = [s for s in signals if "Absorption" in s.get("indicator_name", "")]

    # Build text
    lines = [
        f"[ATAS Order Flow - last {hours}H, {total} x 5min bars, tick-level precision]",
        "",
        "Delta & CVD:",
        f"  Positive delta bars: {pos_n} / Negative: {neg_n}",
        f"  {hours}H CVD net change: {cvd_chg:+,.1f} ({cvd_dir} accumulating)",
    ]
    if latest_delta is not None:
        lines.append(f"  Latest bar delta: {latest_delta:+,.2f}")

    lines += ["", "POC Key Levels:"]
    if dominant_poc:
        lines.append(f"  Dominant POC (highest frequency): ${dominant_poc:,.0f}")
    if latest_poc and latest_poc != dominant_poc:
        lines.append(f"  Latest bar POC: ${latest_poc:,.0f}")

    # Footprint analysis
    if fp:
        lines.append("")
        lines.append("Footprint Absorption Zones:")
        if fp["bid_absorb"]:
            lines.append("  Support (Bid absorption - institutions absorbing selling):")
            for p, d in fp["bid_absorb"]:
                ratio = d["bid"] / d["ask"] if d["ask"] > 0 else 0
                lines.append(f"    ${p:,.0f}  {d['bid']:.1f}BTC bid / {d['ask']:.1f}BTC ask  ratio:{ratio:.1f}x")
        if fp["ask_absorb"]:
            lines.append("  Resistance (Ask absorption - institutions absorbing buying):")
            for p, d in fp["ask_absorb"]:
                ratio = d["ask"] / d["bid"] if d["bid"] > 0 else 0
                lines.append(f"    ${p:,.0f}  {d['ask']:.1f}BTC ask / {d['bid']:.1f}BTC bid  ratio:{ratio:.1f}x")
        if fp["hvn"]:
            lines.append("  High Volume Nodes (key price memory):")
            for p, d in fp["hvn"]:
                lines.append(f"    ${p:,.0f}  {d['vol']:.1f}BTC total volume")

    lines.append("")
    if trades:
        lines += [
            "Institutional Large Orders (>=20 BTC):",
            f"  Buy: {len(buy_t)} orders {buy_vol:.1f}BTC | Sell: {len(sell_t)} orders {sell_vol:.1f}BTC",
            f"  Net: {'buy' if net_vol>=0 else 'sell'} dominant ({abs(net_vol):.1f}BTC)",
        ]
        for wt in whales[:2]:
            ts_s = str(wt["timestamp"]).split("T")[-1][:8] if "T" in str(wt["timestamp"]) else str(wt["timestamp"])
            lines.append(f"  WHALE: {ts_s} {wt['direction']} {wt['volume']:.0f}BTC @ ${wt['price']:,.0f}")
    else:
        lines.append("Large Orders: no >=20 BTC orders in this window")

    if abs_sigs:
        lines += [
            "",
            f"Absorption Signals: {len(abs_sigs)} triggers in last {hours}H",
            "  (Institutional order absorption at key price levels)",
        ]

    return "\n".join(lines)


if __name__ == "__main__":
    r = get_atas_context()
    print(r if r else "No data (AtasBridge offline or stale)")
