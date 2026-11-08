"""頻道發布的離線驗證，不讀取憑證、不傳送真實 Telegram 訊息。"""
import ast
import asyncio
import logging
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

from telegram.error import TimedOut
from channel_publish import ChannelPublisher, CapturedMessage, capture_message, offer_publish


class ChannelPublishTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.publisher = ChannelPublisher(self.root / "channels.json", lambda user: user == 1)
        self.publisher.channels = {-100123: "測試頻道"}
        self.prompt = NS(message_id=100, edit_text=AsyncMock())
        self.message = NS(chat=NS(type="private"), chat_id=11, message_id=10,
            reply_text=AsyncMock(return_value=self.prompt), reply_photo=AsyncMock(return_value=NS(message_id=21)),
            reply_media_group=AsyncMock(return_value=[NS(message_id=22), NS(message_id=23)]))
        self.bot = NS(id=9, get_chat_member=AsyncMock(return_value=NS(status="administrator", can_post_messages=True)),
                      copy_messages=AsyncMock(return_value=[NS(message_id=31), NS(message_id=32), NS(message_id=33)]))
        self.context = NS(bot=self.bot, args=[])

    async def asyncTearDown(self):
        await self.publisher.shutdown(None)

    async def offer(self):
        message = capture_message(self.message, 1, "twitter", self.publisher)
        await message.reply_photo(photo=b"photo")
        await message.reply_media_group(media=[])
        await offer_publish(message)
        return next(reversed(self.publisher.pending))

    def update(self, token, action="-100123", user=1, chat_id=11):
        return NS(callback_query=NS(data=f"cp|{token}|{action}", answer=AsyncMock()),
                  effective_user=NS(id=user), effective_chat=NS(id=chat_id, type="private"),
                  effective_message=self.prompt)

    async def test_capture_only_admin_private_twitter_and_media(self):
        for user, platform, chat_type in ((2, "twitter", "private"), (1, "pixiv", "private"),
                                           (1, "twitter", "supergroup")):
            self.message.chat.type = chat_type
            self.assertIs(capture_message(self.message, user, platform, self.publisher), self.message)
        self.message.chat.type = "private"
        token = await self.offer()
        choice = self.publisher.pending[token]
        self.assertEqual(choice.media_ids, [21, 22, 23])
        self.assertNotIn(100, choice.media_ids)
        self.bot.copy_messages.assert_not_called()

    async def test_confirm_copies_album_ids_and_double_click_does_not_repeat(self):
        token = await self.offer()
        update = self.update(token)
        await self.publisher.choose(update, self.context)
        await self.publisher.choose(update, self.context)
        self.bot.copy_messages.assert_awaited_once_with(chat_id=-100123, from_chat_id=11, message_ids=[21, 22, 23])
        self.assertIn("已傳送", self.prompt.edit_text.call_args.args[0])

    async def test_skip_and_timeout_never_copy(self):
        token = await self.offer()
        await self.publisher.choose(self.update(token, "skip"), self.context)
        token = await self.offer()
        choice = self.publisher.pending[token]
        choice.expires_at = asyncio.get_running_loop().time() - 1
        await self.publisher.expire(token, choice)
        await self.publisher.choose(self.update(token), self.context)
        self.bot.copy_messages.assert_not_called()
        self.assertIn("逾時", self.prompt.edit_text.call_args.args[0])

    async def test_foreign_user_chat_and_removed_admin_cannot_confirm(self):
        token = await self.offer()
        await self.publisher.choose(self.update(token, user=2), self.context)
        await self.publisher.choose(self.update(token, chat_id=12), self.context)
        self.publisher.is_admin = lambda user: False
        await self.publisher.choose(self.update(token), self.context)
        self.bot.copy_messages.assert_not_called()

    async def test_permission_loss_or_unlisted_target_prevents_post(self):
        token = await self.offer()
        await self.publisher.choose(self.update(token, "-100999"), self.context)
        self.bot.get_chat_member.return_value.can_post_messages = False
        await self.publisher.choose(self.update(token), self.context)
        self.bot.copy_messages.assert_not_called()
        self.assertIn("無頻道發布權限", self.prompt.edit_text.call_args.args[0])

    async def test_copy_timeout_and_partial_copy_are_not_retried(self):
        token = await self.offer()
        self.bot.copy_messages.side_effect = TimedOut()
        await self.publisher.choose(self.update(token), self.context)
        await self.publisher.choose(self.update(token), self.context)
        self.assertEqual(self.bot.copy_messages.await_count, 1)
        self.assertIn("未自動重送", self.prompt.edit_text.call_args.args[0])
        self.bot.copy_messages.side_effect = None
        self.bot.copy_messages.return_value = [NS(message_id=1)]
        token = await self.offer()
        await self.publisher.choose(self.update(token), self.context)
        self.assertIn("1/3", self.prompt.edit_text.call_args.args[0])

    async def test_multiple_channels_are_explicit_choices(self):
        self.publisher.channels[-100456] = "第二頻道"
        await self.offer()
        keyboard = self.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(len(keyboard), 3)
        self.assertIn("-100456", keyboard[1][0].callback_data)
        self.bot.copy_messages.assert_not_called()

    async def test_membership_is_registered_only_by_bot_admin_and_persisted(self):
        event = NS(chat=NS(type="channel", id=-100789, title="新頻道"), from_user=NS(id=2),
                   new_chat_member=NS(status="administrator", can_post_messages=True))
        await self.publisher.membership(NS(my_chat_member=event), self.context)
        self.assertNotIn(-100789, self.publisher.channels)
        event.from_user.id = 1
        await self.publisher.membership(NS(my_chat_member=event), self.context)
        restored = ChannelPublisher(self.publisher.path, lambda user: True)
        self.assertEqual(restored.channels[-100789], "新頻道")
        event.from_user.id = 2
        event.new_chat_member.can_post_messages = False
        await self.publisher.membership(NS(my_chat_member=event), self.context)
        self.assertNotIn(-100789, self.publisher.channels)

    async def test_manual_registration_checks_channel_post_permission(self):
        update = NS(effective_user=NS(id=1), effective_chat=NS(type="private"), effective_message=self.message)
        self.context.args = ["@channel"]
        self.bot.get_chat = AsyncMock(return_value=NS(type="channel", id=-100999, title="手動頻道"))
        await self.publisher.command(update, self.context)
        self.assertIn(-100999, self.publisher.channels)
        self.context.args = ["remove", "-100999"]
        await self.publisher.command(update, self.context)
        self.assertNotIn(-100999, self.publisher.channels)

    async def test_real_parse_flow_offers_for_fresh_and_cached_results(self):
        tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_message")
        status = NS(edit_text=AsyncMock())
        cache = Mock()
        cache.lookup.return_value = None
        self.message.text = "https://x.com/user/status/123"
        self.message.caption = None
        self.message.chat.type = "private"
        async def send(message, *args, **kwargs):
            await message.reply_photo(photo=b"media")
            return 1, "", []
        async def cached(message, *args):
            await message.reply_photo(photo=b"cached")
            return True
        namespace = dict(Update=object, ContextTypes=NS(DEFAULT_TYPE=object), asyncio=asyncio,
            logger=logging.getLogger("channel-flow-test"), shutil=shutil, subprocess=subprocess,
            CHANNEL_PUBLISHER=self.publisher, extract_urls=lambda text: [text],
            user_is_allowed=lambda *args: True, classify_telegram_link=lambda url: False,
            detect_media_platform=lambda url: "twitter", gallery_url=lambda text: None,
            classify_link=lambda url: ("tweet", ""), is_parseable_kind=lambda kind: True,
            PARSE_CACHE=cache, apply_cooldown=lambda user: 0, send_status=AsyncMock(return_value=status),
            make_work_dir=lambda: Path(tempfile.mkdtemp(dir=self.root)), build_caption=lambda url: url,
            download_semaphore=asyncio.Semaphore(1), download_with_gallery_dl=lambda *args: None,
            collect_media_files=lambda path: [path / "image.jpg"], send_original_files=send,
            source_link_options=lambda: None, remember_media_parse=lambda *args: None,
            cleanup_request_messages=AsyncMock(), send_cached_media=cached,
            TimedOut=TimedOut, TelegramError=RuntimeError)
        exec(compile(ast.Module(body=[function], type_ignores=[]), "<actual-handle-message>", "exec"), namespace)
        update = NS(effective_message=self.message, effective_user=NS(id=1))
        await namespace["handle_message"](update, self.context)
        self.assertEqual(len(self.publisher.pending), 1)
        cache.lookup.return_value = {"media": "cached"}
        await namespace["handle_message"](update, self.context)
        self.assertEqual(len(self.publisher.pending), 2)
        update.effective_user.id = 2
        await namespace["handle_message"](update, self.context)
        self.assertEqual(len(self.publisher.pending), 2)


if __name__ == "__main__":
    unittest.main()
