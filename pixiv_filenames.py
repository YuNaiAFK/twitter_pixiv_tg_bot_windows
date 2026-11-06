"""Pixiv 原檔命名與快取重送；只變更檔名，不修改檔案內容。"""
import json
import re
import shutil
from pathlib import Path


PIXIV_FILENAME_VERSION = 1
PIXIV_DOWNLOAD_FILENAME = "{id}_p{num}.{extension}"
_UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')


def _text(value, fallback):
    return _UNSAFE.sub("_", str(value or "")).strip(" .") or fallback


def _number(value, label, *, allow_zero=False):
    text = str(value)
    if not re.fullmatch(r"[0-9]+", text) or (not allow_zero and int(text) == 0):
        raise ValueError(f"Pixiv 中繼資料缺少有效的{label}")
    return str(int(text))


def pixiv_filename(metadata: dict, suffix: str) -> str:
    """作品名稱_作品ID_p頁碼_畫師ID_畫師暱稱，加上原始副檔名。"""
    user = metadata.get("user") or {}
    work_id = _number(metadata.get("id"), "作品 ID")
    page = _number(metadata.get("num"), "頁碼", allow_zero=True)
    artist_id = _number(user.get("id"), "畫師 ID")
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
        raise ValueError("Pixiv 檔案的副檔名無效")
    title = _text(metadata.get("title"), "未命名作品")
    artist = _text(user.get("name"), "未命名畫師")

    def render():
        return f"{title}_{work_id}_p{page}_{artist_id}_{artist}{suffix}"

    # 同時符合 Windows 的檔名長度與 Android 的 UTF-8 位元組限制；保留 ID、頁碼與副檔名。
    name = render()
    while len(name) > 180 or len(name.encode("utf-8")) > 240:
        if len(title) >= len(artist) and len(title) > 1:
            title = title[:-1]
        elif len(artist) > 1:
            artist = artist[:-1]
        else:
            raise ValueError("Pixiv 的作品或畫師 ID 過長")
        name = render()
    return name


def rename_pixiv_media(files) -> None:
    """使用 gallery-dl 同次下載產生的中繼資料命名，無須額外查詢 Pixiv。"""
    plan = []
    targets = set()
    for source in files:
        metadata_path = source.with_name(source.name + ".json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        target = source.with_name(pixiv_filename(metadata, source.suffix))
        identity = str(target).casefold()
        if identity in targets or (target != source and target.exists()):
            raise ValueError("Pixiv 檔名重複，已停止以避免覆寫圖片")
        targets.add(identity)
        plan.append((source, target))
    # 所有命名都確認有效後才開始變更，避免中途資料缺漏造成部分檔案改名。
    for source, target in plan:
        if source != target:
            source.rename(target)


def restore_pixiv_cached_files(files: list[Path], names, scratch: Path) -> list[Path]:
    """將安全的快取檔複製到工作目錄，還原使用者下載時看見的檔名。"""
    if not isinstance(names, list) or len(names) != len(files):
        raise ValueError("Pixiv 快取缺少原始檔名")
    targets = []
    used = set()
    for source, name in zip(files, names):
        if (not isinstance(name, str) or _text(name, "") != name
                or not name or len(name) > 180 or len(name.encode("utf-8")) > 240
                or Path(name).suffix.lower() != source.suffix.lower()
                or name.casefold() in used):
            raise ValueError("Pixiv 快取中的檔名無效")
        target = scratch / name
        if target.resolve().parent != scratch.resolve() or target.exists():
            raise ValueError("Pixiv 快取檔名超出工作目錄或已存在")
        used.add(name.casefold())
        targets.append(target)
    for source, target in zip(files, targets):
        shutil.copy2(source, target)
    return targets
