import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telegram.error import BadRequest
import eh_idle

class IdleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        tasks=list(eh_idle._pending.values())
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        eh_idle._pending.clear()
    async def test_normal_menu_deleted(self):
        bot=NS(delete_message=AsyncMock(),edit_message_reply_markup=AsyncMock())
        with patch.object(eh_idle,"IDLE_SECONDS",0):
            eh_idle.arm(bot,1,2)
            await eh_idle._pending[(1,2)]
        bot.delete_message.assert_awaited_once_with(chat_id=1,message_id=2)
        bot.edit_message_reply_markup.assert_not_awaited()
    async def test_reused_result_only_hides_buttons(self):
        bot=NS(delete_message=AsyncMock(),edit_message_reply_markup=AsyncMock())
        with patch.object(eh_idle,"IDLE_SECONDS",0):
            eh_idle.arm(bot,1,2,hide_only=True)
            await eh_idle._pending[(1,2)]
        bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=1,message_id=2,reply_markup=None)
        bot.delete_message.assert_not_awaited()
    async def test_click_cancels_cleanup(self):
        bot=NS(delete_message=AsyncMock())
        eh_idle.arm(bot,1,2)
        task=eh_idle._pending[(1,2)]
        eh_idle.cancel(1,2)
        await asyncio.gather(task,return_exceptions=True)
        bot.delete_message.assert_not_awaited()
        self.assertFalse(eh_idle._pending)
    async def test_rearm_cancels_old_timer(self):
        bot=NS(delete_message=AsyncMock(),edit_message_reply_markup=AsyncMock())
        eh_idle.arm(bot,1,2)
        old=eh_idle._pending[(1,2)]
        with patch.object(eh_idle,"IDLE_SECONDS",0):
            eh_idle.arm(bot,1,2,hide_only=True)
            await eh_idle._pending[(1,2)]
        await asyncio.gather(old,return_exceptions=True)
        bot.delete_message.assert_not_awaited()
        bot.edit_message_reply_markup.assert_awaited_once()
    async def test_deleted_message_is_handled(self):
        bot=NS(delete_message=AsyncMock(side_effect=BadRequest("not found")))
        with patch.object(eh_idle,"IDLE_SECONDS",0):
            eh_idle.arm(bot,1,2)
            await eh_idle._pending[(1,2)]
        self.assertFalse(eh_idle._pending)
