import ast
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import unittest
import ehentai

class LayoutTests(unittest.IsolatedAsyncioTestCase):
    def test_titles_and_images_only(self):
        gallery=NS(title="原標題",title_jpn="日本語標題",tags={"artist":["不應出現"]},category="分類")
        nodes=ehentai.build_telegraph_content(gallery,["https://img.example/1.jpg"])
        self.assertEqual([n["tag"] for n in nodes],["h3","p","img"])
        self.assertNotIn("不應出現",str(nodes))
        self.assertNotIn("分類",str(nodes))
    async def test_cover_uses_first_page_image(self):
        from telegram import InputMediaPhoto
        from telegram.error import TelegramError
        import logging
        tree=ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="edit_message_to_cover")
        ns=dict(InputMediaPhoto=InputMediaPhoto,TelegramError=TelegramError,logger=logging.getLogger("cover"),public_thumbnail_url=lambda url:url)
        exec(compile(ast.Module(body=[node],type_ignores=[]),"<cover>","exec"),ns)
        status=NS(edit_media=AsyncMock())
        self.assertTrue(await ns["edit_message_to_cover"](status,NS(thumb="https://img.example/thumb.jpg"),"caption",cover_url="https://img.example/full.jpg"))
        self.assertEqual(status.edit_media.call_args.kwargs["media"].media,"https://img.example/full.jpg")
