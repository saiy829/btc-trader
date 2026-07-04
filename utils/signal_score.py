"""
综合信号分 —— 代码确定性计算模块（Phase 7A-2）

背景：2026-07-04 早盘简报里 AI 自算的综合分（-12）与其自报的六维加权和（-4.8）
对不上——大模型心算不可靠。本模块把六维打分和加权求和全部改成确定性代码计算，
AI 只负责引用结果并解释市场含义，不再自己算数。

数据来源：
  - ETF净流 / CB溢价：由 ai_analyst/briefing.py 的 build_prompt() 直接传入
    （这两个值来自外部 API 采集，不在本地滚动历史表里）
  - 资金费率Z-score：直接复用 briefing.binance_briefing_data.get_market_meta()
    的 fr_zscore，不在本模块重新计算，避免和其他地方显示的 Z 值产生数值漂移
  - OI象限 / 近1小时OI变化率 / 大户多空比：本模块直接查询 btc_history.db 的
    binance_structure / binance_oi / binance_ls_top 三张表（与
    briefing/binance_briefing_data.py 用同一批表，口径一致）
    注：大户多空比用 binance_ls_top.ls_ratio 字段本身（Binance按持仓量计算的
    比值），不是 61.7%/38.3% 账户占比换算出来的比值——实测两者差异很大
    （2026-07-04早盘实例：1.231 vs 61.7/38.3=1.611），账户占比换算会导致
    分类档位判断错误。

结果写入 signal_scores 表，供 Phase 5B 回测分数与胜率的相关性。

已知设计缺口（见 Phase 7A-2 任务对话记录，未来校准用）：
  三因子状态实际有14种细分标签，任务卡SCORE_CONFIG.regime_map只定义了6个
  关键词类别的分数。未覆盖的7种（多头/空头被迫平仓、横盘多头/空头拥挤、
  多头拥挤承压、空头拥挤承托、趋势延续-均衡）统一归入"混合信号"记0分，
  这是当前版本的保守处理，不是遗漏。
"""
import json
import sqlite3
import time
from pathlib import Path

from utils.helpers import setup_logger

logger = setup_logger("signal_score")

DB_PATH = Path("/opt/btc-trader/btc_history.db")


# ── 所有阈值/权重集中在这里，便于日后校准 ──────────────────────────────────
SCORE_CONFIG = {
    "weights": {
        "etf":        0.25,
        "funding_z":  0.15,
        "oi_quadrant": 0.20,
        "ls_ratio":   0.15,
        "cb_premium": 0.10,
        "regime":     0.15,
    },
    "etf": {
        "yest_mult": 30, "yest_clamp": 60,   # 最新日净流(亿美元) × 30，clamp ±60
        "week_mult": 8,  "week_clamp": 40,   # 本周累计(亿美元) × 8，clamp ±40
    },
    "funding_z": {
        "low_mult": 10,          # |z|<=1： z×10
        "mid_base": 10,          # 1<|z|<=2： sign(z)×(10+(|z|-1)×20)
        "mid_mult": 20,
        "extreme_score": 60,     # |z|>2： -sign(z)×60（极端拥挤视为反向信号）
    },
    "oi_quadrant": {
        "base": {"Q1": 40, "Q2": -40, "Q3": -25, "Q4": 15, "FLAT": 0},
        "chg1h_mult": 15, "chg1h_clamp": 15,  # 近1小时OI变化率(%) × 15，clamp ±15
    },
    "ls_ratio": {
        # 0.67<=r<=1.5 → 0
        "neutral_lo": 0.67, "neutral_hi": 1.5,
        # 1.5<r<=3.0 → 线性 -20 ~ -60
        "long_crowd_hi": 3.0,
        "long_crowd_score_at_lo": -20, "long_crowd_score_at_hi": -60,
        # r>3.0 → -60
        "extreme_long_score": -60,
        # 0.33<=r<0.67 → 线性 +60 ~ +20
        "short_crowd_lo": 0.33,
        "short_crowd_score_at_lo": 60, "short_crowd_score_at_hi": 20,
        # r<0.33 → +60
        "extreme_short_score": 60,
    },
    "cb_premium": {
        "mult": 0.5, "clamp": 50,   # 溢价USD × 0.5，clamp ±50
    },
    "regime_map": {
        "真实建仓": 50, "健康趋势": 40, "混合信号": 0,
        "挤压酝酿": -10, "过热": -40, "去杠杆清洗": -40,
    },
    "label_thresholds": {
        "neutral": 20,  # |s|<=20 → 中性/信号弱
        "strong":  50,  # 20<|s|<=50 → 偏多/偏空；|s|>50 → 强烈偏多/强烈偏空
    },
}


