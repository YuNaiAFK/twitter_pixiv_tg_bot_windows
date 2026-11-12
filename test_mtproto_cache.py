"""離線確認 MTProto 重送不重新上傳，以及過期與逾時的處理。"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telethon.errors import FileReferenceExpiredError
from telethon.tl.types import InputFile, DocumentAttributeFilename, InputMediaDocument
from telegram_documents import send_gif_document, read_cache


class FakeClient:
    def __init__(self):
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.is_user_authorized = AsyncMock(return_value=True)
        self.get_me = AsyncMock(return_value=NS(bot=True, id=42))
        self.get_input_entity = AsyncMock(return_value=NS())
        self.upload_file = AsyncMock(return_value=InputFile(1, 1, "work.gif", ""))
        self.doc = NS(id=10, access_hash=20, file_reference=b"ref", mime_type="application/octet-stream",
                      attributes=[DocumentAttributeFilename("work.gif")])
        self.get_messages = AsyncMock(return_value=NS(media=NS(document=self.doc)))
        self.requests = []
        self.errors = []
        self.short = False

    async def __call__(self, request):
        self.requests.append(request)
        error = self.errors.pop(0) if self.errors else None
        if error:
            raise error
        message = NS(id=123, media=NS(document=self.doc))
        return message if self.short else NS(updates=[NS(message=message)])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "work.gif"
        self.file.write_bytes(b"GIF89a-original-content")
        self.client = FakeClient()

    def send(self, path=None):
        with patch.dict("os.environ", {"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "hash", "BOT_TOKEN": "42:token"}), \
                patch("telethon.TelegramClient", return_value=self.client):
            return asyncio.run(send_gif_document(path or self.file, chat_id=99, reply_to=None,
                                                 caption=None, session_dir=self.root / "session"))

    def test_repeat_and_new_temp_path_upload_only_once(self):
        self.send()
        new = self.root / "scratch" / self.file.name
        new.parent.mkdir()
        new.write_bytes(self.file.read_bytes())
        self.send(new)
        self.client.upload_file.assert_awaited_once()
        self.assertIsInstance(self.client.requests[-1].media, InputMediaDocument)

    def test_content_change_uploads_again(self):
        self.send()
        self.file.write_bytes(b"different content")
        self.send()
        self.assertEqual(self.client.upload_file.await_count, 2)

    def test_short_sent_response_is_cached_and_reused(self):
        self.client.short = True
        self.assertEqual(self.send(), 123)
        self.assertEqual(self.send(), 123)
        self.client.upload_file.assert_awaited_once()

    def test_filename_change_does_not_reuse_wrong_filename(self):
        self.send()
        other = self.file.with_name("renamed.gif")
        other.write_bytes(self.file.read_bytes())
        self.send(other)
        self.assertEqual(self.client.upload_file.await_count, 2)

    def test_expired_reference_refreshes_without_upload(self):
        self.send()
        self.client.errors = [FileReferenceExpiredError(None), None]
        self.send()
        self.client.get_messages.assert_awaited_once()
        self.client.upload_file.assert_awaited_once()
        self.assertEqual(self.client.requests[-1].random_id, self.client.requests[-2].random_id)

    def test_deleted_source_reuploads_once_after_explicit_rejection(self):
        self.send()
        self.client.errors = [FileReferenceExpiredError(None)]
        self.client.get_messages.return_value = None
        self.send()
        self.assertEqual(self.client.upload_file.await_count, 2)

    def test_timeout_does_not_reupload_or_resend(self):
        self.send()
        self.client.errors = [TimeoutError()]
        with self.assertRaises(TimeoutError):
            self.send()
        self.assertEqual(len(self.client.requests), 2)
        self.client.upload_file.assert_awaited_once()

    def test_bad_cache_is_ignored(self):
        path = self.root / "invalid.json"
        path.write_text("[]")
        self.assertEqual(read_cache(path), {})


if __name__ == "__main__":
    unittest.main()


class EditCacheTests(unittest.TestCase):
    setUp = CacheTests.setUp
    send = CacheTests.send
    def test_edit_uses_original_id_and_cached_document(self):
        from telethon.tl.functions.messages import EditMessageRequest
        self.send()
        uploads=self.client.upload_file.await_count
        with patch.dict("os.environ", {"TELEGRAM_API_ID":"123","TELEGRAM_API_HASH":"hash","BOT_TOKEN":"42:token"}), patch("telethon.TelegramClient",return_value=self.client):
            asyncio.run(send_gif_document(self.file,chat_id=7,reply_to=None,caption=None,session_dir=self.root / "session",edit_message_id=55))
        self.assertEqual(self.client.upload_file.await_count,uploads)
        self.assertIsInstance(self.client.requests[-1],EditMessageRequest)
        self.assertEqual(self.client.requests[-1].id,55)
