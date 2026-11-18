import ast,asyncio,io,logging,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from telegram import InputMediaPhoto
from telegram.error import TelegramError
from PIL import Image
import eh_cover

class CoverTests(unittest.IsolatedAsyncioTestCase):
    def function(self):
        tree=ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="edit_message_to_cover")
        ns=dict(asyncio=asyncio,InputMediaPhoto=InputMediaPhoto,TelegramError=TelegramError,logger=logging.getLogger("cover"),public_thumbnail_url=lambda u:u)
        exec(compile(ast.Module(body=[node],type_ignores=[]),"<cover>","exec"),ns)
        return ns["edit_message_to_cover"]
    async def test_url_failure_uploads_file_to_same_message_with_caption_and_buttons(self):
        status=NS(edit_media=AsyncMock(side_effect=[TelegramError("Wrong type"),None]))
        with patch("eh_cover.download_cover",return_value=b"jpeg") as fetch:
            self.assertTrue(await self.function()(status,NS(thumb="thumb"),"caption","buttons","HTML",cover_url="first"))
        fetch.assert_called_once_with("first")
        media=status.edit_media.call_args.kwargs["media"]
        self.assertEqual(media.caption,"caption");self.assertEqual(media.media.filename,"eh-cover.jpg")
        self.assertEqual(status.edit_media.call_args.kwargs["reply_markup"],"buttons")
    async def test_success_does_not_download(self):
        with patch("eh_cover.download_cover") as fetch:
            self.assertTrue(await self.function()(NS(edit_media=AsyncMock()),NS(thumb="thumb"),"caption"))
            fetch.assert_not_called()
    async def test_download_failure_keeps_text_fallback(self):
        with patch("eh_cover.download_cover",side_effect=ValueError()):
            self.assertFalse(await self.function()(NS(edit_media=AsyncMock(side_effect=TelegramError("bad"))),NS(thumb="thumb"),"caption"))
    def test_png_converted_to_valid_jpeg(self):
        buf=io.BytesIO();Image.new("RGBA",(20,30),(255,0,0,128)).save(buf,format="PNG")
        with patch("eh_cover.r2.fetch_image",return_value=(buf.getvalue(),"image/png")):
            data=eh_cover.download_cover("https://img.test/a")
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.format,"JPEG");self.assertEqual(image.size,(20,30))