# ── 工具函数 ─────────────────────────────────────────────────────────────

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _q(sql: str, params: tuple = ()):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"signal_score DB查询失败: {e}")
        return []


def _query_market_snapshot():
    """
    直接查 btc_history.db，取当前象限 / 近1小时OI变化率(%) / 大户持仓多空比ls_ratio。
    与 briefing/binance_briefing_data.py 用同一批表，保持口径一致。
    任一字段缺失时返回 None，由调用方按"数据缺失"处理。
    """
    now = int(time.time())

    struct = _q("SELECT quadrant FROM binance_structure ORDER BY ts DESC LIMIT 1")
    quadrant = struct[0]["quadrant"] if struct else None

    oi_1h = _q(
        "SELECT oi_usd FROM binance_oi WHERE ts >= ? ORDER BY ts ASC",
        (now - 3600,)
    )
    oi_chg_1h = None
    if len(oi_1h) >= 2 and oi_1h[0]["oi_usd"]:
        oi_chg_1h = (oi_1h[-1]["oi_usd"] - oi_1h[0]["oi_usd"]) / oi_1h[0]["oi_usd"] * 100

    ls_top = _q("SELECT ls_ratio FROM binance_ls_top ORDER BY ts DESC LIMIT 1")
    ls_ratio_r = ls_top[0]["ls_ratio"] if ls_top else None

    return quadrant, oi_chg_1h, ls_ratio_r


# ── 六维打分函数（每个返回 (分数:int, 备注:str|None)）─────────────────────

def _score_etf(etf: dict):
    if not etf or not etf.get("has_data"):
        return 0, "数据缺失"
    cfg = SCORE_CONFIG["etf"]
    # total_yest / total_week 单位是百万美元（见 data_collector/etf_data.py _fmt_cny），
    # 换算成"亿美元"要除以100
    yest_yi = (etf.get("total_yest") or 0) / 100
    week_yi = (etf.get("total_week") or 0) / 100
    s1 = _clamp(yest_yi * cfg["yest_mult"], -cfg["yest_clamp"], cfg["yest_clamp"])
    s2 = _clamp(week_yi * cfg["week_mult"], -cfg["week_clamp"], cfg["week_clamp"])
    return round(s1 + s2), None


def _score_funding_z(fr_zscore):
    if fr_zscore is None:
        return 0, "数据缺失"
    cfg = SCORE_CONFIG["funding_z"]
    z = fr_zscore
    az = abs(z)
    sign = 1 if z >= 0 else -1
    if az <= 1:
        s = z * cfg["low_mult"]
    elif az <= 2:
        s = sign * (cfg["mid_base"] + (az - 1) * cfg["mid_mult"])
    else:
        s = -sign * cfg["extreme_score"]
    return round(s), None


def _score_oi_quadrant(quadrant, oi_chg_1h):
    if quadrant is None:
        return 0, "数据缺失"
    cfg = SCORE_CONFIG["oi_quadrant"]
    base = cfg["base"].get(quadrant, 0)
    chg_component = 0.0
    if oi_chg_1h is not None:
        chg_component = _clamp(oi_chg_1h * cfg["chg1h_mult"], -cfg["chg1h_clamp"], cfg["chg1h_clamp"])
    note = None if oi_chg_1h is not None else "近1小时OI变化率缺失，仅计基础分"
    return round(base + chg_component), note


