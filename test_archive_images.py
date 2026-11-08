"""使用實際 7z 與自製未壓縮 RAR4 圖片封存檔的離線測試。"""
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from archive_images import archive_library
import local_uploads as uploads
from test_local_uploads import picture


def rar_bytes(members):
    # RAR 4.x store 格式；只在測試建立不含加密或分卷的圖片封存檔。
    def header(kind, flags, body=b""):
        payload = struct.pack("<BHH", kind, flags, 7 + len(body)) + body
        return struct.pack("<H", zlib.crc32(payload) & 0xffff) + payload
    output = bytearray(b"Rar!\x1a\x07\x00" + header(0x73, 0, b"\0" * 6))
    for name, data in members:
        name = name.encode("ascii")
        body = struct.pack("<IIBIIBBHI", len(data), len(data), 3, zlib.crc32(data),
                           0x00210000, 20, 0x30, len(name), 0o100644) + name
        output.extend(header(0x74, 0x8000, body) + data)
    output.extend(header(0x7b, 0))
    return bytes(output)


class ArchiveImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def seven_zip(self, members):
        path = self.root / "download"
        with archive_library().file_writer(str(path), "7zip") as archive:
            for name, data in members:
                archive.add_file_from_memory(name, len(data), data)
        return path

    def test_real_7z_images_and_unicode_names_are_prepared_in_order(self):
        source = self.seven_zip([("10.png", picture(color="blue")), ("2.png", picture()),
                                 ("目錄/3.png", picture(color="green"))])
        entries = uploads.prepare_inputs([("作品.7z", source)], self.root / "out")
        self.assertEqual([name for name, _ in entries], ["2.png", "10.png", "目錄/3.png"])
        with Image.open(entries[0][1]) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertGreater(image.getpixel((0, 0))[0], 240)

    def test_real_rar_images_are_prepared_and_original_unchanged(self):
        data = rar_bytes([("10.png", picture(color="blue")), ("2.png", picture())])
        source = self.root / "download"
        source.write_bytes(data)
        entries = uploads.prepare_inputs([("作品.rar", source)], self.root / "out")
        self.assertEqual([name for name, _ in entries], ["2.png", "10.png"])
        self.assertEqual(source.read_bytes(), data)
        with Image.open(entries[0][1]) as image:
            self.assertGreater(image.getpixel((0, 0))[0], 240)

    def test_7z_unsafe_paths_and_duplicate_names_are_rejected(self):
        source = self.seven_zip([("../outside.png", picture())])
        with self.assertRaisesRegex(ValueError, "路徑"):
            uploads.prepare_inputs([("work.7z", source)], self.root / "out")
        source = self.seven_zip([("1.png", picture()), ("1.png", picture())])
        with self.assertRaisesRegex(ValueError, "重複"):
            uploads.prepare_inputs([("work.7z", source)], self.root / "out")

    def test_rar_limits_and_invalid_archives_fail_before_upload(self):
        source = self.root / "download"
        source.write_bytes(rar_bytes([("1.png", picture()), ("2.png", picture())]))
        with patch.object(uploads, "MAX_FILES", 1), self.assertRaisesRegex(ValueError, "500"):
            uploads.prepare_inputs([("work.rar", source)], self.root / "out")
        with patch.object(uploads, "MAX_FRAME_BYTES", 1), self.assertRaisesRegex(ValueError, "上限"):
            uploads.prepare_inputs([("work.rar", source)], self.root / "out")
        source.write_bytes(b"not an archive")
        with self.assertRaisesRegex(ValueError, "解壓失敗"):
            uploads.prepare_inputs([("work.rar", source)], self.root / "out")
