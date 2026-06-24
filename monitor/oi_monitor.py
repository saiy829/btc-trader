"""
OI（持仓量）异动监控
每 5 分钟检查一次，分析过去 1H 的 OI 变化
结合价格方向自动判断信号类型
"""
import time
import requests
from datetime import datetime, timezone, timedelta
from utils.helpers import setup_logger, fmt_usd, fmt_time, now_sgt, SGT
from alert_bot.send import send

logger = setup_logger("oi-monitor")

# ── 阈值配置 ──────────────────────────────────────────────────────
THRESHOLD_ALERT   = 5.0    # 1H 变化 > 5%  → 标准预警
THRESHOLD_URGENT  = 10.0   # 1H 变化 > 10% → 紧急预警
POLL_INTERVAL     = 300    # 轮询间隔：5 分钟
COOLDOWN_STD      = 1800   # 标准预警冷却：30 分钟
COOLDOWN_URGENT   = 900    # 紧急预警冷却：15 分钟

FUTURES_URL = "https://fapi.binance.com"
SPOT_URL    = "https://api.binance.com"

# ── 状态追踪 ──────────────────────────────────────────────────────
last_alert = {}   # {"increase": datetime, "decrease": datetime}


def _can_alert(key: str, urgent: bool = False) -> bool:
    t = last_alert.get(key)
    if not t:
        return True
    cooldown = COOLDOWN_URGENT if urgent else COOLDOWN_STD
    return (datetime.now(timezone.utc) - t).total_seconds() > cooldown


def get_oi_history() -> list:
    """获取过去 ~65 分钟的 OI 历史（5分钟粒度，13个点）"""
    r = requests.get(
        f"{FUTURES_URL}/futures/data/openInterestHist",
        params={"symbol": "BTCUSDT", "period": "5m", "limit": 14},
        timeout=10
    )
    r.raise_for_status()
    return r.json()


def get_current_price() -> dict:
    """获取当前价格 + 1H K线数据"""
    # 当前价格
    r1 = requests.get(
        f"{SPOT_URL}/api/v3/ticker/price",
        params={"symbol": "BTCUSDT"}, timeout=8
    )
    current = float(r1.json()["price"])

    # 1H 前的价格（取最近2根1H K线）
    r2 = requests.get(
        f"{SPOT_URL}/api/v3/klines",
        params={"symbol": "BTCUSDT", "interval": "1h", "limit": 2},
        timeout=8
    )
    klines = r2.json()
    price_1h_ago = float(klines[0][4])  # 前一根K线收盘价 ≈ 1H前价格

    return {
        "current":      current,
        "price_1h_ago": price_1h_ago,
        "change_1h_pct": (current - price_1h_ago) / price_1h_ago * 100
    }


def get_funding_rate() -> float:
    """获取当前 Funding Rate（Binance）"""
    try:
        r = requests.get(
            f"{FUTURES_URL}/fapi/v1/fundingRate",
            params={"symbol": "BTCUSDT", "limit": 1}, timeout=8
        )
        return float(r.json()[-1]["fundingRate"]) * 100
    except Exception:
        return 0.0


def classify_signal(oi_chg_pct: float, price_chg_pct: float) -> dict:
    """根据 OI 和价格变化方向，分类信号性质"""
    oi_up    = oi_chg_pct > 0
    price_up = price_chg_pct > 0

    if oi_up and price_up:
        return {
            "type":    "真实多头建仓",
            "tag":     "[OI BULL]",
            "bias":    "看涨",
            "detail":  "新资金持续流入做多，价格上涨有实质资金支撑",
            "advice": ("> 最强看涨信号，上涨动能有真实资金背书\n"
                       "> 可关注 VP 支撑位附近的做多机会\n"
                       "> 若同时 Funding 中性，信号更干净")
        }
    elif oi_up and not price_up:
        return {
            "type":    "真实空头建仓",
            "tag":     "[OI BEAR]",
            "bias":    "看跌",
            "detail":  "新资金持续流入做空，价格下跌有实质资金支撑",
            "advice": ("> 最强看跌信号，下跌动能有真实资金背书\n"
                       "> 可关注 VP 阻力位附近的做空机会\n"
                       "> 若同时 Funding 偏负，信号更干净")
        }
    elif not oi_up and price_up:
        return {
            "type":    "空头平仓 / 轧空",
            "tag":     "[OI SQUEEZE]",
            "bias":    "谨慎",
            "detail":  "OI 减少但价格上涨，属空头被动平仓推升，非真实买盘",
            "advice": ("> 轧空驱动的上涨，缺乏真实资金支撑\n"
                       "> 空头平仓完毕后，上涨动能可能消失\n"
                       "> 不宜追多，等 OI 重新增加后再判断方向")
        }
    else:
        return {
            "type":    "多头平仓 / 挤多",
            "tag":     "[OI DUMP]",
            "bias":    "谨慎",
            "detail":  "OI 减少且价格下跌，属多头被动平仓压低，非真实卖盘",
            "advice": ("> 挤多驱动的下跌，缺乏真实资金支撑\n"
                       "> 多头平仓完毕后，下跌动能可能消失\n"
                       "> 不宜追空，等 OI 重新增加后再判断方向")
        }


