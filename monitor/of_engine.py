#!/usr/bin/env python3
"""
of_engine.py — 订单流信号引擎（反转承接型 v1，Phase 8）
上传位置：/opt/btc-trader/monitor/of_engine.py
Supervisor 服务名：btc-of-engine

定位（2026-08-02 Sea 裁定）：
  这是"通过 AtasBridge 订单流数据"生成 entry/stop/tp 的第一个正式尝试，跟已被
  干净回测证否的综合分引擎(signal_engine.py)完全分开——独立模块、独立表 of_signals。
  全程【纸面·观察】，不接任何下单接口。目的：把一条可判定的订单流规则实时画到
  AtasBridge 图上、自动记结果，跑够样本用数据判断它到底有没有边际——有就留，
  没有就改或弃。这是"实现结果"的诚实路径，不是又一个假装能预测的黑盒。

v1 规则：反转承接（responsive absorption at value edge）
  做多：现价在近4H区间下1/3(pos<=35%) 且 贴近一个参考支撑(周VAL/PDL/昨POC) 且
        该处有强买方吸收(bid/ask>=3、吸收>=20BTC，来自footprint逐档) → 承接看涨
  做空：镜像(上1/3、贴近阻力、强卖方吸收)
  入场=现价；止损=吸收带/结构下方 − 0.3×ATR15m；止盈=1R/2R(相对结构止损)
  冷却90分钟拦"信号生成"(同 signal_engine P0)，防同向连发污染样本。

数据源：briefing.market_structure.get_market_structure()(币安永续、已审计干净、
  VP/吸收带/4H区间/CVD 现成)。结果跟踪复用 signal_engine 同款(stop/t1/t2/expire,
  且无条件每轮先跑——Bug#38 教训:跟踪只依赖现价不依赖信号输入)。

红线：模拟·纸面·仅供观察，不构成操作依据，不接下单接口。
"""
import json
import sqlite3
import sys
import time
from datetime import datetime

import requests

sys.path.insert(0, "/opt/btc-trader")
from utils.helpers import setup_logger, get_env, now_sgt
from briefing.market_structure import get_market_structure

logger = setup_logger("of_engine")

DB_PATH     = "/opt/btc-trader/btc_history.db"
FUTURE_BASE = "https://fapi.binance.com"
SYMBOL      = "BTCUSDT"
BOT_TOKEN   = get_env("TELEGRAM_BOT_TOKEN")
CHAT_ID     = get_env("TELEGRAM_CHAT_ID")

# ── 常量集中区（供后续按结果调参）─────────────────────────────────
CYCLE_MIN     = 5
COOLDOWN_MIN  = 90       # 同方向信号最小间隔（拦生成，非仅拦TG）
ATR_INTERVAL  = "15m"
ATR_PERIOD    = 14
EXPIRE_HOURS  = 24
PUBLISH_TG    = True      # 订单流是当前在观察的实验，默认推TG(明确标注纸面)；不想要改False

# ── v1 规则参数 ──────────────────────────────────────────────────
POS_LOW       = 35.0      # 现价在4H区间分位 <= 此值 视为"低位"
POS_HIGH      = 65.0      # >= 此值 视为"高位"
REF_TOL_PCT   = 0.0015    # 贴近参考位的容差(现价的比例，~$95@63k)
ABS_RATIO_MIN = 3.0       # 吸收强度：主导方/对手方
ABS_BTC_MIN   = 20.0      # 吸收量下限(BTC)
ABS_TOL_PCT   = 0.0018    # 吸收带距现价容差
STOP_BUF_ATR  = 0.3       # 止损放在结构下方再留 0.3×ATR 缓冲
MIN_R_ATR     = 0.25      # 风险(entry-stop)下限，避免止损过近
MAX_R_ATR     = 2.5       # 风险上限，避免止损过远


