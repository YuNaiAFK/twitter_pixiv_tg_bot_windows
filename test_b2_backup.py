import ast
import asyncio
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import ehentai
import r2


class BackupStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_switch_at_threshold_or_mid_batch(self):
        nodes = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig")).body
        fn = next(n for n in nodes if isinstance(n, ast.AsyncFunctionDef) and n.name == "host_on_r2")
        primary = r2.R2Settings("account", "key", "secret", "r2", "https://r2.test")
        backup = r2.R2Settings("", "key", "secret", "b2", "https://b2.test", provider="B2")
        for start_full in (True, False):
            ns = dict(r2=r2, ehentai=ehentai, asyncio=asyncio, logger=logging.getLogger(__name__),
                      R2_UPLOAD_WORKERS=1, R2_UPLOAD_TIMEOUT=1,
                      cached_r2_urls=lambda *args: [], remember_r2_urls=Mock(),
                      source_link_options=lambda: None, stop_keyboard=lambda *args: None)
            exec(compile(ast.Module(body=[fn], type_ignores=[]), "<host>", "exec"), ns)
            def upload(urls, *, settings, **kwargs):
                if settings == primary:
                    return r2.UploadReport(budget_stopped=True, results=[
                        r2.UploadResult("a", public_url="https://r2.test/a"),
                        r2.UploadResult("b", error="已達 R2 免費額度上限")])
                return r2.UploadReport(results=[r2.UploadResult(u, public_url="https://b2.test/"+u) for u in urls])
            with (patch.object(r2, "r2_settings", return_value=primary),
                  patch.object(r2, "b2_settings", return_value=backup),
                  patch.object(r2, "limit_bytes", return_value=10),
                  patch.object(r2, "storage_usage", side_effect=lambda s: (10 if start_full and s == primary else 0, 0)),
                  patch.object(r2, "upload_with_storage_budget", side_effect=upload) as calls):
                result = await ns["host_on_r2"](["a", "b"], NS(page_url="https://e-hentai.org/g/1/abc/", gid="1", token="abc"),
                                                 "view", NS(edit_text=AsyncMock()))
                self.assertEqual(result, ["https://b2.test/a" if start_full else "https://r2.test/a", "https://b2.test/b"])
                self.assertEqual(calls.call_args.args[0], ["a", "b"] if start_full else ["b"])
                self.assertEqual(calls.call_args.kwargs["budget"], r2.B2_LIMIT_BYTES)

    async def test_refresh_retries_only_failed_source(self):
        tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "host_on_r2")
        primary = r2.R2Settings("account", "key", "secret", "r2", "https://r2.test")
        ns = dict(r2=r2, ehentai=ehentai, asyncio=asyncio, logger=logging.getLogger(__name__),
                  R2_UPLOAD_WORKERS=1, R2_UPLOAD_TIMEOUT=1, cached_r2_urls=lambda *a: [],
                  remember_r2_urls=Mock(), source_link_options=lambda: None, stop_keyboard=lambda *a: None)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<host>", "exec"), ns)
        first = r2.UploadReport(results=[r2.UploadResult("a", public_url="https://r2.test/a"),
                                        r2.UploadResult("b", error="ConnectTimeout", source_failed=True)])
        second = r2.UploadReport(results=[r2.UploadResult("new-b", public_url="https://r2.test/b")])
        refresh = AsyncMock(return_value=["new-a", "new-b"])
        with (patch.object(r2, "r2_settings", return_value=primary),
              patch.object(r2, "b2_settings", return_value=None),
              patch.object(r2, "bucket_usage", return_value=(0, 0)),
              patch.object(r2, "upload_many", side_effect=[first, second]) as upload):
            result = await ns[fn.name](["a", "b"], NS(page_url="url", gid="1", token="abc"),
                                       "view", NS(edit_text=AsyncMock()), refresh_source=refresh)
        self.assertEqual(result, ["https://r2.test/a", "https://r2.test/b"])
        refresh.assert_awaited_once()
        self.assertEqual(upload.call_args.args[0], ["new-b"])

    async def test_refresh_preserves_filtered_page_positions(self):
        tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "refresh_eh_upload_sources")
        sweep = AsyncMock(return_value=(["n1", "n2", "n3"], []))
        ns = dict(gallery_page_urls=sweep, public_embed_urls=AsyncMock(return_value=["n1", "n2", "n3"]))
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<refresh>", "exec"), ns)
        self.assertEqual(await ns[fn.name](None, None, ["a", "b", "c"], ["a", "c"], None), ["n1", "n3"])
        self.assertTrue(sweep.call_args.kwargs["force"])

    def test_b2_keys_and_region(self):
        values = dict(B2_ENDPOINT="https://s3.us-west-004.backblazeb2.com", B2_BUCKET="bucket",
                      B2_KEY_ID="key", B2_APPLICATION_KEY="secret", B2_PUBLIC_BASE="https://files.test/file/bucket")
        with patch.dict("os.environ", values):
            settings = r2.b2_settings()
            self.assertEqual(settings.region, "us-west-004")
            self.assertEqual(settings.public_url("galleries/a"), "https://files.test/file/bucket/galleries/a")
        with patch.dict("os.environ", {**values, "B2_APPLICATION_KEY": ""}):
            with self.assertRaises(r2.R2Error):
                r2.b2_settings()
