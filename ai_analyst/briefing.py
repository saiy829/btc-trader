"""
Claude AI 分析模块 v13
6种会话：morning / morning_monday / noon / europe / evening / ondemand
v6 更新：
- max_tokens 按 session 分级（早盘13节内容多，提高到8192防止截断）
- IB 相关时间显示统一改为"北京时间/SGT"，不再出现UTC
- ETF 区块展示数据来源（Farside/SoSoValue）+ 交叉验证状态 + 今日首发标记
v7 更新：
- ETF 区块新增"当日更新窗口"状态展示（对应 data_collector/etf_data.py v4 新增字段
  is_settling / completeness_note），北京时间 04:00-12:00 期间数据标记为阶段性数值，
  避免早盘简报把未到齐的当日数据当完整信号使用
- 早盘/欧盘/美盘三节 ETF 相关 prompt 文案同步加入"更新中 vs 已稳定"判断要求
v8 更新（Phase 7A）：
- 所有 session 第1节评级新增「综合信号分」（-100~+100）：
  早盘展开 ETF流向/资金费率Z-score/OI象限/大户多空比/CB溢价/三因子市场状态
  六维拆解及权重；欧盘/美盘/按需仅一行综合分+与上一时段对比
- 早盘第11节【今日完整交易计划】升级为保守/稳健/激进三档风险方案（多空各三档），
  保守档仅在 A/B 评级且信号分绝对值>30 时给出，激进档注明仓位减半，
  D 评级三档全部观望
v9 更新（Phase 7A-2）：
- 综合信号分改为 utils/signal_score.py 代码确定性计算（原因：2026-07-04
  早盘简报里 AI 自算的综合分与其自报的六维加权和对不上，大模型心算不可靠）。
  build_prompt() 调用 signal_score.compute_and_save() 算好六维分+综合分+标签，
  以权威数据块形式注入 prompt，AI 只负责引用解释，禁止自行计算或修改数值
- 环比对比改为读 signal_scores 表上一条记录（代替 AI 记忆上一次简报的数字）
- 早盘第11节三档标题【保守档】【稳健档】【激进档】明确禁止用 ### 等 Markdown
  标题语法（此前 AI 会给这三个标题套 Markdown 标题导致 WordPress 渲染异常）
v9 补充裁定（2026-07-04）：
- build_prompt() 额外算出大户多空比实时REST快照传给 signal_score.compute_and_save()，
  作为 DB 表数据 STALE 时的降级兜底（详见 utils/signal_score.py 文档字符串）
v10 更新（Phase 7A-3）：
- generate_briefing() 返回前新增 _sanitize() 后处理：prompt 里反复要求 AI 不用
  Markdown，但 AI 偶尔仍会漏用 ### 或 **，加一道代码兜底清洗，不依赖 AI 是否听话
v11 更新（Phase 7E）：
- morning_monday 的 MON_EXTRA 新增 TradFi 周初开盘窗口提示（全球外汇周初开盘+
  CME Globex股指期货开盘，北京时间夏令时05:00-07:00/冬令时06:00-08:00自动切换，
  _monday_open_window() 用 America/New_York 时区的 dst() 判断），提示该窗口
  常见 BSL/SSL 集中清扫，要求第4节IB分析结合该窗口点评清扫痕迹
v12 更新（Phase 7F）：
- 修复 7E 的点评幻觉：2026-07-06 早盘简报里 AI 被要求点评周一开盘窗口价格行为，
  但没人往 prompt 里塞窗口K线数据，AI 就编了一段"63,115→63,617温和上行"，
  实际当时是 62,610→约63,900 强拉2%。新增 _monday_window_stats() 用 Binance
  5m K线代码计算窗口开高低收/涨跌幅/振幅，PDH/PDL优先复用 binance["yesterday"]
  （与 DATA 数据块显示给 AI 的 PDH/PDL 同一份数据），清扫判定逻辑与
  monitor/structure_monitor.py 的 monday_sweep_loop 一致。结果作为权威数据块
  注入 MON_EXTRA，第4节指令改为"只解读、不得自行推算价格路径"；获取失败时
  明确指令跳过窗口点评，不得凭其他数据推测
v13 更新（2026-07-13 任务卡7M）：
- 新增 noon 正午简报会话（周二至周六 SGT 12:00，7节）：ETF确认数据+亚盘复盘，
  承接从早盘移出的完整ETF解读（12:00后美股披露窗口关闭，数据已确认完整）
- morning 拆为独立分支并去ETF节（13节→12节）：早盘处于披露窗口内，未确认
  ETF数据不再注入DATA块（换单行提示），综合信号分ETF维度改用稳定视图
  （data_collector/etf_data.py v5 stable_flow_m），数据块带"ETF维度数据日"标注
- morning_monday 独立分支，原13节prompt逐字节保留（含ETF节），7P整体替换时清退
- europe/evening/ondemand 三个分支未动
"""
import re
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo

import anthropic
import requests
from utils.helpers import setup_logger, get_env
from utils import signal_score

logger = setup_logger()

# 每个 session 的输出 token 上限（早盘13节内容长，需要更大空间防止截断）
MAX_TOKENS = {
    "morning":        8192,
    "morning_monday": 8192,
    "noon":           2500,   # 7M：正午简报（ETF确认+亚盘复盘，7节）
    "europe":         3000,
    "evening":        3000,
    "ondemand":       2000,
}


# ── 通用数据格式化 ───────────────────────────────────────────────

def _fr_signal(r):
    if r > 0.10:    return "超级极度偏多，挤多风险极高"
    elif r > 0.05:  return "极度偏多，做多风险高"
    elif r > 0.01:  return "偏多，谨慎追多"
    elif r >= -0.01: return "中性，信号最干净"
    elif r >= -0.05: return "偏空，谨慎追空"
    elif r >= -0.10: return "极度偏空，做空风险高"
    else:           return "超级极度偏空，挤空风险极高"


def _monday_open_window() -> tuple:
    """返回周一 TradFi 周初开盘窗口的北京时间区间（随美东夏令时自动切换）。
    FX 周初开盘=美东周日17:00，Globex 股指期货=18:00。
    夏令时 → 北京 05:00-07:00；冬令时 → 06:00-08:00。"""
    try:
        ny = datetime.now(ZoneInfo("America/New_York"))
        if ny.dst():
            return ("夏令时", "05:00", "07:00")
        return ("冬令时", "06:00", "08:00")
    except Exception:
        return ("夏令时5-7点/冬令时6-8点", "05:00", "08:00")


_FUTURE_BASE = "https://fapi.binance.com"
_SWEEP_MIN_PCT = float(get_env("SWEEP_BREACH_MIN_PCT", "0.03"))


