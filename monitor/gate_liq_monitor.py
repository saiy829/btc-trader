"""
Gate.io BTC 永续合约爆仓监控（REST 轮询版）
端点: GET https://api.gateio.ws/api/v4/futures/usdt/liq_orders
原因: Gate.io WS futures.liquidates 需认证，REST 无需认证

合约规格: BTC_USDT，量化乘数 = 0.0001
USD金额 = abs(size) × 0.0001 × fill_price
轮询间隔: 5秒

服务名: btc-gate-liq（与 btc-liq-monitor OKX 并行）
"""
import time
import sqlite3
import requests
from datetime import datetime, timezone, timedelta
from collections import deque
from utils.helpers import setup_logger, get_env
from alert_bot.send import send

logger = setup_logger("gate-liq")

# ── 时区 ─────────────────────────────────────────────────────────
CST = timezone(timedelta(hours=8))   # 北京时间 UTC+8

# ── 合约规格 ─────────────────────────────────────────────────────
QUANTO   = 0.0001        # 每张合约 = 0.0001 BTC
CONTRACT = "BTC_USDT"
API_URL  = "https://api.gateio.ws/api/v4/futures/usdt/liq_orders"
INTERVAL = 5             # 轮询间隔（秒）

# ── 数据库 ───────────────────────────────────────────────────────
DB_PATH  = "/opt/btc-trader/btc_history.db"

# ── 预警阈值（复用 OKX 环境变量）────────────────────────────────
LIQ_SINGLE  = float(get_env("LIQ_SINGLE_USD",  "100000"))
LIQ_HOURLY  = float(get_env("LIQ_HOURLY_USD", "1000000"))

# ── 状态 ─────────────────────────────────────────────────────────
_window: deque = deque()
_seen:   set   = set()
_last_hourly   = 0.0


def _fmt(v: float) -> str:
    """中文单位格式化：万 / 亿，不用 K/M/B"""
    if v >= 100_000_000:        # 1亿+
        return f"${v / 100_000_000:.1f}亿"
    elif v >= 10_000:           # 1万+
        wan = v / 10_000
        if wan >= 100:          # 100万+：整数
            return f"${wan:.0f}万"
        elif wan >= 10:         # 10-99万：1位小数
            return f"${wan:.1f}万"
        else:                   # 1-9.9万：2位小数
            return f"${wan:.2f}万"
    else:
        return f"${v:,.0f}"


def _now_cst() -> str:
    """当前北京时间 HH:MM:SS"""
    return datetime.now(CST).strftime("%H:%M:%S")


def _cleanup():
    cutoff = time.time() - 3600
    while _window and _window[0][0] < cutoff:
        _window.popleft()
    global _seen
    _seen = {(t, s) for (t, s) in _seen if t > time.time() - 300}


def _total(side: str = "all") -> float:
    _cleanup()
    if side == "all":
        return sum(v for _, v, _ in _window)
    return sum(v for _, v, d in _window if d == side)


