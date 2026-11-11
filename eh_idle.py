"""EH 選單閒置：一般解析刪除 bot 選單，沿用結果只隱藏按鈕。"""
import asyncio
import logging
from telegram.error import TelegramError

logger = logging.getLogger(__name__)
IDLE_SECONDS = 300
_pending = {}


def cancel(chat_id, message_id):
    task = _pending.pop((chat_id, message_id), None)
    if task is not None:
        task.cancel()


def arm(bot, chat_id, message_id, *, hide_only=False):
    key = (chat_id, message_id)
    cancel(*key)

    async def expire():
        try:
            await asyncio.sleep(IDLE_SECONDS)
            # 一旦開始刪除，先取走計時器，避免清除後來的新計時器。
            if _pending.get(key) is not asyncio.current_task():
                return
            _pending.pop(key, None)
            if hide_only:
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
            else:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError as exc:
            logger.warning("EH 閒置訊息清理未完成（%s）", type(exc).__name__)
        finally:
            if _pending.get(key) is asyncio.current_task():
                _pending.pop(key, None)

    _pending[key] = asyncio.create_task(expire())
