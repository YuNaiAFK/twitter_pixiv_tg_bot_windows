import ast
import asyncio
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import eh_official_images
import ehentai


JPEG = bytes.fromhex("ffd8ff") + b"test-image-data"


class OfficialImagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_resample_extract_filter_then_host(self):
        with tempfile.TemporaryDirectory() as temp:
            ns = dict(asyncio=asyncio, threading=threading, ehentai=ehentai,
                      source_link_options=lambda: None, stop_keyboard=lambda *a: None,
                      register_running_stop=Mock(), archive_cache_path=Mock(return_value=None),
                      _running_downloads={}, eh_stop_key=lambda chat: chat)
            resolve = Mock(return_value=("https://source.test/archive", "sample.zip", 123))
            ns["resolve_official_archive"] = resolve

            def download(url, path, **kwargs):
                path.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(path, "w") as zip_file:
                    for page in (10, 2, 1):
                        zip_file.writestr(f"{page}.jpg", JPEG + bytes([page]))

            async def progress(work, status, **kwargs):
                work(None)

            async def ads(items, gallery, status, *, stop_event, fetcher):
                self.assertEqual([fetcher(ref)[-1] for ref in items], [1, 2, 10])
                return items[:2]

            async def host(items, gallery, mode, status, **kwargs):
                self.assertTrue(kwargs["archive_only"])
                self.assertEqual([kwargs["download_cache"][ref][0].read_bytes()[-1]
                                  for ref in items], [1, 2])
                return ["https://img.test/1", "https://img.test/2"]

            ns.update(download_official_archive=Mock(side_effect=download), run_with_progress=progress,
                      remove_gallery_ads=ads, host_on_r2=host)
            source = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
            fn = next(node for node in source.body if getattr(node, "name", "") == "host_official_1280")
            exec(compile(ast.Module(body=[fn], type_ignores=[]), "<test>", "exec"), ns)
            gallery = NS(page_url="https://gallery.test/", gid="1", token="a")
            status = NS(chat_id=1, edit_text=AsyncMock())
            urls = await ns[fn.name](gallery, "view", status, Path(temp), 7)
            resolve.assert_called_once_with(gallery, resample=True)
            self.assertEqual(urls, ["https://img.test/1", "https://img.test/2"])

            ns["host_on_r2"] = AsyncMock(return_value=["file:///not-public", "https://img.test/2"])
            with self.assertRaisesRegex(ehentai.EhentaiError, "未全部存入"):
                await ns[fn.name](gallery, "view", status, Path(temp) / "second", 7)

    def test_traversal_and_bad_content_rejected(self):
        for name, data in (("../outside.jpg", JPEG), ("2.jpg", b"<html>not-image</html>")):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with zipfile.ZipFile(root / "test.zip", "w") as source:
                    source.writestr(name, data)
                with self.assertRaises(ehentai.EhentaiError):
                    eh_official_images.unpack(root / "test.zip", root / "images", threading.Event())
                self.assertFalse((root / "outside.jpg").exists())
