"""
Claude AI 分析模块 v7
4种会话：morning / morning_monday / europe / evening / ondemand
v6 更新：
- max_tokens 按 session 分级（早盘13节内容多，提高到8192防止截断）
- IB 相关时间显示统一改为"北京时间/SGT"，不再出现UTC
- ETF 区块展示数据来源（Farside/SoSoValue）+ 交叉验证状态 + 今日首发标记
v7 更新：
- ETF 区块新增"当日更新窗口"状态展示（对应 data_collector/etf_data.py v4 新增字段
  is_settling / completeness_note），北京时间 04:00-12:00 期间数据标记为阶段性数值，
  避免早盘简报把未到齐的当日数据当完整信号使用
- 早盘/欧盘/美盘三节 ETF 相关 prompt 文案同步加入"更新中 vs 已稳定"判断要求
"""
import anthropic
from utils.helpers import setup_logger, get_env

logger = setup_logger()

# 每个 session 的输出 token 上限（早盘13节内容长，需要更大空间防止截断）
MAX_TOKENS = {
    "morning":        8192,
    "morning_monday": 8192,
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

=== BTC 现货 ETF 资金流向 ===
{_etf_block(etf)}

=== CME 期货缺口 ===
{_cme_block(cme)}
"""

    # ── 早盘简报（标准版，周二至周五）──────────────────────────
    if session in ("morning", "morning_monday"):
        IB_DATA = f"""
=== 今日 Initial Balance（Binance USDT永续）===
{_ib_block(ib)}

=== 流动性分布（Stop Hunt 分析）===
{_liq_block(p, ib, y)}
"""
        MON_EXTRA = ""
        if session == "morning_monday":
            MON_EXTRA = f"""
=== 【周一 结构回顾 & CME 历史缺口追踪】===
{_cme_block(cme)}

周一专项提示（CME 24/7 后更新）：
> ⚠️ 2026-05-29 起 CME 切换 7×24 交易，周一不再有「CME 开盘跳空」效应
> 周一关注点：周末现货走势的延续/修复，而非 CME 开盘博弈
> 若上周末出现大幅波动，周一 IB 可能偏宽 → 趋势日概率提升
> 策略：以 IB 宽度和 PDH/PDL/PDC 结构为核心，忽略 CME 开盘时间节点
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
输出要求：纯文本，用 > - = 符号，禁止使用 ** # 等 Markdown 标记；{TIME_RULE}

1.【宏观背景评级】
   综合 ETF流量+Funding+OI+CB溢价，给出 A/B/C/D 评级
   A=信号一致积极操作 B=有偏向正常操作 C=方向不明轻仓 D=建议观望
   一句话说明评级理由

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
   30分钟观察期的价格行为解读

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

11.【今日完整交易计划】
    做多设置：触发条件 | 入场区间 $XX-$XX | 止损 $XX | 目标1 $XX | 目标2 $XX | 确认信号
    做空设置：触发条件 | 入场区间 $XX-$XX | 止损 $XX | 目标1 $XX | 目标2 $XX | 确认信号
    今日观望条件（列出3-4个不操作的情况）

12.【ATAS 订单流确认重点】
    今日在软件里重点监控的信号（结合关键价位）

13.【一句话总结】20字以内"""

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
        result = msg.content[0].text
        stop_reason = msg.stop_reason
        logger.info(f"简报生成完成（{len(result)} 字）[{session}] stop_reason={stop_reason}")
        if stop_reason == "max_tokens":
            logger.warning(f"⚠️ 简报可能被截断！session={session} 已达 max_tokens={max_tok} 上限")
        return result
    except Exception as e:
        logger.error(f"Claude API 失败: {e}")
        return f"AI 分析生成失败：{e}"
