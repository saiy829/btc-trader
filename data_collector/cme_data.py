"""
CME BTC 历史缺口追踪 v2
========================
背景：2026年5月29日，CME Group 正式将 Bitcoin 期货/期权切换为 7×24 小时交易
（每周六 UTC 03:00-05:00 保留两小时系统维护窗口），彻底结束了延续多年的
"周末缺口"机制。自此不再产生新的 CME 周末缺口。

本模块仅追踪 24/7 上线前形成、尚未被价格填补的 3 个历史遗留缺口：
  - 缺口①：$79,200 - $80,400（2026年1月末高位）
  - 缺口②：$78,000 - $78,500（2026年Q1 次高位）
  - 缺口③：$69,000 - $70,000（2026年Q1 中段）

当全部 3 个缺口均被填补后，本模块自动输出"退休"提示，
调用方可根据此信号从简报中移除 CME 分析节。

数据来源：CoinDesk / CCN 报道（2026-05-28），Binance 现货价格近似 CME 期货价格。
"""

import requests
from datetime import datetime, timezone
from utils.helpers import setup_logger

logger = setup_logger()

SPOT_URL = "https://api.binance.com"

# ── CME 24/7 转型关键信息 ─────────────────────────────────────────────────
TRANSITION_DATE    = "2026-05-29"
MAINTENANCE_WINDOW = "每周六 UTC 03:00-05:00（北京时间 11:00-13:00）"

# ── 3 个已知历史遗留缺口（24/7 上线前形成）────────────────────────────────
# gap_bot / gap_top 均为 Binance 现货近似值，与实际 CME 合约价格存在小幅基差
LEGACY_GAPS = [
    {
        "id":      1,
        "name":    "1月末高位缺口",
        "gap_bot": 79_200,
        "gap_top": 80_400,
        "formed":  "2026-01 周末",
        "note":    "BTC 高位回落阶段形成，属于前高区域流动性，填补需价格重返 $79K+",
    },
    {
        "id":      2,
        "name":    "Q1 次高位缺口",
        "gap_bot": 78_000,
        "gap_top": 78_500,
        "formed":  "2026-Q1",
        "note":    "紧邻缺口①下方，两者合并看约 $78K-$80.4K 为密集缺口区",
    },
    {
        "id":      3,
        "name":    "Q1 中段缺口",
        "gap_bot": 69_000,
        "gap_top": 70_000,
        "formed":  "2026-Q1",
        "note":    "修正中段形成，曾一度在价格下方，随 BTC 继续下跌已转至上方",
    },
]


def _current_price() -> float:
    """获取 Binance BTCUSDT 当前现货价格"""
    try:
        r = requests.get(
            f"{SPOT_URL}/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=6,
        )
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        logger.warning(f"CME追踪：获取当前价格失败: {e}")
        return 0.0


def get_cme_gap() -> dict:
    """
    返回 CME 历史缺口追踪数据（v2 格式）。

    返回字段：
        mode              : "legacy" — 固定值，标识 24/7 后的追踪模式
        transition_date   : CME 切换 24/7 的日期
        maintenance_window: 每周维护窗口说明
        current_price     : 当前 BTC 现货价格
        gaps              : list[dict]，每个缺口的完整信息
        unfilled_count    : 未填缺口数量
        all_filled        : bool，True 表示全部填补，模块可退休
        closest_gap       : 距当前价最近的未填缺口（None 表示全部填补）
    """
    price = _current_price()

    enriched_gaps = []
    for g in LEGACY_GAPS:
        bot, top = g["gap_bot"], g["gap_top"]

        # 填补判断：价格区间覆盖缺口下沿即视为填补
        # （上方缺口需价格上涨至 gap_bot 以上才算填补）
        is_filled = (price >= bot) if price > 0 else False

        if price > 0 and not is_filled:
            dist = bot - price          # 距填补所需价格（上方缺口：需涨多少）
            dist_pct = dist / price * 100
        else:
            dist = 0.0
            dist_pct = 0.0

        enriched_gaps.append({
            **g,
            "is_filled":    is_filled,
            "dist_to_fill": round(dist, 0),
            "dist_pct":     round(dist_pct, 2),
            "size":         top - bot,
            "midpoint":     (top + bot) / 2,
        })

    unfilled = [g for g in enriched_gaps if not g["is_filled"]]
    all_filled = len(unfilled) == 0

    # 最近的未填缺口：dist_to_fill 最小的那个
    closest = min(unfilled, key=lambda g: g["dist_to_fill"]) if unfilled else None

    logger.info(
        f"CME历史缺口追踪 | 当前价 ${price:,.0f} | "
        f"未填 {len(unfilled)}/3 | "
        + (f"最近缺口 ${closest['gap_bot']:,}-${closest['gap_top']:,} "
           f"(距 +${closest['dist_to_fill']:,.0f})" if closest else "全部已填补 ✅")
    )

    return {
        "mode":               "legacy",
        "transition_date":    TRANSITION_DATE,
        "maintenance_window": MAINTENANCE_WINDOW,
        "current_price":      price,
        "gaps":               enriched_gaps,
        "unfilled_count":     len(unfilled),
        "all_filled":         all_filled,
        "closest_gap":        closest,
    }
