"""離線驗證 Ugoira 的影格順序、播放時間、透明色與 ZIP 失敗處理。"""
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import GifImagePlugin, Image
import pixiv_ugoira


class UgoiraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.zip"
        self.target = self.root / "animation.gif"
        self.manifest = [{"file": "red.png", "delay": 60},
                         {"file": "blue.png", "delay": 140}]

    def archive(self, *, size=(8, 8), transparent=False):
        with zipfile.ZipFile(self.source, "w") as archive:
            # ZIP 的實際排列故意與播放清單相反。
            for name, color in [("blue.png", "blue"), ("red.png", "red")]:
                mode = "RGBA" if transparent else "RGB"
                with Image.new(mode, size, color) as frame:
                    if transparent:
                        frame.putpixel((0, 0), (0, 0, 0, 0))
                    buffer = io.BytesIO()
                    frame.save(buffer, format="PNG")
                archive.writestr(name, buffer.getvalue())

    def convert(self):
        pixiv_ugoira.convert_ugoira_zip(self.source, {"frames": self.manifest}, self.target)

    def assert_no_partial(self):
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.glob("*.part")), [])
        self.assertTrue(self.source.exists())

    def test_frame_order_timing_and_loop(self):
        self.archive()
        original = self.source.read_bytes()
        self.convert()
        with Image.open(self.target) as gif:
            self.assertEqual(gif.n_frames, 2)
            self.assertEqual(gif.info["loop"], 0)
            self.assertEqual(gif.info["duration"], 60)
            self.assertEqual(gif.convert("RGB").getpixel((1, 1)), (255, 0, 0))
            gif.seek(1)
            self.assertEqual(gif.info["duration"], 140)
            self.assertEqual(gif.convert("RGB").getpixel((1, 1)), (0, 0, 255))
        self.assertEqual(self.source.read_bytes(), original)

    def test_transparent_pixels_survive(self):
        self.archive(transparent=True)
        self.convert()
        with Image.open(self.target) as gif:
            for index in range(gif.n_frames):
                gif.seek(index)
                self.assertEqual(gif.convert("RGBA").getpixel((0, 0))[3], 0)

    def test_original_resolution_is_preserved(self):
        self.archive(size=(1920, 1080))
        self.convert()
        with Image.open(self.target) as gif:
            self.assertEqual(gif.size, (1920, 1080))
            self.assertEqual(gif.n_frames, 2)
            gif.seek(1)
            self.assertEqual(gif.size, (1920, 1080))

    def test_identical_frames_are_not_merged_or_dropped(self):
        self.archive()
        self.manifest[1]["file"] = "red.png"
        self.convert()
        with Image.open(self.target) as gif:
            self.assertEqual(gif.n_frames, 2)
            self.assertEqual(gif.info["duration"], 60)
            gif.seek(1)
            self.assertEqual(gif.info["duration"], 140)
            self.assertEqual(gif.convert("RGB").getpixel((1, 1)), (255, 0, 0))

    def test_missing_frame_leaves_no_output(self):
        self.archive()
        self.manifest[1]["file"] = "missing.png"
        with self.assertRaises(KeyError):
            self.convert()
        self.assert_no_partial()

    def test_path_traversal_is_rejected(self):
        self.archive()
        self.manifest[0]["file"] = "../outside.png"
        with self.assertRaises(ValueError):
            self.convert()
        self.assert_no_partial()
        self.assertFalse((self.root.parent / "outside.png").exists())

    def test_invalid_delay_is_rejected(self):
        self.archive()
        for delay in (-1, float("nan"), "60", True):
            with self.subTest(delay=delay):
                self.manifest[0]["delay"] = delay
                with self.assertRaises(ValueError):
                    self.convert()
                self.assert_no_partial()

    def test_zero_delay_has_explicit_gif_duration(self):
        self.archive()
        self.manifest[0]["delay"] = 0
        self.convert()
        with Image.open(self.target) as gif:
            self.assertEqual(gif.info["duration"], 10)

    def test_excessive_uncompressed_size_is_rejected(self):
        self.archive()
        with patch.object(pixiv_ugoira, "MAX_TOTAL_FRAME_BYTES", 1):
            with self.assertRaises(ValueError):
                self.convert()
        self.assert_no_partial()

    def test_existing_output_is_not_overwritten(self):
        self.archive()
        self.target.write_bytes(b"existing")
        with self.assertRaises(ValueError):
            self.convert()
        self.assertEqual(self.target.read_bytes(), b"existing")

    def test_encoding_failure_cleans_partial_output(self):
        self.archive()
        with patch.object(GifImagePlugin, "getdata", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.convert()
        self.assert_no_partial()


if __name__ == "__main__":
    unittest.main()
