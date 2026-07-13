#!/usr/bin/env python3
"""
signal_engine.py — 常驻信号引擎（Phase 7G）
上传位置：/opt/btc-trader/monitor/signal_engine.py
Supervisor 服务名：btc-signal-engine

背景：utils/signal_score.py 的六维综合分此前只在生成简报时算一次，
分数再高也不会主动提醒。本服务每5分钟独立计算一次综合分，分数突破
阈值时推送【模拟·纸面交易】信号（机械化入场/止损/目标价，不接任何
下单接口），并在同一服务内自动跟踪信号后续触及止损/目标/到期的结果，
为 Phase 5B 的胜率统计积累引擎信号样本。

数据同源原则：六维输入全部复用 briefing/ 和 data_collector/ 里简报
已经在用的现成函数，不另造采集逻辑，保证引擎分数和简报分数口径一致。
任一维度数据获取失败或过期，本轮直接跳过并记日志——绝不用旧值凑数
（Phase 7F 的教训：宁缺勿假）。

状态机（迟滞+冷却，全部常量集中在文件顶部，供 5B 回测后调参）：
  未武装(armed=False) 启动 → 分数回落到 ±REARM_BAND 以内才武装
  → 武装状态下分数触及 ±THRESH 触发一次信号并立即解除武装
  → 必须先回落到 ±REARM_BAND 以内才能再次武装
  冷却 COOLDOWN_MIN 分钟只影响是否发 Telegram，不影响是否触发/记库——
  冷却期内的触发依然写入 engine_signals，只是不发消息，避免刷屏。

红线：本服务产出全部是模拟信号，不接任何交易所下单接口。
"""
import json
import sqlite3
import sys
import time
from datetime import datetime

import requests

# 脚本放在 monitor/ 子目录下，用绝对路径启动时 sys.path[0] 是脚本自己所在
# 目录（monitor/），不是 /opt/btc-trader/，utils/data_collector/briefing 这些
# 兄弟包会 import 不到——显式把项目根目录加进 sys.path
sys.path.insert(0, "/opt/btc-trader")

from utils.helpers import setup_logger, get_env, now_sgt
from utils import signal_score
from data_collector.binance_data import get_spot_and_extras, get_long_short_ratio
from data_collector.etf_data import fetch_etf_flows
from briefing.binance_briefing_data import get_binance_context, get_market_meta

logger = setup_logger("signal_engine")

DB_PATH     = "/opt/btc-trader/btc_history.db"
FUTURE_BASE = "https://fapi.binance.com"
SYMBOL      = "BTCUSDT"

BOT_TOKEN = get_env("TELEGRAM_BOT_TOKEN")
CHAT_ID   = get_env("TELEGRAM_CHAT_ID")

# ── 常量集中区：供 Phase 5B 拿到胜率数据后统一调参 ──────────────────────
CYCLE_MIN     = 5     # 主循环周期（分钟），对齐到整点的整数倍
# 2026-07-13 循证校准(7N)：9日实测|分|max=33/p95=25，±60不可达；冻结至样本≥30
THRESH_LONG   = 25    # 综合分上穿 → LONG 信号
THRESH_SHORT  = -25   # 综合分下穿 → SHORT 信号
REARM_BAND    = 15    # 迟滞带：|分数|回落到此值以内才允许再次武装
COOLDOWN_MIN  = 90    # 同方向信号最小间隔（分钟），只影响是否发TG

ATR_INTERVAL  = "15m"
ATR_PERIOD    = 14
ATR_STOP_MULT = 1.5   # 止损 = entry∓1.5×ATR
ATR_T1_MULT   = 1.5   # 目标1 = entry±1.5×ATR（1R）
ATR_T2_MULT   = 3.0   # 目标2 = entry±3.0×ATR（2R）

EXPIRE_HOURS  = 24     # 开仓超过这个小时数未触及任何边界 → expired


# ══════════════════════════════════════════════════════════════════
#  数据库：engine_signals 表（7G）+ engine_scores 遥测表（7N）
# ══════════════════════════════════════════════════════════════════

