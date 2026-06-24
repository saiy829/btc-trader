"""
BTC 订单簿大额挂单监控 v3
数据源：OKX books + Bybit orderbook.200
关键改进：挂单必须持续 N 秒才预警，过滤高频刷单
"""
import asyncio
import json
import time
from utils.helpers import setup_logger, fmt_usd, fmt_time, get_env
from alert_bot.send import async_send
import websockets

logger = setup_logger("dom-monitor")

THRESHOLD_ALERT  = int(get_env("DOM_ALERT_USD",    "10000000"))
THRESHOLD_STRONG = int(get_env("DOM_STRONG_USD",   "50000000"))
THRESHOLD_MEGA   = int(get_env("DOM_MEGA_USD",    "100000000"))
COOLDOWN_SEC     = 600
SPOOF_MIN_USD    = int(get_env("DOM_STRONG_USD",   "50000000"))
SPOOF_WINDOW_SEC = 60
PENDING_SEC      = int(get_env("DOM_PENDING_SEC",       "15"))   # 持续N秒才预警
MIN_DIST_PCT     = float(get_env("DOM_MIN_DIST_PCT",   "0.5"))   # 忽略价格N%以内
MAX_DIST_PCT     = 5.0
CHECK_INTERVAL   = 1.0

logger.info(f"预警阈值：{fmt_usd(THRESHOLD_ALERT)} | 持续要求：{PENDING_SEC}秒 | 最小距离：{MIN_DIST_PCT}%")

alert_times  = {}
known_large  = {}   # {key: {usd, ts, alerted, first_seen}}
mid_price    = 0.0
last_check   = 0.0


class OrderBook:
    def __init__(self, name):
        self.name = name
        self.bids = {}
        self.asks = {}

    def snapshot(self, bids_data, asks_data):
        self.bids = {r[0]: float(r[1]) for r in bids_data if float(r[1]) > 0}
        self.asks = {r[0]: float(r[1]) for r in asks_data if float(r[1]) > 0}

    def delta(self, bids_data, asks_data):
        for r in bids_data:
            qty = float(r[1])
            if qty == 0: self.bids.pop(r[0], None)
            else:        self.bids[r[0]] = qty
        for r in asks_data:
            qty = float(r[1])
            if qty == 0: self.asks.pop(r[0], None)
            else:        self.asks[r[0]] = qty

    def large_levels(self, min_usd, mid):
        if not mid:
            return []
        results = []
        for side, book in [("bid", self.bids), ("ask", self.asks)]:
            for p_str, qty in book.items():
                price = float(p_str)
                usd   = price * qty
                if usd < min_usd:
                    continue
                dist = abs(price - mid) / mid * 100
                # 过滤太近（正常买卖盘）和太远（无关）的挂单
                if dist < MIN_DIST_PCT or dist > MAX_DIST_PCT:
                    continue
                results.append({
                    "exchange": self.name, "side": side,
                    "price": price, "qty": qty, "usd": usd,
                    "dist": dist,
                    "direction": "下方" if side == "bid" else "上方"
                })
        return sorted(results, key=lambda x: x["usd"], reverse=True)


okx_book   = OrderBook("OKX")
bybit_book = OrderBook("Bybit")


def _can_alert(key):
    t = alert_times.get(key)
    return not t or (time.time() - t) > COOLDOWN_SEC


def _level_tag(usd):
    if usd >= THRESHOLD_MEGA:
        return "[DOM MEGA]", "超大额挂单 紧急预警"
    elif usd >= THRESHOLD_STRONG:
        return "[DOM STRONG]", "大额挂单 强力预警"
    return "[DOM]", "大额挂单预警"


