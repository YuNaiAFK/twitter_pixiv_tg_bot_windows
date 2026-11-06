"""離線驗證 Pixiv 檔名、下載中繼資料與快取重送，不載入 bot 的憑證。"""
import ast
import asyncio
import importlib.util
import io
import json
import logging
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pixiv_filenames import (
    PIXIV_DOWNLOAD_FILENAME, PIXIV_FILENAME_VERSION, pixiv_filename,
    rename_pixiv_media, restore_pixiv_cached_files,
)


class PixivFilenameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.meta = {"id": 12345, "title": "夏日作品", "num": 0,
                     "count": 20, "user": {"id": 6789, "name": "畫師暱稱"}}
        tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        selected = {"build_gallery_dl_command", "download_with_gallery_dl",
                    "media_cache_payload", "send_cached_media", "collect_media_files",
                    "send_single_media", "send_original_files", "is_size_refusal"}
        self.ns = {
            "Path": Path, "shutil": shutil, "logger": logging.getLogger("pixiv-test"),
            "PIXIV_DOWNLOAD_FILENAME": PIXIV_DOWNLOAD_FILENAME,
            "PIXIV_FILENAME_VERSION": PIXIV_FILENAME_VERSION,
            "rename_pixiv_media": rename_pixiv_media,
            "restore_pixiv_cached_files": restore_pixiv_cached_files,
            "find_executable": lambda: "gallery-dl",
            "gallery_dl_config_path": lambda: None,
            "resolve_cookie_file": lambda platform: None,
            "COOKIES_FILE": "", "BASE_DIR": self.root,
            "TelegramError": RuntimeError, "source_link_options": lambda: None,
            "MAX_FILES": 50, "upload_limit_bytes": lambda: 2_000_000_000,
            "TimedOut": TimeoutError, "ALBUM_LIMIT": 10,
        }
        for node in tree.body:
            if (isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id in {
                        "IMAGE_EXTENSIONS", "VIDEO_EXTENSIONS", "ANIMATION_EXTENSIONS",
                        "MEDIA_EXTENSIONS", "PIXIV_MEDIA_EXTENSIONS"} for target in node.targets)):
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<bot-media-extensions>", "exec"), self.ns)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in selected:
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<bot-filename-flow>", "exec"), self.ns)

    def source(self, name="12345_p0.jpg", *, metadata=True):
        path = self.root / name
        path.write_bytes(b"unchanged original bytes")
        if metadata:
            path.with_name(path.name + ".json").write_text(
                json.dumps(self.meta, ensure_ascii=False), encoding="utf-8")
        return path

    def test_exact_requested_format(self):
        self.assertEqual(pixiv_filename(self.meta, ".jpg"),
                         "夏日作品_12345_p0_6789_畫師暱稱.jpg")

    def test_page_number_is_not_total_page_count(self):
        self.meta["num"] = 10
        self.assertIn("_p10_", pixiv_filename(self.meta, ".png"))
        self.assertNotIn("_p20_", pixiv_filename(self.meta, ".png"))

    def test_invalid_path_characters_are_replaced(self):
        self.meta["title"] = 'a/b\\c:*?"<>|\x00'
        self.meta["user"]["name"] = "artist/name"
        name = pixiv_filename(self.meta, ".webp")
        self.assertNotRegex(name, r'[\x00-\x1f<>:"/\\|?*]')
        self.assertTrue(name.endswith("_6789_artist_name.webp"))

    def test_long_unicode_names_keep_ids_page_and_extension(self):
        self.meta["title"] = "作品🌸" * 100
        self.meta["user"]["name"] = "畫師🎨" * 100
        name = pixiv_filename(self.meta, ".jpeg")
        self.assertLessEqual(len(name), 180)
        self.assertLessEqual(len(name.encode("utf-8")), 240)
        self.assertIn("_12345_p0_6789_", name)
        self.assertTrue(name.endswith(".jpeg"))

    def test_missing_artist_id_is_rejected(self):
        self.meta["user"].pop("id")
        with self.assertRaises(ValueError):
            pixiv_filename(self.meta, ".jpg")

    def test_rename_preserves_original_bytes(self):
        source = self.source()
        rename_pixiv_media([source])
        self.assertEqual((self.root / pixiv_filename(self.meta, ".jpg")).read_bytes(),
                         b"unchanged original bytes")
        self.assertFalse(source.exists())

    def test_existing_destination_is_not_overwritten(self):
        source = self.source()
        target = self.root / pixiv_filename(self.meta, ".jpg")
        target.write_bytes(b"existing")
        with self.assertRaises(ValueError):
            rename_pixiv_media([source])
        self.assertEqual(source.read_bytes(), b"unchanged original bytes")
        self.assertEqual(target.read_bytes(), b"existing")

    def test_cache_replay_restores_name_and_bytes(self):
        source = self.source("aabbccddeeff_001.jpg", metadata=False)
        scratch = self.root / "scratch"
        scratch.mkdir()
        name = pixiv_filename(self.meta, ".jpg")
        restored = restore_pixiv_cached_files([source], [name], scratch)
        self.assertEqual(restored[0].name, name)
        self.assertEqual(restored[0].read_bytes(), source.read_bytes())

    def test_cache_path_traversal_is_rejected(self):
        source = self.source(metadata=False)
        scratch = self.root / "scratch"
        scratch.mkdir()
        for name in ["../outside.jpg", "C:\\outside.jpg", "/outside.jpg"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                restore_pixiv_cached_files([source], [name], scratch)
        self.assertEqual(list(scratch.iterdir()), [])

    def test_only_pixiv_downloads_request_metadata_before_url_terminator(self):
        build = self.ns["build_gallery_dl_command"]
        pixiv = build("https://www.pixiv.net/artworks/12345", self.root, "pixiv")
        self.assertEqual(pixiv[pixiv.index("--filename") + 1], PIXIV_DOWNLOAD_FILENAME)
        self.assertLess(pixiv.index("--write-metadata"), pixiv.index("--"))
        twitter = build("https://x.com/u/status/1", self.root, "twitter")
        self.assertNotIn("--write-metadata", twitter)
        self.assertNotIn("--filename", twitter)

    def test_actual_download_flow_renames_before_sending(self):
        self.source()
        self.ns.update({
            "begin_download_diagnosis": lambda: None,
            "record_download_output": lambda output: None,
            "subprocess": SimpleNamespace(run=Mock(return_value=SimpleNamespace(
                returncode=0, stdout="", stderr=""))),
            "gallery_dl_env": lambda platform: {}, "MAX_DOWNLOAD_SECONDS": 60,
        })
        self.ns["download_with_gallery_dl"]("https://www.pixiv.net/artworks/12345", self.root, "pixiv")
        self.assertTrue((self.root / pixiv_filename(self.meta, ".jpg")).is_file())

    def test_ugoira_zip_download_is_converted_named_and_sent_as_animation(self):
        from PIL import Image

        source = self.root / "12345_p0.zip"
        with zipfile.ZipFile(source, "w") as archive:
            for name, color in [("000000.png", "red"), ("000001.png", "blue")]:
                buffer = io.BytesIO()
                with Image.new("RGB", (8, 8), color) as frame:
                    frame.save(buffer, format="PNG")
                archive.writestr(name, buffer.getvalue())
        self.meta["type"] = "ugoira"
        self.meta["frames"] = [{"file": "000000.png", "delay": 60},
                               {"file": "000001.png", "delay": 140}]
        source.with_name(source.name + ".json").write_text(json.dumps(self.meta), encoding="utf-8")
        self.ns.update({
            "begin_download_diagnosis": lambda: None,
            "record_download_output": lambda output: None,
            "subprocess": SimpleNamespace(run=Mock(return_value=SimpleNamespace(
                returncode=0, stdout="", stderr=""))),
            "gallery_dl_env": lambda platform: {}, "MAX_DOWNLOAD_SECONDS": 60,
        })
        self.ns["download_with_gallery_dl"]("pixiv-url", self.root, "pixiv")
        files = self.ns["collect_media_files"](self.root)
        self.assertEqual([path.name for path in files], [pixiv_filename(self.meta, ".gif")])
        self.assertFalse(source.exists())
        sent = []

        async def animation(**kwargs):
            sent.append((kwargs["filename"], kwargs["animation"].read()))

        message = SimpleNamespace(reply_animation=AsyncMock(side_effect=animation))
        self.assertTrue(asyncio.run(self.ns["send_single_media"](
            message, files[0], "pixiv-url", self.root, prefer_original=True)))
        self.assertEqual(sent[0][0], pixiv_filename(self.meta, ".gif"))
        with Image.open(io.BytesIO(sent[0][1])) as gif:
            self.assertEqual(gif.n_frames, 2)
            self.assertEqual(gif.info["loop"], 0)

    def test_legacy_ugoira_zip_cache_is_redownloaded_without_sending_zip(self):
        source = self.source("aabbccddeeff_001.zip", metadata=False)
        sender = AsyncMock()
        self.ns.update({"cached_media_files": lambda payload: [source],
                        "send_original_files": sender})
        payload = {"files": [source.name], "filenames": [pixiv_filename(self.meta, ".zip")],
                   "pixiv_filename_version": PIXIV_FILENAME_VERSION}
        self.assertFalse(asyncio.run(self.ns["send_cached_media"](
            None, None, "url", payload, "pixiv", "")))
        sender.assert_not_called()

    def test_large_gif_is_sent_unchanged_as_document_without_zipping(self):
        source = self.source("animation.gif", metadata=False)
        original = source.read_bytes()
        observed = []

        async def document(**kwargs):
            observed.append((kwargs["filename"], kwargs["document"].read()))

        message = SimpleNamespace(
            reply_animation=AsyncMock(side_effect=RuntimeError("file is too big")),
            reply_document=AsyncMock(side_effect=document),
        )
        result = asyncio.run(self.ns["send_original_files"](
            message, [source], "pixiv-url", self.root, attach_link=False))
        self.assertEqual(result, (1, "", []))
        self.assertEqual(observed, [(source.name, original)])

    def test_gif_document_refusal_does_not_trigger_zip_or_resize(self):
        source = self.source("animation.gif", metadata=False)
        message = SimpleNamespace(
            reply_animation=AsyncMock(side_effect=RuntimeError("file is too big")),
            reply_document=AsyncMock(side_effect=RuntimeError("file is too big")),
        )
        result = asyncio.run(self.ns["send_original_files"](
            message, [source], "pixiv-url", self.root, attach_link=False))
        self.assertEqual(result[0], 0)
        self.assertIn("未壓縮", result[1])
        self.assertEqual(result[2], [])

    def test_cache_payload_records_display_names_and_version(self):
        files = [Path("作品_12345_p0_6789_畫師.jpg")]
        self.ns["cache_media_files"] = lambda files, url: ["aabbccddeeff_001.jpg"]
        self.ns["detect_media_platform"] = lambda url: "pixiv"
        payload = self.ns["media_cache_payload"](files, "pixiv-url")
        self.assertEqual(payload["filenames"], [files[0].name])
        self.assertEqual(payload["pixiv_filename_version"], PIXIV_FILENAME_VERSION)
        self.ns["cache_media_files"] = lambda files, url: []
        self.assertEqual(self.ns["media_cache_payload"](files, "pixiv-url"), {"files": []})

    def test_legacy_pixiv_cache_is_redownloaded_before_any_send(self):
        sender = AsyncMock()
        self.ns["send_original_files"] = sender
        result = asyncio.run(self.ns["send_cached_media"](None, None, "url", {"files": ["old.jpg"]}, "pixiv", ""))
        self.assertFalse(result)
        sender.assert_not_called()

    def test_actual_cached_send_flow_receives_restored_filename(self):
        source = self.source("aabbccddeeff_001.jpg", metadata=False)
        scratch = self.root / "scratch"
        scratch.mkdir()
        observed = []

        async def send(message, files, url, directory, **kwargs):
            observed.extend((file.name, file.read_bytes()) for file in files)
            return 1, "", []

        self.ns.update({"cached_media_files": lambda payload: [source],
                        "make_work_dir": lambda: scratch, "send_original_files": send})
        payload = {"files": [source.name], "filenames": [pixiv_filename(self.meta, ".jpg")],
                   "pixiv_filename_version": PIXIV_FILENAME_VERSION}
        result = asyncio.run(self.ns["send_cached_media"](
            None, SimpleNamespace(edit_text=AsyncMock()), "url", payload, "pixiv", ""))
        self.assertTrue(result)
        self.assertEqual(observed, [(pixiv_filename(self.meta, ".jpg"), b"unchanged original bytes")])
        self.assertFalse(scratch.exists())

    @unittest.skipUnless(importlib.util.find_spec("gallery_dl"), "未安裝 gallery-dl")
    def test_gallery_dl_metadata_writer_matches_rename_input(self):
        from gallery_dl.postprocessor.metadata import MetadataPP

        source = self.source(metadata=False)
        job = SimpleNamespace(get_logger=logging.getLogger, register_hooks=lambda *args: None)
        writer = MetadataPP(job, {})
        writer.run(SimpleNamespace(filename=source.name, kwdict=self.meta,
                                   extended=False, realdirectory=str(self.root) + os.sep))
        rename_pixiv_media([source])
        self.assertTrue((self.root / pixiv_filename(self.meta, ".jpg")).is_file())


if __name__ == "__main__":
    unittest.main()
