"""
统一爆仓采集器（任务卡 9B）—— OKX / Bybit / Gate 三家一次性接入

设计要点（全部来自 9A 实测，见 reports/LIQ_PROBE_9A_20260811.md）：

1. 订阅报文直接复用 9A 探针里实测成功的写法，不自己重写。
2. **每个 topic 单独发一条 subscribe**。Bybit 的 subscribe 是整批语义，
   一次放多个 topic 时只要有一个无效（9A 实测 liquidation.BTCUSDT 已下线，
   返回 error:handler not found），整批被拒、同连接上所有订阅一起失效。
   为口径统一，OKX / Gate 也逐条发送。
3. Gate 用 futures.public_liquidates（公开频道），不用 futures.liquidates
   —— 后者 9A 实测返回 authentication required（code 4）。
4. OKX 的 liquidation-orders **不支持 instId 维度**（9B 实测 code 60018:
   "channel:liquidation-orders,instId:BTC-USDT-SWAP doesn't exist"），
   instFamily 也会被服务端静默丢弃（应答里 arg 只回 instType）。
   因此只能订阅 instType=SWAP 全市场，在解析入口第一行做合约过滤。
5. 双流健康检查：每路在同一连接上再订阅一条高频对照流，只计数不落库。
   连接健康 = 对照流 60 秒内有消息；对照流静默 >120 秒 → 主动断开重连；
   连续 3 次重连后对照流仍静默 → 推 Telegram。
   **爆仓流本身长时间无数据不告警** —— 9A 实测 Gate 首条爆仓延迟达
   39~53 分钟，属正常。
6. 不设金额门槛，全部落库。过滤在查询层做。
7. 内部时间戳统一 UTC 毫秒，仅在展示层转 UTC+8。

并行部署：本服务与 btc-liq-monitor / btc-gate-liq 同时运行，
写独立的 liquidations 表，不碰 binance_liq / gate_liquidations。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
import traceback
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
import websockets
from websockets.asyncio.client import connect as ws_connect

from utils.helpers import setup_logger, get_env
from alert_bot.send import async_send

logger = setup_logger("liq-unified")

# ── 常量 ──────────────────────────────────────────────────────────
DB_PATH      = "/opt/btc-trader/btc_history.db"
HEALTH_JSON  = Path("/opt/btc-trader/data/liq_unified_health.json")
HEALTH_HOST  = "127.0.0.1"
HEALTH_PORT  = 8011          # 独立端口，不动 btc-api(8001)

UTC8 = timezone(timedelta(hours=8))

CTRL_HEALTHY_SEC   = 60      # 对照流 60 秒内有消息 = 连接健康
CTRL_STALE_SEC     = 120     # 对照流静默超过 120 秒 → 主动断开重连
SILENT_ALERT_AFTER = 3       # 连续 3 次重连仍静默 → 告警
ALERT_COOLDOWN_SEC = 1800
RECONNECT_SLEEP    = 5
RECV_TIMEOUT_SEC   = 30      # recv 超时只用于驱动看门狗，不等于异常
LOG_EVERY_SEC      = 300
ERR_WINDOW_SEC     = 3600

# 合约规格（2026-08-11 实测核对，不靠记忆）
#   OKX  /api/v5/public/instruments: BTC-USDT-SWAP ctVal=0.01 BTC, ctMult=1
#        注意同时存在 BTC-USD-SWAP，ctVal=100 **USD**（反向合约，量纲不同），
#        所以合约过滤必须精确匹配，不能用 startswith("BTC")
#   Gate /api/v4/futures/usdt/contracts/BTC_USDT: quanto_multiplier=0.0001
OKX_CT_VAL   = 0.01
GATE_QUANTO  = 0.0001

OKX_INST     = "BTC-USDT-SWAP"
BYBIT_SYMBOL = "BTCUSDT"
GATE_CONTRACT = "BTC_USDT"
UNIFIED_SYMBOL = "BTCUSDT"

SIDE_LONG    = "LONG_LIQ"    # 多头被强平 → 产生强制卖出
SIDE_SHORT   = "SHORT_LIQ"   # 空头被强平 → 产生强制买入
SIDE_UNKNOWN = "UNKNOWN"     # 方向无法确证时落这个值，绝不猜

COLLECTOR_TAG = "unified"


def now_ms() -> int:
    return int(time.time() * 1000)


def fmt_utc8(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC8).strftime("%Y-%m-%d %H:%M:%S")


def log_exc(where: str, exc: BaseException) -> None:
    """记录完整异常类型 + 消息 + 堆栈。本文件不存在任何静默吞异常的分支。"""
    logger.warning(f"异常 @ {where}: {type(exc).__name__}: {exc}")
    logger.warning(traceback.format_exc())


# ══════════════════════════════════════════════════════════════════
#  建表
# ══════════════════════════════════════════════════════════════════
DDL = """
CREATE TABLE IF NOT EXISTS liquidations (
  uid         TEXT PRIMARY KEY,
  ts_ms       INTEGER NOT NULL,
  ts          INTEGER NOT NULL,
  exchange    TEXT NOT NULL,
  symbol      TEXT NOT NULL,
  side        TEXT NOT NULL,
  price       REAL NOT NULL,
  qty_btc     REAL NOT NULL,
  qty_usd     REAL NOT NULL,
  raw         TEXT,
  collector   TEXT NOT NULL,
  ingested_at INTEGER NOT NULL
);
"""
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_liq_ts      ON liquidations(ts)",
    "CREATE INDEX IF NOT EXISTS idx_liq_ex_ts   ON liquidations(exchange, ts)",
    "CREATE INDEX IF NOT EXISTS idx_liq_side_ts ON liquidations(side, ts)",
]


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute(DDL)
        for sql in INDEXES:
            conn.execute(sql)
        conn.commit()
        logger.info("liquidations 表与索引就绪（IF NOT EXISTS，不影响既有表）")
    finally:
        conn.close()


def _insert_sync(rows: list[tuple]) -> int:
    """INSERT OR IGNORE，靠 uid 主键去重。返回实际新增行数。"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO liquidations "
            "(uid, ts_ms, ts, exchange, symbol, side, price, qty_btc, qty_usd, "
            " raw, collector, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        return cur.rowcount if cur.rowcount is not None else 0
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════
#  每路状态
# ══════════════════════════════════════════════════════════════════
class ExState:
    def __init__(self, name: str):
        self.name = name
        self.connected = False
        self.last_ctrl_ms: int | None = None
        self.last_liq_ms: int | None = None
        self.reconnects = 0
        self.silent_streak = 0        # 连续"整条连接期内对照流零消息"的次数
        self.liq_events = 0           # 解析出的爆仓事件数
        self.ctrl_msgs = 0            # 对照流消息数
        self.rows_written = 0         # 实际写入数据库的行数（去重后）
        self.rows_dup = 0             # 被 uid 去重挡掉的行数
        self.skipped = 0              # 字段异常被跳过的事件数
        self.errors: deque = deque()  # (ts_ms, type, msg)
        self.acks: list[str] = []
        self.last_alert_ms = 0

    def mark_ctrl(self) -> None:
        self.last_ctrl_ms = now_ms()
        self.ctrl_msgs += 1

    def add_error(self, phase: str, exc: BaseException) -> None:
        self.errors.append((now_ms(), f"{phase}/{type(exc).__name__}", str(exc)[:300]))
        self.prune_errors()

    def prune_errors(self) -> None:
        cut = now_ms() - ERR_WINDOW_SEC * 1000
        while self.errors and self.errors[0][0] < cut:
            self.errors.popleft()

    def ctrl_age_sec(self) -> float | None:
        if self.last_ctrl_ms is None:
            return None
        return round((now_ms() - self.last_ctrl_ms) / 1000, 1)

    def liq_age_sec(self) -> float | None:
        if self.last_liq_ms is None:
            return None
        return round((now_ms() - self.last_liq_ms) / 1000, 1)

    def health(self) -> dict:
        self.prune_errors()
        ctrl_age = self.ctrl_age_sec()
        return {
            "connected": self.connected,
            "ctrl_healthy": ctrl_age is not None and ctrl_age <= CTRL_HEALTHY_SEC,
            "last_ctrl_msg_sec_ago": ctrl_age,
            "last_liq_event_sec_ago": self.liq_age_sec(),
            "reconnects": self.reconnects,
            "errors_1h": len(self.errors),
            "recent_errors": [
                {"at_utc8": fmt_utc8(t), "type": k, "message": m}
                for t, k, m in list(self.errors)[-5:]
            ],
            "liq_events": self.liq_events,
            "ctrl_msgs": self.ctrl_msgs,
            "rows_written": self.rows_written,
            "rows_dup_ignored": self.rows_dup,
            "events_skipped": self.skipped,
            "subscribe_acks": self.acks[:6],
        }