def _fetch_prev_day_high_low(prev_day_utc):
    """
    按 UTC 自然日取前一日高低（PDH/PDL）。用 limit=3 取 k[-2]（而非 limit=2
    取 k[0]）：与 data_collector/binance_data.py 的 get_yesterday_ohlcv() /
    monitor/structure_monitor.py 的 _get_pdh_pdl() 用同一套已验证写法。
    仅在调用方未能传入已算好的 PDH/PDL 时才会被调用（见 _monday_window_stats）。
    """
    try:
        resp = requests.get(
            f"{_FUTURE_BASE}/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1d", "limit": 3},
            timeout=10,
        )
        resp.raise_for_status()
        k = resp.json()
        y = k[-2]
        return float(y[2]), float(y[3])
    except Exception as e:
        logger.warning(f"_fetch_prev_day_high_low 获取失败: {e}")
        return None, None


def _sweep_status_side(extreme_price, pd_price, close_price, is_upper: bool) -> str:
    """
    单侧（上方PDH/下方PDL）清扫判定，与 monitor/structure_monitor.py 的
    _check_sweep() 判定逻辑一致：突破深度>=阈值 且 窗口收盘已收回 -> 清扫；
    突破但未收回 -> 突破延续；未达突破深度阈值（含未触及）-> 无。
    """
    if pd_price is None:
        return "无（PDH/PDL不可用）"
    if is_upper:
        if extreme_price <= pd_price:
            return "无"
        breach_pct = (extreme_price - pd_price) / pd_price * 100
        if breach_pct < _SWEEP_MIN_PCT:
            return "无"
        if close_price < pd_price:
            return f"清扫（极值${extreme_price:,.0f}，深度{breach_pct:.3f}%）"
        return f"突破延续（极值${extreme_price:,.0f}，深度{breach_pct:.3f}%）"
    else:
        if extreme_price >= pd_price:
            return "无"
        breach_pct = (pd_price - extreme_price) / pd_price * 100
        if breach_pct < _SWEEP_MIN_PCT:
            return "无"
        if close_price > pd_price:
            return f"清扫（极值${extreme_price:,.0f}，深度{breach_pct:.3f}%）"
        return f"突破延续（极值${extreme_price:,.0f}，深度{breach_pct:.3f}%）"


def _monday_window_stats(day_utc=None, pdh=None, pdl=None):
    """
    周一 TradFi 周初开盘窗口（美东周日17:00 -> 北京时间08:00）的实测行情统计。
    代码算好后作为权威数值注入 prompt，AI 只解读不得自行推算价格路径
    （修复：2026-07-06 早盘简报未注入窗口K线，AI 拼接其他数字编造窗口走势）。

    day_utc: 窗口终点（北京08:00）所在的 UTC 日期，默认今天，可传入历史日期回测。
    pdh/pdl: 若调用方已有简报流程算好的 PDH/PDL（binance["yesterday"]的
    high/low），直接传入复用，避免重复请求且与 DATA 数据块里显示给 AI 的
    PDH/PDL 保持同一份数据；不传则退化为自行按 UTC 前一日高低计算。

    返回 dict；任何异常均返回 None，只 log 不抛异常，不影响简报主流程。
    """
    try:
        if day_utc is None:
            day_utc = datetime.now(timezone.utc).date()
        prev_day = day_utc - timedelta(days=1)

        # 用窗口起点当天美东时间判断夏/冬令时，与 _monday_open_window() 同款逻辑，
        # 但用窗口发生的实际日期而非"现在"，保证传入历史日期回测时判断正确
        ny_ref = datetime.combine(prev_day, dtime(17, 0), tzinfo=ZoneInfo("America/New_York"))
        start_hour_utc = 21 if ny_ref.dst() else 22

        window_start = datetime.combine(prev_day, dtime(start_hour_utc, 0), tzinfo=timezone.utc)
        window_end   = datetime.combine(day_utc,  dtime(0, 0),             tzinfo=timezone.utc)

        resp = requests.get(
            f"{_FUTURE_BASE}/fapi/v1/klines",
            params={
                "symbol": "BTCUSDT", "interval": "5m",
                "startTime": int(window_start.timestamp() * 1000),
                "endTime":   int(window_end.timestamp() * 1000),
            },
            timeout=10,
        )
        resp.raise_for_status()
        klines = resp.json()
        if not klines:
            return None

        opens  = [float(k[1]) for k in klines]
        highs  = [float(k[2]) for k in klines]
        lows   = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]

        open_px, close_px = opens[0], closes[-1]
        high_px, low_px   = max(highs), min(lows)
        chg_pct   = (close_px - open_px) / open_px * 100
        range_pct = (high_px - low_px) / open_px * 100

        if pdh is None or pdl is None:
            pdh, pdl = _fetch_prev_day_high_low(prev_day)

        pdh_status = _sweep_status_side(high_px, pdh, close_px, is_upper=True)
        pdl_status = _sweep_status_side(low_px,  pdl, close_px, is_upper=False)

        return {
            "open": open_px, "close": close_px, "high": high_px, "low": low_px,
            "chg_pct": chg_pct, "range_pct": range_pct,
            "pdh": pdh, "pdl": pdl,
            "pdh_status": pdh_status, "pdl_status": pdl_status,
        }
    except Exception as e:
        logger.warning(f"_monday_window_stats 获取失败: {e}")
        return None


def _oi_signal(oi_chg, price_chg):
    if oi_chg > 0 and price_chg > 0:   return "OI上+价上 真实多头建仓（最强看涨）"
    elif oi_chg > 0 and price_chg < 0: return "OI上+价下 真实空头建仓（最强看跌）"
    elif oi_chg < 0 and price_chg > 0: return "OI下+价上 空头平仓/挤空（谨慎追多）"
    else:                               return "OI下+价下 多头平仓/挤多（谨慎追空）"


def _mf_block(mf):
    if not mf or not mf.get("exchanges"):
        return "多交所数据暂不可用"
    lines = [f"  {e['exchange']:<10} {e['rate_str']}" for e in mf["exchanges"]]
    lines.append(f"  5所均值: {mf.get('avg_rate',0):+.4f}%  {_fr_signal(mf.get('avg_rate',0))}")
    return "\n".join(lines)


def _etf_block(etf):
    if not etf or not etf.get("has_data"):
        return "ETF 数据暂不可用"
    fresh = etf.get("freshness", "")
    src   = etf.get("source", "-")
    cv    = "（双源交叉验证一致）" if etf.get("cross_validated") else ""
    newly = "🆕 今日首次更新 -> " if etf.get("newly_published") else ""
    settle = etf.get("completeness_note", "")
    return (
        f"  数据来源：{src}{cv}\n"
        f"  {newly}最新净流量：{etf['yest_str']}（{etf['date']}{fresh}）\n"
        f"  {settle}\n"
        f"  主要贡献：\n{etf.get('top3_lines','  -')}\n"
        f"  本周累计：{etf['week_str']}  本月累计：{etf['month_str']}\n"
        f"  连续状态：已连续 {etf['streak_days']} 天{etf['streak_dir']}\n"
        f"  信号解读：{etf['signal']}"
    )


