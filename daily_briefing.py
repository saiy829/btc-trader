"""
BTC AI 每日简报系统 v8
会话：morning / weekly / noon / europe / evening / ondemand
（noon为7M新增；weekly为7P新增，周一09:30自动路由，取代已退役的morning_monday）
数据：IB(60min+30min观察) + VP(POC/VAH/VAL) + ETF + CME缺口 + 现货 + CB溢价

v8 新增：
- TG Header 增加资金费率 Z-score（基于24H历史分位的极端度判断）
- TG Header 增加三因子市场状态（OI动向×费率极端度×多空拥挤度）
- 同步更新 binance_briefing_data.get_market_meta() 调用
"""
from datetime import datetime, timezone
from utils.helpers import setup_logger, now_sgt
from data_collector.binance_data   import collect_all as binance_collect, get_todays_ib
from data_collector.binance_data   import get_spot_and_extras, get_yesterday_volume_profile
from data_collector.multi_funding  import collect_multi_funding
from data_collector.etf_data       import fetch_etf_flows
from data_collector.cme_data       import get_cme_gap
from ai_analyst.briefing           import generate_briefing
from alert_bot.send                import send
from publisher.wordpress           import publish_briefing

logger = setup_logger("daily-briefing")

SESSION_NAMES = {
    "morning":         "早盘简报·当日交易计划",
    "weekly":          "周报·上周复盘与下周展望",       # 7P新增，周一 SGT 09:30（取代morning_monday）
    "noon":            "正午简报·ETF确认与亚盘复盘",   # 7M新增，周二至周六 SGT 12:00
    "europe":          "欧盘简报·策略更新",
    "evening":         "美盘简报·NY Kill Zone方案",
    "ondemand":        "实时快速简报",
}


def _is_monday() -> bool:
    return now_sgt().weekday() == 0


def build_header(binance, mf, extras, session) -> str:
    p       = binance.get("price", {})
    price   = p.get("price", 0)
    chg     = p.get("change_pct", 0)
    fr      = binance.get("funding", {}).get("rate", 0)
    oi_c    = binance.get("oi", {}).get("change_24h_pct", 0)
    avg_fr  = mf.get("avg_rate", 0)
    spot    = extras.get("spot_price", 0)
    spot_chg= extras.get("spot_chg_pct", 0)
    vol_str = extras.get("spot_vol_str", "-")
    cb_prem = extras.get("cb_premium", 0)
    cb_sig  = extras.get("cb_signal", "-")
    basis   = price - spot if spot and price else 0
    arrow   = "UP" if chg > 0 else "DOWN" if chg < 0 else "-"
    sgt     = now_sgt().strftime("%Y-%m-%d %H:%M SGT")
    name    = SESSION_NAMES.get(session, "简报")

    # ── [v8] 从 market_meta 取 Z-score 和市场状态 ─────────────────────────
    meta        = binance.get("market_meta", {})
    z           = meta.get("fr_zscore")
    z_label     = meta.get("fr_zscore_label", "")
    regime      = meta.get("regime", "")
    regime_act  = meta.get("regime_action", "")

    # 费率 Z-score 显示（无数据则省略）
    if z is not None:
        fr_z_str = f"{z:+.2f}（{z_label}）"
    else:
        fr_z_str = "计算中…"

    # 市场状态显示（无数据则省略整行）
    regime_line = f"市场状态 ：{regime}\n" if regime else ""
    # ───────────────────────────────────────────────────────────────────────

    return (
        f"{'='*36}\n"
        f"BTC {name}\n"
        f"{sgt}\n"
        f"{'-'*36}\n"
        f"永续合约 ：${price:,.0f}  {arrow} {chg:+.2f}%\n"
        f"现货价格 ：${spot:,.0f}（{spot_chg:+.2f}%）  基差：{basis:+.0f}\n"
        f"资金费率 ：{fr:+.4f}%  均值：{avg_fr:+.4f}%\n"
        f"费率Z分  ：{fr_z_str}\n"
        f"24H 成交额：{vol_str}\n"
        f"CB 溢价  ：{cb_prem:+.0f} USD（{cb_sig}）\n"
        f"OI 24H   ：{oi_c:+.2f}%\n"
        f"{regime_line}"
        f"{'='*36}\n\n"
    )


