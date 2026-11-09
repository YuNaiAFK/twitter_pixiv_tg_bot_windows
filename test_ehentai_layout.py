import ast
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import unittest
import ehentai
from unittest.mock import Mock, patch

def bot_function(name, namespace):
    tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<regression>", "exec"), namespace)
    return namespace[name]

class RegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_metadata_refresh_preserves_page_and_r2(self):
        existing = {"telegraph": {"view": {"path": "existing"}}, "r2": {"view": ["r2-url"]}, "display_urls": ["image"]}
        cache = NS(store=Mock(), needs_pruning=lambda: False, save=Mock())
        ns = dict(GALLERY_CACHE_ENABLED=True, gallery_cache_lookup=lambda *a: existing,
            gallery_cache_key=lambda *a: "key", gallery_cache_payload=lambda *a: {"gallery": {"title": "new"}},
            PARSE_CACHE=cache, prune_orphaned_cache_files=lambda: None)
        bot_function("remember_gallery", ns)("url", NS())
        payload = cache.store.call_args.args[2]
        self.assertEqual(payload["gallery"], {"title": "new"})
        for key in ("telegraph", "r2", "display_urls"):
            self.assertEqual(payload[key], existing[key])

    async def test_publish_only_view_with_page_and_cover(self):
        import asyncio, threading, logging
        for mode, page_ok, cover_ok in [("view", True, True), ("view", True, False), ("view", False, True), ("zip1280", True, True)]:
            with self.subTest(mode=mode, page=page_ok, cover=cover_ok):
                page = NS(url="https://telegra.ph/page")
                create = Mock(return_value=page) if page_ok else Mock(side_effect=ehentai.TelegraphError("PAGE_SAVE_FAILED"))
                ns = dict(Path=Path, asyncio=asyncio, threading=threading,
                    cached_telegraph_entry=lambda *a: None, gallery_page_urls=AsyncMock(return_value=(["image"], [])),
                    source_link_options=lambda: None, public_embed_urls=AsyncMock(return_value=["image"]),
                    host_on_r2=AsyncMock(return_value=["image"]), ehentai=NS(create_telegraph_page=create, TelegraphError=ehentai.TelegraphError),
                    TELEGRAPH_AUTHOR="test", remember_telegraph_page=Mock(), build_gallery_info=lambda *a, **kw: "summary",
                    escape_html=lambda x:x, remember_regenerate=Mock(), regenerate_keyboard=lambda *a: None,
                    edit_message_to_cover=AsyncMock(return_value=cover_ok), logger=logging.getLogger("regression"), EH_CHANNEL_PUBLISHER=NS())
                with patch("channel_publish.offer_eh_publish", new_callable=AsyncMock) as offer:
                    await bot_function("run_gallery_images", ns)(NS(chat_id=1), NS(edit_text=AsyncMock()), NS(page_url="url", gid="1", token="a"), mode, Path("."), "", NS(id=1))
                    self.assertEqual(offer.await_count, int(mode == "view" and page_ok and cover_ok))

    def test_save_failed_retry_is_bounded(self):
        page = {"ok": True, "result": {"url": "https://telegra.ph/page", "path": "page"}}
        failure = {"ok": False, "error": "PAGE_SAVE_FAILED"}
        def client_for(replies):
            return NS(post=Mock(side_effect=[NS(json=lambda r=r:r) for r in replies]))
        kwargs = dict(access_token="token", title="title", author_name="", author_url="", content="[]", timeout=1)
        with patch.object(ehentai.time, "sleep"):
            client = client_for([failure, failure, page])
            self.assertEqual(ehentai._telegraph_post("createPage", http_client=client, **kwargs).path, "page")
            self.assertEqual(client.post.call_count, 3)
            client = client_for([failure]*3)
            with self.assertRaisesRegex(ehentai.TelegraphError, "PAGE_SAVE_FAILED"):
                ehentai._telegraph_post("editPage/page", http_client=client, **kwargs)
            self.assertEqual(client.post.call_count, 3)
            client = client_for([{"ok": False, "error": "ACCESS_TOKEN_INVALID"}])
            with self.assertRaisesRegex(ehentai.TelegraphError, "ACCESS_TOKEN_INVALID"):
                ehentai._telegraph_post("editPage/page", http_client=client, **kwargs)
            self.assertEqual(client.post.call_count, 1)
            client = NS(post=Mock(side_effect=TimeoutError("timeout")))
            with self.assertRaises(ehentai.TelegraphError):
                ehentai._telegraph_post("createPage", http_client=client, **kwargs)
            self.assertEqual(client.post.call_count, 1)

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
