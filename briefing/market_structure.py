"""
briefing/market_structure.py — 多时间框架市场结构（面板实时读盘）
================================================================
对外接口：get_market_structure() -> dict（供 GET /api/market/structure）

设计原则（2026-07-26 立）：
1. 纯计算、无AI：所有数值和状态标签都由代码确定性算出，可高频刷新、零API成本，
   且不存在AI编数风险（本项目血的教训：宁缺勿假）。
2. 只描述、不预测：输出"价格在周VA的什么位置""4H Delta方向""哪里有吸收"这类
   客观状态，不产出买卖信号——回测已证否本系统的方向预测能力（综合分-0.31R/单、
   吸收信号方向边际≈0）。面板给背景，扳机由用户按。
3. 自上而下：周 → 昨日 → 今日 → 4H → 订单流 → 宏观，层层收敛到关键价位表。

数据源全部为已审计干净的表（2026-07-26 完整性审计）：
  atas_bars(binance/perp，×10污染已修) / binance_structure / binance_funding /
  binance_oi / binance_ls_top / atas_large_trades / data_collector.etf_data
日界约定：北京时间 08:00（与项目一致）；周界：周一 08:00。
任一子块失败只让该块为 None，绝不拖垮整体（各自 try/except）。
"""
import json
import sqlite3
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

DB_PATH = "/opt/btc-trader/btc_history.db"
BJT = timezone(timedelta(hours=8))
STALE_MIN = 30          # atas_bars 超过此分钟数未更新视为订单流离线


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


# footprint版VP要解析~2000根footprint(周窗口)，单次全量计算~3秒。bar每5分钟
# 才更新，故按"最新bar时间戳"缓存整份结果：两次bar之间的所有轮询直接返回
# 缓存，全量重算只在有新bar时发生(~每5分钟一次)，避免阻塞单worker的API。
_CACHE = {"key": None, "data": None}
# ETF为日粒度且fetch会爬Farside网站，单独加30分钟缓存，避免每根bar都去爬。
_ETF_CACHE = {"ts": 0.0, "data": None}


def _etf_cached():
    import time as _t
    if _ETF_CACHE["data"] is not None and _t.time() - _ETF_CACHE["ts"] < 1800:
        return _ETF_CACHE["data"]
    from data_collector.etf_data import fetch_etf_flows
    e = fetch_etf_flows()
    if e and e.get("has_data"):
        _ETF_CACHE["ts"] = _t.time()
        _ETF_CACHE["data"] = e
    return e