def run(session: str = "ondemand"):
    # 7P：周一 09:30 的 morning 自动路由为 weekly 周报（morning_monday 已退役）
    if session == "morning" and _is_monday():
        session = "weekly"
        logger.info("检测到周一，morning 路由为 weekly 周报会话（7P）")
    logger.info("=" * 50)
    logger.info(f"BTC AI 简报 v8 | {SESSION_NAMES.get(session, session)}")
    logger.info("=" * 50)
    try:
        logger.info("[1/7] 采集 Binance 永续 + 现货数据...")
        binance = binance_collect()
        extras  = get_spot_and_extras()
        if not binance.get("price"):
            raise RuntimeError("Binance 价格数据为空")

        logger.info("[2/7] 采集多交所 Funding Rate...")
        mf = collect_multi_funding()

        logger.info("[3/7] 采集 ETF 资金流量...")
        etf = fetch_etf_flows()

        logger.info("[4/7] 追踪 CME 历史遗留缺口（24/7 后不再新增）...")
        cme = get_cme_gap()

        logger.info("[5/7] 计算今日 IB（60分钟 + 30分钟观察期）...")
        ib = get_todays_ib() or {}
        if ib:
            logger.info(f"      IB: H={ib.get('ib_high')}  L={ib.get('ib_low')}  类型={ib.get('opening_type','?')}")
        else:
            logger.warning("      IB 数据返回空")

        logger.info("      计算昨日 Volume Profile（POC/VAH/VAL）...")
        vp = get_yesterday_volume_profile() or {}
        if vp:
            logger.info(f"      VP: POC=${vp.get('poc')}  VA=${vp.get('val')}-${vp.get('vah')}")
        else:
            logger.warning("      VP 数据返回空")

        binance["spot"] = extras

        # ── [v8] Binance 市场结构数据（OI趋势/费率/多空比/象限 + Z-score + 状态）──
        # 数据来源：btc_history.db 的 binance_* 表，由 btc-binance-data 服务后台采集
        # try/except 保证：即使本模块失败，简报主流程完全不受影响
        try:
            from briefing.binance_briefing_data import get_binance_context, get_market_meta
            binance["market_ctx"]  = get_binance_context()
            binance["market_meta"] = get_market_meta()          # ← v8 新增
            if binance["market_ctx"]:
                meta = binance["market_meta"]
                logger.info(
                    f"      市场结构数据已载入 | Z={meta.get('fr_zscore','N/A')} "
                    f"| 状态={meta.get('regime','N/A')}"
                )
            else:
                logger.warning("      Binance 市场结构数据为空（btc-binance-data 可能未运行）")
        except Exception as _e:
            logger.warning(f"      Binance 市场结构数据跳过: {_e}")
            binance["market_ctx"]  = ""
            binance["market_meta"] = {}                          # ← v8 新增

        # ATAS 订单流数据（AtasBridge.dll 本地推送，STALE > 30min 自动跳过）
        # 7M：noon 显式传4小时窗口（08:00-12:00亚盘复盘口径）；其余 session
        # 维持无参调用（get_atas_context 默认值本就是 hours=4，行为不变）
        try:
            from briefing.atas_briefing_data import get_atas_context
            if session == "noon":
                binance["atas_ctx"] = get_atas_context(hours=4)
            else:
                binance["atas_ctx"] = get_atas_context()
            if binance["atas_ctx"]:
                logger.info("      ATAS 订单流数据已载入（Delta/CVD/POC/大单）")
            else:
                logger.warning("      ATAS 数据为空（AtasBridge 未运行或超时）")
        except Exception as _atas_e:
            logger.warning(f"      ATAS 数据跳过: {_atas_e}")
            binance["atas_ctx"] = ""

        # 7P：weekly 会话注入周数据聚合块（W1-W10）。独立 try/except：
        # 失败降级为 morning 等价数据继续生成（weekly_ctx 为空时 prompt 侧
        # 有兜底提示），日志记 ERROR
        if session == "weekly":
            try:
                from briefing.weekly_briefing_data import get_weekly_context
                binance["weekly_ctx"] = get_weekly_context()
                logger.info("      周报数据块已载入（W1-W10）")
            except Exception as _wk_e:
                logger.error(f"      周报数据块生成失败，降级为常规数据继续: {_wk_e}")
                binance["weekly_ctx"] = ""
        # ───────────────────────────────────────────────────────────────────

        logger.info(f"[6/7] Claude AI 分析中 [{session}]...")
        briefing = generate_briefing(binance, mf, ib, etf, cme, vp, session)

        logger.info("[7/7] 发送 Telegram + WordPress...")
        full_msg = build_header(binance, mf, extras, session) + briefing
        tg_ok = send(full_msg)
        if tg_ok:
            logger.info("OK - Telegram 发送成功")
        # 7M/7P：noon/weekly 传 session_title（WP标题带会话名），其余 session
        # 不传参，标题维持"BTC 交易简报·时间"与既往完全一致（Sea收窄裁定）
        if session == "noon":
            wp_link = publish_briefing(briefing, binance, extras, session_title="正午简报")
        elif session == "weekly":
            wp_link = publish_briefing(briefing, binance, extras, session_title="周报")
        else:
            wp_link = publish_briefing(briefing, binance, extras)
        if wp_link:
            logger.info(f"OK - WordPress: {wp_link}")
            send(f"[WP] {SESSION_NAMES.get(session,'')} 已发布\n{wp_link}")
    except Exception as e:
        logger.error(f"流程异常: {e}")
        send(
            f"BTC AI 简报失败\n"
            f"时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"错误：{e}"
        )


if __name__ == "__main__":
    run("ondemand")
