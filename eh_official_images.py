"""Safely unpack the official EH 1280 ZIP without fetching viewer-page images."""
import re
import shutil
import stat
import zipfile
from pathlib import PurePosixPath

import ehentai
import r2

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif"}
MAX_ARCHIVE_IMAGES = 3000
MAX_UNPACKED = 4 * 1024 ** 3
DISK_RESERVE = 256 * 1024 ** 2


def _natural(name):
    return tuple((1, int(part)) if part.isdecimal() else (0, part.casefold())
                 for part in re.split(r"([0-9]+)", name))


def unpack(archive, directory, stop_event):
    """Return an ordered mapping of local references to (path, MIME)."""
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as source:
            entries = []
            names = set()
            total = 0
            for entry in source.infolist():
                name = PurePosixPath(entry.filename)
                if name.is_absolute() or ".." in name.parts or chr(92) in entry.orig_filename:
                    raise ehentai.EhentaiError("官方歸檔含不安全的檔案路徑。")
                if entry.is_dir() or "__MACOSX" in name.parts or name.suffix.lower() not in IMAGE_TYPES:
                    continue
                if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1 or name in names:
                    raise ehentai.EhentaiError("官方歸檔含連結、加密或重複的圖片檔案。")
                names.add(name)
                entries.append((name, entry))
                total += entry.file_size
                if (not 0 < entry.file_size <= r2.MAX_IMAGE_BYTES or len(entries) > MAX_ARCHIVE_IMAGES
                        or total > MAX_UNPACKED):
                    raise ehentai.EhentaiError("官方歸檔超過圖片解壓限制。")
            if not entries:
                raise ehentai.EhentaiError("官方 1280 歸檔沒有可用的圖片。")
            if total + DISK_RESERVE > shutil.disk_usage(directory).free:
                raise ehentai.EhentaiError("空間不足，無法解開官方 1280 歸檔。")
            result = {}
            for index, (name, entry) in enumerate(sorted(entries, key=lambda x: _natural(str(x[0]))), 1):
                if stop_event.is_set():
                    raise ehentai.EhentaiError("已停止解開官方歸檔。")
                suffix = name.suffix.lower()
                target = directory / f"{index:05d}{suffix}"
                with source.open(entry) as stream, target.open("xb") as output:
                    lead = stream.read(32)
                    if not ehentai._looks_like_an_image(lead):
                        raise ehentai.EhentaiError(f"官方歸檔第 {index} 張不是有效圖片。")
                    output.write(lead)
                    shutil.copyfileobj(stream, output, length=256 * 1024)
                result[target.resolve().as_uri()] = (target, IMAGE_TYPES[suffix])
            return result
    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError) as exc:
        if isinstance(exc, ehentai.EhentaiError):
            raise
        raise ehentai.EhentaiError("官方 1280 歸檔無法解開，沒有建立頁面。") from exc