def _score_ls_ratio(r):
    if r is None or r <= 0:
        return 0, "数据缺失"
    cfg = SCORE_CONFIG["ls_ratio"]
    if cfg["neutral_lo"] <= r <= cfg["neutral_hi"]:
        s = 0.0
    elif r > cfg["long_crowd_hi"]:
        s = cfg["extreme_long_score"]
    elif r > cfg["neutral_hi"]:                     # 1.5 < r <= 3.0
        frac = (r - cfg["neutral_hi"]) / (cfg["long_crowd_hi"] - cfg["neutral_hi"])
        s = cfg["long_crowd_score_at_lo"] + frac * (
            cfg["long_crowd_score_at_hi"] - cfg["long_crowd_score_at_lo"])
    elif r < cfg["short_crowd_lo"]:
        s = cfg["extreme_short_score"]
    else:                                            # 0.33 <= r < 0.67
        frac = (r - cfg["short_crowd_lo"]) / (cfg["neutral_lo"] - cfg["short_crowd_lo"])
        s = cfg["short_crowd_score_at_lo"] + frac * (
            cfg["short_crowd_score_at_hi"] - cfg["short_crowd_score_at_lo"])
    return round(s), None


def _score_cb_premium(cb_premium):
    if cb_premium is None:
        return 0, "数据缺失"
    cfg = SCORE_CONFIG["cb_premium"]
    s = _clamp(cb_premium * cfg["mult"], -cfg["clamp"], cfg["clamp"])
    return round(s), None


def _score_regime(regime_label: str):
    """
    三因子状态查表打分。任务卡只定义了6个关键词类别的分数，但实际系统
    _regime_lookup() 会产出14种细分标签。这里用关键词匹配覆盖能对应上的
    6类，其余（被迫平仓/横盘拥挤/拥挤承压承托/趋势延续等7种）统一按
    "混合信号"处理记0分——不在SCORE_CONFIG范围内的类别不擅自定义新分数。
    """
    m = SCORE_CONFIG["regime_map"]
    if not regime_label:
        return 0, "数据缺失"
    if "过热" in regime_label:
        return m["过热"], "过热"
    if "挤压酝酿" in regime_label:
        return m["挤压酝酿"], "挤压酝酿"
    if "真实" in regime_label and "建仓" in regime_label:
        return m["真实建仓"], "真实建仓"
    if "去杠杆" in regime_label:
        return m["去杠杆清洗"], "去杠杆清洗"
    if "健康趋势" in regime_label:
        return m["健康趋势"], "健康趋势"
    return m["混合信号"], "混合信号（含SCORE_CONFIG未覆盖的细分类别，按0分处理）"


def _label(score: int) -> str:
    th = SCORE_CONFIG["label_thresholds"]
    a = abs(score)
    if a <= th["neutral"]:
        return "中性/信号弱"
    if a <= th["strong"]:
        return "偏多" if score > 0 else "偏空"
    return "强烈偏多" if score > 0 else "强烈偏空"


# ── 主计算入口 ───────────────────────────────────────────────────────────

def compute_scores(fr_zscore, etf: dict, cb_premium, regime_label: str) -> dict:
    """
    完整六维计算（含三因子状态），composite/label 以此为准。
    regime_label 从调用方传入的 binance["market_meta"]["regime"] 获取。
    """
    quadrant, oi_chg_1h, ls_ratio_r = _query_market_snapshot()

    etf_s,    etf_note    = _score_etf(etf)
    fr_s,     fr_note     = _score_funding_z(fr_zscore)
    quad_s,   quad_note   = _score_oi_quadrant(quadrant, oi_chg_1h)
    ls_s,     ls_note     = _score_ls_ratio(ls_ratio_r)
    cb_s,     cb_note     = _score_cb_premium(cb_premium)
    regime_s, regime_note = _score_regime(regime_label)

    w = SCORE_CONFIG["weights"]
    composite = round(
        w["etf"] * etf_s + w["funding_z"] * fr_s + w["oi_quadrant"] * quad_s +
        w["ls_ratio"] * ls_s + w["cb_premium"] * cb_s + w["regime"] * regime_s
    )
    label = _label(composite)

    return {
        "composite": composite,
        "label": label,
        "etf_s": etf_s, "fr_s": fr_s, "quad_s": quad_s,
        "ls_s": ls_s, "cb_s": cb_s, "regime_s": regime_s,
        "detail": {
            "raw": {
                "quadrant": quadrant, "oi_chg_1h": oi_chg_1h, "ls_ratio_r": ls_ratio_r,
                "fr_zscore": fr_zscore, "cb_premium": cb_premium,
                "regime_label": regime_label,
                "etf_total_yest": etf.get("total_yest") if etf else None,
                "etf_total_week": etf.get("total_week") if etf else None,
            },
            "notes": {
                "etf": etf_note, "funding_z": fr_note, "oi_quadrant": quad_note,
                "ls_ratio": ls_note, "cb_premium": cb_note, "regime": regime_note,
            },
        },
    }