def _save_db(rec_time: int, direction: str, fill_price: float, usd_val: float):
    """写入数据库（供面板读取，表不存在则自动创建）"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gate_liquidations (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        INTEGER NOT NULL,
                direction TEXT NOT NULL,
                price     REAL NOT NULL,
                usd_value REAL NOT NULL,
                exchange  TEXT DEFAULT 'Gate'
            )
        """)
        conn.execute(
            "INSERT INTO gate_liquidations (ts, direction, price, usd_value) VALUES (?,?,?,?)",
            (rec_time, direction, fill_price, usd_val)
        )
        # 只保留最近7天
        cutoff = int(time.time()) - 7 * 86400
        conn.execute("DELETE FROM gate_liquidations WHERE ts < ?", (cutoff,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"DB写入失败: {e}")


def _process(record: dict):
    global _last_hourly

    size_raw   = record.get("size", 0)
    fill_price = float(record.get("fill_price", 0))
    rec_time   = record.get("time", 0)

    if not size_raw or not fill_price:
        return

    key = (rec_time, size_raw)
    if key in _seen:
        return
    _seen.add(key)

    size      = abs(int(size_raw))
    usd_val   = size * QUANTO * fill_price
    # 修正：Gate官方文档示例显示 size 为正数时对应从高价跌至低价被强平的仓位，
    # 即"size>0 = 多头爆仓"，原来写的 "<0" 刚好反了（连带TG推送方向也一直是反的）
    is_long   = int(size_raw) > 0
    direction = "多头爆仓" if is_long else "空头爆仓"
    side_key  = "long" if is_long else "short"
    emoji     = "🟢" if is_long else "🔴"   # 绿=多头爆仓，红=空头爆仓，跟仪表板颜色约定一致

    ts = time.time()
    _window.append((ts, usd_val, side_key))

    # 只保存1万美元以上的爆仓到数据库（过滤小额）
    if usd_val >= 10_000:
        _save_db(rec_time, direction, fill_price, usd_val)

    logger.debug(f"Gate.io {direction} {_fmt(usd_val)} @ ${fill_price:,.0f} ({size}张)")

    # ── 单笔大额预警 ─────────────────────────────────────────────
    if usd_val >= LIQ_SINGLE:
        h_total = _total()
        h_long  = _total("long")
        h_short = _total("short")
        msg = (
            f"{emoji} <b>Gate.io 大额爆仓</b>\n"
            f"方向：{direction}\n"
            f"金额：{_fmt(usd_val)}\n"
            f"价格：${fill_price:,.1f}\n"
            f"──────────────\n"
            f"近1小时累计：{_fmt(h_total)}\n"
            f"  多头：{_fmt(h_long)}  空头：{_fmt(h_short)}\n"
            f"北京时间：{_now_cst()}"
        )
        send(msg)
        logger.info(f"单笔预警 {_fmt(usd_val)} {direction}")

    # ── 小时累计预警（冷却30分钟）────────────────────────────────
    h_total = _total()
    if h_total >= LIQ_HOURLY and (ts - _last_hourly) > 1800:
        _last_hourly = ts
        h_long   = _total("long")
        h_short  = _total("short")
        dominant = "空头主导" if h_short > h_long else "多头主导"
        msg = (
            f"⚠️ <b>Gate.io 爆仓累计预警</b>\n"
            f"近1小时：{_fmt(h_total)}（{dominant}）\n"
            f"  多头：{_fmt(h_long)}  空头：{_fmt(h_short)}\n"
            f"北京时间：{_now_cst()}"
        )
        send(msg)
        logger.info(f"小时累计预警 {_fmt(h_total)}")


def _fetch(from_ts: int) -> list:
    resp = requests.get(
        API_URL,
        params={"contract": CONTRACT, "from": from_ts, "limit": 100},
        timeout=8,
    )
    resp.raise_for_status()
    return resp.json() or []


def main():
    logger.info("=" * 45)
    logger.info("Gate.io 爆仓监控 启动（REST轮询）")
    logger.info(f"合约: {CONTRACT}  QUANTO: {QUANTO}")
    logger.info(f"轮询: {INTERVAL}s  单笔阈值: {_fmt(LIQ_SINGLE)}  小时阈值: {_fmt(LIQ_HOURLY)}")
    logger.info("=" * 45)

    last_ts   = int(time.time()) - 60
    err_count = 0

    while True:
        try:
            records = _fetch(last_ts)
            if records:
                max_ts = max(r.get("time", 0) for r in records)
                if max_ts > last_ts:
                    last_ts = max_ts
                for r in records:
                    _process(r)
            err_count = 0

        except requests.exceptions.RequestException as e:
            err_count += 1
            wait = min(30, INTERVAL * err_count)
            logger.warning(f"网络错误({err_count}): {e}，{wait}s后重试")
            time.sleep(wait)
            continue
        except Exception as e:
            logger.error(f"未预期错误: {e}")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
