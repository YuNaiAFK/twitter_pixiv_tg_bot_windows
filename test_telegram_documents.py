"""檢查真正的 MTProto 文件內容，避免文件 API 仍被桌面端視為 GIF。"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from telethon.tl.types import InputFile, DocumentAttributeFilename
from telegram_documents import upload_media, proxy_settings, send_gif_document


class DocumentTests(unittest.TestCase):
    def test_gif_uses_generic_mime_and_filename_only(self):
        uploaded = InputFile(1, 1, "upload.gif", "")
        media = upload_media(uploaded, "作品_123_p0_45_畫師.gif")
        self.assertEqual(media.mime_type, "application/octet-stream")
        self.assertTrue(media.force_file)
        self.assertIs(media.file, uploaded)
        self.assertEqual(len(media.attributes), 1)
        self.assertIsInstance(media.attributes[0], DocumentAttributeFilename)
        self.assertEqual(media.attributes[0].file_name, "作品_123_p0_45_畫師.gif")

    def test_proxy_keeps_credentials_and_remote_dns(self):
        proxy = proxy_settings("socks5://user:p%40ss@127.0.0.1:1080")
        self.assertEqual(proxy["password"], "p@ss")
        self.assertEqual(proxy["proxy_type"], "socks5")
        self.assertTrue(proxy["rdns"])
        self.assertIsNone(proxy_settings(""))
        with self.assertRaises(ValueError):
            proxy_settings("file:///tmp/proxy")

    def test_missing_credentials_does_not_connect(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True), \
                patch("telethon.TelegramClient") as client:
            with self.assertRaises(ValueError):
                asyncio.run(send_gif_document(Path(folder) / "work.gif", chat_id=1,
                                              reply_to=None, caption=None,
                                              session_dir=Path(folder)))
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