async def check_books():
    global last_check, mid_price
    now = time.time()
    if now - last_check < CHECK_INTERVAL:
        return
    last_check = now

    all_bids = {**okx_book.bids, **bybit_book.bids}
    all_asks = {**okx_book.asks, **bybit_book.asks}
    if all_bids and all_asks:
        best_bid = max(float(p) for p in all_bids)
        best_ask = min(float(p) for p in all_asks)
        mid_price = (best_bid + best_ask) / 2

    seen_keys = set()
    for book in [okx_book, bybit_book]:
        for lv in book.large_levels(THRESHOLD_ALERT, mid_price):
            key = (lv["exchange"], lv["side"], round(lv["price"]))
            seen_keys.add(key)
            prev = known_large.get(key)

            if prev is None:
                # 新出现的大单 → 先记录，不立即预警
                known_large[key] = {
                    "usd":        lv["usd"],
                    "ts":         now,
                    "first_seen": now,
                    "alerted":    False
                }
                logger.info(f"[待观察] {lv['exchange']} {lv['side'].upper()} "
                            f"${lv['price']:,.0f} | {fmt_usd(lv['usd'])} | 等待{PENDING_SEC}秒确认")
            else:
                exist_sec = now - prev["first_seen"]

                # 判断是否达到持续时间要求 → 发首次预警
                if (not prev["alerted"] and
                    exist_sec >= PENDING_SEC and
                    _can_alert(key)):
                    await _send_alert(lv, exist_sec)
                    known_large[key]["alerted"] = True
                    alert_times[key] = now

                # 金额大幅增加 → 再次预警
                elif (prev["alerted"] and
                      lv["usd"] > prev["usd"] * 1.3 and
                      _can_alert(key)):
                    await _send_alert(lv, exist_sec, updated=True)
                    known_large[key]["usd"] = lv["usd"]
                    alert_times[key] = now

                known_large[key]["usd"] = lv["usd"]

    # Spoof 检测：已预警的大单快速消失
    disappeared = set(known_large.keys()) - seen_keys
    for key in list(disappeared):
        order = known_large.pop(key, None)
        if not order or not order.get("alerted"):
            continue
        duration = now - order["first_seen"]
        if order["usd"] >= SPOOF_MIN_USD and duration < SPOOF_WINDOW_SEC:
            await _send_spoof(key[0], key[1], key[2], order["usd"], duration)


async def _send_alert(lv, exist_sec, updated=False):
    tag, lvl = _level_tag(lv["usd"])
    status = f"确认（持续 {exist_sec:.0f}s）" if not updated else "金额更新"
    side = lv["side"]

    if side == "bid":
        label = "BID 买方挂单（潜在支撑）"
        tip   = ("> 确认存在的买方支撑位\n"
                 "> 价格下跌至此处将面临极强阻力\n"
                 "> ATAS：关注此价位 Sell Absorption")
    else:
        label = "ASK 卖方挂单（潜在阻力）"
        tip   = ("> 确认存在的卖方阻力位\n"
                 "> 价格上涨至此处将面临极强阻力\n"
                 "> ATAS：关注此价位 Buy Absorption")

    extra = ("!! 极其罕见的超大额订单 !!"
             if lv["usd"] >= THRESHOLD_MEGA else
             "重要机构级别支撑/阻力位。"
             if lv["usd"] >= THRESHOLD_STRONG else "")

    msg = (
        f"{tag} {lvl}\n"
        f"{'='*34}\n"
        f"状  态：{status}\n"
        f"交易所：{lv['exchange']}\n"
        f"方  向：{label}\n"
        f"价  格：${lv['price']:,.0f}\n"
        f"挂单金额：{fmt_usd(lv['usd'])}\n"
        f"持续时长：{exist_sec:.0f} 秒\n"
        f"距当前价：{lv['dist']:+.2f}%（{lv['direction']}方向）\n"
        f"当前 BTC：${mid_price:,.0f}\n"
        f"时  间：{fmt_time(short=True)}\n"
        f"{'-'*34}\n"
        f"{extra}\n"
        f"{tip}"
    )
    await async_send(msg)
    logger.info(f"大单预警 | {lv['exchange']} {lv['side'].upper()} "
                f"${lv['price']:,.0f} | {fmt_usd(lv['usd'])} | 持续{exist_sec:.0f}s")


