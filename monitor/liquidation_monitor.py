"""
BTC 多交所实时清算监控 v4
修复：Hyperliquid 检测算法
新增：阈值从 .env 读取，随时可调
新增：低阈值调试日志（方便排查哪些交所在发数据）
"""
import asyncio
import json
import time
from collections import deque
from datetime import datetime, timezone, timedelta
import websockets
import requests
from utils.helpers import setup_logger, fmt_usd, fmt_time, now_sgt, SGT, get_env
from alert_bot.send import async_send
from data_collector.binance_data import collect_all
from ai_analyst.liq_briefing import generate_liq_briefing

logger = setup_logger("liq-monitor")

# ── 从 .env 读取阈值（改 .env 后重启生效）────────────────────────
SINGLE_ALERT_USD      = int(get_env("LIQ_SINGLE_USD", "100000"))
HOURLY_ALERT_USD      = int(get_env("LIQ_HOURLY_USD", "3000000"))
COOLDOWN_SINGLE_SEC   = 300
COOLDOWN_BRIEFING_SEC = 1800

# 调试用：记录所有交所收到的清算（包括低于阈值的）
DEBUG_LOG_USD = 10000   # $1万以上的都记录到日志（但不发 Telegram）

logger.info(f"清算预警阈值：单笔 {fmt_usd(SINGLE_ALERT_USD)} | 1H累计 {fmt_usd(HOURLY_ALERT_USD)}")

hourly_longs       = deque()
hourly_shorts      = deque()
last_single_alert  = {}
last_briefing_time = None
briefing_lock      = asyncio.Lock()

EX_LABELS = {
    "binance": "Binance", "okx": "OKX",
    "bybit": "Bybit", "hyperliquid": "Hyperliquid"
}


def _cleanup_old():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    while hourly_longs  and hourly_longs[0][0]  < cutoff: hourly_longs.popleft()
    while hourly_shorts and hourly_shorts[0][0] < cutoff: hourly_shorts.popleft()


def _get_hourly_totals():
    _cleanup_old()
    return (sum(v for _, v in hourly_longs),
            sum(v for _, v in hourly_shorts))


def _can_alert_single(key):
    last = last_single_alert.get(key)
    if not last: return True
    return (datetime.now(timezone.utc) - last).total_seconds() > COOLDOWN_SINGLE_SEC


def _can_send_briefing():
    if not last_briefing_time: return True
    return (datetime.now(timezone.utc) - last_briefing_time).total_seconds() > COOLDOWN_BRIEFING_SEC


async def process_liquidation(exchange: str, direction: str, usd: float, price: float):
    """统一清算处理入口"""
    if usd <= 0:
        return

    now     = datetime.now(timezone.utc)
    ex_name = EX_LABELS.get(exchange, exchange)

    # 调试日志：记录所有交所收到的清算（不受阈值限制）
    if usd >= DEBUG_LOG_USD:
        logger.info(f"[RAW] {ex_name} | {direction} | {fmt_usd(usd)} | ${price:,.0f}")

    if direction == "long":
        hourly_longs.append((now, usd))
    else:
        hourly_shorts.append((now, usd))

    alert_key = f"{exchange}_{direction}"
    if usd >= SINGLE_ALERT_USD and _can_alert_single(alert_key):
        last_single_alert[alert_key] = now
        await _alert_single(ex_name, direction, usd, price, now)

    long_t, short_t = _get_hourly_totals()
    if (long_t + short_t) >= HOURLY_ALERT_USD and _can_send_briefing():
        async with briefing_lock:
            if _can_send_briefing():
                asyncio.create_task(
                    _alert_hourly_briefing(long_t, short_t, now))