# ══ 表：of_signals（独立于 engine_signals）══════════════════════
def _ensure_table():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS of_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT, direction TEXT, entry REAL, stop REAL,
            t1 REAL, t2 REAL, atr REAL, risk REAL, reason TEXT,
            status TEXT DEFAULT 'open', t1_touched INTEGER DEFAULT 0,
            outcome_price REAL, outcome_at TEXT
        );
    """)
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_of_signals_status ON of_signals(status)")
    conn.commit()
    conn.close()
    logger.info("of_signals 表已就绪")


def _send_tg(text: str):
    if not PUBLISH_TG:
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": text}, timeout=10)
        if r.status_code != 200:
            logger.warning(f"TG失败 {r.status_code}: {r.text[:150]}")
    except Exception as e:
        logger.warning(f"TG异常: {e}")


def _fetch_entry_and_atr():
    try:
        p = requests.get(f"{FUTURE_BASE}/fapi/v1/ticker/price",
                         params={"symbol": SYMBOL}, timeout=8)
        p.raise_for_status()
        entry = float(p.json()["price"])
        k = requests.get(f"{FUTURE_BASE}/fapi/v1/klines",
                         params={"symbol": SYMBOL, "interval": ATR_INTERVAL, "limit": ATR_PERIOD + 1},
                         timeout=8)
        k.raise_for_status()
        kl = k.json()
        if len(kl) < ATR_PERIOD + 1:
            return None, None
        trs = []
        pc = float(kl[0][4])
        for row in kl[1:]:
            h, l, cl = float(row[2]), float(row[3]), float(row[4])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            pc = cl
        return entry, sum(trs) / len(trs)
    except Exception as e:
        logger.warning(f"entry/ATR 获取失败: {e}")
        return None, None


# ══ v1 规则评估（纯逻辑，输入 market_structure 结果 + entry/atr）══
def evaluate_rule(ms: dict, entry: float, atr: float):
    """返回 (direction, stop, t1, t2, risk, reason) 或 None。"""
    if ms.get("stale"):
        return None
    h4 = ms.get("h4") or {}
    pos = h4.get("pos_pct")
    if pos is None:
        return None
    week = ms.get("week") or {}
    y = ms.get("yesterday") or {}
    ab = ms.get("absorption") or {}
    tol_ref = entry * REF_TOL_PCT
    tol_abs = entry * ABS_TOL_PCT

    def _near(refs):
        cand = [(abs(entry - r), r) for r in refs if r]
        if not cand:
            return None
        d, r = min(cand)
        return r if d <= tol_ref else None

    # ── LONG：低位 + 贴近支撑 + 强买方吸收 ──
    if pos <= POS_LOW:
        sup = _near([week.get("val"), y.get("pdl"), (y.get("vp") or {}).get("poc")])
        strong = [b for b in ab.get("bid", [])
                  if b.get("ratio", 0) >= ABS_RATIO_MIN and b.get("bid", 0) >= ABS_BTC_MIN
                  and abs(entry - b["price"]) <= tol_abs]
        if sup is not None and strong:
            a = max(strong, key=lambda x: x["bid"])
            base = min(a["price"], sup, entry)
            stop = base - STOP_BUF_ATR * atr
            risk = entry - stop
            if MIN_R_ATR * atr <= risk <= MAX_R_ATR * atr:
                reason = (f"低位{pos:.0f}% 贴近支撑${sup:,.0f} "
                          f"买方吸收{a['ratio']:.1f}x/{a['bid']:.0f}BTC@${a['price']:,.0f}")
                return "LONG", stop, entry + risk, entry + 2 * risk, risk, reason

    # ── SHORT：高位 + 贴近阻力 + 强卖方吸收 ──
    if pos >= POS_HIGH:
        res = _near([week.get("vah"), y.get("pdh"), (y.get("vp") or {}).get("poc")])
        strong = [a for a in ab.get("ask", [])
                  if a.get("ratio", 0) >= ABS_RATIO_MIN and a.get("ask", 0) >= ABS_BTC_MIN
                  and abs(entry - a["price"]) <= tol_abs]
        if res is not None and strong:
            a = max(strong, key=lambda x: x["ask"])
            base = max(a["price"], res, entry)
            stop = base + STOP_BUF_ATR * atr
            risk = stop - entry
            if MIN_R_ATR * atr <= risk <= MAX_R_ATR * atr:
                reason = (f"高位{pos:.0f}% 贴近阻力${res:,.0f} "
                          f"卖方吸收{a['ratio']:.1f}x/{a['ask']:.0f}BTC@${a['price']:,.0f}")
                return "SHORT", stop, entry - risk, entry - 2 * risk, risk, reason
    return None


# ══ 冷却（拦生成，P0 同款）══════════════════════════════════════
_last_signal_ts = {"LONG": 0.0, "SHORT": 0.0}


def _cooldown_ok(direction):
    return time.time() - _last_signal_ts.get(direction, 0.0) >= COOLDOWN_MIN * 60


def fire_signal(direction, stop, t1, t2, risk, reason, entry, atr):
    e, s, a1, a2, rk = round(entry), round(stop), round(t1), round(t2), round(risk)
    now_bj = now_sgt()
    created = now_bj.strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH, timeout=5)
    cur = conn.execute(
        "INSERT INTO of_signals (created_at,direction,entry,stop,t1,t2,atr,risk,reason) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (created, direction, e, s, a1, a2, round(atr, 1), rk, reason))
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    _last_signal_ts[direction] = time.time()
    msg = (f"📈 订单流信号 #{sid}【纸面·观察】\n"
           f"方向：{direction}（反转承接 v1）\n"
           f"依据：{reason}\n"
           f"入场：${e:,.0f}\n止损：${s:,.0f}（风险${rk:,.0f}）\n"
           f"目标1：${a1:,.0f}（1R）  目标2：${a2:,.0f}（2R）\n"
           f"⚠️ 规则验证期，仅供观察，不构成操作依据\n"
           f"时间：{now_bj.strftime('%H:%M')}（北京）")
    _send_tg(msg)
    logger.info(f"信号#{sid} {direction} 入{e} 止{s} T2{a2} | {reason}")


# ══ 结果跟踪（复用 signal_engine 同款，无条件每轮先跑）══════════
_OUTCOME = {"stopped": "已止损", "t2_hit": "已触及目标2",
            "t1_then_stop": "触目标1后回落止损", "expired": "24小时到期"}


def evaluate_signal(sig, price, now_bj):
    d, stop, t1, t2 = sig["direction"], sig["stop"], sig["t1"], sig["t2"]
    t1t = bool(sig["t1_touched"])
    age_h = (now_bj.replace(tzinfo=None)
             - datetime.strptime(sig["created_at"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
    if d == "LONG":
        hit_t2, hit_stop, hit_t1 = price >= t2, price <= stop, price >= t1
    else:
        hit_t2, hit_stop, hit_t1 = price <= t2, price >= stop, price <= t1
    if hit_t2:
        return {"status": "t2_hit", "terminal": True}
    if hit_stop:
        return {"status": "t1_then_stop" if t1t else "stopped", "terminal": True}
    if hit_t1 and not t1t:
        return {"t1_touched": True, "terminal": False}
    if age_h >= EXPIRE_HOURS:
        return {"status": "expired", "terminal": True}
    return None


def check_outcomes():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM of_signals WHERE status='open'").fetchall()]
    conn.close()
    if not rows:
        return
    try:
        p = requests.get(f"{FUTURE_BASE}/fapi/v1/ticker/price",
                         params={"symbol": SYMBOL}, timeout=8)
        p.raise_for_status()
        price = float(p.json()["price"])
    except Exception as e:
        logger.warning(f"结果跟踪取价失败: {e}")
        return
    now_bj = now_sgt()
    now_str = now_bj.strftime("%Y-%m-%d %H:%M:%S")
    for sig in rows:
        act = evaluate_signal(sig, price, now_bj)
        if not act:
            continue
        if act.get("terminal"):
            conn = sqlite3.connect(DB_PATH, timeout=5)
            conn.execute("UPDATE of_signals SET status=?,outcome_price=?,outcome_at=? WHERE id=?",
                         (act["status"], price, now_str, sig["id"]))
            conn.commit(); conn.close()
            _send_tg(f"订单流#{sig['id']} {sig['direction']} {_OUTCOME.get(act['status'],act['status'])}\n"
                     f"结果价：${price:,.0f}　{now_bj.strftime('%H:%M')}（北京）")
            logger.info(f"信号#{sig['id']} 终态={act['status']} 价={price}")
        elif act.get("t1_touched"):
            conn = sqlite3.connect(DB_PATH, timeout=5)
            conn.execute("UPDATE of_signals SET t1_touched=1 WHERE id=?", (sig["id"],))
            conn.commit(); conn.close()
            logger.info(f"信号#{sig['id']} 触及目标1")


def _sleep_until_next_cycle():
    period = CYCLE_MIN * 60
    now = time.time()
    time.sleep(max(1.0, (int(now // period) + 1) * period - now))


def run_cycle():
    logger.info("── 订单流引擎新一轮 ──")
    check_outcomes()   # 无条件先跑(Bug#38教训)
    try:
        ms = get_market_structure()
    except Exception as e:
        logger.warning(f"market_structure 获取失败，本轮跳过生成: {e}")
        return
    if ms.get("stale"):
        logger.info(f"订单流数据滞后{ms.get('stale_min')}分钟，本轮不生成信号")
        return
    entry, atr = _fetch_entry_and_atr()
    if entry is None or atr is None:
        logger.warning("entry/ATR 不可用，本轮跳过")
        return
    h4 = ms.get("h4") or {}
    logger.info(f"评估 price={entry:.0f} 4H分位={h4.get('pos_pct')}% ATR={atr:.0f}")
    res = evaluate_rule(ms, entry, atr)
    if not res:
        return
    direction = res[0]
    if not _cooldown_ok(direction):
        logger.info(f"{direction} 命中规则但冷却中，本次抑制不记库")
        return
    fire_signal(direction, res[1], res[2], res[3], res[4], res[5], entry, atr)


def main():
    _ensure_table()
    logger.info("=" * 50)
    logger.info("订单流信号引擎启动（反转承接 v1，Phase 8）")
    logger.info(f"参数 低位<={POS_LOW}% 高位>={POS_HIGH}% 吸收>={ABS_RATIO_MIN}x/{ABS_BTC_MIN}BTC "
                f"冷却{COOLDOWN_MIN}min 周期{CYCLE_MIN}min TG={PUBLISH_TG}")
    logger.info("=" * 50)
    while True:
        _sleep_until_next_cycle()
        try:
            run_cycle()
        except Exception as e:
            logger.error(f"主循环异常: {e}", exc_info=True)


if __name__ == "__main__":
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("TG 配置缺失")
        raise SystemExit(1)
    main()