def _cme_block(cme):
    """
    v2：CME 24/7 后的历史缺口追踪格式。
    2026-05-29 起不再产生新缺口，本函数追踪 3 个历史遗留缺口。
    """
    if not cme:
        return "CME 数据未获取"

    # ── 兼容旧格式（过渡期保护）──────────────────────────────────────
    if "mode" not in cme:
        if not cme.get("has_gap"):
            return "CME 24/7 已上线（2026-05-29），本周无新缺口"
        return (
            f"  缺口区间：${cme.get('gap_bot',0):,.0f} - ${cme.get('gap_top',0):,.0f}"
            f"  状态：{'已填补' if cme.get('is_filled') else '未填补'}"
        )

    # ── 新版 legacy 追踪模式 ──────────────────────────────────────────
    if cme.get("all_filled"):
        return (
            "【CME 历史缺口追踪·已完成】\n"
            "  2026-05-29 前形成的 3 个历史遗留缺口已全部填补。\n"
            "  CME 已切换 24/7 交易，不再产生新周末缺口。\n"
            "  本分析维度正式退休，后续简报将移除此节。"
        )

    price   = cme.get("current_price", 0)
    gaps    = cme.get("gaps", [])
    unfilled = [g for g in gaps if not g["is_filled"]]
    filled   = [g for g in gaps if g["is_filled"]]
    closest  = cme.get("closest_gap")

    lines = [
        f"【CME 历史缺口追踪】2026-05-29 后不再新增缺口",
        f"  当前价：${price:,.0f}  未填：{len(unfilled)}/3  已填：{len(filled)}/3",
        "",
    ]
    for g in gaps:
        if g["is_filled"]:
            lines.append(f"  缺口{g['id']} {g['name']}（{g['formed']}）：✅ 已填补")
        else:
            lines.append(
                f"  缺口{g['id']} {g['name']}（{g['formed']}）：⬆ 待填补"
                f"  ${g['gap_bot']:,.0f}-${g['gap_top']:,.0f}"
                f"  距 +${g['dist_to_fill']:,.0f}（+{g['dist_pct']:.1f}%）"
            )
            lines.append(f"     > {g['note']}")

    if closest:
        lines.extend([
            "",
            f"  【最近待填缺口】缺口{closest['id']} {closest['name']}",
            f"     区间 ${closest['gap_bot']:,.0f}-${closest['gap_top']:,.0f}"
            f"（宽度 ${closest['size']:,.0f}）",
            f"     需涨 +{closest['dist_pct']:.1f}%（约 +${closest['dist_to_fill']:,.0f}）",
            "     历史遗留缺口具备一定磁力，但 24/7 后已非核心信号，",
            "     优先参考 VP/MP 结构，缺口仅作次级价格目标。",
        ])
    return "\n".join(lines)


