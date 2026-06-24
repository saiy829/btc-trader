"""
Binance 市场结构数据摘要
上传位置：/opt/btc-trader/briefing/binance_briefing_data.py

供 daily_briefing.py 调用，将结构化数据注入 AI 提示词。
直接运行可验证输出：python briefing/binance_briefing_data.py

调用方式（在 daily_briefing.py 的 prompt 构建处追加）：
    from binance_briefing_data import get_binance_context
    # 在 prompt 字符串末尾加入：
    prompt += "\\n\\n" + get_binance_context()
"""

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/opt/btc-trader/btc_history.db")


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


def get_binance_context() -> str:
    """
    返回供 AI 简报使用的 Binance 市场结构数据段落。
    输出为中文纯文本，可直接追加到 Claude prompt。
    若数据库无数据（服务未启动），返回空字符串，不影响简报流程。
    """
    now = int(time.time())

    oi      = _q("SELECT * FROM binance_oi        ORDER BY ts DESC LIMIT 1")
    fr      = _q("SELECT * FROM binance_funding    ORDER BY ts DESC LIMIT 1")
    ls_top  = _q("SELECT * FROM binance_ls_top     ORDER BY ts DESC LIMIT 1")
    ls_gl   = _q("SELECT * FROM binance_ls_global  ORDER BY ts DESC LIMIT 1")
    struct  = _q("SELECT * FROM binance_structure  ORDER BY ts DESC LIMIT 1")

    # 若最新结构数据超过 30 分钟，说明采集服务可能异常，跳过
    if not struct or now - struct[0]["ts"] > 1800:
        return ""

    # 近 1 小时 OI 趋势
    oi_1h = _q(
        "SELECT oi_usd FROM binance_oi WHERE ts >= ? ORDER BY ts ASC",
        (now - 3600,)
    )

    # 近 6 小时有效象限分布（排除 FLAT）
    q_dist = _q("""
        SELECT quadrant, COUNT(*) AS cnt
        FROM binance_structure
        WHERE ts >= ? AND quadrant != 'FLAT'
        GROUP BY quadrant
        ORDER BY cnt DESC
    """, (now - 21600,))

    lines = ["【Binance 市场结构数据（实时）】"]

    # ── OI ───────────────────────────────────────────────
    if oi:
        o = oi[0]
        lines.append(f"持仓量（OI）：{_fmt_b(o['oi_usd'])} USD / {o['oi_btc']:.0f} BTC")

        if len(oi_1h) >= 2:
            chg = (oi_1h[-1]["oi_usd"] - oi_1h[0]["oi_usd"]) / oi_1h[0]["oi_usd"] * 100
            direction = "↑增加" if chg > 0 else "↓减少"
            lines.append(f"  近1小时OI变化：{chg:+.2f}%（{direction}）")

    # ── 资金费率 ──────────────────────────────────────────
    if fr:
        f = fr[0]
        rate_pct = f["rate"] * 100
        next_dt  = datetime.fromtimestamp(f["next_settle"], tz=timezone.utc)
        next_str = next_dt.strftime("%H:%M UTC")

        if rate_pct > 0.05:
            rate_note = "（偏高，多头持仓成本上升，潜在回调压力）"
        elif rate_pct < -0.02:
            rate_note = "（偏低，空头成本上升，轧空风险上升）"
        else:
            rate_note = "（中性）"

        lines.append(f"资金费率：{rate_pct:+.4f}%{rate_note}")
        lines.append(f"  Mark/Index溢价：{f['premium_pct']:+.4f}%  下次结算：{next_str}")

    # ── 大户多空比 ─────────────────────────────────────────
    if ls_top:
        l  = ls_top[0]
        ls = l["ls_ratio"]
        lp = l["long_pct"] * 100
        sp = l["short_pct"] * 100

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
        lines.append(
            f"全市场账户多空比：{l['ls_ratio']:.3f}"
            f"（多{l['long_pct']*100:.1f}% / 空{l['short_pct']*100:.1f}%）"
        )

    # ── 当前象限 ───────────────────────────────────────────
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

    # ── 近6小时象限分布 ────────────────────────────────────
    if q_dist:
        dist_str = " | ".join(f"{r['quadrant']}×{r['cnt']}次" for r in q_dist)
        lines.append(f"近6小时象限分布（排除震荡）：{dist_str}")
    else:
        lines.append("近6小时：持续震荡，无明显方向性象限")

    return "\n".join(lines)


if __name__ == "__main__":
    result = get_binance_context()
    if result:
        print(result)
    else:
        print("暂无数据（服务未启动或数据超时）")