STATES: dict[str, ExState] = {
    "OKX": ExState("OKX"),
    "Bybit": ExState("Bybit"),
    "Gate": ExState("Gate"),
}


# ══════════════════════════════════════════════════════════════════
#  落库
# ══════════════════════════════════════════════════════════════════
async def store(st: ExState, exchange: str, ts_ms: int, side: str,
                price: float, qty_btc: float, raw: str) -> None:
    qty_usd = qty_btc * price
    uid = hashlib.md5(
        f"{exchange}|{ts_ms}|{side}|{price}|{qty_btc}".encode()
    ).hexdigest()
    row = (uid, ts_ms, ts_ms // 1000, exchange, UNIFIED_SYMBOL, side,
           price, qty_btc, qty_usd, raw[:4000], COLLECTOR_TAG, now_ms() // 1000)
    try:
        n = await asyncio.to_thread(_insert_sync, [row])
        st.rows_written += n
        if n == 0:
            st.rows_dup += 1
        st.liq_events += 1
        st.last_liq_ms = now_ms()
        logger.info(
            f"[{exchange}] {side} {qty_btc:.6f} BTC @ ${price:,.1f} "
            f"= ${qty_usd:,.0f} | 事件时间(UTC+8) {fmt_utc8(ts_ms)} | "
            f"{'新增' if n else '重复已忽略'}"
        )
    except Exception as exc:
        log_exc(f"{exchange} 落库", exc)
        st.add_error("db_insert", exc)


# ══════════════════════════════════════════════════════════════════
#  三家的订阅报文与解析
#    订阅一律「一个 topic 一条 subscribe」
# ══════════════════════════════════════════════════════════════════
def okx_subs() -> list[str]:
    return [
        json.dumps({"op": "subscribe", "args": [
            {"channel": "liquidation-orders", "instType": "SWAP"}]}),
        json.dumps({"op": "subscribe", "args": [
            {"channel": "trades", "instId": OKX_INST}]}),
    ]


async def okx_handle(raw: str, st: ExState) -> None:
    d = json.loads(raw)
    if d.get("event"):
        if len(st.acks) < 6:
            st.acks.append(raw[:300])
            logger.info(f"[OKX] 订阅应答: {raw[:260]}")
        if d.get("event") == "error":
            st.errors.append((now_ms(), "subscribe/error", raw[:300]))
        return
    ch = (d.get("arg") or {}).get("channel")
    if ch == "trades":
        st.mark_ctrl()
        return
    if ch != "liquidation-orders":
        return
    for item in d.get("data", []):
        # ── 解析入口第一行：精确匹配合约。
        #    不能用 startswith("BTC")：BTC-USD-SWAP 是 ctVal=100 USD 的反向
        #    合约，混进来会被当成张数×0.01 BTC 算，量纲错。
        if item.get("instId") != OKX_INST:
            continue
        for det in item.get("details", []):
            try:
                sz = float(det.get("sz", 0))
                px = float(det.get("bkPx", 0))
                ts_ms = int(det.get("ts", 0))
            except (TypeError, ValueError) as exc:
                log_exc("OKX 字段转换", exc)
                st.add_error("parse_field", exc)
                st.skipped += 1
                continue
            if sz <= 0 or px <= 0 or ts_ms <= 0:
                logger.warning(f"[OKX] 字段不合法，跳过: {det}")
                st.skipped += 1
                continue
            side_raw = det.get("side")
            # OKX: side 是**系统方向**。sell=系统强制卖出=多头被平；buy=反之
            if side_raw == "sell":
                side = SIDE_LONG
            elif side_raw == "buy":
                side = SIDE_SHORT
            else:
                side = SIDE_UNKNOWN
                logger.warning(f"[OKX] 未知 side={side_raw!r}，按 UNKNOWN 落库: {det}")
            await store(st, "OKX", ts_ms, side, px, sz * OKX_CT_VAL, raw)


def bybit_subs() -> list[str]:
    # 【强制】一个 topic 一条 subscribe。9A 实测整批语义下
    # liquidation.BTCUSDT 无效会连带废掉 allLiquidation 和 publicTrade。
    return [
        json.dumps({"op": "subscribe", "args": [f"allLiquidation.{BYBIT_SYMBOL}"]}),
        json.dumps({"op": "subscribe", "args": [f"publicTrade.{BYBIT_SYMBOL}"]}),
    ]


async def bybit_handle(raw: str, st: ExState) -> None:
    d = json.loads(raw)
    if "success" in d or d.get("op") in ("subscribe", "pong", "ping"):
        if len(st.acks) < 6:
            st.acks.append(raw[:300])
            logger.info(f"[Bybit] 订阅应答: {raw[:260]}")
        if d.get("success") is False:
            st.errors.append((now_ms(), "subscribe/rejected", raw[:300]))
            logger.warning(f"[Bybit] 订阅被拒: {raw[:260]}")
        return
    topic = d.get("topic")
    if topic == f"publicTrade.{BYBIT_SYMBOL}":
        st.mark_ctrl()
        return
    if topic != f"allLiquidation.{BYBIT_SYMBOL}":
        return
    for it in d.get("data", []) or []:
        if it.get("s") != BYBIT_SYMBOL:
            continue
        try:
            v = float(it.get("v", 0))        # 已是 BTC 数量
            p = float(it.get("p", 0))
            ts_ms = int(it.get("T", 0))
        except (TypeError, ValueError) as exc:
            log_exc("Bybit 字段转换", exc)
            st.add_error("parse_field", exc)
            st.skipped += 1
            continue
        if v <= 0 or p <= 0 or ts_ms <= 0:
            logger.warning(f"[Bybit] 字段不合法，跳过: {it}")
            st.skipped += 1
            continue
        # ── S 的语义（2026-08-11 实测 + 官方文档双重确证）──────────────
        #  Bybit 官方文档 allLiquidation 对 S 的原文：
        #    "Position side. Buy,Sell. When you receive a Buy update,
        #     this means that a long position has been liquidated"
        #  即 S 是**被强平持仓的方向**，不是委托单方向。故 Buy = 多头被平。
        #  实测印证：S=Buy 的 46 条事件里 44 条落在 1 分钟 K 线下跌段，
        #  且同期 OKX 的 posSide 全部为 long。
        #  ⚠️ 任务卡 9B 给出的映射（S=="Sell" → LONG_LIQ）与此相反，
        #     经上述两条独立证据判定卡片映射有误，此处按实测结论实现。
        s_raw = it.get("S")
        if s_raw == "Buy":
            side = SIDE_LONG
        elif s_raw == "Sell":
            side = SIDE_SHORT
        else:
            side = SIDE_UNKNOWN
            logger.warning(f"[Bybit] 未知 S={s_raw!r}，按 UNKNOWN 落库: {it}")
        await store(st, "Bybit", ts_ms, side, p, v, raw)


def gate_subs() -> list[str]:
    t = int(time.time())
    return [
        json.dumps({"time": t, "channel": "futures.public_liquidates",
                    "event": "subscribe", "payload": [GATE_CONTRACT]}),
        json.dumps({"time": t, "channel": "futures.trades",
                    "event": "subscribe", "payload": [GATE_CONTRACT]}),
    ]


async def gate_handle(raw: str, st: ExState) -> None:
    d = json.loads(raw)
    ev = d.get("event")
    ch = d.get("channel")
    if ev in ("subscribe", "unsubscribe") or d.get("error"):
        if len(st.acks) < 6:
            st.acks.append(raw[:300])
            logger.info(f"[Gate] 订阅应答: {raw[:260]}")
        if d.get("error"):
            st.errors.append((now_ms(), "subscribe/error", raw[:300]))
            logger.warning(f"[Gate] 订阅报错: {raw[:260]}")
        return
    if ch == "futures.trades":
        st.mark_ctrl()
        return
    if ch != "futures.public_liquidates":
        return
    res = d.get("result")
    items = res if isinstance(res, list) else ([res] if isinstance(res, dict) else [])
    for it in items:
        if it.get("contract") != GATE_CONTRACT:
            continue
        try:
            size = int(it.get("size", 0))
            p = float(it.get("price", 0))
            ts_ms = int(it.get("time", 0))
        except (TypeError, ValueError) as exc:
            log_exc("Gate 字段转换", exc)
            st.add_error("parse_field", exc)
            st.skipped += 1
            continue
        # 注意 price 的语义：实测 WS 的 price == REST 的 order_price
        # （6/6 精确吻合到 0.1），是**强平触发/委托价，不是成交价**。
        # 同期 REST 的 fill_price 比它高约 186 USD（约 0.3%）。
        # 因此本表 Gate 行的 price/qty_usd 是「委托价口径」，与 OKX(bkPx
        # 破产价)/Bybit(p 破产价) 同属"非成交价"，横比时口径一致；
        # 若将来要成交价口径，需另用 REST fill_price 回补。
        if size == 0 or p <= 0 or ts_ms <= 0:
            logger.warning(f"[Gate] 字段不合法，跳过: {it}")
            st.skipped += 1
            continue
        # ══ size 符号语义（2026-08-11 实测，WS 与 REST 约定相反，务必看完）══
        #
        #  【关键坑】WS futures.public_liquidates 与 REST liq_orders 的 size
        #  符号**完全相反**，绝不能混用同一套判定。实测证据：把 6 个 WS 事件
        #  按(秒, |size|)与 REST 记录配对，绝对值一一对应
        #  （77↔77 / 2↔2 / 8↔8 / 677↔677 / 299↔299 / 8↔8），
        #  符号 6/6 全部相反。
        #
        #  REST 侧（159 样本，价格走势检验）：
        #     size>0 → 104 次落在下跌分钟 / 9 次上涨  ⇒ size>0 = 多头爆仓
        #     size<0 →  40 次落在上涨分钟 / 5 次下跌  ⇒ size<0 = 空头爆仓
        #     即 REST 的 size 是**持仓**方向（正=多头持仓）。
        #
        #  WS 侧（本频道，即本函数处理的数据）：符号取反，
        #     **size<0 = 多头爆仓（LONG_LIQ）**，size>0 = 空头爆仓。
        #     WS 的 size 是**强平委托单**方向（卖出为负）。
        #     独立复核：WS size<0 的事件 5/6 落在下跌分钟；且同期 OKX 的
        #     posSide 全部为 long，两者一致。
        #
        #  ⚠️ 旧代码 monitor/gate_liq_monitor.py 注释称 "size>0 = 多头爆仓"
        #     —— 那是 **REST** 口径，对它自己（REST 轮询）是对的，
        #     但不能照搬到 WS。本卡按要求独立验证，未沿用其结论。
        if size < 0:
            side = SIDE_LONG
        elif size > 0:
            side = SIDE_SHORT
        else:
            side = SIDE_UNKNOWN
        await store(st, "Gate", ts_ms, side, p, abs(size) * GATE_QUANTO, raw)


EXCHANGES = {
    "OKX": {
        "url": "wss://ws.okx.com:8443/ws/v5/public",
        "subs": okx_subs,
        "handle": okx_handle,
    },
    "Bybit": {
        "url": "wss://stream.bybit.com/v5/public/linear",
        "subs": bybit_subs,
        "handle": bybit_handle,
    },
    "Gate": {
        "url": "wss://fx-ws.gateio.ws/v4/ws/usdt",
        "subs": gate_subs,
        "handle": gate_handle,
    },
}


# ══════════════════════════════════════════════════════════════════
#  单路采集主循环（含双流看门狗）
# ══════════════════════════════════════════════════════════════════
async def run_exchange(name: str) -> None:
    cfg = EXCHANGES[name]
    st = STATES[name]
    while True:
        got_ctrl_this_conn = False
        try:
            async with ws_connect(cfg["url"], ping_interval=20, ping_timeout=20,
                                  open_timeout=20, max_size=8 * 1024 * 1024) as ws:
                st.connected = True
                conn_start = now_ms()
                st.last_ctrl_ms = None
                for msg in cfg["subs"]():
                    await ws.send(msg)
                    await asyncio.sleep(0.3)      # 逐条发送，互不牵连
                logger.info(f"[{name}] 已连接，逐条发送 {len(cfg['subs']())} 条订阅")

                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_SEC)
                    except asyncio.TimeoutError:
                        raw = None               # 超时只是为了跑一次看门狗
                    if raw is not None:
                        try:
                            await cfg["handle"](raw, st)
                            if st.last_ctrl_ms is not None:
                                got_ctrl_this_conn = True
                        except json.JSONDecodeError as exc:
                            log_exc(f"{name} JSON 解析", exc)
                            st.add_error("json_decode", exc)
                        except Exception as exc:
                            log_exc(f"{name} 消息处理", exc)
                            st.add_error("handle", exc)

                    # ── 双流看门狗：只看对照流，不看爆仓流 ──
                    ref = st.last_ctrl_ms if st.last_ctrl_ms is not None else conn_start
                    silent = (now_ms() - ref) / 1000
                    if silent > CTRL_STALE_SEC:
                        logger.warning(
                            f"[{name}] 对照流静默 {silent:.0f}s（阈值 {CTRL_STALE_SEC}s），"
                            f"主动断开重连")
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_exc(f"{name} 连接/接收", exc)
            st.add_error("connect_or_recv", exc)

        st.connected = False
        st.reconnects += 1
        if got_ctrl_this_conn:
            st.silent_streak = 0
        else:
            st.silent_streak += 1
            logger.warning(f"[{name}] 本次连接期内对照流零消息，"
                           f"连续静默重连次数={st.silent_streak}")
            if st.silent_streak >= SILENT_ALERT_AFTER:
                await maybe_alert(st, name)
        await asyncio.sleep(RECONNECT_SLEEP)


