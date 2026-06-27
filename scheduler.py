"""
BTC AI 调度系统 v3（完整版）
三个定时任务：早盘/欧盘/美盘 + /b 命令随时触发
"""
import asyncio
from datetime import time as dt_time, timezone
from telegram import Update
from telegram.ext import (Application, CommandHandler,
                          MessageHandler, filters, ContextTypes)
from utils.helpers import setup_logger, get_env, now_sgt
from daily_briefing import run as run_briefing

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
        f"早盘简报：每天 SGT 09:30\n"
        f"欧盘简报：每天 SGT 15:00\n"
        f"美盘简报：每天 SGT 20:30\n"
        f"─────────────────\n"
        f"发送 /b 立刻生成简报"
    )


# ── 启动通知 ─────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    try:
        await application.bot.send_message(
            chat_id=CHAT_ID,
            text=(
                "[OK] BTC AI 调度系统 v3 已启动\n"
                "早盘简报：每天 SGT 09:30（IB+ETF+CME缺口）\n"
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
    logger.info("早盘：UTC 01:30（SGT 09:30）")
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
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r"^简报$") & ~filters.COMMAND,
        cmd_briefing
    ))

    # ── 三个定时任务（UTC时间）───────────────────────────────────
    app.job_queue.run_daily(
        job_morning,
        time=dt_time(1, 30, 0, tzinfo=UTC),   # UTC 01:30 = SGT 09:30
        name="morning_briefing"
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

    logger.info("三个定时任务已注册")
    logger.info("Bot 开始监听命令...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
