"""
Binance 市场结构数据摘要 v2
上传位置：/opt/btc-trader/briefing/binance_briefing_data.py

新增功能（v2）：
  - 资金费率 Z-score：基于近24小时滚动窗口标准化当前费率
  - 三因子市场状态分类：OI变化 × 费率极端度 × 多空拥挤度 → 市场状态标签
  - get_market_meta()：供 build_header() 提取结构化数据注入 TG Header

供 daily_briefing.py 调用，将结构化数据注入 AI 提示词。
直接运行可验证输出：python briefing/binance_briefing_data.py
"""

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/opt/btc-trader/btc_history.db")

# ── 模块级缓存：get_binance_context() 运行后由 get_market_meta() 取用 ──────
_MARKET_META: dict = {}


# ── 内部工具函数 ───────────────────────────────────────────────────────────

def _q(sql: str, params: tuple = ()):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _fmt_b(val):
    """格式化为亿美元，与项目 fmt_usd() 风格一致"""
    if val is None:
        return "N/A"
    return f"{val / 1e8:.2f}亿"


# ── 新增：资金费率 Z-score 计算 ────────────────────────────────────────────

def _calc_funding_zscore(fr_hist: list, current_rate_pct: float) -> dict:
    """
    资金费率 Z-score（24小时滚动窗口）

    fr_hist         : 近24小时的 binance_funding 记录，每条含 'rate' 字段（小数形式）
    current_rate_pct: 当前实时资金费率（已 ×100，百分比形式）

    最少需要 12 条记录（约1小时 @5分钟采集频率）才返回有效 Z-score。
    不足时返回 zscore=None，调用方需做防空判断。
    """
    if len(fr_hist) < 12:
        return {
            "zscore": None,
            "mean": None,
            "std": None,
            "label": "数据不足（< 1小时）",
            "sample_n": len(fr_hist),
        }

    rates = [r["rate"] * 100 for r in fr_hist]   # 全部转为百分比
    n = len(rates)
    mean = sum(rates) / n
    std = (sum((r - mean) ** 2 for r in rates) / n) ** 0.5

    if std < 1e-8:
        return {
            "zscore": 0.0,
            "mean": round(mean, 4),
            "std": round(std, 6),
            "label": "费率极度平稳（无差异）",
            "sample_n": n,
        }

    zscore = (current_rate_pct - mean) / std

    if zscore > 2.0:
        label = "极端偏高 ⚠️ 多头严重拥挤，不宜追多"
    elif zscore > 1.0:
        label = "中度偏高，多头偏重，谨慎追多"
    elif zscore < -2.0:
        label = "极端偏低 ⚠️ 空头严重拥挤，关注轧空"
    elif zscore < -1.0:
        label = "中度偏低，空头偏重，谨慎追空"
    else:
        label = "中性，信号相对干净"

    return {
        "zscore": round(zscore, 2),
        "mean": round(mean, 4),
        "std": round(std, 4),
        "label": label,
        "sample_n": n,
    }


# ── 新增：三因子市场状态分类 ───────────────────────────────────────────────

