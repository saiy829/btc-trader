"""
综合信号分 —— 代码确定性计算模块（Phase 7A-2）

背景：2026-07-04 早盘简报里 AI 自算的综合分（-12）与其自报的六维加权和（-4.8）
对不上——大模型心算不可靠。本模块把六维打分和加权求和全部改成确定性代码计算，
AI 只负责引用结果并解释市场含义，不再自己算数。

数据来源原则：一律优先用注入 AI 数据块的同一份数据，避免"分数和叙述打架"：
  - ETF净流 / CB溢价：由 ai_analyst/briefing.py 的 build_prompt() 直接传入
    （这两个值来自外部 API 采集，不在本地滚动历史表里，且与 DATA 数据块里
    展示给 AI 看的是同一个变量）
  - 资金费率Z-score：直接复用 briefing.binance_briefing_data.get_market_meta()
    的 fr_zscore，不在本模块重新计算，避免和 market_ctx 文本里显示的 Z 值
    产生数值漂移（两者同一次 get_binance_context() 调用产生）
  - OI象限 / 近1小时OI变化率 / 近1小时价格变化：本模块直接查询 btc_history.db
    的 binance_structure / binance_oi 两张表（与 briefing/binance_briefing_data.py
    用同一批表，口径一致）
  - 大户多空比：优先用 binance_ls_top.ls_ratio 字段本身（Binance按持仓量计算的
    比值，不是 61.7%/38.3% 账户占比换算出来的比值——实测两者差异很大，
    2026-07-04早盘实例：1.231 vs 61.7/38.3=1.611，账户占比换算会导致分类档位
    判断错误）。若该表最新记录超过 SCORE_CONFIG["ls_stale_sec"]（15分钟）未更新，
    视为 STALE，降级改用调用方传入的实时 REST 快照（build_prompt 里的
    binance["ls_ratio"]），并在 detail_json 标注 ls_source

结果写入 signal_scores 表，供 Phase 5B 回测分数与胜率的相关性。

三因子状态14档完整映射（2026-07-04 补充裁定，权威定义，数值改动需用户确认）：
  见 SCORE_CONFIG["regime_map"]。防御规则：未来出现表里没有的新标签，
  一律记0分 + 写 WARNING 日志 + detail_json 标注"未映射状态:标签名"，
  绝不猜测赋分（与 AtasBridge 的 Unset 默认值同一设计哲学：宁可报警不可编数）。
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
    # 三因子状态14档完整映射（2026-07-04 补充裁定，权威定义，禁止自行调整）
    "regime_map": {
        "真实建仓":            50,
        "健康趋势_多方主导":    40,
        "趋势延续_涨":          30,   # 近1小时价格上涨
        "趋势延续_平":           0,   # |涨跌|<0.05%
        "趋势延续_跌":         -30,   # 近1小时价格下跌
        "空头拥挤承托":         35,
        "横盘空头拥挤":         25,
        "空头被迫平仓":         15,
        "混合信号":              0,
        "挤压酝酿":            -10,
        "横盘多头拥挤":        -25,
        "多头拥挤承压":        -35,
        "过热":                -40,
        "去杠杆清洗":          -40,
        "健康趋势_空方主导":   -40,
        "多头被迫平仓":        -45,
    },
    # 趋势延续档判定"平"的价格变化阈值（近1小时百分比）
    "regime_trend_flat_pct": 0.05,
    # 大户多空比数据新鲜度阈值：DB表最新记录超过这个秒数视为STALE，
    # 降级改用 build_prompt 传入的实时REST快照（2026-07-04 补充裁定）
    "ls_stale_sec": 900,
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
    直接查 btc_history.db，取：
      - 当前象限
      - 近1小时OI变化率(%)
      - 近1小时价格变化率(%)（供"趋势延续"档判定方向用，来自 binance_structure.mark_px）
      - 大户持仓多空比 ls_ratio + 该记录距今秒数（供新鲜度判断用）
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

    px_1h = _q(
        "SELECT mark_px FROM binance_structure WHERE ts >= ? ORDER BY ts ASC",
        (now - 3600,)
    )
    price_chg_1h = None
    if len(px_1h) >= 2 and px_1h[0]["mark_px"]:
        price_chg_1h = (px_1h[-1]["mark_px"] - px_1h[0]["mark_px"]) / px_1h[0]["mark_px"] * 100

    ls_top = _q("SELECT ts, ls_ratio FROM binance_ls_top ORDER BY ts DESC LIMIT 1")
    ls_ratio_r = ls_top[0]["ls_ratio"] if ls_top else None
    ls_age_sec = (now - ls_top[0]["ts"]) if ls_top else None

    return quadrant, oi_chg_1h, price_chg_1h, ls_ratio_r, ls_age_sec


# ── 六维打分函数（每个返回 (分数:int, 备注:str|None)）─────────────────────

def _score_etf(etf: dict):
    # 7M ETF稳定视图：单日分量只用"最近一个已确认完整交易日"的净流量
    # （stable_flow_m，见 data_collector/etf_data.py v5），不再用可能处于
    # 披露窗口内的 total_yest 阶段值。stable_flow_m 缺失（首次部署 state
    # 尚无记录）→ 整个维度按"数据缺失"处理（简报侧记0分，引擎侧由
    # signal_engine 自己的门槛判断宁缺勿假跳过本轮）。
    # 已知副作用（7M裁定"正确性优先"）：该维度从连续更新变为每日12:00后
    # 阶跃一次，与7N校准时的分布存在轻微偏差，Phase 5B 回测时知悉即可；
    # 另外周累计分量 total_week 仍含披露窗口内的当日阶段值（本卡未改），
    # 残余影响最大约 40分×25%权重=10个综合分点，已向 Sea 报告留待裁定。
    if not etf or not etf.get("has_data"):
        return 0, "数据缺失"
    stable = etf.get("stable_flow_m")
    if stable is None:
        return 0, "数据缺失（稳定视图暂无记录，etf_source=missing）"
    cfg = SCORE_CONFIG["etf"]
    # stable_flow_m / total_week 单位是百万美元（见 data_collector/etf_data.py _fmt_cny），
    # 换算成"亿美元"要除以100
    yest_yi = stable / 100
    week_yi = (etf.get("total_week") or 0) / 100
    s1 = _clamp(yest_yi * cfg["yest_mult"], -cfg["yest_clamp"], cfg["yest_clamp"])
    s2 = _clamp(week_yi * cfg["week_mult"], -cfg["week_clamp"], cfg["week_clamp"])
    note = None
    if etf.get("stable_date") and etf.get("stable_date") != etf.get("date"):
        note = f"稳定视图：用{etf['stable_date']}已确认数据（当日值仍在披露窗口内）"
    return round(s1 + s2), note


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


def _score_regime(regime_label: str, price_chg_1h):
    """
    三因子状态14档完整查表打分（2026-07-04 补充裁定，权威定义）。
    关键词互不重叠，匹配顺序不影响结果（已核对：没有一个关键词是另一个的子串）。
    "趋势延续"档需要额外用近1小时价格变化的符号决定 +30/0/-30。
    命中不了任何已知类别的新标签 → 记0分 + WARNING日志 + detail标注，
    绝不猜测赋分。
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
    if "多头被迫平仓" in regime_label:
        return m["多头被迫平仓"], "多头被迫平仓"
    if "空头被迫平仓" in regime_label:
        return m["空头被迫平仓"], "空头被迫平仓"
    if "去杠杆" in regime_label:
        return m["去杠杆清洗"], "去杠杆清洗"
    if "横盘多头拥挤" in regime_label:
        return m["横盘多头拥挤"], "横盘多头拥挤"
    if "横盘空头拥挤" in regime_label:
        return m["横盘空头拥挤"], "横盘空头拥挤"
    if "多头拥挤承压" in regime_label:
        return m["多头拥挤承压"], "多头拥挤承压"
    if "空头拥挤承托" in regime_label:
        return m["空头拥挤承托"], "空头拥挤承托"
    if "健康趋势" in regime_label and "多方主导" in regime_label:
        return m["健康趋势_多方主导"], "健康趋势（多方主导）"
    if "健康趋势" in regime_label and "空方主导" in regime_label:
        return m["健康趋势_空方主导"], "健康趋势（空方主导）"
    if "趋势延续" in regime_label:
        flat_pct = SCORE_CONFIG["regime_trend_flat_pct"]
        if price_chg_1h is None:
            return m["趋势延续_平"], "趋势延续（均衡），近1小时价格变化数据缺失，按0分处理"
        if abs(price_chg_1h) < flat_pct:
            return m["趋势延续_平"], f"趋势延续（均衡），近1小时价格变化{price_chg_1h:+.3f}%<{flat_pct}%阈值"
        if price_chg_1h > 0:
            return m["趋势延续_涨"], f"趋势延续（均衡），近1小时价格上涨{price_chg_1h:+.3f}%"
        return m["趋势延续_跌"], f"趋势延续（均衡），近1小时价格下跌{price_chg_1h:+.3f}%"
    if "混合信号" in regime_label:
        return m["混合信号"], "混合信号"

    # ── 防御规则：未知新标签，绝不猜测赋分 ──────────────────────────────
    logger.warning(f"signal_score: 三因子状态出现未映射标签 {regime_label!r}，按0分处理")
    return 0, f"未映射状态:{regime_label}"