def _sgt_time_fix(text: str) -> str:
    """把IB相关文案中残留的UTC时间统一替换为北京时间/SGT，避免时区混乱"""
    if not text:
        return text
    replacements = [
        ("UTC 00:00-01:00", "北京时间 08:00-09:00"),
        ("UTC 01:00-01:30", "北京时间 09:00-09:30"),
        ("UTC 00:00",       "北京时间 08:00"),
        ("UTC 01:00",       "北京时间 09:00"),
        ("UTC 01:30",       "北京时间 09:30"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def _ib_block(ib):
    if not ib:
        return "IB 数据暂不可用"
    ot_detail = _sgt_time_fix(ib.get("ot_detail", ""))
    position  = _sgt_time_fix(ib.get("position", ""))
    return (
        f"  IB 高  ：${ib['ib_high']:,.0f}\n"
        f"  IB 低  ：${ib['ib_low']:,.0f}\n"
        f"  IB 中点：${ib['ib_mid']:,.0f}\n"
        f"  IB 开盘：${ib['ib_open']:,.0f}（北京时间 08:00）\n"
        f"  IB 宽度：${ib['ib_range']:,.0f}（{ib['ib_pct']:.2f}%）\n"
        f"  IB 类型：{ib['ib_type']}\n"
        f"\n"
        f"  -- 30分钟观察期（北京时间 09:00-09:30）--\n"
        f"  当前价格：${ib['obs_close']:,.0f}（{position}）\n"
        f"  观察期高：${ib['obs_high']:,.0f}  低：${ib['obs_low']:,.0f}\n"
        f"  开盘类型：{ib['opening_type']}\n"
        f"  类型含义：{ot_detail}"
    )


def _vp_block(vp):
    if not vp or not vp.get("has_data"):
        return "Volume Profile 数据暂不可用（K线数据不足）"
    hvn_str = "、".join(f"${x:,.0f}" for x in vp.get("hvn", [])) or "-"
    lvn_str = "、".join(f"${x:,.0f}" for x in vp.get("lvn", [])) or "-"
    return (
        f"  数据日期：{vp['date']}（Binance 5分钟K线实算，非ATAS逐笔精确值）\n"
        f"  POC（成交量最大价位）：${vp['poc']:,.0f}\n"
        f"  Value Area：${vp['val']:,.0f} - ${vp['vah']:,.0f}（含70%成交量）\n"
        f"  HVN（高量节点，潜在支撑/阻力）：{hvn_str}\n"
        f"  LVN（低量节点，价格真空区）：{lvn_str}\n"
        f"  Profile 形态：{vp.get('profile_shape','-')}"
    )


def _liq_block(p, ib, y):
    """流动性分布推导（基于价格结构）"""
    price = p.get("price", 0)
    pdh   = y.get("high", 0)
    pdl   = y.get("low", 0)
    ib_h  = ib.get("ib_high", 0) if ib else 0
    ib_l  = ib.get("ib_low", 0) if ib else 0

    bsl = []
    ssl = []
    if pdh: bsl.append(f"  ${pdh:,.0f} -> PDH（昨日高点，空头止损密集处）")
    if ib_h and ib_h != pdh: bsl.append(f"  ${ib_h:,.0f} -> IB High（今日 Buy Side Liquidity）")
    if pdl: ssl.append(f"  ${pdl:,.0f} -> PDL（昨日低点，多头止损密集处）")
    if ib_l and ib_l != pdl: ssl.append(f"  ${ib_l:,.0f} -> IB Low（今日 Sell Side Liquidity）")

    return (
        f"  上方 BSL（空头止损 / 机构扫流动性目标）：\n"
        + ("\n".join(bsl) if bsl else "  暂无明显上方流动性池") + "\n\n"
        f"  下方 SSL（多头止损 / 机构扫流动性目标）：\n"
        + ("\n".join(ssl) if ssl else "  暂无明显下方流动性池") + "\n\n"
        f"  Stop Hunt 提示：\n"
        f"  > 机构常在 Kill Zone 内先扫一侧流动性再反向运行\n"
        f"  > 价格触及 BSL/SSL 后若出现快速反转 = 高概率 Stop Hunt\n"
        f"  > ATAS：在关键位观察 Absorption + CVD 背离确认"
    )


# ── Prompt 构建 ─────────────────────────────────────────────────

def build_prompt(binance, mf, ib, etf, cme, vp, session):
    p   = binance.get("price", {})
    y   = binance.get("yesterday", {})
    f   = binance.get("funding", {})
    oi  = binance.get("oi", {})
    ls  = binance.get("ls_ratio", {})
    ext = binance.get("spot", {})

    # ── [新增] Binance 5分钟粒度市场结构数据（OI趋势/费率/多空比/象限）
    market_ctx = binance.get("market_ctx", "")
    atas_ctx   = binance.get("atas_ctx", "")

    price     = p.get("price", 0)
    chg       = p.get("change_pct", 0)
    fr        = f.get("rate", 0)
    oi_chg    = oi.get("change_24h_pct", 0)
    cb_prem   = ext.get("cb_premium", 0)
    cb_sig    = ext.get("cb_signal", "-")
    spot_p    = ext.get("spot_price", 0)
    perp_vol  = ext.get("perp_vol_str", "-")
    spot_vol  = ext.get("spot_vol_str", "-")
    ts        = binance.get("timestamp", "")

    TIME_RULE = "时间统一使用北京时间/SGT表述，不要出现UTC"

    # ── [Phase 7A-2] 综合信号分：代码确定性计算，写入 signal_scores 表，
    # AI 只负责引用解释，不再自己心算（原因见模块顶部文档字符串）
    _market_meta = binance.get("market_meta", {}) or {}
    # 大户多空比的实时REST快照（与上面 DATA 数据块里"大户：xx%多"用的是同一个 ls），
    # 仅当 signal_score 里 DB 表数据 STALE(>15分钟) 时才会被启用做降级兜底
    _rest_ls_r = None
    if ls.get("top_long_pct") and ls.get("top_short_pct"):
        _rest_ls_r = ls["top_long_pct"] / ls["top_short_pct"]
    _sig = signal_score.compute_and_save(
        _market_meta.get("fr_zscore"), etf, cb_prem,
        _market_meta.get("regime", ""), session, rest_ls_ratio_r=_rest_ls_r
    )
    SIGNAL_BLOCK = signal_score.format_authoritative_block(_sig)

    # ── [7M] DATA块ETF段按session条件拼接：仅morning换成单行提示（早盘处于
    # 美股披露窗口内，未确认数据不再注入），morning_monday/europe/evening/
    # ondemand/noon 均注入完整ETF块（非morning路径的拼接结果与7M前逐字节一致）
    if session == "morning":
        ETF_SECTION = "=== ETF ===\n早盘不注入未确认ETF数据（美股结算窗口内），完整数据见12:00正午简报"
    else:
        ETF_SECTION = f"=== BTC 现货 ETF 资金流向 ===\n{_etf_block(etf)}"

    # ── [新增] 市场结构块（仅当数据存在时追加）
    market_ctx_block = f"""
=== Binance 市场结构（5分钟粒度实时数据，比上方OI/多空比更精细）===
{market_ctx}
""" if market_ctx else ""
    atas_ctx_block = f"""
=== ATAS 订单流（本地交易终端 AtasBridge 推送，tick 级精度，比 Binance API 更精确）===
{atas_ctx}
""" if atas_ctx else ""

    DATA = f"""
=== 价格数据 ===
永续合约：${price:,.0f}  24H：{chg:+.2f}%
现货价格：${spot_p:,.0f}
24H成交额：永续 {perp_vol} | 现货 {spot_vol}
24H高：${p.get("high_24h",0):,.0f}  低：${p.get("low_24h",0):,.0f}

=== 昨日结构（Market Profile，Binance永续合约，北京时间 昨日08:00-今日08:00）===
PDH：${y.get("high",0):,.0f}  PDL：${y.get("low",0):,.0f}  PDC：${y.get("close",0):,.0f}
PDO：${y.get("open",0):,.0f}  昨日振幅：${y.get("high",0)-y.get("low",0):,.0f}

=== 昨日 Volume Profile（成交量分布）===
{_vp_block(vp)}

=== 多交所 Funding Rate ===
{_mf_block(mf)}

=== OI 持仓量 ===
当前OI：{oi.get("current",0):,.0f} BTC  24H变化：{oi_chg:+.2f}%
OI信号：{_oi_signal(oi_chg, chg)}

=== 多空比 ===
大户：{ls.get("top_long_pct",50):.1f}%多 / {ls.get("top_short_pct",50):.1f}%空
全账户：{ls.get("global_long_pct",50):.1f}%多 / {ls.get("global_short_pct",50):.1f}%空
{market_ctx_block}
{atas_ctx_block}
=== Coinbase 溢价 ===
${ext.get("cb_price",0):,.0f}  溢价：{cb_prem:+.0f} USD  {cb_sig}
（正值=美国机构买入溢价；负值=美国机构抛售折价）

{ETF_SECTION}

=== CME 期货缺口 ===
{_cme_block(cme)}

{SIGNAL_BLOCK}
"""

    # ── 周一早盘简报（7M拆分：原morning/morning_monday共用分支，按Sea
    # 2026-07-13补充裁定拆开。本分支保留原13节prompt逐字节不变（含ETF节），
    # 临时重复属计划内，7P整体替换周一版时清退）──────────────────────
    if session == "morning_monday":
        IB_DATA = f"""
=== 今日 Initial Balance（Binance USDT永续）===
{_ib_block(ib)}

=== 流动性分布（Stop Hunt 分析）===
{_liq_block(p, ib, y)}
"""
        MON_EXTRA = ""
        MONDAY_IB_REVIEW = ""
        if session == "morning_monday":
            dst_label, w_start, w_end = _monday_open_window()
            # [Phase 7F] 窗口实测数据代码计算注入，AI 只解读不得推算
            # （修复：2026-07-06 早盘简报未注入窗口K线，AI 拼接其他区块数字编造窗口走势）
            _wstats = _monday_window_stats(pdh=y.get("high"), pdl=y.get("low"))
            if _wstats:
                WINDOW_STATS_BLOCK = f"""
【周一开盘窗口实测数据】（系统代码计算，权威数值，必须原样引用，禁止修改或推算任何数字）
> 窗口区间：北京时间 {w_start}-08:00
> 开盘 ${_wstats['open']:,.0f} → 收盘 ${_wstats['close']:,.0f}（涨跌 {_wstats['chg_pct']:+.2f}%），最高 ${_wstats['high']:,.0f} / 最低 ${_wstats['low']:,.0f}（振幅 {_wstats['range_pct']:.2f}%）
> PDH 清扫判定：{_wstats['pdh_status']}
> PDL 清扫判定：{_wstats['pdl_status']}"""
                MONDAY_IB_REVIEW = ("\n   基于上方【周一开盘窗口实测数据】解读今晨窗口行为与 IB 方向的关系，"
                                     "只解读、不得自行描述或推算价格路径")
            else:
                WINDOW_STATS_BLOCK = """
【周一开盘窗口实测数据】窗口实测数据获取失败"""
                MONDAY_IB_REVIEW = ("\n   周一开盘窗口实测数据获取失败，本节跳过窗口点评，"
                                     "禁止凭其他数据推测窗口走势")

            MON_EXTRA = f"""
=== 【周一 结构回顾 & CME 历史缺口追踪】===
{_cme_block(cme)}

周一专项提示（CME 24/7 后更新）：
> ⚠️ 2026-05-29 起 CME 切换 7×24 交易，周一不再有「CME 开盘跳空」效应
> 周一关注点：周末现货走势的延续/修复，而非 CME 开盘博弈
> 若上周末出现大幅波动，周一 IB 可能偏宽 → 趋势日概率提升
> 策略：以 IB 宽度和 PDH/PDL/PDC 结构为核心，忽略 CME 开盘时间节点

周一开盘窗口提示（TradFi 周初重启）：
> 北京时间 {w_start}-{w_end}（美东{dst_label}）= 全球外汇市场周初开盘 + CME Globex 股指期货开盘
> 该窗口周末薄流动性切换回正常深度，常见 BSL/SSL 集中清扫（插针/假突破）
> IB 形成前（08:00 前）出现的插针优先视为流动性清扫而非趋势，勿追第一波方向
{WINDOW_STATS_BLOCK}
"""

        return f"""你是专业 BTC 永续合约交易分析师（Binance BTCUSDT），
精通 AMT（拍卖市场理论）、Market Profile、Volume Profile、Order Flow。
交易工具：ATAS（订单流分析软件）。

当前时间：{ts}（SGT 09:30，IB已形成，30分钟观察期完成）
简报类型：早盘简报·当日完整交易计划

{DATA}
{IB_DATA}
{MON_EXTRA}

=== 请按以下13节结构输出中文简报（纯文本，不用Markdown）===
输出要求：纯文本，用 > - = 符号，禁止使用 ** # 等 Markdown 标记（含11节三档标题
在内，一律不得使用 ### 或任何标题级 Markdown 语法）；{TIME_RULE}

1.【宏观背景评级】
   综合 ETF流量+Funding+OI+CB溢价，给出 A/B/C/D 评级
   A=信号一致积极操作 B=有偏向正常操作 C=方向不明轻仓 D=建议观望
   一句话说明评级理由

   【综合信号分】上方数据块中的"综合信号分"和"六维明细"由系统代码计算完成，
   是权威数值，必须原样引用，禁止自行计算或修改任何数字。
   把六维明细（ETF/费率/象限/多空比/溢价/状态）逐项原样列出，并分别用一句话
   解释每一维得分背后的市场含义（你的任务只是解释含义，不是重新算分）

2.【BTC 现货 ETF 资金流向解读】
   说明数据来源（Farside/SoSoValue，是否双源交叉验证）、最新净流量数据及其日期
   （若数据滞后请如实说明，不要假设是"昨日"；若标注"今日首次更新"请明确指出这是新到数据）
   若数据块标注"更新中"（阶段性数值），必须明确说明当前净流量还在陆续披露、
   不代表当日最终结果，只能作为方向参考，完整数据会在欧盘/美盘简报中确认；
   若标注"已稳定"，可直接作为当日机构信号使用
   解读本周/本月累计趋势：是机构持续买入还是持续抛售
   结合价格走势，判断ETF资金流向与价格是否同步（背离=警惕信号）
   ETF流向对今日操作的具体影响（加分/减分/中性，一句话）

3.【CME 历史缺口追踪】
   2026-05-29 CME 已切换 24/7 交易，不再产生新周末缺口
   报告 3 个历史遗留缺口中尚未填补的数量及最近缺口位置
   判断最近未填缺口是否在近期走势中具备磁力效应（结合 VP 结构综合判断）
   若全部已填补，简短说明本节正式退休

4.【今日 IB 分析·开盘类型确认】
   IB 宽度含义（趋势日/平衡日判断）
   开盘类型的具体含义与策略含义
   30分钟观察期的价格行为解读{MONDAY_IB_REVIEW}

5.【昨日 Market Profile 结构】
   当前价格相对 PDH/PDL/PDC 的位置含义
   昨日结构对今日操作的影响

6.【昨日 Volume Profile 概览】
   POC（成交量最大价位）相对当前价格的位置含义（上方阻力/下方支撑）
   当前价格在 Value Area 内部还是外部，分别代表什么
   HVN 视为强支撑阻力位；LVN 视为价格真空区（速度区，不宜在此入场，易快速穿越）
   Profile 形态（P型/b型/正态）对今日方向的提示

7.【流动性分布·Stop Hunt 分析】
   上方 BSL 的具体价格和风险
   下方 SSL 的具体价格和风险
   近期是否有 Stop Hunt 痕迹

8.【衍生品深度解读】
   Funding 多交所分析（方向共识/分歧）
   资金费率 Z-score：报告当前Z值，判断费率是否极端
     Z > +2 = 多头严重拥挤，不宜追多
     Z < -2 = 空头严重拥挤，关注轧空机会
     -1 ~ +1 = 中性，信号干净
   OI 信号（真实建仓 vs 挤仓）+ 结合5分钟粒度OI趋势与市场象限（Q1/Q2/Q3/Q4）综合判断
   CB 溢价的机构行为判断
   大户多空比分析（结合5分钟粒度数据，是否存在极端拥挤信号）
   三因子市场状态：引用数据块中的分类标签（过热/挤压酝酿/去杠杆/真实建仓/健康趋势/混合信号）
     并说明该状态对今日操作的具体含义

9.【AMT 市场状态·今日框架】
   首先引用三因子市场状态分类（已在市场结构数据块中提供），一句话说明当前所处阶段
   平衡市 or 失衡市
   Initiative vs Responsive
   今日应使用均值回归还是趋势跟随
   综合评估风险等级（高/中/低）及建议最大仓位比例

10.【今日关键价格层】（6-8个，含来源说明，建议结合 POC/VAH/VAL 补充关键位）
    格式：$价格 -> 类型 -> 到达此处的预期反应

11.【今日完整交易计划】（三档风险方案，多空各三档；若第1节评级为 D，
    直接写"三档全部观望"并说明原因，不给出任何具体入场价位，跳过以下三档明细）
    三档标题必须各自单独写成一行纯文本【保守档】【稳健档】【激进档】，
    标题前后不得加 ### 或任何 Markdown 标题符号，也不要加粗，
    与正文其他行的排版方式完全一致：

    【保守档】
    仅当第1节评级为 A 或 B，且综合信号分绝对值>30 时给出；
    不满足此前提时写"条件不足，本时段不提供保守档方案"，跳过下面两行：
      做多：触发条件 | 入场区间 $XX-$XX | 止损 $XX | 目标1 $XX | 目标2 $XX | 确认信号
      做空：触发条件 | 入场区间 $XX-$XX | 止损 $XX | 目标1 $XX | 目标2 $XX | 确认信号

    【稳健档】
    标准仓位，正常触发确认：
      做多：触发条件 | 入场区间 $XX-$XX | 止损 $XX | 目标1 $XX | 目标2 $XX | 确认信号
      做空：触发条件 | 入场区间 $XX-$XX | 止损 $XX | 目标1 $XX | 目标2 $XX | 确认信号

    【激进档】
    Kill Zone 扫流动性反手机会，仓位减半：
      做多：触发条件（如 SSL 扫除后反转）| 入场区间 $XX-$XX | 止损 $XX | 目标1 $XX | 确认信号 |【仓位减半】
      做空：触发条件（如 BSL 扫除后反转）| 入场区间 $XX-$XX | 止损 $XX | 目标1 $XX | 确认信号 |【仓位减半】

    今日观望条件（列出3-4个不操作的情况，适用于稳健/激进档）

12.【ATAS 订单流确认重点】
    今日在软件里重点监控的信号（结合关键价位）

13.【一句话总结】20字以内"""

    # ── 早盘简报（7M新版：12节，去ETF节。ETF未确认数据不再进入早盘，
    # 完整ETF分析移至12:00正午简报；综合信号分的ETF维度用稳定视图
    # （最近已确认完整交易日）计算，数据块中有"ETF维度数据日"标注──
    elif session == "morning":
        IB_DATA = f"""
=== 今日 Initial Balance（Binance USDT永续）===
{_ib_block(ib)}

=== 流动性分布（Stop Hunt 分析）===
{_liq_block(p, ib, y)}
"""
        return f"""你是专业 BTC 永续合约交易分析师（Binance BTCUSDT），
精通 AMT（拍卖市场理论）、Market Profile、Volume Profile、Order Flow。
交易工具：ATAS（订单流分析软件）。

当前时间：{ts}（SGT 09:30，IB已形成，30分钟观察期完成）
简报类型：早盘简报·当日完整交易计划

{DATA}
{IB_DATA}

=== 请按以下12节结构输出中文简报（纯文本，不用Markdown）===
输出要求：纯文本，用 > - = 符号，禁止使用 ** # 等 Markdown 标记（含10节三档标题
在内，一律不得使用 ### 或任何标题级 Markdown 语法）；{TIME_RULE}

1.【宏观背景评级】
   综合 Funding+OI+CB溢价+综合信号分，给出 A/B/C/D 评级
   A=信号一致积极操作 B=有偏向正常操作 C=方向不明轻仓 D=建议观望
   一句话说明评级理由

   【综合信号分】上方数据块中的"综合信号分"和"六维明细"由系统代码计算完成，
   是权威数值，必须原样引用，禁止自行计算或修改任何数字。
   把六维明细（ETF/费率/象限/多空比/溢价/状态）逐项原样列出，并分别用一句话
   解释每一维得分背后的市场含义（你的任务只是解释含义，不是重新算分）。
   其中ETF一维用的是最近一个已确认完整交易日的数据（见数据块"ETF维度数据日"
   标注），解释该维时必须注明这个日期；早盘不展开ETF当日流向分析（数据尚在
   美股披露窗口内未确认），完整ETF解读在12:00正午简报

2.【CME 历史缺口追踪】
   2026-05-29 CME 已切换 24/7 交易，不再产生新周末缺口
   报告 3 个历史遗留缺口中尚未填补的数量及最近缺口位置
   判断最近未填缺口是否在近期走势中具备磁力效应（结合 VP 结构综合判断）
   若全部已填补，简短说明本节正式退休

3.【今日 IB 分析·开盘类型确认】
   IB 宽度含义（趋势日/平衡日判断）
   开盘类型的具体含义与策略含义
   30分钟观察期的价格行为解读

4.【昨日 Market Profile 结构】
   当前价格相对 PDH/PDL/PDC 的位置含义
   昨日结构对今日操作的影响

5.【昨日 Volume Profile 概览】
   POC（成交量最大价位）相对当前价格的位置含义（上方阻力/下方支撑）
   当前价格在 Value Area 内部还是外部，分别代表什么
   HVN 视为强支撑阻力位；LVN 视为价格真空区（速度区，不宜在此入场，易快速穿越）
   Profile 形态（P型/b型/正态）对今日方向的提示

6.【流动性分布·Stop Hunt 分析】
   上方 BSL 的具体价格和风险
   下方 SSL 的具体价格和风险
   近期是否有 Stop Hunt 痕迹

7.【衍生品深度解读】
   Funding 多交所分析（方向共识/分歧）
   资金费率 Z-score：报告当前Z值，判断费率是否极端
     Z > +2 = 多头严重拥挤，不宜追多
     Z < -2 = 空头严重拥挤，关注轧空机会
     -1 ~ +1 = 中性，信号干净
   OI 信号（真实建仓 vs 挤仓）+ 结合5分钟粒度OI趋势与市场象限（Q1/Q2/Q3/Q4）综合判断
   CB 溢价的机构行为判断
   大户多空比分析（结合5分钟粒度数据，是否存在极端拥挤信号）
   三因子市场状态：引用数据块中的分类标签（过热/挤压酝酿/去杠杆/真实建仓/健康趋势/混合信号）
     并说明该状态对今日操作的具体含义

8.【AMT 市场状态·今日框架】
   首先引用三因子市场状态分类（已在市场结构数据块中提供），一句话说明当前所处阶段
   平衡市 or 失衡市
   Initiative vs Responsive
   今日应使用均值回归还是趋势跟随
   综合评估风险等级（高/中/低）及建议最大仓位比例

9.【今日关键价格层】（6-8个，含来源说明，建议结合 POC/VAH/VAL 补充关键位）
   格式：$价格 -> 类型 -> 到达此处的预期反应

10.【今日完整交易计划】（三档风险方案，多空各三档；若第1节评级为 D，
    直接写"三档全部观望"并说明原因，不给出任何具体入场价位，跳过以下三档明细）
    三档标题必须各自单独写成一行纯文本【保守档】【稳健档】【激进档】，
    标题前后不得加 ### 或任何 Markdown 标题符号，也不要加粗，
    与正文其他行的排版方式完全一致：

    【保守档】
    仅当第1节评级为 A 或 B，且综合信号分绝对值>30 时给出；
    不满足此前提时写"条件不足，本时段不提供保守档方案"，跳过下面两行：
      做多：触发条件 | 入场区间 $XX-$XX | 止损 $XX | 目标1 $XX | 目标2 $XX | 确认信号
      做空：触发条件 | 入场区间 $XX-$XX | 止损 $XX | 目标1 $XX | 目标2 $XX | 确认信号

    【稳健档】
    标准仓位，正常触发确认：
      做多：触发条件 | 入场区间 $XX-$XX | 止损 $XX | 目标1 $XX | 目标2 $XX | 确认信号
      做空：触发条件 | 入场区间 $XX-$XX | 止损 $XX | 目标1 $XX | 目标2 $XX | 确认信号

    【激进档】
    Kill Zone 扫流动性反手机会，仓位减半：
      做多：触发条件（如 SSL 扫除后反转）| 入场区间 $XX-$XX | 止损 $XX | 目标1 $XX | 确认信号 |【仓位减半】
      做空：触发条件（如 BSL 扫除后反转）| 入场区间 $XX-$XX | 止损 $XX | 目标1 $XX | 确认信号 |【仓位减半】

    今日观望条件（列出3-4个不操作的情况，适用于稳健/激进档）

11.【ATAS 订单流确认重点】
    今日在软件里重点监控的信号（结合关键价位）

12.【一句话总结】20字以内"""

    # ── 欧盘简报 SGT 15:00（UTC 07:00）─────────────────────────
    elif session == "europe":
        return f"""你是专业 BTC 永续合约交易分析师，精通 AMT、Market Profile、Volume Profile、Order Flow。
交易工具：ATAS。

当前时间：{ts}（SGT 15:00，伦敦开盘，London Kill Zone SGT 16:00-19:00 即将开始）
简报类型：欧盘简报·策略更新

{DATA}

亚盘今日数据（供复盘）：
IB 数据：${ib.get("ib_low",0):,.0f}-${ib.get("ib_high",0):,.0f} | 类型：{ib.get("opening_type","") if ib else "-"}

=== 请按以下6节结构输出中文简报（纯文本，800字以内）===
输出要求：纯文本，用 > - = 符号，禁止使用 ** # 等 Markdown 标记；{TIME_RULE}

1.【亚盘复盘·计划执行情况】
   亚盘价格区间和振幅
   IB 是否被突破（方向）
   早盘交易计划哪些触发/哪些未触发
   亚盘形成的新摆动高低点（更新 BSL/SSL）

   【综合信号分】上方数据块中"综合信号分"和"上一条记录"均由系统代码算好，
   直接原样引用这两行（综合信号分本身 + 与上一条记录/早盘的对比），
   禁止自行计算或修改数字，不展开六维明细

2.【欧盘开盘评估】
   欧盘开盘价格在什么结构中（可结合昨日VP的POC/VA位置）
   重点提示 London Kill Zone（SGT 16:00-19:00）特征：
   > 伦敦开盘常在亚盘极值处制造 Stop Hunt
   > 扫完流动性后出现反向信号才是真正入场时机
   当前最有可能被扫的流动性位置
   结合市场象限（Q1/Q2/Q3/Q4）判断趋势真实性

3.【衍生品实时更新】
   Funding 相比早盘的变化方向和含义
   资金费率 Z-score：当前值，是否进入极端区间（比早盘升温/降温）
   OI 变化（结合5分钟粒度象限数据一句话）
   CB 溢价变化趋势
   三因子市场状态：若相比早盘出现状态切换，重点说明（如从"真实建仓"变为"过热"）
   若 ETF 数据有更新（数据来源标注"今日首次更新"），在此一并说明；
   若早盘时数据标注"更新中"、现在已变为"已稳定"，需明确指出数据已确认，
   并对比早盘的阶段性数值是否有明显变化（可能影响早盘对ETF流向的判断）

4.【欧盘关键触发价位】（2-3个最重要的）
   $价格 -> 突破含义 / 跌破含义

5.【欧盘操作方案】
   是否维持早盘计划 or 需要更新
   做多触发：$XX 止损：$XX 目标：$XX
   做空触发：$XX 止损：$XX 目标：$XX
   London Kill Zone 操作建议

6.【一句话更新】10字以内"""

    # ── 美盘简报 SGT 20:30（UTC 12:30）─────────────────────────
    elif session == "evening":
        return f"""你是专业 BTC 永续合约交易分析师，精通 AMT、Market Profile、Volume Profile、Order Flow。
交易工具：ATAS。

当前时间：{ts}（SGT 20:30，纽约 Kill Zone SGT 21:30-23:00 前1小时）
简报类型：美盘简报·NY Kill Zone 最终操作方案

{DATA}

今日IB参考：${ib.get("ib_low",0):,.0f}-${ib.get("ib_high",0):,.0f} | {ib.get("opening_type","") if ib else "-"}

=== 请按以下6节结构输出中文简报（纯文本，1000字以内）===
输出要求：纯文本，用 > - = 符号，禁止使用 ** # 等 Markdown 标记；{TIME_RULE}

1.【全天行情回顾】
   今日价格区间和振幅（高点/低点）
   今日市场类型最终确认（与早盘预判对比）
   IB 被突破的方向（已突破）或仍在区间内
   今日交易计划哪些兑现/哪些失效（简短评价）

   【综合信号分】上方数据块中"综合信号分"和"上一条记录"均由系统代码算好，
   直接原样引用这两行（综合信号分本身 + 与上一条记录/欧盘的对比），
   禁止自行计算或修改数字，不展开六维明细

2.【ETF 数据与美盘影响】
   最新 ETF 净流量数据（含来源、日期，若标注"今日首次更新"请明确指出这是当天新公布的数据，
   早盘/欧盘时段尚未发布，现在首次纳入分析）对今晚尾盘的潜在影响
   此时点（SGT 20:30）数据通常已过北京时间12:00的披露窗口，应为"已稳定"状态，
   可作为当日最终机构信号使用；若数据块仍标注"更新中"（罕见情况），需如实说明并降低该信号权重
   机构资金方向是否与今日价格走势一致

3.【美盘前衍生品状态】
   Funding 全日变化趋势（较早盘变化方向）
   资金费率 Z-score：当前极端度，与早盘对比，说明一天内的情绪演变
   OI 最终状态（结合全日象限分布判断趋势性质）
   CB 溢价全天趋势（机构美盘前的态度）
   大户多空比当前状态（是否出现极端拥挤）
   三因子市场状态：美盘前的最终状态分类 + 操作导向建议

4.【流动性更新·NY Kill Zone Stop Hunt 预警】
   更新后的 BSL/SSL（基于今日形成的摆动高低点）
   纽约 Kill Zone 提示（SGT 21:30-23:00）：
   > 常见走法：先扫今日高点(BSL)后下跌 / 先扫今日低点(SSL)后上涨
   > 建议：21:25前不进场，等开盘初期方向确认
   今晚最有可能被扫的流动性位置：$价格（说明）

5.【美盘最终操作方案】（给出具体价格）
   做多设置：触发 $XX（SSL扫除反转确认）| 止损 $XX | 目标1 $XX | 目标2 $XX
   做空设置：触发 $XX（BSL扫除反转确认）| 止损 $XX | 目标1 $XX | 目标2 $XX
   今晚观望条件（2-3个）

6.【一句话总结】20字以内"""

    # ── 正午简报 SGT 12:00（UTC 04:00，周二至周六，7M新增）──────
    # 定位：承接当日已确认完整的美股ETF数据（12:00后披露窗口关闭）+
    # 亚盘08:00-12:00四小时复盘。ETF完整解读从早盘移到这里。
    elif session == "noon":
        return f"""你是专业 BTC 永续合约交易分析师（Binance BTCUSDT），
精通 AMT（拍卖市场理论）、Market Profile、Volume Profile、Order Flow。
交易工具：ATAS（订单流分析软件）。

当前时间：{ts}（SGT 12:00，亚盘已运行4小时，美股ETF数据已确认完整）
简报类型：正午简报·ETF确认与亚盘复盘

{DATA}
今日IB参考：${ib.get("ib_low",0):,.0f}-${ib.get("ib_high",0):,.0f} | 类型：{ib.get("opening_type","") if ib else "-"}

=== 请按以下7节结构输出中文简报（纯文本，1000字以内）===
输出要求：纯文本，用 > - = 符号，禁止使用 ** # 等 Markdown 标记；{TIME_RULE}

1.【ETF确认数据·机构动向】
   昨日美股ETF完整净流量：说明数据来源、对应交易日日期，并明确标注"已确认完整"
   （此时点已过北京时间12:00披露窗口，数据块应显示"已稳定"；若罕见地仍标注
   "更新中"，如实说明并降低该信号权重）
   主力品种贡献（IBIT/FBTC等哪几只主导）
   本周/本月累计与连续净流入/净流出状态
   ETF流向与近期价格走势的同步/背离判断（背离=警惕信号）
   对今日午后（欧美盘）操作的具体含义一句话

   【综合信号分】上方数据块中"综合信号分"和"上一条记录"均由系统代码算好，
   直接原样引用这两行（综合信号分本身 + 与上一条记录/早盘的对比），
   禁止自行计算或修改数字，不展开六维明细

2.【亚盘四小时复盘】
   08:00-12:00 价格区间与振幅
   IB 突破状态与方向（已突破上/下沿 or 仍在区间内）
   开盘类型演变（早盘判定的开盘类型是否兑现）
   早盘计划哪些已触发/哪些未触发

3.【市场结构午间快照】
   象限近4小时分布、资金费率 Z-score 当前值
   OI 近4小时变化、大户多空比
   三因子市场状态（若较早盘发生状态切换，重点说明切换含义）

4.【订单流午间摘要】
   基于上方 ATAS 订单流数据（4小时窗口）：Delta/CVD方向、POC位置、
   吸收区（支撑/阻力）、大单净向

5.【午后关键触发位】（2-3个，欧盘开盘前视角）
   $价格 -> 突破含义 / 跌破含义

6.【计划修正】
   维持或修正早盘方案（一句话结论+理由）
   做多触发：$XX 止损：$XX 目标：$XX
   做空触发：$XX 止损：$XX 目标：$XX

7.【一句话】15字以内"""

    # ── 随时触发（快速版）────────────────────────────────────────
    else:
        return f"""你是专业 BTC 永续合约交易分析师，精通 AMT、Market Profile、Order Flow。

当前时间：{ts}
简报类型：实时快速简报（用户手动触发）

{DATA}
IB参考：${ib.get("ib_low",0):,.0f}-${ib.get("ib_high",0):,.0f} | {ib.get("opening_type","") if ib else "-"}

=== 请按以下5节输出（纯文本，600字以内）===
输出要求：纯文本，用 > - = 符号，禁止使用 ** # 等 Markdown 标记；{TIME_RULE}

1.【当前市场评级】一句话+字母评级（A/B/C/D）
   【综合信号分】上方数据块中"综合信号分"和"上一条记录"均由系统代码算好，
   直接原样引用这两行，禁止自行计算或修改数字，不展开六维明细
2.【价格结构】当前价格相对 IB/PDH/PDL/PDC 的位置与含义
3.【衍生品快照】
   Funding（含Z-score极端度判断）+ OI + CB溢价各一行
   三因子市场状态分类标签 + 操作导向（数据块已提供，直接引用）
   当前象限（Q1/Q2/Q3/Q4）综合判断
4.【当前最优操作思路】
   做多条件：$XX（触发）止损 $XX 目标 $XX
   做空条件：$XX（触发）止损 $XX 目标 $XX
   观望条件：...
5.【一句话总结】15字以内"""


# ── Markdown 清洗器（Phase 7A-3，代码兜底）──────────────────────────────
# prompt 里已经反复要求 AI 不要用 Markdown，但 AI 偶尔还是会漏用 ### 或 **，
# 这里在返回前做最后一道代码清洗，不依赖 AI 是否听话。

def _sanitize(text: str) -> str:
    """
    清洗简报正文里残留的 Markdown 符号：
    1. 每行开头 1~6 个 # 及其后空格 → 删除，保留行内其余文字
    2. 成对的 ** 包裹 → 去掉星号保留文字；残留的孤立 ** 也删掉
    不动 > - = 等我们自己的排版符号，不动数字和 $ 金额。
    """
    if not text:
        return text
    lines = [re.sub(r'^#{1,6}\s*', '', line) for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = text.replace("**", "")
    return text


def generate_briefing(binance, mf=None, ib=None,
                      etf=None, cme=None, vp=None, session="ondemand"):
    try:
        client = anthropic.Anthropic(api_key=get_env("ANTHROPIC_API_KEY"))
        prompt = build_prompt(binance, mf or {}, ib or {},
                              etf or {}, cme or {}, vp or {}, session)
        max_tok = MAX_TOKENS.get(session, 3000)
        logger.info(f"Claude API 调用 | 会话: {session} | max_tokens: {max_tok}")
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=max_tok,
            messages=[{"role": "user", "content": prompt}]
        )
        result = _sanitize(msg.content[0].text)
        stop_reason = msg.stop_reason
        logger.info(f"简报生成完成（{len(result)} 字）[{session}] stop_reason={stop_reason}")
        if stop_reason == "max_tokens":
            logger.warning(f"⚠️ 简报可能被截断！session={session} 已达 max_tokens={max_tok} 上限")
        return result
    except Exception as e:
        logger.error(f"Claude API 失败: {e}")
        return f"AI 分析生成失败：{e}"
