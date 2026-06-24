"""
CME BTC 期货缺口计算
原理：
  CME 每周五 ~23:00 UTC 关盘，周日 ~23:00 UTC（周一 00:00 UTC）开盘
  使用 Binance 现货价格近似 CME 价格
  Gap = 周一 00:00 UTC 开盘价 - 上周五 23:00 UTC 收盘价
"""
import requests
from datetime import datetime, timedelta, timezone
from utils.helpers import setup_logger

logger = setup_logger()

SPOT_URL = "https://api.binance.com"


def _get_hourly_close(target_dt: datetime) -> float:
    """获取指定 UTC 小时的 Binance 现货 BTC/USDT 收盘价"""
    ts_ms = int(target_dt.timestamp() * 1000)
    try:
        r = requests.get(
            f"{SPOT_URL}/api/v3/klines",
            params={
                "symbol":    "BTCUSDT",
                "interval":  "1h",
                "startTime": ts_ms,
                "limit":     1,
            },
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        if data:
            return float(data[0][4])  # 收盘价
    except Exception as e:
        logger.warning(f"获取价格失败 ({target_dt}): {e}")
    return 0.0


def _get_price_range_since(since_dt: datetime) -> tuple:
    """获取指定时间之后到现在的价格区间（用于判断缺口是否填补）"""
    ts_ms = int(since_dt.timestamp() * 1000)
    try:
        r = requests.get(
            f"{SPOT_URL}/api/v3/klines",
            params={
                "symbol":    "BTCUSDT",
                "interval":  "1h",
                "startTime": ts_ms,
                "limit":     200,  # 最多200小时
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data:
            highs = [float(k[2]) for k in data]
            lows  = [float(k[3]) for k in data]
            return max(highs), min(lows)
    except Exception as e:
        logger.warning(f"获取价格区间失败: {e}")
    return 0.0, 0.0


def get_cme_gap() -> dict:
    """
    计算最近一个 CME 周末缺口
    返回缺口信息：方向、大小、是否已填补、距填补的距离
    """
    now = datetime.now(timezone.utc)
    weekday = now.weekday()  # 0=Mon, 6=Sun

    # ── 找到上周五 23:00 UTC 和本周一 00:00 UTC ─────────────
    # 本周一 00:00 UTC
    days_to_mon = weekday  # 距本周一过了多少天
    this_monday  = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=days_to_mon)

    # 上周五 23:00 UTC = 本周一 00:00 - 73 小时
    last_friday_23 = this_monday - timedelta(hours=73)

    # ── 如果今天是周日或周六，用上上周的数据 ───────────────
    if weekday >= 5:  # 周六(5) 或 周日(6)
        this_monday    = this_monday - timedelta(weeks=1)
        last_friday_23 = last_friday_23 - timedelta(weeks=1)

    # ── 获取价格 ─────────────────────────────────────────────
    friday_close = _get_hourly_close(last_friday_23)
    monday_open  = _get_hourly_close(this_monday)

    if not friday_close or not monday_open:
        logger.warning("CME 缺口：无法获取价格数据")
        return {"has_gap": False}

    gap_size = monday_open - friday_close   # 正=上方缺口，负=下方缺口
    gap_pct  = gap_size / friday_close * 100 if friday_close else 0

    if abs(gap_size) < 50:  # 小于 $50 视为无缺口
        logger.info(f"CME：无显著缺口（差价 ${gap_size:+.0f}）")
        return {"has_gap": False}

    # 缺口区间
    if gap_size > 0:
        gap_top = monday_open
        gap_bot = friday_close
        direction = "上方"
        gap_desc  = "周末价格上跳（上方缺口），CME 低于现货"
    else:
        gap_top = friday_close
        gap_bot = monday_open
        direction = "下方"
        gap_desc  = "周末价格下跳（下方缺口），CME 高于现货"

    # ── 判断是否已填补 ───────────────────────────────────────
    range_high, range_low = _get_price_range_since(this_monday)
    current_price = _get_hourly_close(
        now.replace(minute=0, second=0, microsecond=0)
    ) or friday_close

    if gap_size > 0:
        # 上方缺口：价格需要回落到 gap_bot 以下才算填补
        is_filled = range_low <= gap_bot
        dist_to_fill = current_price - gap_bot if not is_filled else 0
        dist_pct = dist_to_fill / current_price * 100 if current_price else 0
    else:
        # 下方缺口：价格需要反弹到 gap_top 以上才算填补
        is_filled = range_high >= gap_top
        dist_to_fill = gap_top - current_price if not is_filled else 0
        dist_pct = dist_to_fill / current_price * 100 if current_price else 0

    days_open = (now - this_monday).days

    result = {
        "has_gap":       True,
        "direction":     direction,
        "gap_size":      abs(gap_size),
        "gap_pct":       abs(gap_pct),
        "gap_top":       gap_top,
        "gap_bot":       gap_bot,
        "friday_close":  friday_close,
        "monday_open":   monday_open,
        "is_filled":     is_filled,
        "dist_to_fill":  abs(dist_to_fill),
        "dist_pct":      abs(dist_pct),
        "days_open":     days_open,
        "gap_desc":      gap_desc,
        "friday_date":   last_friday_23.strftime("%m-%d"),
        "monday_date":   this_monday.strftime("%m-%d"),
    }

    status = "已填补" if is_filled else f"未填补（距填补 ${abs(dist_to_fill):,.0f} | {abs(dist_pct):.2f}%）"
    logger.info(
        f"CME 缺口: {direction} ${gap_bot:,.0f}-${gap_top:,.0f} "
        f"(${abs(gap_size):,.0f} | {abs(gap_pct):.2f}%) | {status}"
    )
    return result