def _label(score: int) -> str:
    th = SCORE_CONFIG["label_thresholds"]
    a = abs(score)
    if a <= th["neutral"]:
        return "中性/信号弱"
    if a <= th["strong"]:
        return "偏多" if score > 0 else "偏空"
    return "强烈偏多" if score > 0 else "强烈偏空"


# ── 主计算入口 ───────────────────────────────────────────────────────────

def compute_scores(fr_zscore, etf: dict, cb_premium, regime_label: str,
                    rest_ls_ratio_r=None) -> dict:
    """
    完整六维计算（含三因子状态），composite/label 以此为准。
    regime_label 从调用方传入的 binance["market_meta"]["regime"] 获取。
    rest_ls_ratio_r：build_prompt 里实时REST快照算出的大户多空比（可选），
    仅当 DB 里的 binance_ls_top 数据 STALE（超过 ls_stale_sec）时才会启用，
    作为降级兜底，避免综合分和AI正文引用的数据块完全脱节。
    """
    quadrant, oi_chg_1h, price_chg_1h, ls_ratio_r, ls_age_sec = _query_market_snapshot()

    stale_sec = SCORE_CONFIG["ls_stale_sec"]
    ls_source = "db_5min"
    if ls_ratio_r is None:
        ls_source = "unavailable"
        if rest_ls_ratio_r is not None:
            ls_ratio_r = rest_ls_ratio_r
            ls_source = "rest_fallback（DB无数据）"
    elif ls_age_sec is not None and ls_age_sec > stale_sec:
        if rest_ls_ratio_r is not None:
            ls_ratio_r = rest_ls_ratio_r
            ls_source = f"rest_fallback（DB数据STALE，{ls_age_sec}秒未更新）"
        else:
            ls_source = f"db_5min（STALE，{ls_age_sec}秒未更新，且无REST兜底数据）"

    etf_s,    etf_note    = _score_etf(etf)
    fr_s,     fr_note     = _score_funding_z(fr_zscore)
    quad_s,   quad_note   = _score_oi_quadrant(quadrant, oi_chg_1h)
    ls_s,     ls_note     = _score_ls_ratio(ls_ratio_r)
    cb_s,     cb_note     = _score_cb_premium(cb_premium)
    regime_s, regime_note = _score_regime(regime_label, price_chg_1h)

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
                "quadrant": quadrant, "oi_chg_1h": oi_chg_1h, "price_chg_1h": price_chg_1h,
                "ls_ratio_r": ls_ratio_r, "ls_source": ls_source, "ls_age_sec": ls_age_sec,
                "fr_zscore": fr_zscore, "cb_premium": cb_premium,
                "regime_label": regime_label,
                "etf_total_yest": etf.get("total_yest") if etf else None,
                "etf_total_week": etf.get("total_week") if etf else None,
                # 7M稳定视图溯源：etf_source=stable(用已确认数据)/missing(暂无记录)
                "etf_source": ("stable" if (etf and etf.get("has_data")
                                            and etf.get("stable_flow_m") is not None)
                               else "missing"),
                "etf_stable_flow_m": etf.get("stable_flow_m") if etf else None,
                "etf_stable_date": etf.get("stable_date") if etf else None,
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