def _regime_lookup(oi_s: str, fr_s: str, ls_s: str) -> tuple:
    """
    状态矩阵映射。
    返回 (状态标签, 操作导向)。
    """
    # ── 明确分类 ─────────────────────────────────────────────────────────
    if oi_s == "↑上升" and fr_s in ("极高", "偏高") and ls_s == "多头拥挤":
        return ("过热 / 顶部风险 ⚠️",
                "不追多 | 等待轧多信号 | 多头踩踏风险高")

    if oi_s == "↑上升" and fr_s in ("极低", "偏低") and ls_s == "空头拥挤":
        return ("挤压酝酿中 🔄",
                "关注空头挤压启动 | 轻多方向 | 需IB结构确认")

    if oi_s == "↑上升" and fr_s == "中性" and ls_s == "多空均衡":
        return ("真实趋势建仓中 📊",
                "新仓进场中 | 可顺势跟随 | 持续关注OI方向")

    if oi_s == "↓下降" and fr_s in ("极高", "偏高") and ls_s == "多头拥挤":
        return ("多头被迫平仓 ⬇️",
                "不接多 | 等待OI企稳和FR回归后再定方向")

    if oi_s == "↓下降" and fr_s in ("极低", "偏低") and ls_s == "空头拥挤":
        return ("空头被迫平仓 ⬆️",
                "不接空 | 等待OI企稳和FR回归后再定方向")

    if oi_s == "↓下降" and fr_s == "中性":
        return ("去杠杆 / 清洗中 📉",
                "降低仓位 | 等待方向确认 | 不追单边")

    if oi_s == "→平稳" and fr_s in ("极高", "偏高") and ls_s == "多头拥挤":
        return ("横盘多头拥挤 ⚠️",
                "警惕向下清算 | 减轻多头敞口 | 不宜加仓")

    if oi_s == "→平稳" and fr_s in ("极低", "偏低") and ls_s == "空头拥挤":
        return ("横盘空头拥挤 ⚠️",
                "警惕向上挤压 | 减轻空头敞口 | 不宜加仓")

    # ── 修复（v2.1）：ls_s == "多空均衡" 才属于健康趋势 ──────────────────
    # 用 FR 极端度区分多/空主导（偏高=多方成本高→偏多，偏低=空方成本高→偏空）
    # 中性FR单独处理，避免两个条件同时包含"中性"导致第一个永远命中
    if oi_s == "→平稳" and fr_s == "偏高" and ls_s == "多空均衡":
        return ("健康趋势（多方主导）✅",
                "顺势方向做多 | 回踩关键支撑入场")

    if oi_s == "→平稳" and fr_s == "偏低" and ls_s == "多空均衡":
        return ("健康趋势（空方主导）✅",
                "顺势方向做空 | 反弹关键阻力入场")

    if oi_s == "→平稳" and fr_s == "中性" and ls_s == "多空均衡":
        return ("趋势延续（均衡）✅",
                "多空均衡信号最干净 | 顺当前象限方向操作")

    # ── 新增：平稳OI + 中性FR + 单边拥挤 ────────────────────────────────
    # 典型场景：散户多头67%但FR中性，市场横盘但多头积压，是下行风险而非健康趋势
    if oi_s == "→平稳" and ls_s == "多头拥挤":
        return ("多头拥挤承压 ⚠️",
                "多头仓位拥挤，不宜追多 | 等待拥挤消化 | 关注向下扫多止损")

    if oi_s == "→平稳" and ls_s == "空头拥挤":
        return ("空头拥挤承托 ⚠️",
                "空头仓位拥挤，不宜追空 | 等待拥挤消化 | 关注向上挤压信号")

    # ── 混合信号 / 默认 ──────────────────────────────────────────────────
    return (
        f"混合信号（OI{oi_s} | FR{fr_s} | {ls_s}）",
        "信号分歧 | 轻仓观望 | 等待结构清晰"
    )


def _classify_market_regime(oi_chg_1h: float, fr_zscore, long_pct: float) -> dict:
    """
    三因子市场状态分类

    oi_chg_1h : 近1小时 OI 变化百分比（正=上升，负=下降）
    fr_zscore : 资金费率 Z-score（None = 数据不足）
    long_pct  : 全市场账户多头占比 0-100

    Returns dict 含 regime / action / oi_status / fr_status / ls_status
    """
    # ── 因子1：OI变化 ─────────────────────────────────────────────────────
    if oi_chg_1h > 0.3:
        oi_s = "↑上升"
    elif oi_chg_1h < -0.3:
        oi_s = "↓下降"
    else:
        oi_s = "→平稳"

    # ── 因子2：资金费率极端度 ──────────────────────────────────────────────
    if fr_zscore is None:
        fr_s = "未知"
    elif fr_zscore > 2.0:
        fr_s = "极高"
    elif fr_zscore > 1.0:
        fr_s = "偏高"
    elif fr_zscore < -2.0:
        fr_s = "极低"
    elif fr_zscore < -1.0:
        fr_s = "偏低"
    else:
        fr_s = "中性"

    # ── 因子3：多空拥挤度 ──────────────────────────────────────────────────
    if long_pct > 60:
        ls_s = "多头拥挤"
    elif long_pct < 40:
        ls_s = "空头拥挤"
    else:
        ls_s = "多空均衡"

    regime, action = _regime_lookup(oi_s, fr_s, ls_s)

    return {
        "regime": regime,
        "action": action,
        "oi_status": oi_s,
        "fr_status": fr_s,
        "ls_status": ls_s,
    }


# ── 主函数 ────────────────────────────────────────────────────────────────

