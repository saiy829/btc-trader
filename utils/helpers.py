import os
from pathlib import Path
from dotenv import load_dotenv
from loguru import logger
import sys

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / '.env')

def setup_logger(name="btc-trader"):
    log_file = BASE_DIR / 'logs' / f'{name}.log'
    log_file.parent.mkdir(exist_ok=True)
    logger.remove()
    logger.add(sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        colorize=True)
    logger.add(log_file, rotation="1 day", retention="30 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}", encoding="utf-8")
    return logger

def get_env(key, default=None):
    value = os.environ.get(key, default)
    if value is None:
        raise ValueError(f"环境变量 {key} 未设置，请检查 .env 文件")
    return value

# ── 时间与金额格式化工具 ─────────────────────────────────────────
from datetime import timezone, timedelta

SGT = timezone(timedelta(hours=8))  # 新加坡时间 UTC+8

def fmt_time(dt=None, short=False) -> str:
    """格式化时间为新加坡时间（SGT = UTC+8）"""
    from datetime import datetime
    if dt is None:
        dt = datetime.now(SGT)
    else:
        dt = dt.astimezone(SGT)
    return dt.strftime("%H:%M SGT") if short else dt.strftime("%Y-%m-%d %H:%M SGT")

def now_sgt():
    """获取当前新加坡时间"""
    from datetime import datetime
    return datetime.now(SGT)

def fmt_usd(amount: float) -> str:
    """
    格式化美元金额，附带中文单位
    $8,500          -> $8,500
    $148,231        -> $148,231（约14.8万）
    $1,500,000      -> $1,500,000（约150万）
    $12,000,000     -> $12,000,000（约1,200万）
    """
    if amount < 10_000:
        return f"${amount:,.0f}"
    wan = amount / 10_000
    if wan >= 10_000:
        approx = f"约{wan/10_000:.1f}亿"
    elif wan >= 100:
        approx = f"约{wan:.0f}万"
    else:
        approx = f"约{wan:.1f}万"
    return f"${amount:,.0f}（{approx}）"
