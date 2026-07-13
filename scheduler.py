"""
BTC AI 调度系统 v3（完整版）
四个定时任务：早盘/正午(周二至周六,7M新增)/欧盘/美盘 + /b 命令随时触发
"""
import asyncio
from datetime import time as dt_time, timezone
from telegram import Update
from telegram.ext import (Application, CommandHandler,
                          MessageHandler, filters, ContextTypes)
from utils.helpers import setup_logger, get_env, now_sgt
from daily_briefing import run as run_briefing
from utils import position_calc

logger  = setup_logger("scheduler")
CHAT_ID = get_env("TELEGRAM_CHAT_ID")
TOKEN   = get_env("TELEGRAM_BOT_TOKEN")
UTC     = timezone.utc


# ── 定时任务回调 ─────────────────────────────────────────────────

async def job_morning(context):
    """UTC 01:30（SGT 09:30）早盘简报"""
    logger.info("定时触发：早盘简报 SGT 09:30")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_briefing, "morning")


async def job_noon(context):
    """UTC 04:00（SGT 12:00）正午简报，周二至周六（7M新增）"""
    # 首行记录当前北京时间+星期名，供首次自动触发时人工核对
    # run_daily 的 days 编号约定是否正确（PTB v22.8：0=周日…6=周六）
    now_bj = now_sgt()
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now_bj.weekday()]
    logger.info(f"定时触发：正午简报 | 当前北京时间 {now_bj.strftime('%Y-%m-%d %H:%M')} {weekday_cn}")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_briefing, "noon")


async def job_europe(context):
    """UTC 07:00（SGT 15:00）欧盘简报"""
    logger.info("定时触发：欧盘简报 SGT 15:00")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_briefing, "europe")


async def job_evening(context):
    """UTC 12:30（SGT 20:30）美盘简报"""
    logger.info("定时触发：美盘简报 SGT 20:30")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_briefing, "evening")


# ── Telegram 命令处理 ─────────────────────────────────────────────

async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/b 命令 → 立刻生成实时简报"""
    if str(update.effective_chat.id) != str(CHAT_ID):
        return
    logger.info(f"手动触发简报: {update.message.text}")
    await update.message.reply_text("收到！正在生成实时简报，约1分钟后发送...")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_briefing, "ondemand")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status 命令 → 查看系统状态"""
    if str(update.effective_chat.id) != str(CHAT_ID):
        return
    sgt = now_sgt().strftime("%Y-%m-%d %H:%M SGT")
    await update.message.reply_text(
        f"BTC AI 系统运行中\n"
        f"时间：{sgt}\n"
        f"─────────────────\n"
        f"早盘简报：周二至周日 SGT 09:30\n"
        f"周报　　：周一 SGT 09:30\n"
        f"正午简报：周二至周六 SGT 12:00\n"
        f"欧盘简报：每天 SGT 15:00\n"
        f"美盘简报：每天 SGT 20:30\n"
        f"─────────────────\n"
        f"发送 /b 立刻生成简报\n"
        f"发送 /pos <入场价> <止损价> [资金USDT] [风险%] 计算仓位风控方案"
    )


async def cmd_pos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pos 命令 → 仓位风控计算（固定风险比例，纯本地计算，无网络请求）"""
    if str(update.effective_chat.id) != str(CHAT_ID):
        return
    try:
        default_account = float(get_env("POS_ACCOUNT_USDT", "10000"))
        default_risk = float(get_env("POS_RISK_PCT", "1.0"))
        result = position_calc.parse_and_calc(
            context.args, default_account, default_risk
        )
        reply = position_calc.format_message(result)
    except position_calc.PositionCalcError as e:
        reply = f"{position_calc.USAGE_TEXT}\n\n错误：{e}"
    except Exception as e:
        logger.error(f"/pos 计算异常: {e}")
        reply = f"{position_calc.USAGE_TEXT}\n\n错误：计算失败，请检查参数"
    await update.message.reply_text(reply)


# ── 启动通知 ─────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    try:
        await application.bot.send_message(
            chat_id=CHAT_ID,
            text=(
                "[OK] BTC AI 调度系统 v3 已启动\n"
                "早盘简报：周二至周日 SGT 09:30\n"
                "周报：周一 SGT 09:30（上周复盘+下周展望，7P）\n"
                "正午简报：周二至周六 SGT 12:00（ETF确认+亚盘复盘）\n"
                "欧盘简报：每天 SGT 15:00（伦敦开盘）\n"
                "美盘简报：每天 SGT 20:30（NY Kill Zone前）\n"
                "发送 /b 可随时触发实时简报"
            )
        )
        logger.info("启动通知已发送")
    except Exception as e:
        logger.warning(f"启动通知失败: {e}")


# ── 主函数 ───────────────────────────────────────────────────────

def main():
    # ── 启动授权验证（密钥在 .env，不进 GitHub）──────────────────
    from startup_guard import verify
    verify()
    # ─────────────────────────────────────────────────────────────
    logger.info("=" * 50)
    logger.info("BTC AI 调度系统 v3 启动")
    logger.info("早盘：UTC 01:30（SGT 09:30）周一自动路由为周报(7P)")
    logger.info("正午：UTC 04:00（SGT 12:00）周二至周六")
    logger.info("欧盘：UTC 07:00（SGT 15:00）")
    logger.info("美盘：UTC 12:30（SGT 20:30）")
    logger.info("=" * 50)

    app = (Application.builder()
           .token(TOKEN)
           .post_init(post_init)
           .build())

    # 命令处理器
    app.add_handler(CommandHandler("b",      cmd_briefing))
    app.add_handler(CommandHandler("B",      cmd_briefing))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pos",    cmd_pos))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r"^简报$") & ~filters.COMMAND,
        cmd_briefing
    ))

    # ── 四个定时任务（UTC时间）───────────────────────────────────
    app.job_queue.run_daily(
        job_morning,
        time=dt_time(1, 30, 0, tzinfo=UTC),   # UTC 01:30 = SGT 09:30
        name="morning_briefing"
    )
    app.job_queue.run_daily(
        job_noon,
        time=dt_time(4, 0, 0, tzinfo=UTC),    # UTC 04:00 = SGT 12:00
        # PTB v22.8 约定（已读安装库源码 docstring 确认）：days 0-6 对应
        # sunday-saturday，即 0=周日 1=周一 2=周二 3=周三 4=周四 5=周五 6=周六
        # （v20.0 起从"0=周一"改成了"0=周日"，禁止凭旧记忆填写）
        # (2,3,4,5,6) = 周二至周六：美股周一~周五收盘后次日北京中午确认ETF
        days=(2, 3, 4, 5, 6),
        name="noon_briefing"
    )
    app.job_queue.run_daily(
        job_europe,
        time=dt_time(7, 0, 0, tzinfo=UTC),    # UTC 07:00 = SGT 15:00
        name="europe_briefing"
    )
    app.job_queue.run_daily(
        job_evening,
        time=dt_time(12, 30, 0, tzinfo=UTC),  # UTC 12:30 = SGT 20:30
        name="evening_briefing"
    )

    logger.info("四个定时任务已注册（含正午简报 周二至周六）")
    logger.info("Bot 开始监听命令...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