def get_binance_context() -> str:
    """
    返回供 AI 简报使用的 Binance 市场结构数据段落。
    输出为中文纯文本，可直接追加到 Claude prompt。

    v2 新增：
      - 资金费率 24H Z-score（判断当前费率在历史分位中的极端程度）
      - 三因子市场状态分类（OI × 费率极端度 × 多空拥挤度）
      同时将结构化数据写入 _MARKET_META，供 get_market_meta() 取用（TG Header使用）。

    若数据库无数据（服务未启动），返回空字符串，不影响简报流程。
    """
    global _MARKET_META
    now = int(time.time())

    # ── 基础数据查询 ────────────────────────────────────────────────────────
    oi      = _q("SELECT * FROM binance_oi        ORDER BY ts DESC LIMIT 1")
    fr      = _q("SELECT * FROM binance_funding    ORDER BY ts DESC LIMIT 1")
    ls_top  = _q("SELECT * FROM binance_ls_top     ORDER BY ts DESC LIMIT 1")
    ls_gl   = _q("SELECT * FROM binance_ls_global  ORDER BY ts DESC LIMIT 1")
    struct  = _q("SELECT * FROM binance_structure  ORDER BY ts DESC LIMIT 1")

    # 若最新结构数据超过 30 分钟，说明采集服务可能异常，跳过
    if not struct or now - struct[0]["ts"] > 1800:
        _MARKET_META = {}
        return ""

    # ── 近1小时 OI 趋势 ────────────────────────────────────────────────────
    oi_1h = _q(
        "SELECT oi_usd FROM binance_oi WHERE ts >= ? ORDER BY ts ASC",
        (now - 3600,)
    )

    # ── 近 24 小时资金费率历史（用于 Z-score）──────────────────────────────
    fr_hist = _q(
        "SELECT rate FROM binance_funding WHERE ts >= ? ORDER BY ts ASC",
        (now - 86400,)
    )

    # ── 近6小时有效象限分布（排除 FLAT）──────────────────────────────────
    q_dist = _q("""
        SELECT quadrant, COUNT(*) AS cnt
        FROM binance_structure
        WHERE ts >= ? AND quadrant != 'FLAT'
        GROUP BY quadrant
        ORDER BY cnt DESC
    """, (now - 21600,))

    lines = ["【Binance 市场结构数据（实时）】"]

    # ── OI ─────────────────────────────────────────────────────────────────
    oi_chg_1h = 0.0
    if oi:
        o = oi[0]
        lines.append(f"持仓量（OI）：{_fmt_b(o['oi_usd'])} USD / {o['oi_btc']:.0f} BTC")

        if len(oi_1h) >= 2:
            oi_chg_1h = (oi_1h[-1]["oi_usd"] - oi_1h[0]["oi_usd"]) / oi_1h[0]["oi_usd"] * 100
            direction = "↑增加" if oi_chg_1h > 0 else "↓减少"
            lines.append(f"  近1小时OI变化：{oi_chg_1h:+.2f}%（{direction}）")

    # ── 资金费率 + Z-score ─────────────────────────────────────────────────
    current_rate_pct = 0.0
    zscore_result = {"zscore": None, "label": "数据不足", "mean": None, "sample_n": 0}

    if fr:
        f0 = fr[0]
        current_rate_pct = f0["rate"] * 100
        next_dt  = datetime.fromtimestamp(f0["next_settle"], tz=timezone.utc)
        next_str = next_dt.strftime("%H:%M UTC")

        if current_rate_pct > 0.05:
            rate_note = "（偏高，多头持仓成本上升，潜在回调压力）"
        elif current_rate_pct < -0.02:
            rate_note = "（偏低，空头成本上升，轧空风险上升）"
        else:
            rate_note = "（中性）"

        lines.append(f"资金费率：{current_rate_pct:+.4f}%{rate_note}")
        lines.append(f"  Mark/Index溢价：{f0['premium_pct']:+.4f}%  下次结算：{next_str}")

        # ── Z-score 计算 ──────────────────────────────────────────────────
        zscore_result = _calc_funding_zscore(fr_hist, current_rate_pct)
        z = zscore_result["zscore"]
        z_str = f"{z:+.2f}" if z is not None else "N/A"
        sample_h = round(zscore_result["sample_n"] / 12, 1)  # 换算为小时（@5分钟频率）
        lines.append(
            f"  费率Z-score：{z_str}（基于近{sample_h:.0f}小时{zscore_result['sample_n']}条数据）"
        )
        lines.append(f"  Z-score解读：{zscore_result['label']}")

    # ── 大户多空比 ────────────────────────────────────────────────────────
    long_pct_for_regime = 50.0   # 默认中性
    if ls_top:
        l   = ls_top[0]
        ls  = l["ls_ratio"]
        lp  = l["long_pct"] * 100
        sp  = l["short_pct"] * 100

        if ls > 3.0:
            ls_note = "（大户极度偏多——逆向信号，警惕回调）"
        elif ls > 2.0:
            ls_note = "（大户明显偏多）"
        elif ls < 0.5:
            ls_note = "（大户极度偏空——逆向信号，警惕轧空）"
        elif ls < 0.8:
            ls_note = "（大户明显偏空）"
        else:
            ls_note = "（多空相对均衡）"

        lines.append(f"大户持仓多空比：{ls:.3f}（多{lp:.1f}% / 空{sp:.1f}%）{ls_note}")

    if ls_gl:
        l = ls_gl[0]
        glong_pct  = l["long_pct"] * 100
        gshort_pct = l["short_pct"] * 100
        long_pct_for_regime = glong_pct   # 全市场账户用于三因子分类
        lines.append(
            f"全市场账户多空比：{l['ls_ratio']:.3f}"
            f"（多{glong_pct:.1f}% / 空{gshort_pct:.1f}%）"
        )

    # ── 当前象限 ──────────────────────────────────────────────────────────
    if struct:
        s    = struct[0]
        quad = s["quadrant"]
        note = s["note"]

        interp_map = {
            "Q1":   "多头新仓主导，OI与价格同步上升，趋势可持续性强，顺势做多方向",
            "Q2":   "空头新仓主导，OI上升价格下降，趋势可持续性强，顺势做空方向",
            "Q3":   "空头被迫平仓引发上涨，无新多头进场，可持续性弱，谨慎追多",
            "Q4":   "多头被迫平仓引发下跌，无新空头进场，可持续性弱，警惕反弹",
            "FLAT": "OI与价格变化均不显著，市场处于震荡积累阶段",
        }
        interp = interp_map.get(quad, "")

        lines.append(f"当前市场象限：{quad} — {note}")
        lines.append(f"  解读：{interp}")
        lines.append(f"  OI变化：{s['oi_chg']:+.3f}%  价格变化：{s['px_chg']:+.3f}%")

    # ── 近6小时象限分布 ───────────────────────────────────────────────────
    if q_dist:
        dist_str = " | ".join(f"{r['quadrant']}×{r['cnt']}次" for r in q_dist)
        lines.append(f"近6小时象限分布（排除震荡）：{dist_str}")
    else:
        lines.append("近6小时：持续震荡，无明显方向性象限")

    # ── 三因子市场状态分类（NEW）──────────────────────────────────────────
    regime_result = _classify_market_regime(
        oi_chg_1h,
        zscore_result["zscore"],
        long_pct_for_regime
    )

    lines.append("")
    lines.append("【三因子市场状态分类（实时）】")
    lines.append(
        f"  OI动向：{regime_result['oi_status']}  "
        f"费率极端度：{regime_result['fr_status']}  "
        f"多空拥挤：{regime_result['ls_status']}"
    )
    lines.append(f"  => 当前状态：{regime_result['regime']}")
    lines.append(f"  => 操作导向：{regime_result['action']}")

    # ── 写入缓存供 get_market_meta() 使用 ──────────────────────────────────
    _MARKET_META = {
        "fr_zscore":       zscore_result.get("zscore"),
        "fr_zscore_label": zscore_result.get("label", ""),
        "regime":          regime_result["regime"],
        "regime_action":   regime_result["action"],
        "oi_status":       regime_result["oi_status"],
        "fr_status":       regime_result["fr_status"],
        "ls_status":       regime_result["ls_status"],
    }

    return "\n".join(lines)


def get_market_meta() -> dict:
    """
    返回结构化的市场状态数据，供 build_header() 注入 TG Header。
    必须在 get_binance_context() 之后调用（数据由后者写入缓存）。

    返回字段：
        fr_zscore       : float | None  资金费率 Z-score
        fr_zscore_label : str           Z-score 解读标签
        regime          : str           三因子状态标签（如"过热 / 顶部风险 ⚠️"）
        regime_action   : str           操作导向（如"不追多 | 等待轧多信号"）
        oi_status       : str           OI动向（↑上升 / ↓下降 / →平稳）
        fr_status       : str           费率极端度（极高/偏高/中性/偏低/极低）
        ls_status       : str           多空拥挤度（多头拥挤/多空均衡/空头拥挤）
    """
    return _MARKET_META.copy()


if __name__ == "__main__":
    result = get_binance_context()
    if result:
        print(result)
        print("\n--- market_meta ---")
        import json
        print(json.dumps(get_market_meta(), ensure_ascii=False, indent=2))
    else:
        print("暂无数据（服务未启动或数据超时）")