async def maybe_alert(st: ExState, name: str) -> None:
    if now_ms() - st.last_alert_ms < ALERT_COOLDOWN_SEC * 1000:
        return
    st.last_alert_ms = now_ms()
    msg = (
        f"⚠️ <b>统一爆仓采集器 · {name} 通道异常</b>\n"
        f"对照流连续 {st.silent_streak} 次重连后仍无数据\n"
        f"重连累计：{st.reconnects} 次\n"
        f"1小时内异常：{len(st.errors)} 条\n"
        f"注：爆仓流本身无数据不会触发本告警\n"
        f"北京时间：{fmt_utc8(now_ms())}"
    )
    try:
        await async_send(msg)
        logger.warning(f"[{name}] 已推送 Telegram 告警")
    except Exception as exc:
        log_exc(f"{name} Telegram 告警", exc)
        st.add_error("telegram", exc)


# ══════════════════════════════════════════════════════════════════
#  健康快照 / HTTP 端点 / 周期日志
# ══════════════════════════════════════════════════════════════════
def build_health() -> dict:
    ms = now_ms()
    return {
        "collector": COLLECTOR_TAG,
        "server_ts_ms": ms,
        "server_time_utc8": fmt_utc8(ms),
        "note": "爆仓流长时间无数据属正常（Gate 首条延迟实测可达39~53分钟），"
                "健康判据只看对照流",
        "thresholds": {
            "ctrl_healthy_sec": CTRL_HEALTHY_SEC,
            "ctrl_stale_reconnect_sec": CTRL_STALE_SEC,
            "silent_alert_after_reconnects": SILENT_ALERT_AFTER,
        },
        "exchanges": {n: s.health() for n, s in STATES.items()},
    }