# ── DB 落库（signal_scores 表）───────────────────────────────────────────

def _ensure_table(conn):
    # 先建表，再建索引（历史教训见 api/main.py _atas_db_init() 的注释：
    # 索引依赖的字段如果表还没建好会直接报错中断）
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signal_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            session TEXT NOT NULL,
            composite INTEGER NOT NULL,
            label TEXT NOT NULL,
            etf_s INTEGER NOT NULL,
            fr_s INTEGER NOT NULL,
            quad_s INTEGER NOT NULL,
            ls_s INTEGER NOT NULL,
            cb_s INTEGER NOT NULL,
            regime_s INTEGER NOT NULL,
            detail_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_signal_scores_ts ON signal_scores(ts);
    """)


def get_previous_score() -> dict:
    """读取 signal_scores 表最新一条记录，供环比对比。必须在 save_score() 之前调用。"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        _ensure_table(conn)
        row = conn.execute(
            "SELECT * FROM signal_scores ORDER BY ts DESC, id DESC LIMIT 1"
        ).fetchone()
        conn.commit()
        conn.close()
        return dict(row) if row else {}
    except Exception as e:
        logger.warning(f"signal_scores 读取上一条记录失败: {e}")
        return {}


def save_score(session: str, result: dict) -> None:
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO signal_scores "
            "(ts, session, composite, label, etf_s, fr_s, quad_s, ls_s, cb_s, regime_s, detail_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(time.time()), session, result["composite"], result["label"],
                result["etf_s"], result["fr_s"], result["quad_s"],
                result["ls_s"], result["cb_s"], result["regime_s"],
                json.dumps(result["detail"], ensure_ascii=False),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"signal_scores 写入失败: {e}")


def compute_and_save(fr_zscore, etf: dict, cb_premium, regime_label: str, session: str) -> dict:
    """
    briefing.py 应该调用的唯一入口：
    读取上一条记录（环比用）→ 计算六维+综合分 → 写入 signal_scores → 一起返回。
    """
    prev = get_previous_score()
    result = compute_scores(fr_zscore, etf, cb_premium, regime_label)
    save_score(session, result)
    result["prev"] = prev
    return result


# ── 供 prompt 注入的权威数据块文本 ─────────────────────────────────────────

def format_authoritative_block(result: dict) -> str:
    """
    生成注入 Claude prompt 的权威数据块。六维分/综合分/标签均已由代码算好，
    AI 必须原样引用，不得自行计算或修改任何数值。
    """
    prev = result.get("prev") or {}
    lines = [
        "【综合信号分——以下由系统代码计算完成，是权威数值，禁止自行计算或修改，"
        "你的任务只是解释每一维得分背后的市场含义】",
        f"综合信号分：{result['composite']:+d}（{result['label']}）",
        f"六维明细：ETF{result['etf_s']:+d} 费率{result['fr_s']:+d} 象限{result['quad_s']:+d} "
        f"多空比{result['ls_s']:+d} 溢价{result['cb_s']:+d} 状态{result['regime_s']:+d}",
        "权重：ETF流向25% 资金费率Z-score15% OI象限20% 大户多空比15% CB溢价10% 三因子状态15%",
    ]
    if prev:
        lines.append(
            f"上一条记录（{prev.get('session','-')}）：综合信号分 "
            f"{prev.get('composite',0):+d}（{prev.get('label','-')}）"
        )
    else:
        lines.append("上一条记录：暂无历史记录（这是本表第一条数据）")
    return "\n".join(lines)
