"""
etf_confirm_push.py — ETF 数据确认二次推送服务  v2
==================================================
部署位置：/opt/btc-trader/services/etf_confirm_push.py

设计思路：
  早盘简报 09:30 UTC+8 推送时，ETF 数据可能仍在当日更新窗口内（各发行商披露
  有先后，通常北京时间 04:00 美股收盘后陆续发布，12:00 前后基本到齐）。
  本脚本在 12:00 UTC+8 重新拉取数据，若此时数据已"稳定"（不在更新窗口内），
  则向 Telegram 推送一条"ETF 数据确认"补充通知，不重新发 WordPress 文章。

v2 更新（相比 v1）：
  - 不再从 SQLite 表 etf_flow / morning_brief_cache.json 读取数据——
    daily_briefing.py 从未往这两处写过数据，v1 版本实际上一直取不到数据，
    等于没有真正运行过。
  - 改为直接调用 data_collector.etf_data.fetch_etf_flows()，与三次定时简报
    共用同一份数据源和同一套"更新窗口"判断逻辑（is_settling 字段），
    不再依赖 utils/etf_timing.py 里按 12 只 ETF 清单数数的完整性判断
    （不同数据源品种清单不一致，按固定清单数数会经常误判）。

触发方式（cron，UTC 时间）：
  北京时间 12:00 = UTC 04:00，仅工作日运行（排除北京周日、周一）
  Cron: 0 4 * * 2-6   # UTC 周二~六 04:00 = 北京 周二~六 12:00
  命令: /opt/btc-trader/venv/bin/python /opt/btc-trader/services/etf_confirm_push.py

状态文件：/opt/btc-trader/data/etf_confirm_state.json
  记录已发送确认通知的数据日期，防止重复推送
"""

from __future__ import annotations
import json
import os
import sys
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── 路径设置 ─────────────────────────────────────────────────────────────────
BASE_DIR = Path('/opt/btc-trader')
sys.path.insert(0, str(BASE_DIR))

# ─── 时区常量（与 utils/etf_timing.py 保持一致，避免相互依赖）───────────────
BEIJING_TZ = timezone(timedelta(hours=8))
WEEKDAYS_CN = {
    0: '星期一', 1: '星期二', 2: '星期三',
    3: '星期四', 4: '星期五', 5: '星期六', 6: '星期日',
}

# ─── 配置 ─────────────────────────────────────────────────────────────────────
STATE_FILE = BASE_DIR / 'data' / 'etf_confirm_state.json'
LOG_FILE   = BASE_DIR / 'logs' / 'etf_confirm_push.log'

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.getenv('TELEGRAM_CHAT_ID', '')

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


# ─── 状态管理 ─────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def already_confirmed(state: dict, us_date_str: str) -> bool:
    return state.get(us_date_str, {}).get('confirmed', False)


def mark_confirmed(state: dict, us_date_str: str, flow_m: float) -> dict:
    state[us_date_str] = {
        'confirmed': True,
        'flow_m': flow_m,
        'confirmed_at': datetime.now(BEIJING_TZ).isoformat(),
    }
    # 只保留最近 14 天的记录
    cutoff = (datetime.now(BEIJING_TZ) - timedelta(days=14)).date().isoformat()
    state = {k: v for k, v in state.items() if k >= cutoff}
    return state


def beijing_weekday_str(now_bj: datetime) -> str:
    return WEEKDAYS_CN[now_bj.weekday()]


# ─── Telegram 推送 ────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    """向 Telegram 发送消息"""
    import urllib.request

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error('TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未配置')
        return False

    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    payload = json.dumps({
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': 'HTML',
    }).encode()

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get('ok'):
                logger.info('Telegram 推送成功')
                return True
            else:
                logger.error(f'Telegram API 返回错误: {result}')
                return False
    except Exception as e:
        logger.error(f'Telegram 推送异常: {e}')
        return False


# ─── 推送内容构建 ─────────────────────────────────────────────────────────────
def build_confirm_message(now_bj: datetime, etf: dict) -> str:
    lines = [
        f'<b>📊 BTC ETF 数据确认（{beijing_weekday_str(now_bj)} 12:00 UTC+8）</b>',
        f'对应交易日：{etf["date"]}',
        f'',
        f'<b>确认净流量：{etf["yest_str"]}</b>',
        f'本周累计：{etf["week_str"]}　本月累计：{etf["month_str"]}',
        f'已连续 {etf["streak_days"]} 天{etf["streak_dir"]}',
        f'',
        f'早盘简报中的数值为阶段性数据，现已稳定，以此为准',
        f'',
        f'<i>来源：{etf.get("source","-")}</i>',
        f'<i>确认时间：北京时间 {now_bj.year}.{now_bj.month}.{now_bj.day} {now_bj.strftime("%H:%M")}</i>',
    ]
    return '\n'.join(lines)


# ─── 主流程 ──────────────────────────────────────────────────────────────────
def main() -> None:
    now_bj = datetime.now(BEIJING_TZ)
    wd = now_bj.weekday()
    logger.info(f'etf_confirm_push 启动，北京时间: {now_bj.strftime("%Y-%m-%d %H:%M")} 星期{wd+1}')

    # 北京时间周日（wd=6）：美股周末，无新数据
    if wd == 6:
        logger.info('北京时间周日，美股周末无新数据，跳过')
        return

    # 北京时间周一（wd=0）：美股周一尚未收盘（北京时间周二04:00才收盘）
    if wd == 0:
        logger.info('北京时间周一，美股周一尚未收盘，跳过')
        return

    # 直接复用与三次简报相同的数据源
    from data_collector.etf_data import fetch_etf_flows
    etf = fetch_etf_flows()

    if not etf.get('has_data'):
        logger.error('ETF 数据获取失败，退出')
        return

    if etf.get('is_settling'):
        logger.info('12:00 数据仍在更新窗口内（少见情况），跳过本次，等待下次 cron 运行')
        return

    us_date = etf['date']

    state = load_state()
    if already_confirmed(state, us_date):
        logger.info(f'{us_date} 确认通知已发送过，跳过')
        return

    msg = build_confirm_message(now_bj, etf)
    logger.info('准备发送 ETF 确认通知...')
    logger.debug(f'消息内容:\n{msg}')

    success = send_telegram(msg)
    if success:
        state = mark_confirmed(state, us_date, etf.get('total_yest', 0))
        save_state(state)
        logger.info(f'✅ ETF 确认通知已发送，{us_date} 净流量: {etf["yest_str"]}')
    else:
        logger.error('❌ Telegram 推送失败，下次运行将重试')


if __name__ == '__main__':
    main()
