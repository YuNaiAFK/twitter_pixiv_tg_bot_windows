"""本機檔案製作的離線測試；Telegram、R2 與 Telegraph 不連線。"""
import asyncio
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from PIL import Image
import local_uploads as uploads
import r2
from test_r2_dedup import MemoryBucket


def picture(size=(20, 10), color="red", mode="RGB"):
    with Image.new(mode, size, color) as image, io.BytesIO() as buffer:
        image.save(buffer, format="PNG")
        return buffer.getvalue()


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, members):
        path = self.root / "input"
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in members:
                archive.writestr(name, data)
        return path

    def test_resize_preserves_original_and_aspect_ratio(self):
        data = picture((2000, 1000))
        source = self.root / "original.png"
        source.write_bytes(data)
        entries = uploads.prepare_inputs([("作品.png", source)], self.root / "out")
        with Image.open(entries[0][1]) as result:
            self.assertEqual(result.size, (1280, 640))
            self.assertEqual(result.format, "JPEG")
        self.assertEqual(source.read_bytes(), data)

    def test_small_transparent_image_not_upscaled_white_background(self):
        destination = self.root / "out.jpg"
        uploads.prepare_image(picture((20, 10), (0, 0, 0, 0), "RGBA"), destination)
        with Image.open(destination) as result:
            self.assertEqual(result.size, (20, 10))
            self.assertEqual(result.getpixel((0, 0)), (255, 255, 255))

    def test_exif_orientation_is_applied(self):
        with Image.new("RGB", (40, 20), "red") as image, io.BytesIO() as buffer:
            exif = image.getexif()
            exif[274] = 6
            image.save(buffer, format="JPEG", exif=exif)
            uploads.prepare_image(buffer.getvalue(), self.root / "out.jpg")
        with Image.open(self.root / "out.jpg") as result:
            self.assertEqual(result.size, (20, 40))

    def test_archive_natural_order_and_metadata_ignored(self):
        source = self.archive([("10.png", picture(color="blue")),
                               ("__MACOSX/._1.png", b"metadata"),
                               ("2.png", picture()), ("notes.txt", b"notes")])
        entries = uploads.prepare_inputs([("作品.cbz", source)], self.root / "out")
        self.assertEqual([name for name, _ in entries], ["2.png", "10.png"])
        with Image.open(entries[0][1]) as result:
            self.assertGreater(result.getpixel((0, 0))[0], 240)

    def test_archive_bad_paths_rejected(self):
        for name in ("../outside.png", "/absolute.png", "a\\bad.png"):
            with self.subTest(name=name):
                source = self.archive([(name, picture())])
                if "\\" in name:
                    source.write_bytes(source.read_bytes().replace(b"a/bad.png", b"a\\bad.png"))
                with self.assertRaisesRegex(ValueError, "路徑"):
                    uploads.prepare_inputs([("work.zip", source)], self.root / "out")
        self.assertFalse((self.root.parent / "outside.png").exists())

    def test_archive_symlink_rejected(self):
        member = zipfile.ZipInfo("link.png")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        source = self.archive([(member, b"/etc/passwd")])
        with self.assertRaisesRegex(ValueError, "符號連結"):
            uploads.prepare_inputs([("work.zip", source)], self.root / "out")

    def test_limits_and_empty_archive_rejected(self):
        source = self.archive([("1.png", picture()), ("2.png", picture())])
        with patch.object(uploads, "MAX_FILES", 1), self.assertRaises(ValueError):
            uploads.prepare_inputs([("work.zip", source)], self.root / "out")
        with patch.object(uploads, "MAX_FRAME_BYTES", 1), self.assertRaises(ValueError):
            uploads.prepare_inputs([("work.zip", source)], self.root / "out")
        source = self.archive([("notes.txt", b"empty")])
        with self.assertRaisesRegex(ValueError, "沒有找到"):
            uploads.prepare_inputs([("work.zip", source)], self.root / "out")

    def test_page_title_first_and_no_silent_truncation(self):
        urls = ["https://img.example/2", "https://img.example/10"]
        nodes = json.loads(uploads.page_content("作品", urls))
        self.assertEqual(nodes[0], {"tag": "h3", "children": ["作品"]})
        self.assertTrue(nodes[1]["children"][0].startswith("製作日期："))
        self.assertEqual([node["attrs"]["src"] for node in nodes[2:]], urls)
        with self.assertRaisesRegex(ValueError, "上限"):
            uploads.page_content("作品", ["https://img.example/" + "x" * 64000])

    def test_create_page_omits_author_url_from_api_request(self):
        with patch("httpx.Client") as factory:
            client = factory.return_value.__enter__.return_value
            client.post.side_effect = [
                NS(json=lambda: {"ok": True, "result": {"access_token": "test"}}),
                NS(json=lambda: {"ok": True, "result": {"path": "test", "url": "https://telegra.ph/test"}}),
            ]
            result = uploads.create_page("test", ["https://img.example/1.jpg"])
            self.assertEqual(result.url, "https://telegra.ph/test")
            self.assertNotIn("author_url", client.post.call_args.kwargs["data"])

    def test_reply_escapes_filename_only_preview_hyperlinked(self):
        text = uploads.result_text('<作品 & "作者">', "https://telegra.ph/test")
        self.assertIn("&lt;作品 &amp;", text)
        self.assertEqual(text.count("<a "), 1)
        self.assertTrue(text.startswith('預覽：<a href="https://telegra.ph/test">'))

    def test_local_r2_injection_reuses_duplicate_content(self):
        bucket = MemoryBucket()
        settings = r2.R2Settings("account", "key", "secret", "bucket", "https://img.example")
        data = picture()
        with patch.object(r2, "_make_client", return_value=bucket), patch.object(r2, "fetch_image") as remote:
            report = r2.upload_many([0, 1], settings=settings, workers=2,
                                   image_fetcher=lambda index: (data, "image/png"))
        self.assertEqual((report.uploaded, report.reused, report.failed), (1, 1, 0))
        self.assertEqual(len(bucket.puts), 1)
        self.assertEqual(report.urls()[0], report.urls()[1])
        remote.assert_not_called()


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.handlers = []
        self.panels = {}
        self.app = NS(add_handler=self.handlers.append, post_shutdown=None)
        self.sequence = 0

        def work_dir():
            path = self.root / f"batch{self.sequence}"
            self.sequence += 1
            path.mkdir()
            return path

        class Guard:
            ok = True
            def __init__(self, user): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass

        self.runtime = dict(user_is_allowed=lambda *args: True, make_work_dir=work_dir,
            user_is_admin=lambda user: True,
            BASE_DIR=self.root,
            extract_urls=lambda text: [], detect_media_platform=lambda url: None,
            ehentai=NS(is_gallery_url=lambda url: False), handle_message=AsyncMock(),
            RequestGuard=Guard, busy_message=lambda user: "忙碌中", gallery_semaphore=asyncio.Semaphore(1),
            R2_UPLOAD_WORKERS=2, R2_UPLOAD_TIMEOUT=5, redact_secrets=str,
            request_r2_retry_decision=AsyncMock(return_value="stop"), _pending_r2_decisions={},
            cleanup_request_messages=AsyncMock(), source_link_options=lambda: None)
        uploads.register(self.app, self.runtime)
        self.publish = next(handler.callback for handler in self.handlers if "publish" in getattr(handler, "commands", []))
        self.cancel = next(handler.callback for handler in self.handlers if "cancel_upload" in getattr(handler, "commands", []))
        self.collect = self.handlers[-1].callback
        self.choose = self.handlers[0].callback
        self.settings = r2.R2Settings("account", "key", "secret", "bucket", "https://img.example")
        self.page = patch.object(uploads, "create_page", return_value=NS(url="https://telegra.ph/work"))
        self.create_page = self.page.start()
        self.addCleanup(self.page.stop)
        for name, value in (("r2_settings", self.settings), ("bucket_usage", (0, 0)), ("limit_bytes", 0)):
            patcher = patch.object(r2, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def update(self, name=None, user=1, chat=2, data=None):
        status = self.panels.setdefault((chat, user), NS(message_id=900, edit_text=AsyncMock(), edit_media=AsyncMock()))
        message = NS(document=None, photo=[], caption=None, message_id=10,
                     reply_text=AsyncMock(return_value=status), reply_photo=AsyncMock(), reply_to_message=None)
        if name:
            source = self.root / f"source{os.urandom(4).hex()}"
            source.write_bytes(data if data is not None else picture())
            message.document = NS(file_name=name, file_size=source.stat().st_size,
                get_file=AsyncMock(return_value=NS(file_path=str(source))))
        return NS(effective_message=message, effective_user=NS(id=user),
                  effective_chat=NS(id=chat, type="private"))

    def upload_ok(self, indexes, **kwargs):
        for index in indexes:
            data, mime = kwargs["image_fetcher"](index)
            self.assertEqual(mime, "image/jpeg")
            with Image.open(io.BytesIO(data)) as image:
                self.assertLessEqual(max(image.size), 1280)
        return r2.UploadReport(total=len(indexes), results=[
            r2.UploadResult(index, public_url=f"https://img.example/{index}") for index in indexes])

    def callback(self, message_update, data, user=1):
        update = self.update(user=user)
        update.effective_message = message_update.effective_message
        update.effective_chat = message_update.effective_chat
        update.callback_query = NS(data=data, answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
        return update

    async def test_receiving_archive_waits_for_upload_choice(self):
        update = self.update("work.zip")
        with patch.object(r2, "upload_many", side_effect=self.upload_ok) as upload:
            await self.collect(update, None)
            upload.assert_not_called()
            self.create_page.assert_not_called()
        markup = update.effective_message.reply_text.return_value.edit_text.call_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].text, "上傳並製作")
        self.assertEqual([b.text for row in markup.inline_keyboard for b in row], ["上傳並製作", "取消"])
        await self.choose(self.callback(update, markup.inline_keyboard[0][1].callback_data), None)
        self.assertFalse((self.root / "batch0").exists())

    async def test_non_admin_files_commands_and_buttons_are_silent(self):
        self.runtime["user_is_admin"] = lambda user: user == 1
        update = self.update("work.zip", user=2)
        await self.collect(update, None)
        await self.publish(update, None)
        await self.cancel(update, None)
        self.assertEqual(self.sequence, 0)
        update.effective_message.reply_text.assert_not_called()
        update.effective_message.document.get_file.assert_not_called()
        callback = self.callback(update, "lu|stale|publish", user=2)
        await self.choose(callback, None)
        callback.callback_query.answer.assert_awaited_once_with()
        callback.effective_message.reply_text.assert_not_called()

    async def test_private_attachment_filter_accepts_rar_and_7z(self):
        from datetime import datetime, timezone
        from telegram import Chat, Document, Message, Update, User
        for filename in ("作品.7z", "作品.rar", "作品.RAR"):
            with self.subTest(filename=filename):
                message = Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(1, "private"),
                    from_user=User(1, "Admin", False), document=Document("file", "unique", file_name=filename))
                self.assertTrue(self.handlers[-1].check_update(Update(1, message=message)))

    async def test_revoked_admin_cannot_continue_existing_batch(self):
        update = self.update("work.png")
        await self.collect(update, None)
        panel = update.effective_message.reply_text.return_value
        menu = panel.edit_text.call_args.kwargs["reply_markup"]
        count = panel.edit_text.await_count
        self.runtime["user_is_admin"] = lambda user: False
        await self.choose(self.callback(update, menu.inline_keyboard[0][0].callback_data), None)
        await self.publish(self.update(), None)
        self.assertEqual(panel.edit_text.await_count, count)
        self.create_page.assert_not_called()

    async def test_non_admin_media_caption_still_uses_normal_link_parser(self):
        self.runtime["user_is_admin"] = lambda user: False
        self.runtime["extract_urls"] = lambda text: ["https://x.com/user/status/1"]
        self.runtime["detect_media_platform"] = lambda url: "twitter"
        update = self.update("image.png", user=2)
        update.effective_message.caption = "https://x.com/user/status/1"
        await self.collect(update, None)
        self.runtime["handle_message"].assert_awaited_once_with(update, None)
        self.assertEqual(self.sequence, 0)

    async def test_public_menus_and_help_hide_upload_commands(self):
        import ast
        import logging
        from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat
        from telegram.error import TelegramError
        tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        ns = dict(Application=object, BotCommand=BotCommand, BotCommandScopeAllPrivateChats=BotCommandScopeAllPrivateChats,
            BotCommandScopeChat=BotCommandScopeChat, TelegramError=TelegramError,
            BOT_DESCRIPTION="test", BOT_SHORT_DESCRIPTION="test", AUTHORIZATION=NS(admins={1}),
            logger=logging.getLogger("menu-test"), user_is_admin=lambda user: user == 1,
            MAX_FILES=20, SUPPORTED_SITES_NOTICE="支援的連結")
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {"commands_hint", "post_init"}:
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<actual-command-menu>", "exec"), ns)
        bot = NS(set_my_description=AsyncMock(), set_my_short_description=AsyncMock(), set_my_commands=AsyncMock())
        await ns["post_init"](NS(bot=bot))
        for call in bot.set_my_commands.call_args_list:
            commands = {command.command for command in call.args[0]}
            if isinstance(call.kwargs.get("scope"), BotCommandScopeChat):
                self.assertTrue({"publish", "cancel_upload"} <= commands)
            else:
                self.assertFalse({"publish", "cancel_upload"} & commands)
        self.assertNotIn("/publish", ns["commands_hint"](2))
        self.assertNotIn("/cancel_upload", ns["commands_hint"]())
        self.assertIn("/publish", ns["commands_hint"](1))

    async def test_entire_workflow_edits_one_message_including_final_cover(self):
        first, second = self.update("1.png"), self.update("2.png")
        await self.collect(first, None)
        await self.collect(second, None)
        panel = first.effective_message.reply_text.return_value
        command = self.update()
        with patch.object(r2, "upload_many", side_effect=self.upload_ok):
            await self.publish(command, None)
        first.effective_message.reply_text.assert_awaited_once()
        for update in (second, command):
            update.effective_message.reply_text.assert_not_called()
            update.effective_message.reply_photo.assert_not_called()
        panel.edit_media.assert_awaited_once()
        self.assertGreater(panel.edit_text.await_count, 3)
        self.runtime["cleanup_request_messages"].assert_not_called()

    async def test_old_or_other_user_buttons_cannot_publish(self):
        update = self.update("work.png")
        await self.collect(update, None)
        markup = update.effective_message.reply_text.return_value.edit_text.call_args.kwargs["reply_markup"]
        data = markup.inline_keyboard[0][0].callback_data
        await self.choose(self.callback(update, data, user=99), None)
        await self.cancel(self.update(), None)
        await self.collect(self.update("new.png"), None)
        await self.choose(self.callback(update, data), None)
        self.create_page.assert_not_called()

    async def test_group_images_are_not_automatically_collected(self):
        from datetime import datetime, timezone
        from telegram import Chat, Document, Message, Update, User
        update = self.update("work.png")
        update.effective_chat.type = "supergroup"
        await self.collect(update, None)
        self.assertEqual(self.sequence, 0)
        update.effective_message.reply_text.assert_not_called()
        message = Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(-1, "supergroup"),
                          from_user=User(1, "User", False), document=Document("a", "b", file_name="work.zip"))
        self.assertFalse(self.handlers[-1].check_update(Update(1, message=message)))

    async def test_group_reply_archive_waits_for_choice_then_publishes(self):
        with io.BytesIO() as buffer:
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("1.png", picture())
            source = self.update("群組作品.zip", user=2, data=buffer.getvalue()).effective_message
        command = self.update(chat=-100)
        command.effective_chat.type = "supergroup"
        command.effective_message.reply_to_message = source
        await self.publish(command, None)
        self.create_page.assert_not_called()
        menu = command.effective_message.reply_text.return_value.edit_text.call_args.kwargs["reply_markup"]
        self.assertEqual(menu.inline_keyboard[0][0].text, "上傳並製作")
        # 重複回覆同一個壓縮檔，不再加入一次。
        await self.publish(command, None)
        callback = self.callback(command, menu.inline_keyboard[0][0].callback_data)
        with patch.object(r2, "upload_many", side_effect=self.upload_ok):
            await self.choose(callback, None)
        self.create_page.assert_called_once_with("群組作品", ["https://img.example/0"])
        callback.effective_message.reply_text.return_value.edit_media.assert_awaited_once()

    async def test_group_rejects_image_reply_and_unauthorized_group(self):
        update = self.update(chat=-100)
        update.effective_chat.type = "group"
        update.effective_message.reply_to_message = self.update("image.png").effective_message
        await self.publish(update, None)
        self.assertIn("圖片請傳到", update.effective_message.reply_text.call_args.args[0])
        self.assertEqual(self.sequence, 0)
        self.runtime["user_is_allowed"] = lambda *args: False
        update.effective_message.reply_text.reset_mock()
        await self.publish(update, None)
        update.effective_message.reply_text.assert_not_called()

    async def test_direct_images_publish_in_filename_order_and_cleanup(self):
        await self.collect(self.update("10.png", data=picture(color="blue")), None)
        await self.collect(self.update("2.png"), None)
        self.create_page.assert_not_called()
        seen = []

        def upload(indexes, **kwargs):
            for index in indexes:
                with Image.open(io.BytesIO(kwargs["image_fetcher"](index)[0])) as image:
                    seen.append(image.getpixel((0, 0)))
            return self.upload_ok(indexes, **kwargs)

        update = self.update()
        with patch.object(r2, "upload_many", side_effect=upload):
            await self.publish(update, None)
        self.assertGreater(seen[0][0], 240)
        self.assertGreater(seen[1][2], 240)
        self.create_page.assert_called_once_with("10", ["https://img.example/0", "https://img.example/1"])
        sent = update.effective_message.reply_text.return_value.edit_media.call_args.kwargs["media"]
        self.assertIn("預覽：", sent.caption)
        self.assertEqual(sent.parse_mode, "HTML")
        self.assertEqual(sent.media.filename, "00001.jpg")
        self.assertFalse((self.root / "batch0").exists())

    async def test_archive_publish_uses_archive_filename(self):
        with io.BytesIO() as buffer:
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("1.png", picture())
            update = self.update("我的作品.cbz", data=buffer.getvalue())
        await self.collect(update, None)
        with patch.object(r2, "upload_many", side_effect=self.upload_ok):
            await self.publish(self.update(), None)
        self.create_page.assert_called_once_with("我的作品", ["https://img.example/0"])
        media = update.effective_message.reply_text.return_value.edit_media.call_args.kwargs["media"]
        self.assertEqual(media.caption, '預覽：<a href="https://telegra.ph/work">我的作品.cbz</a>')

    async def test_three_failures_stop_before_telegraph(self):
        await self.collect(self.update("work.png"), None)
        def fail(indexes, **kwargs):
            return r2.UploadReport(total=len(indexes), failed=len(indexes), results=[
                r2.UploadResult(index, error="測試失敗") for index in indexes])
        with patch.object(r2, "upload_many", side_effect=fail) as upload, patch.object(uploads.asyncio, "sleep", new=AsyncMock()):
            await self.publish(self.update(), None)
        self.assertEqual(upload.call_count, 3)
        self.runtime["request_r2_retry_decision"].assert_awaited_once()
        self.create_page.assert_not_called()

    async def test_partial_failure_retries_only_failed_image(self):
        await self.collect(self.update("1.png"), None)
        await self.collect(self.update("2.png"), None)
        calls = []
        def upload(indexes, **kwargs):
            calls.append(list(indexes))
            if len(calls) == 1:
                return r2.UploadReport(total=2, results=[r2.UploadResult(0, public_url="https://img.example/0"),
                                                        r2.UploadResult(1, error="暫時失敗")])
            return self.upload_ok(indexes, **kwargs)
        with patch.object(r2, "upload_many", side_effect=upload), patch.object(uploads.asyncio, "sleep", new=AsyncMock()):
            await self.publish(self.update(), None)
        self.assertEqual(calls, [[0, 1], [1]])
        self.create_page.assert_called_once()

    async def test_permission_and_owner_isolation_cancel(self):
        self.runtime["user_is_allowed"] = lambda *args: False
        update = self.update("blocked.png")
        await self.collect(update, None)
        update.effective_message.reply_text.assert_not_called()
        self.assertEqual(self.sequence, 0)
        self.runtime["user_is_allowed"] = lambda *args: True
        await self.collect(self.update("one.png", user=1), None)
        await self.collect(self.update("two.png", user=2), None)
        await self.cancel(self.update(user=1), None)
        self.assertFalse((self.root / "batch0").exists())
        self.assertTrue((self.root / "batch1").exists())
        await self.app.post_shutdown(self.app)
        self.assertFalse((self.root / "batch1").exists())

    async def test_budget_stop_prevents_page(self):
        await self.collect(self.update("work.png"), None)
        report = r2.UploadReport(total=1, budget_stopped=True, results=[r2.UploadResult(0, error="額度")])
        with patch.object(r2, "upload_many", return_value=report):
            await self.publish(self.update(), None)
        self.create_page.assert_not_called()

    async def test_failed_result_reply_reuses_already_created_page(self):
        await self.collect(self.update("work.png"), None)
        update = self.update()
        update.effective_message.reply_text.return_value.edit_media.side_effect = RuntimeError("reply failed")
        with patch.object(r2, "upload_many", side_effect=self.upload_ok) as upload:
            await self.publish(update, None)
            update.effective_message.reply_text.return_value.edit_media.side_effect = None
            await self.publish(self.update(), None)
        self.assertEqual(upload.call_count, 1)
        self.create_page.assert_called_once()

    async def test_termux_proot_download_mapping(self):
        prefix = self.root / "usr"
        source = prefix / "var/lib/proot-distro/containers/botapi/rootfs/root/telegram-bot-api/token/photos/file.jpg"
        source.parent.mkdir(parents=True)
        source.write_bytes(picture())
        remote = NS(file_path="https://api.telegram.org/file/botdummy//root/telegram-bot-api/token/photos/file.jpg", download_to_drive=AsyncMock())
        target = self.root / "download"
        with patch.dict(os.environ, {"PREFIX": str(prefix), "CHROOT_NAME": "botapi"}):
            await uploads.download_file(NS(get_file=AsyncMock(return_value=remote)), target, self.runtime)
        self.assertEqual(target.read_bytes(), source.read_bytes())
        remote.download_to_drive.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows Docker 檔案橋接")
    async def test_windows_docker_local_path_bridge(self):
        raw = "/var/lib/telegram-bot-api/dummy/documents/file.zip"
        remote = NS(file_path=f"https://api.telegram.org/file/botdummy/{raw}", download_to_drive=AsyncMock())
        target = self.root / "download"
        def copy(args, **kwargs):
            self.assertEqual(args[:3], ["docker", "cp", "telegram-bot-api:" + raw])
            target.write_bytes(b"archive")
            return NS(returncode=0)
        with patch.object(uploads.subprocess, "run", side_effect=copy):
            await uploads.download_file(NS(get_file=AsyncMock(return_value=remote)), target, self.runtime)
        self.assertEqual(target.read_bytes(), b"archive")
        remote.download_to_drive.assert_not_called()


if __name__ == "__main__":
    unittest.main()
