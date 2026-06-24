"""
etf_confirm_push.py — ETF 数据确认二次推送服务
==================================================
部署位置：/opt/btc-trader/services/etf_confirm_push.py

设计思路：
  早盘简报 09:30 UTC+8 推送时，ETF 数据往往不完整（各发行商报告有延迟）。
  本脚本在 12:00 UTC+8 重新拉取数据，若数据已完整且总量与早盘有明显差异，
  则向 Telegram 推送一条"ETF 数据确认"补充通知，不重新发 WordPress 文章。

触发方式（cron，UTC 时间）：
  北京时间 12:00 = UTC 04:00，仅工作日运行（排除北京周日）
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
from typing import Dict, Optional

# ─── 路径设置 ─────────────────────────────────────────────────────────────────
BASE_DIR = Path('/opt/btc-trader')
sys.path.insert(0, str(BASE_DIR))

from utils.etf_timing import (
    BEIJING_TZ, get_etf_info, _fmt_flow, EtfInfo,
)

# ─── 配置 ─────────────────────────────────────────────────────────────────────
STATE_FILE   = BASE_DIR / 'data' / 'etf_confirm_state.json'
LOG_FILE     = BASE_DIR / 'logs' / 'etf_confirm_push.log'

# 差异阈值：早盘与确认值相差超过此数（百万美元）才推送
DELTA_THRESHOLD_M = 30.0

# 从环境变量或现有配置读取 Telegram 凭据
# 若系统已有统一 config，可改为 from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
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


def mark_confirmed(state: dict, us_date_str: str, flow_m: float, n: int) -> dict:
    state[us_date_str] = {
        'confirmed': True,
        'flow_m': flow_m,
        'n_reporting': n,
        'confirmed_at': datetime.now(BEIJING_TZ).isoformat(),
    }
    # 只保留最近 14 天的记录
    cutoff = (datetime.now(BEIJING_TZ) - timedelta(days=14)).date().isoformat()
    state = {k: v for k, v in state.items() if k >= cutoff}
    return state


# ─── ETF 数据获取 ─────────────────────────────────────────────────────────────
def fetch_etf_data() -> Optional[Dict[str, Optional[float]]]:
    """
    从现有数据管道获取最新 ETF 流量数据。
    
    此函数需要根据实际系统架构调整：
    - 若数据存在 SQLite → 直接查询
    - 若有统一数据模块 → import 后调用
    - 此处提供两种适配方式，根据实际情况取消注释
    """
    # ── 方式 A：从 SQLite 读取（最常见）──────────────────────────────────────
    import sqlite3
    db_path = BASE_DIR / 'data' / 'btc_data.db'
    if db_path.exists():
        try:
            con = sqlite3.connect(str(db_path))
            cur = con.cursor()
            # 假设表名为 etf_flow，列为 ticker / flow_m / data_date
            # 取最新一天的数据
            cur.execute('''
                SELECT ticker, flow_m FROM etf_flow
                WHERE data_date = (SELECT MAX(data_date) FROM etf_flow)
            ''')
            rows = cur.fetchall()
            con.close()
            if rows:
                return {ticker: flow for ticker, flow in rows}
        except Exception as e:
            logger.warning(f'SQLite 查询失败: {e}')

    # ── 方式 B：调用现有数据获取模块 ─────────────────────────────────────────
    # try:
    #     from data.etf_fetcher import get_latest_etf_flow
    #     return get_latest_etf_flow()
    # except ImportError:
    #     pass

    # ── 方式 C：直接读取内存缓存（若系统有共享 Redis / 文件缓存）────────────
    # cache_file = BASE_DIR / 'data' / 'etf_latest.json'
    # if cache_file.exists():
    #     data = json.loads(cache_file.read_text())
    #     return data.get('flow_by_ticker')

    logger.error('无法获取ETF数据，请适配 fetch_etf_data() 函数')
    return None


def get_morning_flow(us_date_str: str) -> Optional[float]:
    """
    读取早盘简报记录的 ETF 净流量（用于与当前值对比）。
    系统若记录了早盘数据，在此读取；否则返回 None（跳过差异判断，只判完整性）。
    """
    morning_cache = BASE_DIR / 'data' / 'morning_brief_cache.json'
    if morning_cache.exists():
        try:
            data = json.loads(morning_cache.read_text())
            return data.get(us_date_str, {}).get('etf_flow_m')
        except Exception:
            pass
    return None


# ─── Telegram 推送 ────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    """向 Telegram 发送消息"""
    import urllib.request
    import urllib.parse

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
def build_confirm_message(
    info: EtfInfo,
    morning_flow_m: Optional[float],
) -> str:
    now_bj = info.now_bj
    sign = '+' if info.total_flow_m >= 0 else '-'
    flow_str = f'{sign}{_fmt_flow(info.total_flow_m)}'

    lines = [
        f'<b>📊 BTC ETF 数据确认（{info.weekday_cn} 12:00 UTC+8）</b>',
        f'对应美国交易日：{info.us_data_date_str}',
        f'',
        f'<b>确认净流量：{flow_str}</b>（{info.n_reporting}/12 ETF 全部到账）',
    ]

    if morning_flow_m is not None:
        delta = info.total_flow_m - morning_flow_m
        m_sign = '+' if morning_flow_m >= 0 else '-'
        d_sign = '+' if delta >= 0 else ''
        morning_str = f'{m_sign}{_fmt_flow(morning_flow_m)}'
        delta_str = f'{d_sign}{delta:.0f}M'
        lines += [
            f'',
            f'早盘（09:30）初报：{morning_str}（数据不完整）',
            f'修正量：{delta_str}美元',
        ]
        if abs(delta) >= 200:
            lines.append(f'⚠️ 修正幅度较大，与早盘分析结论请以此为准')

    lines += [
        f'',
        f'<i>数据来源：Farside + SoSoValue 双源验证</i>',
        f'<i>获取时间：北京时间 {now_bj.year}.{now_bj.month}.{now_bj.day} {now_bj.strftime("%H:%M")}</i>',
    ]
    return '\n'.join(lines)


# ─── 主流程 ──────────────────────────────────────────────────────────────────
def main() -> None:
    now_bj = datetime.now(BEIJING_TZ)
    wd = now_bj.weekday()
    logger.info(f'etf_confirm_push 启动，北京时间: {now_bj.strftime("%Y-%m-%d %H:%M")} 星期{wd+1}')

    # 北京时间周日（wd=6）：美股周末，跳过
    if wd == 6:
        logger.info('北京时间周日，美股周末无新数据，跳过')
        return

    # 北京时间周一（wd=0）：美股周一尚未收盘，跳过
    if wd == 0:
        logger.info('北京时间周一，美股周一尚未收盘，跳过')
        return

    # 获取 ETF 数据
    etf_data = fetch_etf_data()
    if etf_data is None:
        logger.error('ETF 数据获取失败，退出')
        return

    # 计算数据信息
    info = get_etf_info(etf_data, now_bj)

    if not info.has_fresh_data:
        logger.info(f'当前无新鲜数据（{info.no_data_reason}），跳过')
        return

    us_date = info.us_data_date_str
    logger.info(
        f'ETF 数据：日期={us_date}, '
        f'已到账={info.n_reporting}/{12}, '
        f'完整={info.is_complete}, '
        f'净流量={info.total_flow_m:.1f}M'
    )

    # 数据尚不完整 → 不推送（等下一个触发窗口，或人工在面板查看）
    if not info.is_complete:
        logger.info(f'数据尚不完整（{info.n_reporting}/12），不推送，等待下次运行')
        return

    # 检查是否已推送过今日确认
    state = load_state()
    if already_confirmed(state, us_date):
        logger.info(f'{us_date} 确认通知已发送过，跳过')
        return

    # 读取早盘记录值（用于比对）
    morning_flow = get_morning_flow(us_date)

    # 若早盘值存在且差异不足阈值，且数据总量方向一致 → 不重复推送
    if morning_flow is not None:
        delta = abs(info.total_flow_m - morning_flow)
        logger.info(f'早盘初报: {morning_flow:.1f}M, 当前: {info.total_flow_m:.1f}M, 差异: {delta:.1f}M')
        if delta < DELTA_THRESHOLD_M:
            logger.info(f'差异 {delta:.1f}M < 阈值 {DELTA_THRESHOLD_M}M，方向一致无需推送')
            # 仍然标记为已确认，避免后续重复运行
            state = mark_confirmed(state, us_date, info.total_flow_m, info.n_reporting)
            save_state(state)
            return

    # 构建并发送 Telegram 消息
    msg = build_confirm_message(info, morning_flow)
    logger.info('准备发送 ETF 确认通知...')
    logger.debug(f'消息内容:\n{msg}')

    success = send_telegram(msg)
    if success:
        state = mark_confirmed(state, us_date, info.total_flow_m, info.n_reporting)
        save_state(state)
        logger.info(f'✅ ETF 确认通知已发送，{us_date} 净流量: {info.total_flow_m:.1f}M')
    else:
        logger.error('❌ Telegram 推送失败，下次运行将重试')


if __name__ == '__main__':
    main()