def _ensure_table():
    # 全新表，没有历史迁移需要做，但仍遵守"先建表→再索引"的顺序约定
    # （历史教训见 api/main.py 的 _atas_db_init() 注释：索引依赖的字段
    # 如果表还没建好会直接报错中断）
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS engine_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            direction TEXT,
            score INTEGER,
            dims_json TEXT,
            entry REAL,
            stop REAL,
            t1 REAL,
            t2 REAL,
            atr REAL,
            status TEXT DEFAULT 'open',
            t1_touched INTEGER DEFAULT 0,
            outcome_price REAL,
            outcome_at TEXT
        );
    """)
    conn.commit()
    conn.close()

    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_engine_signals_status ON engine_signals(status)")
    conn.commit()
    conn.close()

    # 7N：每轮评分遥测表。ts(epoch秒)做主键天然去重；无额外索引。
    # 与 signal_scores 同名列语义对齐，但两张表严格分离——signal_scores
    # 只属于简报链路（环比对比读它的最新一条），引擎绝不能写入。
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS engine_scores (
            ts INTEGER PRIMARY KEY,
            composite REAL, label TEXT,
            etf_s REAL, fr_s REAL, quad_s REAL, ls_s REAL, cb_s REAL, regime_s REAL,
            detail_json TEXT
        )
    """)
    conn.commit()
    conn.close()
    logger.info("engine_signals / engine_scores 表已就绪")


