"""
etf_timing.py — BTC 现货 ETF 数据时间与完整性工具
======================================================
部署位置：/opt/btc-trader/utils/etf_timing.py

解决的三个问题：
  1. 统一使用北京时间（UTC+8）标注数据时间，格式：
     "北京时间 星期二 2026.6.23 09:30 数据"
  2. 自动识别无数据日（北京时间周日、周一早盘）并给出明确说明
  3. 检测数据完整性（ETF 到账数量、关键品种 IBIT/GBTC 是否缺失）

集成方式：
  from utils.etf_timing import get_etf_info, format_etf_block
  # 在现有简报生成代码中替换原 ETF 段落构建逻辑
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta, date
from typing import Dict, Optional, List

# ─── 时区常量 ────────────────────────────────────────────────────────────────
BEIJING_TZ = timezone(timedelta(hours=8))

WEEKDAYS_CN: Dict[int, str] = {
    0: '星期一', 1: '星期二', 2: '星期三',
    3: '星期四', 4: '星期五', 5: '星期六', 6: '星期日',
}

# ─── ETF 品种定义 ────────────────────────────────────────────────────────────
# 所有已知品种（完整性基准）
ALL_ETF_TICKERS: List[str] = [
    'IBIT', 'FBTC', 'BITB', 'ARKB', 'BTCO',
    'EZBC', 'BRRR', 'HODL', 'BTCW', 'MSBT', 'GBTC', 'BTC',
]
TOTAL_ETF_COUNT = len(ALL_ETF_TICKERS)       # = 12

# 关键品种：缺失时单独标注（持仓规模最大，最影响总量）
CRITICAL_TICKERS: List[str] = ['IBIT', 'GBTC']

# 数据"基本完整"判断阈值（已到账品种 ≥ 10 且关键品种均在）
COMPLETE_THRESHOLD = 10


# ─── 数据结构 ────────────────────────────────────────────────────────────────
@dataclass
class EtfInfo:
    """ETF 数据日期、完整性及格式化文本的综合结果"""

    # ── 时间信息 ──
    now_bj: datetime = None              # 当前北京时间
    weekday_cn: str = ''                 # 中文星期名
    fetch_time_str: str = ''             # "北京时间 星期X YYYY.M.D HH:MM 数据"
    us_data_date: date = None            # 对应的美国交易日（date 对象）
    us_data_date_str: str = ''           # "YYYY-MM-DD" 字符串

    # ── 数据可用性 ──
    has_fresh_data: bool = True          # 是否有今日新增数据
    no_data_reason: str = ''             # 无新数据的原因说明（周日/周一）

    # ── 完整性 ──
    n_reporting: int = 0                 # 已到账 ETF 品种数
    is_complete: bool = False            # 是否达到完整阈值
    missing_critical: List[str] = field(default_factory=list)   # 缺失关键品种列表
    total_flow_m: float = 0.0            # 合计净流量（百万美元，已知部分之和）

    # ── 格式化文本 ──
    header_block: str = ''               # 完整的 ETF 段落文本（供简报直接插入）
    completeness_note: str = ''          # 仅完整性那一行
    flow_display: str = ''               # 仅净流量那一行


# ─── 核心函数 ────────────────────────────────────────────────────────────────
def get_etf_info(
    etf_data: Optional[Dict[str, Optional[float]]] = None,
    now_bj: Optional[datetime] = None,
) -> EtfInfo:
    """
    计算 ETF 数据时间、可用性与完整性信息。

    Args:
        etf_data : {代码: 净流量(百万美元)} 字典；None 表示数据尚未获取。
                   流量为 None 或 0.0 视为该品种"未到账"。
        now_bj   : 当前北京时间；None 则自动取 datetime.now(BEIJING_TZ)。

    Returns:
        EtfInfo 实例，包含所有计算结果和格式化文本。
    """
    if now_bj is None:
        now_bj = datetime.now(BEIJING_TZ)

    info = EtfInfo()
    info.now_bj = now_bj

    wd = now_bj.weekday()          # 0=Mon … 6=Sun
    info.weekday_cn = WEEKDAYS_CN[wd]

    # ── 格式化"数据时间"字符串 ─────────────────────────────────────────────
    info.fetch_time_str = (
        f"北京时间 {info.weekday_cn} "
        f"{now_bj.year}.{now_bj.month}.{now_bj.day} "
        f"{now_bj.strftime('%H:%M')} 数据"
    )

    # ── 推算对应美国交易日 ─────────────────────────────────────────────────
    #
    # 时差规则（夏令时 EDT = UTC-4）：
    #   美东 16:00 收盘 = UTC 20:00 = 北京时间次日 04:00
    #
    # 北京时间早盘（09:30）对应的最近完整美股交易日：
    #
    #   星期日(wd=6): 美股周六、周日休市，最近完整数据 = 上周五
    #                  days_back = 2  (周日 - 2 = 周五)
    #   星期一(wd=0): 美股周一收盘在北京时间周二 04:00，
    #                  09:30 时周一美盘尚未收盘，最近完整数据 = 上周五
    #                  days_back = 3  (周一 - 3 = 上周五)
    #   星期二~六  : 前一日美股已于 04:00 收盘，数据新鲜
    #                  days_back = 1  (周二 - 1 = 周一，etc.)

    if wd == 6:        # 星期日
        days_back = 2
        info.has_fresh_data = False
        info.no_data_reason = (
            "⚠️ 今日（北京时间周日）无新增ETF数据\n"
            "   美股周六、周日休市，最近有效数据为上周五"
        )
    elif wd == 0:      # 星期一
        days_back = 3
        info.has_fresh_data = False
        info.no_data_reason = (
            "⚠️ 今日（北京时间周一）美股尚未收盘\n"
            "   美股周一收盘时间：北京时间周二 04:00\n"
            "   当前ETF数据为上周五，周二早盘简报将更新周一数据"
        )
    else:              # 星期二~六
        days_back = 1
        info.has_fresh_data = True

    info.us_data_date = (now_bj - timedelta(days=days_back)).date()
    info.us_data_date_str = str(info.us_data_date)

    # ── 完整性检查 ─────────────────────────────────────────────────────────
    if etf_data:
        reporting = {
            k: v for k, v in etf_data.items()
            if v is not None and v != 0.0
        }
        info.n_reporting = len(reporting)
        info.total_flow_m = sum(reporting.values())
        info.missing_critical = [
            t for t in CRITICAL_TICKERS
            if etf_data.get(t) in (None, 0.0)
        ]
        info.is_complete = (
            info.n_reporting >= COMPLETE_THRESHOLD
            and len(info.missing_critical) == 0
        )

        if info.is_complete:
            info.completeness_note = (
                f"✅ 数据完整（{info.n_reporting}/{TOTAL_ETF_COUNT} 只ETF已到账）"
            )
        else:
            crit_str = (
                f"，{'/'.join(info.missing_critical)}未到账"
                if info.missing_critical else ""
            )
            info.completeness_note = (
                f"⚠️ 数据待补全（{info.n_reporting}/{TOTAL_ETF_COUNT} 只ETF已到账{crit_str}）\n"
                f"   总额仅反映已到账部分，以美盘简报为准"
            )

        # 流量显示
        sign = '+' if info.total_flow_m >= 0 else '-'
        info.flow_display = (
            f"净流量：{sign}{_fmt_flow(info.total_flow_m)}"
            f"（{info.us_data_date_str}，已到账{info.n_reporting}只）"
        )
    
    # ── 组合完整 ETF 段落 ─────────────────────────────────────────────────
    info.header_block = _build_block(info)
    return info


# ─── 格式化辅助 ─────────────────────────────────────────────────────────────
def format_etf_block(
    etf_data: Optional[Dict[str, Optional[float]]] = None,
    now_bj: Optional[datetime] = None,
    source: str = 'Farside + SoSoValue 双源交叉验证',
) -> str:
    """
    一步返回完整 ETF 段落文本，供简报直接插入。

    Usage:
        etf_section = format_etf_block(etf_dict)
        briefing_text = briefing_text.replace('{ETF_BLOCK}', etf_section)
    """
    info = get_etf_info(etf_data, now_bj)
    return _build_block(info, source)


def _build_block(info: EtfInfo, source: str = 'Farside + SoSoValue 双源交叉验证') -> str:
    lines = [
        '【BTC 现货 ETF 资金流向解读】',
        f'数据来源：{source}',
        f'数据时间：{info.fetch_time_str}',
    ]

    if not info.has_fresh_data:
        # 周日 / 周一早盘：无新数据
        lines.append(info.no_data_reason)
        if info.total_flow_m != 0.0:
            # 仍展示上周五的旧数据供参考
            sign = '+' if info.total_flow_m >= 0 else '-'
            lines.append(
                f'参考（{info.us_data_date_str} 上周五数据）：'
                f'{sign}{_fmt_flow(info.total_flow_m)}'
            )
    else:
        # 正常交易日
        if info.flow_display:
            lines.append(info.flow_display)
        if info.completeness_note:
            lines.append(info.completeness_note)

    return '\n'.join(lines)


def _fmt_flow(m: float) -> str:
    """百万美元 → 中文易读格式（纯美元，不含正负号，调用方自行加前缀）"""
    abs_m = abs(m)
    if abs_m >= 1000:
        return f'{abs_m / 1000:.2f}亿美元'
    elif abs_m >= 100:
        return f'{abs_m:.0f}百万美元'
    else:
        return f'{abs_m:.1f}百万美元'


# ─── 快捷判断函数 ────────────────────────────────────────────────────────────
def is_no_data_day(now_bj: Optional[datetime] = None) -> bool:
    """当前北京时间是否处于无新ETF数据状态（周日或周一早盘）"""
    if now_bj is None:
        now_bj = datetime.now(BEIJING_TZ)
    return now_bj.weekday() in (0, 6)


def now_beijing() -> datetime:
    """返回当前北京时间"""
    return datetime.now(BEIJING_TZ)


def beijing_weekday_str(now_bj: Optional[datetime] = None) -> str:
    """返回北京时间的中文星期名"""
    if now_bj is None:
        now_bj = datetime.now(BEIJING_TZ)
    return WEEKDAYS_CN[now_bj.weekday()]


# ─── 调试/测试入口 ──────────────────────────────────────────────────────────
if __name__ == '__main__':
    from datetime import datetime as dt

    # 模拟不同场景
    scenarios = [
        ("2026-06-23 09:30", {'IBIT': None, 'FBTC': 57.4, 'ARKB': 64.0, 'GBTC': -81.0, 'MSBT': 8.1}),
        ("2026-06-22 09:30", {'IBIT': -172.0, 'FBTC': 57.4, 'ARKB': 64.0, 'GBTC': -81.0, 'MSBT': 8.1,
                               'BITB': 0.0, 'BTCO': 3.7, 'EZBC': 0.0, 'BRRR': 0.0, 'HODL': 3.4,
                               'BTCW': 0.0, 'BTC': 48.1}),
        ("2026-06-21 09:30", None),   # 周日，无新数据
        ("2026-06-20 09:30", {'IBIT': 66.4, 'FBTC': -8.7, 'GBTC': -124.0, 'ARKB': -6.6,
                               'BITB': 0.0, 'BTCO': 0.0, 'EZBC': -5.8, 'BRRR': 0.0,
                               'HODL': -6.1, 'MSBT': 0.0, 'BTCW': 0.0, 'BTC': 10.6}),
    ]

    for ts, data in scenarios:
        now = dt.fromisoformat(ts).replace(tzinfo=BEIJING_TZ)
        block = format_etf_block(data, now)
        print('─' * 60)
        print(f'[模拟时间] {ts}')
        print(block)
        print()