async def health_endpoint(_request: web.Request) -> web.Response:
    return web.json_response(build_health())


async def health_server() -> None:
    app = web.Application()
    app.router.add_get("/api/liq/health", health_endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HEALTH_HOST, HEALTH_PORT)
    await site.start()
    logger.info(f"健康端点已启动: http://{HEALTH_HOST}:{HEALTH_PORT}/api/liq/health")
    while True:
        await asyncio.sleep(3600)


async def health_snapshot_writer() -> None:
    """把健康快照写文件，供 api/liq_routes.py 挂进 btc-api 后读取（跨进程）"""
    HEALTH_JSON.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            tmp = HEALTH_JSON.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(build_health(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, HEALTH_JSON)      # 原子替换，避免读到半截文件
        except Exception as exc:
            log_exc("健康快照写文件", exc)
        await asyncio.sleep(5)


async def periodic_log() -> None:
    while True:
        await asyncio.sleep(LOG_EVERY_SEC)
        parts = []
        for n, s in STATES.items():
            h = s.health()
            parts.append(
                f"{n}: 连接={'是' if h['connected'] else '否'} "
                f"对照流={h['last_ctrl_msg_sec_ago']}s前({'健康' if h['ctrl_healthy'] else '异常'}) "
                f"爆仓事件={h['liq_events']}(入库{h['rows_written']},重复{h['rows_dup_ignored']}) "
                f"末次爆仓={h['last_liq_event_sec_ago']}s前 "
                f"重连={h['reconnects']} 1h异常={h['errors_1h']}"
            )
        logger.info("【5分钟汇总】" + " || ".join(parts))


# ══════════════════════════════════════════════════════════════════
async def run() -> None:
    logger.info("=" * 66)
    logger.info("统一爆仓采集器（9B）启动 —— OKX / Bybit / Gate 三路")
    logger.info(f"  websockets={websockets.__version__}  DB={DB_PATH}")
    logger.info(f"  OKX ctVal={OKX_CT_VAL} BTC/张（精确匹配 {OKX_INST}）")
    logger.info(f"  Gate quanto={GATE_QUANTO} BTC/张")
    logger.info(f"  健康判据：对照流 {CTRL_HEALTHY_SEC}s 内有消息；"
                f"静默 {CTRL_STALE_SEC}s 重连；连续 {SILENT_ALERT_AFTER} 次静默告警")
    logger.info("=" * 66)

    if get_env("UNIFIED_LIQ_ENABLED", "0").strip() != "1":
        logger.warning("UNIFIED_LIQ_ENABLED != 1，采集器进入空转（不连接、不落库）。"
                       "改 .env 后重启 btc-liq-unified 生效。")
        while True:
            await asyncio.sleep(LOG_EVERY_SEC)
            logger.info("空转中（UNIFIED_LIQ_ENABLED != 1）")

    init_db()

    tasks = [asyncio.create_task(health_server(), name="health-http"),
             asyncio.create_task(health_snapshot_writer(), name="health-file"),
             asyncio.create_task(periodic_log(), name="periodic-log")]
    for n in EXCHANGES:
        tasks.append(asyncio.create_task(run_exchange(n), name=f"collector-{n}"))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for t, r in zip(tasks, results):
        if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
            logger.error(f"任务 {t.get_name()} 以异常结束: {type(r).__name__}: {r}")
