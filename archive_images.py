"""以 libarchive 串流讀取 7z／RAR 圖片，不寫入封存檔提供的路徑。"""
import os
from pathlib import Path, PurePosixPath


def archive_library():
    if os.name == "nt" and not os.environ.get("LIBARCHIVE"):
        native = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/archiveint.dll"
        if native.is_file():
            os.environ["LIBARCHIVE"] = str(native)
    try:
        import libarchive
        return libarchive
    except (ImportError, OSError) as exc:
        raise ValueError("缺少 libarchive 解壓元件，請安裝 libarchive-c 與系統 libarchive。") from exc


def read_images(source, suffixes, *, max_files, max_image_bytes, max_total_bytes):
    library = archive_library()
    from libarchive.exception import ArchiveError
    seen = set()
    count = total = 0
    try:
        with library.file_reader(str(source)) as archive:
            for entry in archive:
                name = entry.pathname
                if not isinstance(name, str):
                    raise ValueError("壓縮檔內的檔名無法解碼")
                path = PurePosixPath(name)
                if (path.is_absolute() or ".." in path.parts or "\\" in name or "\x00" in name
                        or (path.parts and ":" in path.parts[0])):
                    raise ValueError("壓縮檔內含不安全的路徑")
                if name in seen:
                    raise ValueError("壓縮檔內有重複檔名")
                seen.add(name)
                if entry.issym or entry.islnk:
                    raise ValueError("壓縮檔不支援符號連結或硬連結")
                if entry.isdir or "__MACOSX" in path.parts or path.suffix.lower() not in suffixes:
                    continue
                if not entry.isfile:
                    raise ValueError("壓縮檔內含不支援的圖片項目")
                count += 1
                if count > max_files:
                    raise ValueError("每份作品最多 500 張圖片，請分批製作")
                if entry.size is not None and (entry.size > max_image_bytes or total + entry.size > max_total_bytes):
                    raise ValueError("圖片解壓大小超過上限")
                data = bytearray()
                for block in entry.get_blocks():
                    if len(data) + len(block) > max_image_bytes or total + len(data) + len(block) > max_total_bytes:
                        raise ValueError("圖片解壓大小超過上限")
                    data.extend(block)
                total += len(data)
                yield name, bytes(data)
    except ArchiveError as exc:
        raise ValueError("7z／RAR 解壓失敗，請確認檔案完整且未加密；分卷請先合併為單一壓縮檔。") from exc