def check_once():
    """执行一次 OI 检查"""
    logger.info("检查 OI 异动...")

    try:
        oi_hist = get_oi_history()
        prices  = get_current_price()
        fr      = get_funding_rate()
    except Exception as e:
        logger.error(f"数据获取失败: {e}")
        return

    if len(oi_hist) < 2:
        logger.warning("OI 历史数据不足，跳过")
        return

    # 计算 1H OI 变化
    oi_now      = float(oi_hist[-1]["sumOpenInterest"])       # 当前 BTC
    oi_1h_ago   = float(oi_hist[0]["sumOpenInterest"])        # ~1H 前 BTC
    oi_val_now  = float(oi_hist[-1]["sumOpenInterestValue"])  # 当前 USD

    oi_chg_btc  = oi_now - oi_1h_ago
    oi_chg_pct  = (oi_now - oi_1h_ago) / oi_1h_ago * 100 if oi_1h_ago else 0
    oi_chg_usd  = oi_chg_btc * prices["current"]

    price_chg   = prices["change_1h_pct"]
    current_p   = prices["current"]
    p_1h_ago    = prices["price_1h_ago"]

    # 记录日志
    logger.info(f"  OI 1H 变化：{oi_chg_pct:+.2f}%（{fmt_usd(abs(oi_chg_usd))}）")
    logger.info(f"  价格 1H 变化：{price_chg:+.2f}%（${p_1h_ago:,.0f} → ${current_p:,.0f}）")

    # 判断是否需要预警
    abs_chg = abs(oi_chg_pct)
    if abs_chg < THRESHOLD_ALERT:
        logger.info(f"  OI 变化 {abs_chg:.2f}% 未超阈值（{THRESHOLD_ALERT}%），跳过")
        return

    urgent    = abs_chg >= THRESHOLD_URGENT
    direction = "increase" if oi_chg_pct > 0 else "decrease"

    if not _can_alert(direction, urgent=urgent):
        logger.info(f"  在冷却期内，跳过本次预警")
        return

    last_alert[direction] = datetime.now(timezone.utc)

    # 分类信号
    sig = classify_signal(oi_chg_pct, price_chg)

    # Funding 与 OI 方向一致性判断
    if sig["bias"] == "看涨" and fr > 0.01:
        fr_note = f"Funding {fr:+.4f}% 偏多，与 OI 信号一致，看涨增强"
    elif sig["bias"] == "看涨" and fr < -0.01:
        fr_note = f"Funding {fr:+.4f}% 偏空，与 OI 信号分歧，注意风险"
    elif sig["bias"] == "看跌" and fr < -0.01:
        fr_note = f"Funding {fr:+.4f}% 偏空，与 OI 信号一致，看跌增强"
    elif sig["bias"] == "看跌" and fr > 0.01:
        fr_note = f"Funding {fr:+.4f}% 偏多，与 OI 信号分歧，注意风险"
    else:
        fr_note = f"Funding {fr:+.4f}%（中性，信号干净）"

    level = "紧急预警" if urgent else "异动预警"
    now_str = fmt_time(short=False)

    msg = (
        f"{sig['tag']} OI 持仓量{level}\n"
        f"{'='*34}\n"
        f"信号类型：{sig['type']}\n"
        f"偏  向：{sig['bias']}\n"
        f"{'─'*34}\n"
        f"OI 1H 变化：{oi_chg_pct:+.2f}%\n"
        f"变化金额：{fmt_usd(abs(oi_chg_usd))}\n"
        f"当前 OI：{fmt_usd(oi_val_now)}\n"
        f"{'─'*34}\n"
        f"价格 1H 前：${p_1h_ago:,.0f}\n"
        f"价格 当前：${current_p:,.0f}（{price_chg:+.2f}%）\n"
        f"{'─'*34}\n"
        f"市场含义：{sig['detail']}\n"
        f"{'─'*34}\n"
        f"{sig['advice']}\n"
        f"{'─'*34}\n"
        f"Funding：{fr_note}\n"
        f"时  间：{now_str}"
    )

    send(msg)
    logger.info(f"OI 预警已发送 | {sig['type']} | {oi_chg_pct:+.2f}% | {fmt_usd(abs(oi_chg_usd))}")


def run():
    logger.info("=" * 50)
    logger.info("OI 持仓量异动监控启动")
    logger.info(f"标准预警阈值：1H 变化 > ±{THRESHOLD_ALERT}%")
    logger.info(f"紧急预警阈值：1H 变化 > ±{THRESHOLD_URGENT}%")
    logger.info(f"轮询间隔：{POLL_INTERVAL // 60} 分钟")
    logger.info("=" * 50)

    send(
        "[OK] OI 持仓量异动监控已启动\n"
        f"标准预警：1H 变化 > ±{THRESHOLD_ALERT}%\n"
        f"紧急预警：1H 变化 > ±{THRESHOLD_URGENT}%\n"
        "自动识别：真实建仓 / 被动平仓（挤空/挤多）\n"
        f"时间显示：新加坡时间（SGT）"
    )

    # 启动后立即检查一次
    check_once()

    while True:
        time.sleep(POLL_INTERVAL)
        check_once()


if __name__ == "__main__":
    run()