def _record_score(result: dict):
    """
    7N遥测：每轮成功完成评分后落一行 engine_scores（跳过轮不写）。
    写库失败仅告警，绝不影响主循环。
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute(
            "INSERT OR REPLACE INTO engine_scores "
            "(ts, composite, label, etf_s, fr_s, quad_s, ls_s, cb_s, regime_s, detail_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), result["composite"], result["label"],
             result["etf_s"], result["fr_s"], result["quad_s"],
             result["ls_s"], result["cb_s"], result["regime_s"],
             json.dumps(result["detail"], ensure_ascii=False))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"engine_scores 遥测写入失败: {e}")


# ══════════════════════════════════════════════════════════════════
#  Telegram 发送（同步，engine 主循环本身是同步阻塞式，不引入 asyncio）
# ══════════════════════════════════════════════════════════════════

def _send_tg(text: str):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"TG 发送失败 HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"TG 发送异常: {e}")


# ══════════════════════════════════════════════════════════════════
#  六维数据采集（与简报同源，不另造采集逻辑）
# ══════════════════════════════════════════════════════════════════

# 7N：ETF维度进程内TTL缓存。ETF是日粒度数据，但此前引擎每5分钟全量执行
# 双源采集（含爬Farside网站），288次/天有封IP风险且毫无必要。成功结果
# 缓存1小时；TTL内直接复用不发网络请求；缓存过期且本轮实取失败 → 维持
# 原"宁缺勿假"逻辑跳过本轮，绝不拿过期缓存凑数。
ETF_CACHE_TTL_SEC = 3600
_etf_cache = {"ts": 0.0, "data": None}


def _fetch_etf_cached():
    now = time.time()
    age = now - _etf_cache["ts"]
    if _etf_cache["data"] is not None and age < ETF_CACHE_TTL_SEC:
        logger.info(f"ETF(缓存)：复用{int(age)}秒前的采集结果，TTL={ETF_CACHE_TTL_SEC}秒")
        return _etf_cache["data"]
    etf = fetch_etf_flows()   # 异常向上抛，由调用方按原逻辑跳过本轮
    if etf and etf.get("has_data"):
        _etf_cache["ts"] = now
        _etf_cache["data"] = etf
        logger.info(f"ETF(实取)：采集成功，缓存{ETF_CACHE_TTL_SEC}秒")
    return etf


def compute_current_score():
    """
    采集 compute_scores() 所需全部输入并调用之。
    任一环节数据不全 → 返回 None，调用方负责跳过本轮（已在这里记好日志）。
    """
    market_ctx = get_binance_context()
    if not market_ctx:
        logger.warning("跳过本轮：binance_structure 数据超过30分钟未更新（get_binance_context返回空）")
        return None

    meta         = get_market_meta()
    fr_zscore    = meta.get("fr_zscore")
    regime_label = meta.get("regime")
    if fr_zscore is None or not regime_label:
        logger.warning(f"跳过本轮：fr_zscore={fr_zscore} regime={regime_label!r} 数据不全")
        return None

    try:
        etf = _fetch_etf_cached()
    except Exception as e:
        logger.warning(f"跳过本轮：ETF数据获取异常: {e}")
        return None
    if not etf or not etf.get("has_data"):
        logger.warning("跳过本轮：ETF数据完全不可用（has_data=False）")
        return None

    try:
        extras = get_spot_and_extras()
    except Exception as e:
        logger.warning(f"跳过本轮：CB溢价数据获取异常: {e}")
        return None
    cb_premium = extras.get("cb_premium")
    if cb_premium is None:
        logger.warning("跳过本轮：CB溢价数据不可用（spot或coinbase任一路失败）")
        return None

    # 大户多空比实时REST快照，仅当 DB 里 binance_ls_top STALE 时才会被
    # compute_scores() 内部启用做降级兜底——与 briefing.py 的做法一致
    rest_ls_ratio_r = None
    try:
        ls_live = get_long_short_ratio()
        if ls_live.get("top_long_pct") and ls_live.get("top_short_pct"):
            rest_ls_ratio_r = ls_live["top_long_pct"] / ls_live["top_short_pct"]
    except Exception:
        pass   # 纯降级兜底用，拿不到不影响本轮是否跳过的判断

    return signal_score.compute_scores(
        fr_zscore, etf, cb_premium, regime_label, rest_ls_ratio_r
    )


# ══════════════════════════════════════════════════════════════════
#  入场价 + ATR(14, 15m)：纯机械计算，不调用AI
# ══════════════════════════════════════════════════════════════════

def _fetch_entry_and_atr():
    """
    entry = 当前 Binance USDT永续最新成交价（/fapi/v1/ticker/price）
    ATR   = 最近 ATR_PERIOD 根 15分钟K线的简单平均真实波幅
    任何异常返回 (None, None)，调用方负责跳过本次信号。
    """
    try:
        p = requests.get(
            f"{FUTURE_BASE}/fapi/v1/ticker/price",
            params={"symbol": SYMBOL}, timeout=8
        )
        p.raise_for_status()
        entry = float(p.json()["price"])

        k = requests.get(
            f"{FUTURE_BASE}/fapi/v1/klines",
            params={"symbol": SYMBOL, "interval": ATR_INTERVAL, "limit": ATR_PERIOD + 1},
            timeout=8
        )
        k.raise_for_status()
        klines = k.json()
        if len(klines) < ATR_PERIOD + 1:
            logger.warning(f"ATR K线数量不足: {len(klines)}")
            return None, None

        trs = []
        prev_close = float(klines[0][4])
        for row in klines[1:]:
            high, low, close = float(row[2]), float(row[3]), float(row[4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
            prev_close = close
        atr = sum(trs) / len(trs)
        return entry, atr
    except Exception as e:
        logger.warning(f"entry/ATR 获取失败: {e}")
        return None, None


# ══════════════════════════════════════════════════════════════════
#  状态机：迟滞武装 + 阈值触发（纯逻辑，无IO，方便离线单测）
# ══════════════════════════════════════════════════════════════════

class EngineState:
    """引擎重启后以'未武装'状态启动——内存状态，不持久化，重启清零。"""
    def __init__(self):
        self.armed = False
        self.last_tg_ts = {"LONG": 0.0, "SHORT": 0.0}   # 上次实际发出TG的时间戳


def check_trigger(state: EngineState, score: int):
    """
    返回 None（无触发）或 "LONG"/"SHORT"。
    冷却判断不在这里做——冷却只影响是否发TG，不影响触发/记库。
    """
    direction = None
    if state.armed:
        if score >= THRESH_LONG:
            direction = "LONG"
        elif score <= THRESH_SHORT:
            direction = "SHORT"

    if direction is not None:
        state.armed = False
    elif abs(score) <= REARM_BAND:
        state.armed = True

    return direction


def should_send_tg(state: EngineState, direction: str) -> bool:
    """同方向距离上次实际发送TG是否已超过 COOLDOWN_MIN 分钟"""
    last = state.last_tg_ts.get(direction, 0.0)
    return (time.time() - last) >= COOLDOWN_MIN * 60


# ══════════════════════════════════════════════════════════════════
#  信号生成 + 落库 + 推送
# ══════════════════════════════════════════════════════════════════

def _build_signal_msg(signal_id, direction, score_result, entry, stop, t1, t2, now_bj) -> str:
    d = score_result
    thresh = THRESH_LONG if direction == "LONG" else THRESH_SHORT
    cross  = "上穿阈值" if direction == "LONG" else "下穿阈值"
    dims_line = (
        f"ETF {d['etf_s']:+d} | 费率Z {d['fr_s']:+d} | OI象限 {d['quad_s']:+d} | "
        f"多空比 {d['ls_s']:+d} | CB溢价 {d['cb_s']:+d} | 市场状态 {d['regime_s']:+d}"
    )
    return (
        f"🎯 引擎信号 #{signal_id}【模拟·纸面交易】\n"
        f"方向：{direction}（综合分 {d['composite']:+d} {cross} {thresh:+d}）\n"
        f"六维明细：{dims_line}\n"
        f"入场参考：${entry:,.0f}\n"
        f"止损：${stop:,.0f}（1.5×ATR15m）\n"
        f"目标1：${t1:,.0f}（1R）  目标2：${t2:,.0f}（2R）\n"
        f"⚠️ 系统处于胜率验证期，仅供观察，不构成操作依据\n"
        f"时间：{now_bj.strftime('%H:%M')}（北京）"
    )


def fire_signal(direction: str, score_result: dict, state: EngineState):
    entry, atr = _fetch_entry_and_atr()
    if entry is None or atr is None:
        logger.warning(f"{direction}信号触发，但entry/ATR获取失败，本次信号放弃（不记库）")
        return

    if direction == "LONG":
        stop = entry - ATR_STOP_MULT * atr
        t1   = entry + ATR_T1_MULT * atr
        t2   = entry + ATR_T2_MULT * atr
    else:
        stop = entry + ATR_STOP_MULT * atr
        t1   = entry - ATR_T1_MULT * atr
        t2   = entry - ATR_T2_MULT * atr

    entry_r, stop_r, t1_r, t2_r = round(entry), round(stop), round(t1), round(t2)

    dims = {k: score_result[k] for k in
            ("etf_s", "fr_s", "quad_s", "ls_s", "cb_s", "regime_s")}

    now_bj = now_sgt()
    created_at = now_bj.strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_PATH, timeout=5)
    cur = conn.execute(
        "INSERT INTO engine_signals "
        "(created_at, direction, score, dims_json, entry, stop, t1, t2, atr) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (created_at, direction, score_result["composite"],
         json.dumps(dims, ensure_ascii=False),
         entry_r, stop_r, t1_r, t2_r, round(atr, 1))
    )
    signal_id = cur.lastrowid
    conn.commit()
    conn.close()

    if should_send_tg(state, direction):
        msg = _build_signal_msg(signal_id, direction, score_result,
                                 entry_r, stop_r, t1_r, t2_r, now_bj)
        _send_tg(msg)
        state.last_tg_ts[direction] = time.time()
        logger.info(f"信号#{signal_id} {direction} 综合分{score_result['composite']:+d} 已推送TG")
    else:
        logger.info(f"信号#{signal_id} {direction} 综合分{score_result['composite']:+d} 冷却中，仅记库不发TG")


# ══════════════════════════════════════════════════════════════════
#  结果自跟踪：stop / t1_touched / t2_hit / t1_then_stop / expired
# ══════════════════════════════════════════════════════════════════

_OUTCOME_LABEL = {
    "stopped":      "已触发止损",
    "t2_hit":       "已触及目标2",
    "t1_then_stop": "曾触及目标1后回落止损（保本/小亏归类）",
    "expired":      "持仓超24小时未触及任何边界，标记到期",
}


def evaluate_signal(sig: dict, price: float, now_bj: datetime):
    """
    纯逻辑判定（方便离线单测），返回：
      None                              — 无变化，继续持有
      {"t1_touched": True}              — 首次触及目标1，非终态
      {"status": "...", "terminal": True} — 终态
    """
    direction = sig["direction"]
    stop, t1, t2 = sig["stop"], sig["t1"], sig["t2"]
    t1_touched = bool(sig["t1_touched"])

    created_dt = datetime.strptime(sig["created_at"], "%Y-%m-%d %H:%M:%S")
    age_hours = (now_bj.replace(tzinfo=None) - created_dt).total_seconds() / 3600

    if direction == "LONG":
        hit_t2, hit_stop, hit_t1 = price >= t2, price <= stop, price >= t1
    else:
        hit_t2, hit_stop, hit_t1 = price <= t2, price >= stop, price <= t1

    if hit_t2:
        return {"status": "t2_hit", "terminal": True}
    if hit_stop:
        return {"status": "t1_then_stop" if t1_touched else "stopped", "terminal": True}
    if hit_t1 and not t1_touched:
        return {"t1_touched": True, "terminal": False}
    if age_hours >= EXPIRE_HOURS:
        return {"status": "expired", "terminal": True}
    return None


def _finalize_signal(signal_id, status, price, now_str):
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute(
        "UPDATE engine_signals SET status=?, outcome_price=?, outcome_at=? WHERE id=?",
        (status, price, now_str, signal_id)
    )
    conn.commit()
    conn.close()


def _mark_t1_touched(signal_id):
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("UPDATE engine_signals SET t1_touched=1 WHERE id=?", (signal_id,))
    conn.commit()
    conn.close()


def _send_outcome_receipt(sig: dict, status: str, price: float, now_bj: datetime):
    msg = (
        f"信号#{sig['id']} {sig['direction']} {_OUTCOME_LABEL.get(status, status)}\n"
        f"结果价：${price:,.0f}　时间：{now_bj.strftime('%H:%M')}（北京）"
    )
    _send_tg(msg)


def check_outcomes():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in
            conn.execute("SELECT * FROM engine_signals WHERE status='open'").fetchall()]
    conn.close()
    if not rows:
        return

    try:
        p = requests.get(
            f"{FUTURE_BASE}/fapi/v1/ticker/price",
            params={"symbol": SYMBOL}, timeout=8
        )
        p.raise_for_status()
        current_price = float(p.json()["price"])
    except Exception as e:
        logger.warning(f"结果自跟踪：获取最新价失败，本轮跳过跟踪: {e}")
        return

    now_bj = now_sgt()
    now_str = now_bj.strftime("%Y-%m-%d %H:%M:%S")

    for sig in rows:
        action = evaluate_signal(sig, current_price, now_bj)
        if action is None:
            continue
        if action.get("terminal"):
            _finalize_signal(sig["id"], action["status"], current_price, now_str)
            _send_outcome_receipt(sig, action["status"], current_price, now_bj)
            logger.info(f"信号#{sig['id']} {sig['direction']} 终态={action['status']} 结果价={current_price}")
        elif action.get("t1_touched"):
            _mark_t1_touched(sig["id"])
            logger.info(f"信号#{sig['id']} {sig['direction']} 已触及目标1，标记t1_touched")


# ══════════════════════════════════════════════════════════════════
#  主循环：每5分钟对齐一轮
# ══════════════════════════════════════════════════════════════════

def _sleep_until_next_cycle():
    now = time.time()
    period = CYCLE_MIN * 60
    next_tick = (int(now // period) + 1) * period
    time.sleep(max(1.0, next_tick - now))


def run_cycle(state: EngineState):
    logger.info("── 新一轮采集 ──")
    result = compute_current_score()
    if result is None:
        return

    score = result["composite"]
    logger.info(f"综合分={score:+d}（{result['label']}）armed={state.armed}")
    _record_score(result)   # 7N遥测：只有成功评分的轮次会走到这里

    direction = check_trigger(state, score)
    if direction:
        fire_signal(direction, result, state)

    check_outcomes()


def main():
    _ensure_table()
    logger.info("=" * 50)
    logger.info("BTC 信号引擎启动（Phase 7G）")
    logger.info(f"阈值 LONG>={THRESH_LONG:+d} SHORT<={THRESH_SHORT:+d} "
                f"迟滞±{REARM_BAND} 冷却{COOLDOWN_MIN}分钟 周期{CYCLE_MIN}分钟")
    logger.info("=" * 50)

    state = EngineState()

    while True:
        _sleep_until_next_cycle()
        try:
            run_cycle(state)
        except Exception as e:
            logger.error(f"信号引擎主循环异常: {e}", exc_info=True)


if __name__ == "__main__":
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未配置，检查 .env")
        raise SystemExit(1)
    main()