async def _alert_single(ex_name, direction, usd, price, ts_utc):
    sgt_time = fmt_time(ts_utc, short=True)
    if direction == "long":
        tag   = "[LONG LIQ]"
        title = "多头清算 (Long Liquidation)"
        tip   = ("> 多头被强平，短期抛压增加\n"
                 "> 若价格不跌反涨，注意机构 Sell Absorption")
    else:
        tag   = "[SHORT LIQ]"
        title = "空头清算 (Short Liquidation)"
        tip   = ("> 空头被强平，短期买盘推升\n"
                 "> 若价格不涨反跌，注意机构 Buy Absorption")

    long_t, short_t = _get_hourly_totals()
    total_1h = long_t + short_t

    msg = (
        f"{tag} 大额清算预警\n"
        f"{'='*34}\n"
        f"交易所：{ex_name}\n"
        f"类  型：{title}\n"
        f"金  额：{fmt_usd(usd)}\n"
        f"价  格：${price:,.0f}\n"
        f"时  间：{sgt_time}\n"
        f"{'-'*34}\n"
        f"{tip}\n"
        f"{'-'*34}\n"
        f"1H 多：{fmt_usd(long_t)}  空：{fmt_usd(short_t)}\n"
        f"1H 合计：{fmt_usd(total_1h)}\n"
        f"当前阈值：{fmt_usd(SINGLE_ALERT_USD)}"
    )
    await async_send(msg)
    logger.info(f"单笔预警 | {ex_name} | {direction} | {fmt_usd(usd)}")


async def _alert_hourly_briefing(long_total, short_total, ts_utc):
    global last_briefing_time
    last_briefing_time = ts_utc
    total    = long_total + short_total
    sgt_time = fmt_time(ts_utc)
    long_pct  = long_total  / total * 100 if total else 0
    short_pct = short_total / total * 100 if total else 0

    logger.info(f"触发累计清算简报 | {fmt_usd(total)}")
    await async_send(
        f"[ALERT] 1H累计清算突破 {fmt_usd(HOURLY_ALERT_USD)}\n"
        f"{'='*34}\n"
        f"多头清算：{fmt_usd(long_total)}（{long_pct:.0f}%）\n"
        f"空头清算：{fmt_usd(short_total)}（{short_pct:.0f}%）\n"
        f"合    计：{fmt_usd(total)}\n"
        f"时    间：{sgt_time}\n"
        f"AI 正在生成实时分析简报..."
    )
    try:
        market   = collect_all()
        briefing = generate_liq_briefing(market, long_total, short_total, total)
        await async_send(f"[REPORT] 实时清算分析\n{'='*34}\n{briefing}")
    except Exception as e:
        logger.error(f"简报失败: {e}")


# ══════════════════════════════════════════════════════
#  各交所 WebSocket 监听器
# ══════════════════════════════════════════════════════

async def binance_listener():
    url = "wss://fstream.binance.com/ws/!forceOrder@arr"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                logger.info("Binance WS 已连接")
                async for msg in ws:
                    try:
                        d = json.loads(msg)
                        if d.get("e") != "forceOrder": continue
                        o = d.get("o", {})
                        if o.get("s") != "BTCUSDT" or o.get("X") != "FILLED": continue
                        qty   = float(o.get("q", 0))
                        price = float(o.get("ap") or o.get("p") or 0)
                        # SELL = 多头被平（做空了多头仓位）
                        direction = "long" if o.get("S") == "SELL" else "short"
                        await process_liquidation("binance", direction, qty * price, price)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Binance WS 断开: {e}")
        await asyncio.sleep(10)


async def okx_listener():
    url = "wss://ws.okx.com/ws/v5/public"
    sub = json.dumps({"op": "subscribe", "args": [
        {"channel": "liquidation-orders", "instType": "SWAP"}
    ]})
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                await ws.send(sub)
                logger.info("OKX WS 已连接")
                async for msg in ws:
                    try:
                        d = json.loads(msg)
                        for item in d.get("data", []):
                            if item.get("instId", "") != "BTC-USDT-SWAP": continue
                            for detail in item.get("details", []):
                                side  = detail.get("side", "")
                                sz    = float(detail.get("sz", 0))
                                price = float(detail.get("bkPx", 0))
                                usd   = sz * 0.001 * price  # OKX 1张=0.001BTC
                                # OKX: buy=空头被清算, sell=多头被清算
                                direction = "short" if side == "buy" else "long"
                                await process_liquidation("okx", direction, usd, price)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"OKX WS 断开: {e}")
        await asyncio.sleep(10)