async def _send_spoof(exchange, side, price, usd, duration):
    direction = "BID 买单（支撑）" if side == "bid" else "ASK 卖单（阻力）"
    consequence = ("> BID 大单消失 → 支撑撤走，价格可能向下加速"
                   if side == "bid" else
                   "> ASK 大单消失 → 阻力撤走，价格可能向上加速")
    msg = (
        f"[SPOOF] 已确认大单撤销！\n"
        f"{'='*34}\n"
        f"交易所：{exchange}\n"
        f"方  向：{direction}\n"
        f"价  格：${price:,.0f}\n"
        f"原挂单：{fmt_usd(usd)}\n"
        f"总存在：{duration:.0f} 秒\n"
        f"时  间：{fmt_time(short=True)}\n"
        f"{'-'*34}\n"
        f"该挂单曾被确认（存在>{PENDING_SEC}s），现已撤销！\n"
        f"{consequence}\n"
        f"> 立即重新评估方向，打开 ATAS 确认"
    )
    await async_send(msg)
    logger.info(f"Spoof预警 | {exchange} {side} ${price:,.0f} | {fmt_usd(usd)} | {duration:.0f}s")


async def okx_listener():
    url = "wss://ws.okx.com/ws/v5/public"
    sub = json.dumps({"op":"subscribe","args":[{"channel":"books","instId":"BTC-USDT-SWAP"}]})
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                await ws.send(sub)
                logger.info("OKX 订单簿 WS 已连接")
                async for msg in ws:
                    try:
                        d = json.loads(msg)
                        if not d.get("data"):
                            continue
                        data = d["data"][0]
                        action = d.get("action","")
                        if action == "snapshot":
                            okx_book.snapshot(data.get("bids",[]), data.get("asks",[]))
                        elif action == "update":
                            okx_book.delta(data.get("bids",[]), data.get("asks",[]))
                        await check_books()
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"OKX 订单簿 WS 断开: {e}")
        await asyncio.sleep(10)


async def bybit_listener():
    url = "wss://stream.bybit.com/v5/public/linear"
    sub = json.dumps({"op":"subscribe","args":["orderbook.200.BTCUSDT"]})
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                await ws.send(sub)
                logger.info("Bybit 订单簿 WS 已连接")
                async for msg in ws:
                    try:
                        d = json.loads(msg)
                        if "orderbook" not in d.get("topic",""):
                            continue
                        data = d.get("data",{})
                        tp   = d.get("type","")
                        if tp == "snapshot":
                            bybit_book.snapshot(data.get("b",[]), data.get("a",[]))
                        elif tp == "delta":
                            bybit_book.delta(data.get("b",[]), data.get("a",[]))
                        await check_books()
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Bybit 订单簿 WS 断开: {e}")
        await asyncio.sleep(10)


async def run():
    logger.info("="*50)
    logger.info("订单簿大额挂单监控 v3 启动")
    logger.info(f"预警阈值：{fmt_usd(THRESHOLD_ALERT)}")
    logger.info(f"持续要求：挂单需存在 {PENDING_SEC} 秒才预警")
    logger.info(f"距离过滤：忽略 {MIN_DIST_PCT}% 以内的挂单")
    logger.info("="*50)

    await async_send(
        "[OK] 订单簿大额挂单监控 v3 已启动\n"
        "数据源：OKX + Bybit\n"
        f"预警阈值：{fmt_usd(THRESHOLD_ALERT)}\n"
        f"持续要求：挂单 > {PENDING_SEC}s 才预警（过滤刷单）\n"
        f"距离过滤：忽略 {MIN_DIST_PCT}% 以内的挂单\n"
        "Spoof：已确认大单快速撤销才预警"
    )

    await asyncio.gather(
        okx_listener(),
        bybit_listener(),
        return_exceptions=True
    )