def _ep(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return None


def _bar_fp(b):
    """惰性解析并缓存单根bar的footprint（逐档 price/volume/bid/ask）。"""
    if "_fp" not in b:
        try:
            fp = json.loads(b["footprint_json"]) if b.get("footprint_json") else None
            b["_fp"] = fp if fp else None
        except Exception:
            b["_fp"] = None
    return b["_fp"]


def _vp(bars, bucket=50.0):
    """
    真·Volume Profile（成交量分布，非TPO）：按 footprint 逐档成交量在价格上聚合，
    取 POC（成交量最大价位）+ 70% 价值区 VAH/VAL。这是"成交量在哪堆积"，
    与基于时间的 TPO（Market Profile）是两回事。
    极少数无 footprint 的bar退化为"bar典型价+总量"近似（覆盖<0.5%）。
    """
    v = defaultdict(float)
    for b in bars:
        fp = _bar_fp(b)
        if fp:
            for lv in fp:
                try:
                    v[round(float(lv["price"]) / bucket) * bucket] += float(lv.get("volume", 0))
                except Exception:
                    continue
        elif b.get("volume"):
            v[round(((b["high"] + b["low"] + b["close"]) / 3) / bucket) * bucket] += b["volume"]
    if not v:
        return None
    tot = sum(v.values())
    sb = sorted(v.items())
    poc, pv = max(v.items(), key=lambda x: x[1])
    pi = [p for p, _ in sb].index(poc)
    cap, lo, hi = pv, pi, pi
    while cap < tot * 0.70 and (lo > 0 or hi < len(sb) - 1):
        l = sb[lo - 1][1] if lo > 0 else -1
        r = sb[hi + 1][1] if hi < len(sb) - 1 else -1
        if l >= r:
            lo -= 1; cap += l
        else:
            hi += 1; cap += r
    return {"poc": poc, "vah": sb[hi][0], "val": sb[lo][0]}


def _pos_pct(px, lo, hi):
    """现价在[lo,hi]区间的分位%（0=底 100=顶）"""
    if hi is None or lo is None or hi <= lo:
        return None
    return round((px - lo) / (hi - lo) * 100)


def get_market_structure() -> dict:
    now = datetime.now(BJT)
    # ── 缓存命中检查：最新bar未变则直接返回上次结果（避免3秒重算）──
    try:
        _cc = _conn()
        _r = _cc.execute("SELECT MAX(timestamp) t FROM atas_bars "
                         "WHERE exchange='binance' AND market_type='perp'").fetchone()
        _cc.close()
        cache_key = _r["t"] if _r else None
    except Exception:
        cache_key = None
    if cache_key and _CACHE["key"] == cache_key and _CACHE["data"] is not None:
        return _CACHE["data"]

    out = {
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"), "generated_at": now.isoformat(),
        "symbol": "币安 BTCUSDT 永续",            # 数据源交易对（AtasBridge binance/perp）
        "vp_type": "Volume Profile（成交量分布，非TPO）",  # POC/VAH/VAL 口径明示
    }
    c = _conn()

    # ── 币安永续 bar（已清洗）──────────────────────────────────────
    bars = [dict(r) for r in c.execute(
        """SELECT timestamp,open,high,low,close,volume,delta,cumulative_delta,poc_price,footprint_json
           FROM atas_bars WHERE exchange='binance' AND market_type='perp'
           ORDER BY timestamp""")]
    for b in bars:
        b["t"] = _ep(b["timestamp"])
    bars = [b for b in bars if b["t"]]

    if not bars:
        c.close()
        out["status"] = "no_data"
        out["note"] = "atas_bars 无币安永续数据（AtasBridge 未推送）"
        return out

    last = bars[-1]
    px = last["close"]
    age_min = (now.timestamp() - last["t"]) / 60
    out["price"] = px
    out["last_bar"] = last["timestamp"][11:19]
    out["stale"] = age_min > STALE_MIN
    out["stale_min"] = round(age_min)
    out["status"] = "stale" if out["stale"] else "ok"

    # ── 本周（周一08:00起）──────────────────────────────────────────
    try:
        ws = (now - timedelta(days=now.weekday())).replace(hour=8, minute=0, second=0, microsecond=0)
        if ws.timestamp() > now.timestamp():
            ws -= timedelta(days=7)
        W = [b for b in bars if b["t"] >= ws.timestamp()]
        if W:
            wo = W[0]["open"]; wh = max(b["high"] for b in W); wl = min(b["low"] for b in W)
            v = _vp(W)
            state = None
            if v:
                state = "在VA内" if v["val"] <= px <= v["vah"] else ("VA上方" if px > v["vah"] else "VA下方")
            out["week"] = {
                "start": ws.strftime("%m-%d 08:00"), "open": wo, "high": wh, "low": wl,
                "chg_pct": round((px - wo) / wo * 100, 2),
                "range": round(wh - wl), "range_pct": round((wh - wl) / wo * 100, 2),
                "pos_pct": _pos_pct(px, wl, wh), "vp": v, "va_state": state,
            }
    except Exception as e:
        out["week"] = {"error": str(e)}

    # ── 昨日 / 今日（08:00日界）─────────────────────────────────────
    def _day(off):
        s = (now - timedelta(days=off)).replace(hour=8, minute=0, second=0, microsecond=0)
        if s.timestamp() > now.timestamp():
            s -= timedelta(days=1)
        e = s + timedelta(days=1)
        return s, [b for b in bars if s.timestamp() <= b["t"] < e.timestamp()]
    try:
        ys, Y = _day(1)
        if Y:
            out["yesterday"] = {
                "date": ys.strftime("%m-%d"),
                "pdh": max(b["high"] for b in Y), "pdl": min(b["low"] for b in Y),
                "pdc": Y[-1]["close"], "pdo": Y[0]["open"],
                "range": round(max(b["high"] for b in Y) - min(b["low"] for b in Y)),
                "vp": _vp(Y),
            }
    except Exception as e:
        out["yesterday"] = {"error": str(e)}
    try:
        ds, D = _day(0)
        if D:
            dh = max(b["high"] for b in D); dl = min(b["low"] for b in D); do = D[0]["open"]
            out["today"] = {
                "date": ds.strftime("%m-%d"), "open": do, "high": dh, "low": dl,
                "chg_pct": round((px - do) / do * 100, 2), "range": round(dh - dl),
                "pos_pct": _pos_pct(px, dl, dh),
            }
    except Exception as e:
        out["today"] = {"error": str(e)}

    # ── 近4H：价格结构 + 订单流 ─────────────────────────────────────
    try:
        H = [b for b in bars if b["t"] >= now.timestamp() - 4 * 3600]
        if H:
            hh = max(b["high"] for b in H); hl = min(b["low"] for b in H)
            d4 = sum(b["delta"] or 0 for b in H)
            cvd0 = H[0]["cumulative_delta"] or 0
            cvd1 = H[-1]["cumulative_delta"] or 0
            pchg = (H[-1]["close"] - H[0]["open"]) / H[0]["open"] * 100
            pocs = Counter(b["poc_price"] for b in H if b["poc_price"])
            out["h4"] = {
                "high": hh, "low": hl, "range": round(hh - hl), "pos_pct": _pos_pct(px, hl, hh),
                "delta": round(d4), "cvd_from": round(cvd0), "cvd_to": round(cvd1),
                "cvd_chg": round(cvd1 - cvd0),
                "pos_bars": sum(1 for b in H if (b["delta"] or 0) > 0), "bars": len(H),
                "price_chg_pct": round(pchg, 2),
                # 背离：价格与Delta方向相反（订单流经典观察点，仅描述不预测）
                "divergence": (d4 > 0) != (pchg > 0),
                "vp": _vp(H),
                "hot_poc": [{"price": p, "n": n} for p, n in pocs.most_common(3)],
            }
            # 足迹吸收带（4H聚合，与 atas_briefing_data 同判据：比值>=2且量>=5）
            buk = defaultdict(lambda: {"bid": 0.0, "ask": 0.0})
            for b in H:
                lv = _bar_fp(b)
                if not lv:
                    continue
                for x in lv:
                    try:
                        p = round(float(x["price"]) / 10) * 10
                        buk[p]["bid"] += float(x.get("bid", 0))
                        buk[p]["ask"] += float(x.get("ask", 0))
                    except Exception:
                        continue
            bid_ab = sorted([(p, d) for p, d in buk.items()
                             if d["ask"] > 0 and d["bid"] / d["ask"] >= 2 and d["bid"] >= 5],
                            key=lambda x: -x[1]["bid"])[:3]
            ask_ab = sorted([(p, d) for p, d in buk.items()
                             if d["bid"] > 0 and d["ask"] / d["bid"] >= 2 and d["ask"] >= 5],
                            key=lambda x: -x[1]["ask"])[:3]
            out["absorption"] = {
                "bid": [{"price": p, "bid": round(d["bid"], 1), "ask": round(d["ask"], 1),
                         "ratio": round(d["bid"] / d["ask"], 1)} for p, d in bid_ab],
                "ask": [{"price": p, "ask": round(d["ask"], 1), "bid": round(d["bid"], 1),
                         "ratio": round(d["ask"] / d["bid"], 1)} for p, d in ask_ab],
            }
    except Exception as e:
        out["h4"] = {"error": str(e)}

    # ── 近8H大单 ────────────────────────────────────────────────────
    try:
        cut = (now - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
        lt = [dict(r) for r in c.execute(
            """SELECT timestamp,direction,volume,price,threshold_level FROM atas_large_trades
               WHERE exchange='binance' AND market_type='perp' AND timestamp>? ORDER BY timestamp""",
            (cut,))]
        bv = sum(x["volume"] for x in lt if x["direction"] == "buy")
        sv = sum(x["volume"] for x in lt if x["direction"] == "sell")
        out["large_trades"] = {
            "hours": 8, "count": len(lt), "buy_vol": round(bv), "sell_vol": round(sv),
            "net": round(bv - sv),
            "top": [{"time": x["timestamp"][11:16], "dir": x["direction"],
                     "vol": round(x["volume"]), "price": x["price"], "lvl": x["threshold_level"]}
                    for x in sorted(lt, key=lambda z: -z["volume"])[:3]],
        }
    except Exception as e:
        out["large_trades"] = {"error": str(e)}

    # ── 宏观背景 ────────────────────────────────────────────────────
    try:
        m = {}
        s = c.execute("SELECT quadrant FROM binance_structure ORDER BY ts DESC LIMIT 1").fetchone()
        if s:
            m["quadrant"] = s["quadrant"]
        q = Counter(r["quadrant"] for r in c.execute(
            "SELECT quadrant FROM binance_structure WHERE ts>=?", (int(now.timestamp()) - 86400,)))
        if q:
            m["quadrant_24h"] = dict(q.most_common())
        fr = [r["rate"] for r in c.execute(
            "SELECT rate FROM binance_funding WHERE ts>=? ORDER BY ts", (int(now.timestamp()) - 86400,))]
        cur = c.execute("SELECT rate FROM binance_funding ORDER BY ts DESC LIMIT 1").fetchone()
        if cur and len(fr) > 2:
            sd = st.pstdev(fr)
            m["funding"] = round(cur["rate"] * 100, 4)
            m["funding_z"] = round((cur["rate"] - st.mean(fr)) / sd, 2) if sd > 0 else 0
        oi = [r["oi_usd"] for r in c.execute(
            "SELECT oi_usd FROM binance_oi WHERE ts>=? ORDER BY ts", (int(now.timestamp()) - 86400,))]
        if len(oi) > 1 and oi[0]:
            m["oi_now_b"] = round(oi[-1] / 1e9, 2)
            m["oi_chg_24h_pct"] = round((oi[-1] - oi[0]) / oi[0] * 100, 2)
        ls = c.execute("SELECT ls_ratio FROM binance_ls_top ORDER BY ts DESC LIMIT 1").fetchone()
        if ls:
            m["ls_ratio"] = round(ls["ls_ratio"], 3)
        out["macro"] = m
    except Exception as e:
        out["macro"] = {"error": str(e)}
    try:
        e = _etf_cached()
        if e and e.get("has_data"):
            out["macro"]["etf_stable_m"] = e.get("stable_flow_m")
            out["macro"]["etf_stable_date"] = e.get("stable_date")
            out["macro"]["etf_week_m"] = e.get("stable_week_m")
            out["macro"]["etf_streak"] = f"{e.get('streak_days')}天{e.get('streak_dir')}"
    except Exception:
        pass

    # ── 关键价位表（自上而下汇总，按价格排序，标注在现价上/下）──────
    try:
        lv = []
        w = out.get("week") or {}
        if w.get("vp"):
            lv += [{"price": w["vp"]["vah"], "label": "周VAH", "tf": "周"},
                   {"price": w["vp"]["poc"], "label": "周POC", "tf": "周"},
                   {"price": w["vp"]["val"], "label": "周VAL", "tf": "周"}]
        if w.get("high"):
            lv.append({"price": w["high"], "label": "本周高", "tf": "周"})
        if w.get("low"):
            lv.append({"price": w["low"], "label": "本周低", "tf": "周"})
        y = out.get("yesterday") or {}
        for k, n in (("pdh", "PDH昨高"), ("pdl", "PDL昨低"), ("pdc", "PDC昨收")):
            if y.get(k):
                lv.append({"price": y[k], "label": n, "tf": "日"})
        if y.get("vp"):
            lv.append({"price": y["vp"]["poc"], "label": "昨POC", "tf": "日"})
        t = out.get("today") or {}
        if t.get("high"):
            lv.append({"price": t["high"], "label": "今日高", "tf": "日"})
        if t.get("low"):
            lv.append({"price": t["low"], "label": "今日低", "tf": "日"})
        h = out.get("h4") or {}
        if h.get("vp"):
            lv.append({"price": h["vp"]["poc"], "label": "4H POC", "tf": "4H"})
        for a in (out.get("absorption") or {}).get("bid", []):
            lv.append({"price": a["price"], "label": f"买方吸收{a['ratio']}x", "tf": "4H", "flow": "bid"})
        for a in (out.get("absorption") or {}).get("ask", []):
            lv.append({"price": a["price"], "label": f"卖方吸收{a['ratio']}x", "tf": "4H", "flow": "ask"})
        # 合并同价位标签、标注方位、按价格降序
        merged = {}
        for x in lv:
            if not x.get("price"):
                continue
            p = round(x["price"])
            if p in merged:
                if x["label"] not in merged[p]["labels"]:
                    merged[p]["labels"].append(x["label"])
                if x.get("flow"):
                    merged[p]["flow"] = x["flow"]
            else:
                merged[p] = {"price": p, "labels": [x["label"]], "tf": x["tf"], "flow": x.get("flow")}
        levels = sorted(merged.values(), key=lambda z: -z["price"])
        for x in levels:
            x["side"] = "above" if x["price"] > px else ("below" if x["price"] < px else "at")
            x["dist_pct"] = round((x["price"] - px) / px * 100, 2)
        out["levels"] = levels
    except Exception as e:
        out["levels"] = []
        out["levels_error"] = str(e)

    c.close()
    if cache_key:
        _CACHE["key"] = cache_key
        _CACHE["data"] = out
    return out



def get_feed_health() -> dict:
    """
    AtasBridge 推送健康检查（每次实查、不走缓存——断流时缓存会冻结在
    "新鲜"状态反而报不出问题，故必须绕开 get_market_structure 的缓存）。
    以"币安永续K线"为主判据(正常每5分钟必有一根)：>15分钟无新bar即判定断流。
    大单/吸收为事件驱动(行情淡时可正常沉默)，仅作辅助信息展示，不作断流判据。
    """
    now = datetime.now(BJT).timestamp()

    def _age_min(sql, params=()):
        try:
            cc = _conn()
            r = cc.execute(sql, params).fetchone()
            cc.close()
            if not r or not r[0]:
                return None
            t = _ep(r[0])
            return round((now - t) / 60, 1) if t else None
        except Exception:
            return None

    bar = _age_min("SELECT MAX(timestamp) FROM atas_bars "
                   "WHERE exchange='binance' AND market_type='perp'")
    lt = _age_min("SELECT MAX(timestamp) FROM atas_large_trades "
                  "WHERE exchange='binance' AND market_type='perp'")
    ab = _age_min("SELECT MAX(timestamp) FROM atas_signals "
                  "WHERE signal_type IN ('bid_absorb','ask_absorb')")

    # 主判据：币安永续bar新鲜度
    if bar is None:
        status, msg = "down", "无数据"
    elif bar <= 10:
        status, msg = "ok", "正常"
    elif bar <= 20:
        status, msg = "warn", "轻微延迟"
    else:
        status, msg = "down", "断流"

    def _fmt(m):
        if m is None:
            return "无"
        if m < 60:
            return f"{m:.0f}分钟前"
        if m < 1440:
            return f"{m/60:.1f}小时前"
        return f"{m/1440:.1f}天前"

    return {
        "status": status, "msg": msg,
        "symbol": "币安 BTCUSDT 永续",
        "bar_age_min": bar, "bar_ago": _fmt(bar),
        "trade_age_min": lt, "trade_ago": _fmt(lt),
        "absorb_age_min": ab, "absorb_ago": _fmt(ab),
        "checked_at": datetime.now(BJT).strftime("%H:%M:%S"),
    }

if __name__ == "__main__":
    print(json.dumps(get_market_structure(), ensure_ascii=False, indent=2))
