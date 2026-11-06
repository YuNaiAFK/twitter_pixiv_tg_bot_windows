"""依 Pixiv 的影格清單與延遲，將 Ugoira ZIP 合成可直接播放的 GIF。"""
import math
import os
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


MAX_FRAMES = 2000
MAX_FRAME_BYTES = 32 * 1024 * 1024
MAX_TOTAL_FRAME_BYTES = 512 * 1024 * 1024
MAX_FRAME_PIXELS = 24_000_000


def convert_ugoira_zip(source: Path, metadata: dict, destination: Path) -> None:
    """解壓記憶體內的影格並寫入 GIF；失敗時不留下部分成品，也不改動 ZIP。"""
    from PIL import GifImagePlugin, Image

    manifest = metadata.get("frames") or metadata.get("_ugoira_frame_data")
    if not isinstance(manifest, list) or not manifest or len(manifest) > MAX_FRAMES:
        raise ValueError("Pixiv 動圖缺少有效影格清單，或影格數超過處理上限")
    if destination.exists():
        raise ValueError("Pixiv 動圖檔名已存在，已停止以避免覆寫")

    partial = None
    original_size = None
    try:
        descriptor, partial_name = tempfile.mkstemp(
            prefix=".ugoira_", suffix=".gif.part", dir=destination.parent)
        os.close(descriptor)
        partial = Path(partial_name)
        with zipfile.ZipFile(source) as archive, partial.open("wb") as output:
            infos = archive.infolist()
            if len({info.filename for info in infos}) != len(infos):
                raise ValueError("Pixiv 動圖 ZIP 內含重複檔名")
            total_bytes = 0
            for entry in manifest:
                if not isinstance(entry, dict):
                    raise ValueError("Pixiv 動圖影格資料無效")
                name = entry.get("file")
                if (not isinstance(name, str) or not name or "\\" in name or "\x00" in name
                        or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts):
                    raise ValueError("Pixiv 動圖影格路徑無效")
                delay = entry.get("delay")
                if isinstance(delay, bool) or not isinstance(delay, (int, float)):
                    raise ValueError("Pixiv 動圖影格缺少有效播放時間")
                if not math.isfinite(delay) or not 0 <= delay <= 655_350:
                    raise ValueError("Pixiv 動圖影格播放時間無效")
                # GIF 時間單位為 10 毫秒；零延遲也使用最短的明確播放時間。
                duration = max(10, int((delay + 5) // 10) * 10)

                info = archive.getinfo(name)
                total_bytes += info.file_size
                if (info.is_dir() or info.file_size > MAX_FRAME_BYTES
                        or total_bytes > MAX_TOTAL_FRAME_BYTES):
                    raise ValueError("Pixiv 動圖解壓大小超過處理上限")
                # 不將 ZIP 路徑寫到磁碟；只解壓清單指定的圖片，避免目錄穿越與多餘檔案。
                with archive.open(info) as stream, Image.open(stream) as image:
                    width, height = image.size
                    if width * height > MAX_FRAME_PIXELS or max(width, height) > 65535:
                        raise ValueError("Pixiv 動圖單張影格尺寸超過處理上限")
                    if original_size is None:
                        original_size = image.size
                    elif image.size != original_size:
                        raise ValueError("Pixiv 動圖影格尺寸不一致")
                    rgba = image.convert("RGBA")
                try:
                    indexed = rgba.convert("P", palette=Image.Palette.ADAPTIVE)
                    try:
                        params = {"duration": duration, "disposal": 2,
                                  "include_color_table": True, "optimize": False}
                        # GIF 每幀最多 256 色；保留透明色，尺寸與影格數完全不縮減。
                        if indexed.palette and indexed.palette.mode == "RGBA":
                            for color, index in indexed.palette.colors.items():
                                if color[3] == 0:
                                    params["transparency"] = index
                                    break
                        if output.tell() == 0:
                            header_info = {"loop": 0, "optimize": False}
                            if "transparency" in params:
                                header_info["background"] = params["transparency"]
                            header, _ = GifImagePlugin.getheader(indexed, info=header_info)
                            for block in header:
                                output.write(block)
                        # 逐幀寫入，記憶體不隨總影格數累積；也避免 save_all 合併相同影格。
                        for block in GifImagePlugin.getdata(indexed, **params):
                            output.write(block)
                    finally:
                        indexed.close()
                finally:
                    rgba.close()
            output.write(b";")
        os.replace(partial, destination)
        partial = None
    finally:
        if partial is not None:
            partial.unlink(missing_ok=True)