def compute_and_save(fr_zscore, etf: dict, cb_premium, regime_label: str, session: str,
                      rest_ls_ratio_r=None) -> dict:
    """
    briefing.py 应该调用的唯一入口：
    读取上一条记录（环比用）→ 计算六维+综合分 → 写入 signal_scores → 一起返回。
    """
    prev = get_previous_score()
    result = compute_scores(fr_zscore, etf, cb_premium, regime_label, rest_ls_ratio_r)
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
    # 7M稳定视图：标注ETF维度用的是哪个已确认交易日的数据（供第1节六维解释引用）
    _raw = (result.get("detail") or {}).get("raw", {})
    if _raw.get("etf_source") == "stable" and _raw.get("etf_stable_date"):
        lines.append(f"ETF维度数据日：{_raw['etf_stable_date']}（已确认完整交易日，稳定视图）")
    elif _raw.get("etf_source") == "missing":
        lines.append("ETF维度：已确认数据暂缺（etf_source=missing，该维记0分）")
    if prev:
        lines.append(
            f"上一条记录（{prev.get('session','-')}）：综合信号分 "
            f"{prev.get('composite',0):+d}（{prev.get('label','-')}）"
        )
    else:
        lines.append("上一条记录：暂无历史记录（这是本表第一条数据）")
    return "\n".join(lines)
