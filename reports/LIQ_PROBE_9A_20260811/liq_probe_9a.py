#!/usr/bin/env python3
"""
任务卡 9A —— 爆仓数据可获取性探针（纯测试，不接入任何生产链路）

设计：每个交易所同时订阅「爆仓流」与「高频对照流」。
      对照流的作用是证明"连接确实在收数据"，从而把
      "地理封锁"（两条流都零数据）与
      "代码/订阅问题"（对照流有数据、爆仓流零数据）区分开。

依赖：websockets / aiohttp（仅此两个第三方库）
要求：Python 3.10+
运行：./venv/bin/python liq_probe_9a.py --duration-sec 14400

本文件在两个测试地点必须逐字节一致（脚本自身会计算并输出自己的 md5）。
所有异常都记录完整类型、消息与堆栈，不存在任何静默吞掉的分支。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import socket
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import aiohttp
import websockets
from websockets.asyncio.client import connect as ws_connect

CST = timezone(timedelta(hours=8))

# ══════════════════════════════════════════════════════════════════
#  流定义
#    kind: "liq"     = 爆仓流（目标）
#          "control" = 高频对照流（连通性证明）
#          "probe"   = 附加探测（Gate 公开爆仓频道的备选名）
# ══════════════════════════════════════════════════════════════════
#    「卡片指定」= 任务卡 9A 明确写出的 URL / 订阅方式，作为主测量
#    「诊断」    = 为隔离变量而额外加的连接。烟测发现两个干扰因素：
#                  ① Binance 组合流连上后完全静默（0 消息 0 错误），
#                     无法区分"整条 WS 被封"与"组合流里某个流名不被接受"；
#                  ② Bybit 把 3 个 topic 放在一次 subscribe 里，只要
#                     liquidation.BTCUSDT 无效（handler not found），
#                     整批订阅被拒，对照流 publicTrade 也拿不到数据，
#                     会把 Bybit 误判成"地理封锁"。
#                  故补充 raw 单流连接与逐 topic 订阅连接各自独立测量。
STREAM_DEFS = {
    "Binance": {
        "url": ("wss://fstream.binance.com/stream?streams="
                "!forceOrder@arr/btcusdt@aggTrade"),
        "note": "卡片指定：组合流一次订阅两条",
        "streams": {
            "!forceOrder@arr": "liq",
            "btcusdt@aggTrade": "control",
        },
    },
    "Binance-rawCtl": {
        "url": "wss://fstream.binance.com/ws/btcusdt@aggTrade",
        "note": "诊断：raw 单流端点，只订阅对照流",
        "raw_single": "btcusdt@aggTrade(raw单流)",
        "streams": {"btcusdt@aggTrade(raw单流)": "control"},
    },
    "Binance-rawLiq": {
        "url": "wss://fstream.binance.com/ws/!forceOrder@arr",
        "note": "诊断：raw 单流端点，只订阅爆仓流",
        "raw_single": "!forceOrder@arr(raw单流)",
        "streams": {"!forceOrder@arr(raw单流)": "liq"},
    },
    "Bybit": {
        "url": "wss://stream.bybit.com/v5/public/linear",
        "note": "卡片指定：3 个 topic 放在同一条 subscribe 里",
        "streams": {
            "liquidation.BTCUSDT": "liq",
            "allLiquidation.BTCUSDT": "liq",
            "publicTrade.BTCUSDT": "control",
        },
    },
    "Bybit-perTopic": {
        "url": "wss://stream.bybit.com/v5/public/linear",
        "note": "诊断：3 个 topic 分别单独 subscribe，互不牵连",
        "per_topic": True,
        "streams": {
            "liquidation.BTCUSDT": "liq",
            "allLiquidation.BTCUSDT": "liq",
            "publicTrade.BTCUSDT": "control",
        },
    },
    "OKX": {
        "url": "wss://ws.okx.com:8443/ws/v5/public",
        "note": "卡片指定",
        "streams": {
            "liquidation-orders": "liq",
            "trades": "control",
        },
    },
    "Gate": {
        "url": "wss://fx-ws.gateio.ws/v4/ws/usdt",
        "note": "卡片指定 + 附加探测 futures.public_liquidates",
        "streams": {
            "futures.liquidates": "liq",
            "futures.public_liquidates": "probe",
            "futures.trades": "control",
        },
    },
    "Hyperliquid": {
        "url": "https://api.hyperliquid.xyz/info",
        "note": "卡片指定：仅测连通性与响应时间，不判定清算",
        "streams": {
            "recentTrades(REST)": "control",
        },
    },
}

MAX_SAMPLES = 3          # 每条流保存的原始 JSON 样本数
SAMPLE_MAXLEN = 4000     # 单条样本最大保存长度（防止巨型消息撑爆报告）
RECONNECT_SLEEP = 5      # 断线重连间隔（秒）
HL_POLL_SEC = 30         # Hyperliquid REST 轮询间隔
HEARTBEAT_SEC = 60       # 累计计数打印间隔


# ══════════════════════════════════════════════════════════════════
#  记录容器
# ══════════════════════════════════════════════════════════════════
class StreamRec:
    """单条流的独立指标"""

    def __init__(self, exchange: str, name: str, kind: str):
        self.exchange = exchange
        self.name = name
        self.kind = kind
        self.msg_count = 0
        self.first_msg_mono: float | None = None   # 首条业务消息的单调时钟
        self.first_msg_wall: str | None = None
        self.last_msg_wall: str | None = None
        self.samples: list[str] = []

    def record(self, raw: str, conn_mono: float | None) -> None:
        self.msg_count += 1
        now_mono = time.monotonic()
        if self.first_msg_mono is None:
            self.first_msg_mono = now_mono
            self.first_msg_wall = _now_iso()
            self.first_delay_ms = (
                round((now_mono - conn_mono) * 1000, 1)
                if conn_mono is not None else None
            )
        self.last_msg_wall = _now_iso()
        if len(self.samples) < MAX_SAMPLES:
            self.samples.append(raw[:SAMPLE_MAXLEN])

    def to_dict(self) -> dict:
        return {
            "exchange": self.exchange,
            "stream": self.name,
            "kind": self.kind,
            "msg_count": self.msg_count,
            "first_msg_delay_ms": getattr(self, "first_delay_ms", None),
            "first_msg_at": self.first_msg_wall,
            "last_msg_at": self.last_msg_wall,
            "samples": self.samples,
        }


class ConnRec:
    """单个交易所连接层面的指标（一个交易所共用一条连接承载多条流）"""

    def __init__(self, exchange: str, url: str):
        self.exchange = exchange
        self.url = url
        self.attempts = 0
        self.successes = 0
        self.handshake_ms: list[float] = []
        self.sub_acks: list[str] = []        # 订阅确认原始内容，原样保存不解析
        self.errors: list[dict] = []
        self.connected_since_mono: float | None = None

    def add_error(self, phase: str, exc: BaseException) -> None:
        self.errors.append({
            "at": _now_iso(),
            "phase": phase,
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        })

    def to_dict(self) -> dict:
        return {
            "exchange": self.exchange,
            "note": STREAM_DEFS.get(self.exchange, {}).get("note", ""),
            "url": self.url,
            "connect_attempts": self.attempts,
            "connect_successes": self.successes,
            "connected_ok": self.successes > 0,
            "handshake_ms_first": self.handshake_ms[0] if self.handshake_ms else None,
            "handshake_ms_all": self.handshake_ms,
            "reconnects": max(0, self.successes - 1),
            "subscribe_acks_raw": self.sub_acks,
            "error_count": len(self.errors),
            "errors": self.errors,
        }


STREAMS: dict[tuple[str, str], StreamRec] = {}
CONNS: dict[str, ConnRec] = {}
UNMAPPED: list[dict] = []     # 收到但无法归入任何已知流的消息（含原始内容）
PRICE = {"hi": None, "lo": None, "first": None, "last": None,
         "first_at": None, "last_at": None, "n": 0}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    cst = datetime.now(CST).strftime("%H:%M:%S")
    print(f"[{ts}Z|{cst}CST] {msg}", flush=True)


def _log_exc(where: str, exc: BaseException) -> None:
    """打印完整类型 + 消息 + 堆栈，绝不静默"""
    _log(f"!! 异常 @ {where}: {type(exc).__name__}: {exc}")
    print(traceback.format_exc(), flush=True)


def _init_records() -> None:
    for ex, cfg in STREAM_DEFS.items():
        CONNS[ex] = ConnRec(ex, cfg["url"])
        for sname, kind in cfg["streams"].items():
            STREAMS[(ex, sname)] = StreamRec(ex, sname, kind)


def _note_price(px: float) -> None:
    """用对照流的成交价记录测试窗口内的 BTC 高低点"""
    if px <= 0:
        return
    PRICE["n"] += 1
    if PRICE["first"] is None:
        PRICE["first"] = px
        PRICE["first_at"] = _now_iso()
    PRICE["last"] = px
    PRICE["last_at"] = _now_iso()
    if PRICE["hi"] is None or px > PRICE["hi"]:
        PRICE["hi"] = px
    if PRICE["lo"] is None or px < PRICE["lo"]:
        PRICE["lo"] = px


def _rec(ex: str, sname: str, raw: str, conn_mono: float | None) -> None:
    key = (ex, sname)
    if key in STREAMS:
        STREAMS[key].record(raw, conn_mono)
    else:
        if len(UNMAPPED) < 40:
            UNMAPPED.append({"at": _now_iso(), "exchange": ex,
                             "stream_guess": sname, "raw": raw[:SAMPLE_MAXLEN]})


# ══════════════════════════════════════════════════════════════════
#  各交易所监听器
# ══════════════════════════════════════════════════════════════════
async def run_binance() -> None:
    ex = "Binance"
    conn = CONNS[ex]
    while True:
        conn.attempts += 1
        t0 = time.monotonic()
        try:
            async with ws_connect(conn.url, ping_interval=20, ping_timeout=20,
                                  open_timeout=20, max_size=8 * 1024 * 1024) as ws:
                hs = round((time.monotonic() - t0) * 1000, 1)
                conn.successes += 1
                conn.handshake_ms.append(hs)
                conn_mono = time.monotonic()
                conn.connected_since_mono = conn_mono
                _log(f"{ex}: 已连接（握手 {hs}ms，组合流 URL 自带订阅，无订阅确认消息）")
                async for raw in ws:
                    try:
                        d = json.loads(raw)
                    except (json.JSONDecodeError, TypeError) as exc:
                        _log_exc(f"{ex} json解析", exc)
                        conn.add_error("json_decode", exc)
                        continue
                    sname = d.get("stream")
                    if sname is None:
                        # 非组合流格式的消息（例如错误响应），原样留档
                        _rec(ex, "__unmapped__", raw, conn_mono)
                        continue
                    _rec(ex, sname, raw, conn_mono)
                    if sname == "btcusdt@aggTrade":
                        try:
                            _note_price(float(d.get("data", {}).get("p", 0)))
                        except (TypeError, ValueError) as exc:
                            _log_exc(f"{ex} 价格提取", exc)
                            conn.add_error("price_parse", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log_exc(f"{ex} 连接/接收", exc)
            conn.add_error("connect_or_recv", exc)
        await asyncio.sleep(RECONNECT_SLEEP)


async def run_binance_raw(ex: str) -> None:
    """诊断用：Binance raw 单流端点（/ws/<stream>），消息体没有 stream 字段"""
    conn = CONNS[ex]
    sname = STREAM_DEFS[ex]["raw_single"]
    while True:
        conn.attempts += 1
        t0 = time.monotonic()
        try:
            async with ws_connect(conn.url, ping_interval=20, ping_timeout=20,
                                  open_timeout=20, max_size=8 * 1024 * 1024) as ws:
                hs = round((time.monotonic() - t0) * 1000, 1)
                conn.successes += 1
                conn.handshake_ms.append(hs)
                conn_mono = time.monotonic()
                _log(f"{ex}: 已连接（握手 {hs}ms，raw 单流 {sname}）")
                async for raw in ws:
                    _rec(ex, sname, raw, conn_mono)
                    if "aggTrade" in sname:
                        try:
                            _note_price(float(json.loads(raw).get("p", 0)))
                        except (json.JSONDecodeError, TypeError, ValueError,
                                AttributeError) as exc:
                            _log_exc(f"{ex} 价格提取", exc)
                            conn.add_error("price_parse", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log_exc(f"{ex} 连接/接收", exc)
            conn.add_error("connect_or_recv", exc)
        await asyncio.sleep(RECONNECT_SLEEP)


async def run_bybit(ex: str = "Bybit") -> None:
    conn = CONNS[ex]
    per_topic = STREAM_DEFS[ex].get("per_topic", False)
    topics = list(STREAM_DEFS[ex]["streams"].keys())
    sub = json.dumps({"op": "subscribe", "args": topics})
    while True:
        conn.attempts += 1
        t0 = time.monotonic()
        try:
            async with ws_connect(conn.url, ping_interval=20, ping_timeout=20,
                                  open_timeout=20, max_size=8 * 1024 * 1024) as ws:
                hs = round((time.monotonic() - t0) * 1000, 1)
                conn.successes += 1
                conn.handshake_ms.append(hs)
                conn_mono = time.monotonic()
                if per_topic:
                    _log(f"{ex}: 已连接（握手 {hs}ms），逐个单独订阅 {len(topics)} 个 topic")
                    for tp in topics:
                        await ws.send(json.dumps({"op": "subscribe", "args": [tp]}))
                        await asyncio.sleep(0.3)
                else:
                    _log(f"{ex}: 已连接（握手 {hs}ms），一次性订阅 {len(topics)} 个 topic")
                    await ws.send(sub)
                async for raw in ws:
                    try:
                        d = json.loads(raw)
                    except (json.JSONDecodeError, TypeError) as exc:
                        _log_exc(f"{ex} json解析", exc)
                        conn.add_error("json_decode", exc)
                        continue
                    if d.get("op") in ("subscribe", "pong") or "success" in d:
                        if len(conn.sub_acks) < 8:
                            conn.sub_acks.append(raw[:SAMPLE_MAXLEN])
                            _log(f"{ex}: 订阅确认原文 = {raw[:400]}")
                        continue
                    topic = d.get("topic")
                    if topic is None:
                        _rec(ex, "__unmapped__", raw, conn_mono)
                        continue
                    _rec(ex, topic, raw, conn_mono)
                    if topic == "publicTrade.BTCUSDT":
                        try:
                            arr = d.get("data") or []
                            if arr:
                                _note_price(float(arr[0].get("p", 0)))
                        except (TypeError, ValueError, AttributeError, IndexError) as exc:
                            _log_exc(f"{ex} 价格提取", exc)
                            conn.add_error("price_parse", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log_exc(f"{ex} 连接/接收", exc)
            conn.add_error("connect_or_recv", exc)
        await asyncio.sleep(RECONNECT_SLEEP)


async def run_okx() -> None:
    ex = "OKX"
    conn = CONNS[ex]
    sub = json.dumps({"op": "subscribe", "args": [
        {"channel": "liquidation-orders", "instType": "SWAP"},
        {"channel": "trades", "instId": "BTC-USDT-SWAP"}]})
    while True:
        conn.attempts += 1
        t0 = time.monotonic()
        try:
            async with ws_connect(conn.url, ping_interval=20, ping_timeout=20,
                                  open_timeout=20, max_size=8 * 1024 * 1024) as ws:
                hs = round((time.monotonic() - t0) * 1000, 1)
                conn.successes += 1
                conn.handshake_ms.append(hs)
                conn_mono = time.monotonic()
                _log(f"{ex}: 已连接（握手 {hs}ms），发送订阅 2 个 channel")
                await ws.send(sub)
                async for raw in ws:
                    try:
                        d = json.loads(raw)
                    except (json.JSONDecodeError, TypeError) as exc:
                        _log_exc(f"{ex} json解析", exc)
                        conn.add_error("json_decode", exc)
                        continue
                    if d.get("event"):
                        if len(conn.sub_acks) < 8:
                            conn.sub_acks.append(raw[:SAMPLE_MAXLEN])
                            _log(f"{ex}: 订阅/事件确认原文 = {raw[:400]}")
                        continue
                    ch = (d.get("arg") or {}).get("channel")
                    if ch is None:
                        _rec(ex, "__unmapped__", raw, conn_mono)
                        continue
                    _rec(ex, ch, raw, conn_mono)
                    if ch == "trades":
                        try:
                            arr = d.get("data") or []
                            if arr:
                                _note_price(float(arr[0].get("px", 0)))
                        except (TypeError, ValueError, AttributeError, IndexError) as exc:
                            _log_exc(f"{ex} 价格提取", exc)
                            conn.add_error("price_parse", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log_exc(f"{ex} 连接/接收", exc)
            conn.add_error("connect_or_recv", exc)
        await asyncio.sleep(RECONNECT_SLEEP)


async def run_gate() -> None:
    ex = "Gate"
    conn = CONNS[ex]
    while True:
        conn.attempts += 1
        t0 = time.monotonic()
        try:
            async with ws_connect(conn.url, ping_interval=20, ping_timeout=20,
                                  open_timeout=20, max_size=8 * 1024 * 1024) as ws:
                hs = round((time.monotonic() - t0) * 1000, 1)
                conn.successes += 1
                conn.handshake_ms.append(hs)
                conn_mono = time.monotonic()
                _log(f"{ex}: 已连接（握手 {hs}ms），逐个订阅 3 个 channel")
                now = int(time.time())
                for ch in ("futures.liquidates", "futures.public_liquidates",
                           "futures.trades"):
                    await ws.send(json.dumps({
                        "time": now, "channel": ch,
                        "event": "subscribe", "payload": ["BTC_USDT"]}))
                    await asyncio.sleep(0.3)
                async for raw in ws:
                    try:
                        d = json.loads(raw)
                    except (json.JSONDecodeError, TypeError) as exc:
                        _log_exc(f"{ex} json解析", exc)
                        conn.add_error("json_decode", exc)
                        continue
                    event = d.get("event")
                    ch = d.get("channel")
                    if event in ("subscribe", "unsubscribe") or d.get("error"):
                        if len(conn.sub_acks) < 12:
                            conn.sub_acks.append(raw[:SAMPLE_MAXLEN])
                            _log(f"{ex}: 订阅应答原文 = {raw[:400]}")
                        continue
                    if ch is None:
                        _rec(ex, "__unmapped__", raw, conn_mono)
                        continue
                    _rec(ex, ch, raw, conn_mono)
                    if ch == "futures.trades":
                        try:
                            arr = d.get("result") or []
                            if isinstance(arr, list) and arr:
                                _note_price(float(arr[0].get("price", 0)))
                        except (TypeError, ValueError, AttributeError, IndexError) as exc:
                            _log_exc(f"{ex} 价格提取", exc)
                            conn.add_error("price_parse", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log_exc(f"{ex} 连接/接收", exc)
            conn.add_error("connect_or_recv", exc)
        await asyncio.sleep(RECONNECT_SLEEP)


async def run_hyperliquid() -> None:
    """仅测连通性与响应时间，不判定清算"""
    ex = "Hyperliquid"
    conn = CONNS[ex]
    rec = STREAMS[(ex, "recentTrades(REST)")]
    latencies: list[float] = []
    statuses: dict[str, int] = {}
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            while True:
                conn.attempts += 1
                t0 = time.monotonic()
                try:
                    async with sess.post(conn.url,
                                         json={"type": "recentTrades", "coin": "BTC"}) as resp:
                        body = await resp.text()
                        ms = round((time.monotonic() - t0) * 1000, 1)
                        latencies.append(ms)
                        statuses[str(resp.status)] = statuses.get(str(resp.status), 0) + 1
                        if resp.status == 200:
                            conn.successes += 1
                            conn.handshake_ms.append(ms)
                            rec.record(body, t0)
                        else:
                            conn.errors.append({
                                "at": _now_iso(), "phase": "http_status",
                                "type": "HTTPStatus", "message": f"status={resp.status} body={body[:300]}",
                                "traceback": "(HTTP 非 200，无 Python 堆栈)"})
                            _log(f"{ex}: HTTP {resp.status} body={body[:200]}")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _log_exc(f"{ex} REST 轮询", exc)
                    conn.add_error("rest_poll", exc)
                await asyncio.sleep(HL_POLL_SEC)
    except asyncio.CancelledError:
        conn.hl_latencies = latencies
        conn.hl_statuses = statuses
        raise
    finally:
        conn.hl_latencies = latencies
        conn.hl_statuses = statuses


# ══════════════════════════════════════════════════════════════════
#  心跳 / 收尾
# ══════════════════════════════════════════════════════════════════
async def heartbeat(deadline_mono: float) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SEC)
        left = int(deadline_mono - time.monotonic())
        parts = []
        for (ex, sname), r in STREAMS.items():
            tag = {"liq": "L", "control": "C", "probe": "P"}[r.kind]
            parts.append(f"{ex}/{sname}[{tag}]={r.msg_count}")
        px = (f"BTC last={PRICE['last']} hi={PRICE['hi']} lo={PRICE['lo']}"
              if PRICE["last"] else "BTC 无价格样本")
        _log(f"[心跳 剩余{left}s] {px}")
        _log("[心跳 计数] " + " | ".join(parts))


async def fetch_window_klines(start_ms: int, end_ms: int) -> dict:
    """收尾时用 REST K 线核对测试窗口内的 BTC 高低价（独立于 WS 流）"""
    out = {"source": None, "high": None, "low": None,
           "open": None, "close": None, "bars": 0, "errors": []}
    timeout = aiohttp.ClientTimeout(total=25)
    attempts = [
        ("binance-fapi",
         "https://fapi.binance.com/fapi/v1/klines",
         {"symbol": "BTCUSDT", "interval": "1m",
          "startTime": start_ms, "endTime": end_ms, "limit": 1500}),
        ("okx-candles",
         "https://www.okx.com/api/v5/market/history-candles",
         {"instId": "BTC-USDT-SWAP", "bar": "1m",
          "before": str(start_ms), "after": str(end_ms), "limit": "300"}),
    ]
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        for name, url, params in attempts:
            try:
                async with sess.get(url, params=params) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        out["errors"].append(f"{name}: HTTP {resp.status} {txt[:200]}")
                        continue
                    data = json.loads(txt)
                    rows = data if isinstance(data, list) else data.get("data") or []
                    if not rows:
                        out["errors"].append(f"{name}: 空数据 {txt[:200]}")
                        continue
                    if name == "binance-fapi":
                        highs = [float(r[2]) for r in rows]
                        lows = [float(r[3]) for r in rows]
                        out["open"] = float(rows[0][1])
                        out["close"] = float(rows[-1][4])
                    else:
                        highs = [float(r[2]) for r in rows]
                        lows = [float(r[3]) for r in rows]
                        out["open"] = float(rows[-1][1])
                        out["close"] = float(rows[0][4])
                    out["source"] = name
                    out["high"] = max(highs)
                    out["low"] = min(lows)
                    out["bars"] = len(rows)
                    return out
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                out["errors"].append(
                    f"{name}: {type(exc).__name__}: {exc}")
                _log_exc(f"K线核对 {name}", exc)
    return out


async def fetch_egress_ip() -> dict:
    info = {"ip": None, "raw": None, "error": None}
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get("https://ipinfo.io/json") as resp:
                txt = await resp.text()
                info["raw"] = txt[:800]
                try:
                    info["ip"] = json.loads(txt).get("ip")
                except json.JSONDecodeError as exc:
                    info["error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        _log_exc("出口IP查询", exc)
    return info


def self_md5() -> str:
    try:
        with open(os.path.abspath(__file__), "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError as exc:
        _log_exc("自身md5", exc)
        return f"ERROR:{type(exc).__name__}"


def verdict(ex: str) -> dict:
    """
    判定规则（"无爆仓事件" 与 "代码/订阅问题" 的最终区分需要人工交叉核对，
    脚本只给出机器可判定的部分，并标注是否需要外部交叉核对）
    """
    liq = [r for (e, _s), r in STREAMS.items() if e == ex and r.kind == "liq"]
    ctl = [r for (e, _s), r in STREAMS.items() if e == ex and r.kind == "control"]
    probe = [r for (e, _s), r in STREAMS.items() if e == ex and r.kind == "probe"]
    liq_n = sum(r.msg_count for r in liq)
    ctl_n = sum(r.msg_count for r in ctl)
    probe_n = sum(r.msg_count for r in probe)
    conn = CONNS[ex]
    if not conn.successes:
        v = "连接失败（疑似地理封锁或网络不可达）"
        need = False
    elif not ctl:
        # 单流诊断连接：本连接内没有对照流，不能独立下地理封锁结论
        v = (f"诊断连接（无对照流，仅记录爆仓流={liq_n} 条），"
             "判定须与同交易所的对照连接合并解读")
        need = liq_n == 0
    elif not liq and not probe:
        v = f"诊断连接（仅对照流={ctl_n} 条），用于证明本 IP 到该交易所的连通性"
        need = False
    elif ctl_n == 0 and liq_n == 0:
        v = "地理封锁（两条流都零数据）"
        need = False
    elif liq_n > 0 and ctl_n > 0:
        v = "可用（爆仓流与对照流都有数据）"
        need = False
    elif ctl_n > 0 and liq_n == 0:
        v = "爆仓流零数据/对照流有数据 —— 需交叉核对以区分【代码或订阅问题】与【无爆仓事件】"
        need = True
    else:
        v = "对照流零数据但爆仓流有数据（异常组合，需人工复核）"
        need = False
    return {"exchange": ex, "liq_msgs": liq_n, "control_msgs": ctl_n,
            "probe_msgs": probe_n, "verdict_auto": v,
            "needs_cross_check": need}


async def main() -> int:
    ap = argparse.ArgumentParser(description="任务卡 9A 爆仓数据可获取性探针")
    ap.add_argument("--duration-sec", type=int, default=14400,
                    help="测试时长（秒），默认 14400 = 4 小时")
    ap.add_argument("--out-dir", default=".", help="结果输出目录")
    args = ap.parse_args()

    _init_records()
    os.makedirs(args.out_dir, exist_ok=True)

    start_mono = time.monotonic()
    start_wall = datetime.now(timezone.utc)
    deadline = start_mono + args.duration_sec

    md5 = self_md5()
    egress = await fetch_egress_ip()

    env = {
        "hostname": socket.gethostname(),
        "egress_ip": egress.get("ip"),
        "egress_raw": egress.get("raw"),
        "egress_error": egress.get("error"),
        "python": sys.version.split()[0],
        "python_full": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "websockets": websockets.__version__,
        "aiohttp": aiohttp.__version__,
        "script_md5": md5,
        "script_path": os.path.abspath(__file__),
        "duration_sec": args.duration_sec,
        "start_utc": start_wall.strftime("%Y-%m-%d %H:%M:%S") + "Z",
        "start_cst": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S") + " CST",
    }

    _log("=" * 70)
    _log("任务卡 9A 爆仓数据可获取性探针 启动")
    for k in ("hostname", "egress_ip", "python", "websockets", "aiohttp",
              "script_md5", "duration_sec", "start_utc", "start_cst"):
        _log(f"  {k:14s} = {env[k]}")
    _log("=" * 70)

    tasks = [
        asyncio.create_task(run_binance(), name="binance"),
        asyncio.create_task(run_binance_raw("Binance-rawCtl"), name="binance-rawCtl"),
        asyncio.create_task(run_binance_raw("Binance-rawLiq"), name="binance-rawLiq"),
        asyncio.create_task(run_bybit("Bybit"), name="bybit"),
        asyncio.create_task(run_bybit("Bybit-perTopic"), name="bybit-perTopic"),
        asyncio.create_task(run_okx(), name="okx"),
        asyncio.create_task(run_gate(), name="gate"),
        asyncio.create_task(run_hyperliquid(), name="hyperliquid"),
        asyncio.create_task(heartbeat(deadline), name="heartbeat"),
    ]

    try:
        await asyncio.sleep(args.duration_sec)
    except asyncio.CancelledError:
        _log("主循环被取消，提前收尾")
    finally:
        for t in tasks:
            t.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for t, r in zip(tasks, results):
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                _log(f"!! 任务 {t.get_name()} 以异常结束: {type(r).__name__}: {r}")

    end_wall = datetime.now(timezone.utc)
    env["end_utc"] = end_wall.strftime("%Y-%m-%d %H:%M:%S") + "Z"
    env["end_cst"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S") + " CST"
    env["actual_duration_sec"] = round(time.monotonic() - start_mono, 1)

    klines = await fetch_window_klines(
        int(start_wall.timestamp() * 1000), int(end_wall.timestamp() * 1000))

    amp = None
    if klines.get("high") and klines.get("low"):
        amp = round((klines["high"] - klines["low"]) / klines["low"] * 100, 4)
    ws_amp = None
    if PRICE["hi"] and PRICE["lo"]:
        ws_amp = round((PRICE["hi"] - PRICE["lo"]) / PRICE["lo"] * 100, 4)

    hl = CONNS["Hyperliquid"]
    lat = getattr(hl, "hl_latencies", [])
    payload = {
        "env": env,
        "window_btc": {
            "from_ws_control_streams": {
                "high": PRICE["hi"], "low": PRICE["lo"],
                "first": PRICE["first"], "last": PRICE["last"],
                "first_at": PRICE["first_at"], "last_at": PRICE["last_at"],
                "tick_samples": PRICE["n"], "amplitude_pct": ws_amp,
            },
            "from_rest_klines": dict(klines, amplitude_pct=amp),
        },
        "connections": {ex: CONNS[ex].to_dict() for ex in STREAM_DEFS},
        "streams": [STREAMS[k].to_dict() for k in STREAMS],
        "hyperliquid_rest": {
            "polls": hl.attempts,
            "http_200": hl.successes,
            "status_counts": getattr(hl, "hl_statuses", {}),
            "latency_ms_min": min(lat) if lat else None,
            "latency_ms_max": max(lat) if lat else None,
            "latency_ms_avg": round(sum(lat) / len(lat), 1) if lat else None,
        },
        "unmapped_messages": UNMAPPED,
        "verdicts_auto": [verdict(ex) for ex in STREAM_DEFS],
    }

    tag = f"{env['hostname']}_{(env['egress_ip'] or 'noip').replace('.', '-')}"
    out = os.path.join(args.out_dir, f"probe9a_{tag}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    _log("=" * 70)
    _log(f"测试结束，结果写入 {out}")
    for v in payload["verdicts_auto"]:
        _log(f"  {v['exchange']:12s} liq={v['liq_msgs']:<8d} ctl={v['control_msgs']:<8d} "
             f"probe={v['probe_msgs']:<6d} → {v['verdict_auto']}")
    _log(f"  BTC 窗口振幅：WS={ws_amp}%  REST-K线={amp}%（源 {klines.get('source')}）")
    _log("=" * 70)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        _log("收到 KeyboardInterrupt，退出")
        sys.exit(130)
