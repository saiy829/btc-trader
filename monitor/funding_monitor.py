"""
Funding Rate 实时监控
每 5 分钟轮询 5 家交所
触发条件：
  单所 |rate| > 0.05%     -> 立即预警（30分钟冷却）
  3所同时极端             -> 强力预警（15分钟冷却）
  单所 |rate| > 0.10%     -> 紧急预警（10分钟冷却）
"""
import time
from datetime import datetime, timezone, timedelta
from utils.helpers import setup_logger, fmt_time, fmt_usd, now_sgt, SGT
from data_collector.multi_funding import collect_multi_funding
from alert_bot.send import send

logger = setup_logger("funding-monitor")

# ── 阈值配置 ────────────────────────────────────────────────────
THRESHOLD_ALERT    = 0.05   # 单所极端阈值 %
THRESHOLD_CRITICAL = 0.10   # 超级极端阈值 %
MULTI_EX_MIN       = 3      # 多所共识：至少几家同时极端
POLL_INTERVAL      = 300    # 轮询间隔：5分钟

COOLDOWN_SINGLE    = 1800   # 单所预警冷却：30分钟
COOLDOWN_MULTI     = 900    # 多所共识冷却：15分钟
COOLDOWN_CRITICAL  = 600    # 超级极端冷却：10分钟

# ── 状态追踪 ────────────────────────────────────────────────────
last_alert    = {}          # {"Binance_long": datetime, ...}
last_multi    = None        # 上次多所共识预警时间
prev_rates    = {}          # {"Binance": 0.0006, ...}


def _signal(rate: float) -> str:
    a = abs(rate)
    d = "偏多" if rate > 0 else "偏空"
    if a >= 0.10: return f"超级极端{d} !!!"
    if a >= 0.05: return f"极度{d}（超阈值）"
    if a >= 0.01: return d
    return "中性"


def _can_alert(key: str, cooldown: int) -> bool:
    t = last_alert.get(key)
    if not t:
        return True
    return (datetime.now(timezone.utc) - t).total_seconds() > cooldown


def _rate_table(exchanges: list) -> str:
    """格式化费率对比表"""
    lines = []
    for e in exchanges:
        r   = e["rate"]
        sig = _signal(r)
        mark = " <--" if abs(r) >= THRESHOLD_ALERT else ""
        lines.append(f"  {e['exchange']:<10} {r:+.4f}%  {sig}{mark}")
    return "\n".join(lines)


def send_single_alert(ex: dict, all_exchanges: list):
    """单所极端 Funding 预警"""
    exchange = ex["exchange"]
    rate     = ex["rate"]
    now      = now_sgt()
    key      = f"{exchange}_{'long' if rate > 0 else 'short'}"

    cooldown = COOLDOWN_CRITICAL if abs(rate) >= THRESHOLD_CRITICAL else COOLDOWN_SINGLE
    if not _can_alert(key, cooldown):
        return

    last_alert[key] = now

    if rate > 0:
        tag     = "[FUNDING]"
        risk    = "多头拥挤，持多仓成本高"
        advice  = ("> 不追多，Funding 越高多头越危险\n"
                   "> 若价格下跌 + CVD 看空 -> 挤多风险\n"
                   "> 等 Funding 回落至中性再评估")
    else:
        tag     = "[FUNDING]"
        risk    = "空头拥挤，持空仓成本高"
        advice  = ("> 不追空，Funding 越负空头越危险\n"
                   "> 若价格上涨 + CVD 看多 -> 挤空风险\n"
                   "> 等 Funding 回升至中性再评估")

    level = "紧急预警" if abs(rate) >= THRESHOLD_CRITICAL else "极端预警"
    prev  = prev_rates.get(exchange, 0)

    msg = (
        f"{tag} {exchange} Funding {level}\n"
        f"{'='*34}\n"
        f"当前费率：{rate:+.4f}%（{_signal(rate)}）\n"
        f"上次读取：{prev:+.4f}%\n"
        f"时  间：{fmt_time(now, short=True)}\n"
        f"{'-'*34}\n"
        f"各所费率对比：\n"
        f"{_rate_table(all_exchanges)}\n"
        f"{'-'*34}\n"
        f"风险：{risk}\n"
        f"{advice}\n"
        f"{'-'*34}\n"
        f"建议：打开 ATAS 检查 CVD 和 Footprint"
    )
    send(msg)
    logger.info(f"单所预警已发送 | {exchange} | {rate:+.4f}%")


