import asyncio
from telegram import Bot
from telegram.error import TelegramError
from utils.helpers import setup_logger, get_env

logger = setup_logger()


def _md_to_html(text: str) -> str:
    """Convert CommonMark ** Markdown to Telegram HTML.
    处理 **粗体**、*斜体*、`代码`、## 标题、--- 分割线。
    """
    import re
    # 先转义 HTML 特殊字符（避免 < > & 被误解释）
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    # **粗体** → <b>粗体</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    # *斜体*（单星号，非粗体）→ <i>斜体</i>
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<i>\1</i>', text)
    # `代码` → <code>代码</code>
    text = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', text)
    # ## 标题 → <b>标题</b>
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    # ---/=== 分割线 → 横线字符
    text = re.sub(r'^[-=]{3,}$', '─' * 22, text, flags=re.MULTILINE)
    return text

def _split(text, n=4096):
    if len(text) <= n:
        return [text]
    parts, buf = [], ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > n:
            if buf:
                parts.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        parts.append(buf)
    return parts

async def _send_async(text):
    try:
        bot     = Bot(token=get_env("TELEGRAM_BOT_TOKEN"))
        chat_id = get_env("TELEGRAM_CHAT_ID")
        parts   = _split(text)
        for i, part in enumerate(parts):
            await bot.send_message(chat_id=chat_id, text=_md_to_html(part), parse_mode="HTML")
            if i < len(parts) - 1:
                await asyncio.sleep(0.5)
        logger.info(f"Telegram 发送成功（{len(parts)} 段）")
        return True
    except TelegramError as e:
        logger.error(f"Telegram 失败: {e}")
        return False

def send(text):
    """同步调用（用于 daily_briefing.py 等普通脚本）"""
    return asyncio.run(_send_async(text))

async def async_send(text):
    """异步调用（用于已有事件循环的场景，如清算监控）"""
    return await _send_async(text)