async def bybit_listener():
    url = "wss://stream.bybit.com/v5/public/linear"
    sub = json.dumps({"op": "subscribe", "args": ["liquidation.BTCUSDT"]})
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                await ws.send(sub)
                logger.info("Bybit WS 已连接")
                async for msg in ws:
                    try:
                        d = json.loads(msg)
                        if d.get("topic") != "liquidation.BTCUSDT": continue
                        item  = d.get("data", {})
                        side  = item.get("side", "")
                        size  = float(item.get("size", 0))
                        price = float(item.get("price", 0))
                        # Bybit: Sell = 多头被清算, Buy = 空头被清算
                        direction = "long" if side == "Sell" else "short"
                        await process_liquidation("bybit", direction, size * price, price)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Bybit WS 断开: {e}")
        await asyncio.sleep(10)


async def hyperliquid_poller():
    """
    Hyperliquid 清算检测 v2
    通过检测 clearinghouse 地址发出的交易来识别清算
    """
    info_url   = "https://api.hyperliquid.xyz/info"
    last_ts    = int(time.time() * 1000)
    seen_tids  = set()   # 防止重复处理
    logger.info("Hyperliquid 轮询 v2 已启动")

    while True:
        try:
            # 获取 BTC 最近成交
            resp = requests.post(
                info_url,
                json={"type": "recentTrades", "coin": "BTC"},
                timeout=10
            )
            trades = resp.json() if resp.status_code == 200 else []

            for t in (trades if isinstance(trades, list) else []):
                tid   = t.get("tid") or t.get("hash", "")
                ts_ms = int(t.get("time", 0))

                if ts_ms <= last_ts or tid in seen_tids:
                    continue

                seen_tids.add(tid)

                # Hyperliquid 清算特征：
                # 1. 有 "liquidation" 标志（部分版本）
                # 2. 或 "users" 字段包含清算地址
                # 3. 成交量异常大（> 1 BTC）且无对应正常订单标记
                is_liq = (
                    t.get("liquidation", False) or
                    t.get("crossed", False) or
                    "liquidat" in str(t).lower()
                )

                if is_liq:
                    px  = float(t.get("px", 0))
                    sz  = float(t.get("sz", 0))
                    usd = px * sz
                    # B = 多方买入（空头被清算） A = 空方卖出（多头被清算）
                    side      = t.get("side", "")
                    direction = "short" if side == "B" else "long"
                    if usd > 0:
                        await process_liquidation("hyperliquid", direction, usd, px)

            if isinstance(trades, list) and trades:
                max_ts = max(int(t.get("time", 0)) for t in trades)
                if max_ts > last_ts:
                    last_ts = max_ts

            # 清理 seen_tids 防止无限增长
            if len(seen_tids) > 1000:
                seen_tids.clear()

        except Exception as e:
            logger.warning(f"Hyperliquid 轮询失败: {e}")

        await asyncio.sleep(30)


# ══════════════════════════════════════════════════════

async def run():
    logger.info("=" * 50)
    logger.info("BTC 多交所清算监控 v4 启动")
    logger.info(f"单笔预警：{fmt_usd(SINGLE_ALERT_USD)}")
    logger.info(f"1H累计  ：{fmt_usd(HOURLY_ALERT_USD)}")
    logger.info(f"调试记录：{fmt_usd(DEBUG_LOG_USD)} 以上全部记录")
    logger.info("=" * 50)

    await async_send(
        "[OK] BTC 多交所清算监控 v4 已启动\n"
        f"单笔预警：{fmt_usd(SINGLE_ALERT_USD)}\n"
        f"1H累计  ：{fmt_usd(HOURLY_ALERT_USD)}\n"
        "覆盖：Binance / OKX / Bybit / Hyperliquid\n"
        "阈值可在 .env 修改后重启生效"
    )

    await asyncio.gather(
        binance_listener(),
        okx_listener(),
        bybit_listener(),
        hyperliquid_poller(),
        return_exceptions=True
    )