def send_multi_alert(direction: str, extreme_list: list, all_exchanges: list):
    """多所同时极端 Funding 强力预警"""
    global last_multi
    now = now_sgt()

    if last_multi and (now - last_multi).total_seconds() < COOLDOWN_MULTI:
        return

    last_multi = now

    names = " / ".join(e["exchange"] for e in extreme_list)
    avg   = sum(e["rate"] for e in all_exchanges) / len(all_exchanges)

    if direction == "long":
        risk   = "市场普遍做多，反向套利压力极大"
        advice = ("> 多头仓位高度风险，强烈建议不追多\n"
                  "> 可能出现快速下行清算连锁（挤多瀑布）\n"
                  "> 若价格出现下跌信号，可考虑小仓试空")
    else:
        risk   = "市场普遍做空，轧空风险极大"
        advice = ("> 空头仓位高度风险，强烈建议不追空\n"
                  "> 可能出现快速上行清算连锁（轧空瀑布）\n"
                  "> 若价格出现上涨信号，可考虑小仓试多")

    msg = (
        f"[FUNDING ALERT] 多所同时{'极度偏多' if direction=='long' else '极度偏空'}！\n"
        f"{'='*34}\n"
        f"{len(extreme_list)}/{len(all_exchanges)} 家交所费率超过"
        f" {'+'if direction=='long' else '-'}{THRESHOLD_ALERT}%\n"
        f"触发交所：{names}\n"
        f"5所均值：{avg:+.4f}%\n"
        f"{'-'*34}\n"
        f"各所费率详情：\n"
        f"{_rate_table(all_exchanges)}\n"
        f"{'-'*34}\n"
        f"风险：{risk}\n"
        f"{advice}\n"
        f"{'-'*34}\n"
        f"时间：{fmt_time(now, short=True)}"
    )
    send(msg)
    logger.info(f"多所共识预警已发送 | {len(extreme_list)} 所极端 | 方向: {direction}")


def check_once():
    """执行一次 Funding Rate 检查"""
    logger.info("检查 Funding Rate...")
    data      = collect_multi_funding()
    exchanges = data.get("exchanges", [])

    if not exchanges:
        logger.warning("未获取到数据，跳过本次检查")
        return

    extreme_long  = []
    extreme_short = []

    for ex in exchanges:
        rate     = ex["rate"]
        exchange = ex["exchange"]

        # 单所极端预警
        if abs(rate) >= THRESHOLD_ALERT:
            send_single_alert(ex, exchanges)
            if rate > 0:
                extreme_long.append(ex)
            else:
                extreme_short.append(ex)

        # 更新历史费率
        prev_rates[exchange] = rate

    # 多所共识预警
    if len(extreme_long) >= MULTI_EX_MIN:
        send_multi_alert("long", extreme_long, exchanges)
    elif len(extreme_short) >= MULTI_EX_MIN:
        send_multi_alert("short", extreme_short, exchanges)

    # 打印当前状态到日志
    for ex in exchanges:
        logger.info(f"  {ex['exchange']:<10} {ex['rate']:+.4f}%  {_signal(ex['rate'])}")


def run():
    logger.info("=" * 50)
    logger.info("Funding Rate 实时监控启动")
    logger.info(f"单所预警阈值：±{THRESHOLD_ALERT}%")
    logger.info(f"超级极端阈值：±{THRESHOLD_CRITICAL}%")
    logger.info(f"多所共识触发：{MULTI_EX_MIN} 所同时极端")
    logger.info(f"轮询间隔：{POLL_INTERVAL} 秒（{POLL_INTERVAL//60} 分钟）")
    logger.info("=" * 50)

    send(
        "[OK] Funding Rate 实时监控已启动\n"
        f"监控：5 家交所（Binance/OKX/Bybit/Bitget/Gate.io）\n"
        f"阈值：单所超 ±{THRESHOLD_ALERT}% 立即预警\n"
        f"      {MULTI_EX_MIN}所同时极端 强力预警\n"
        f"      超 ±{THRESHOLD_CRITICAL}% 紧急预警\n"
        f"间隔：每 {POLL_INTERVAL//60} 分钟检查一次"
    )

    # 启动后立即执行一次
    check_once()

    while True:
        time.sleep(POLL_INTERVAL)
        check_once()


if __name__ == "__main__":
    run()
