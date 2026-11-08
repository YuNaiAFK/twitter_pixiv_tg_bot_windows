"""將使用者上傳的圖片或 ZIP／CBZ 製作為 R2 圖片與 Telegraph。"""
import asyncio
import html
import io
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
MAX_FILES = 500
MAX_INPUT_BYTES = 512 * 1024 * 1024
MAX_FRAME_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_PIXELS = 40_000_000


def natural_key(name):
    return tuple((1, int(part), len(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r"(\d+)", str(name)))


def prepare_image(data, destination):
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "WEBP", "GIF", "BMP")) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("單張圖片超過 4000 萬像素")
        oriented = ImageOps.exif_transpose(image)
        try:
            oriented.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            with oriented.convert("RGBA") as rgba, Image.new("RGB", oriented.size, "white") as output:
                output.paste(rgba, mask=rgba.getchannel("A"))
                output.save(destination, format="JPEG", quality=85, optimize=True)
        finally:
            oriented.close()


def prepare_inputs(records, output_dir):
    """回傳（排序用原始檔名，壓縮副本）；ZIP 路徑完全不寫入檔案系統。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    expanded = 0
    for name, source in records:
        if Path(name).suffix.lower() in {".zip", ".cbz"}:
            with zipfile.ZipFile(source) as archive:
                infos = archive.infolist()
                if len({info.filename for info in infos}) != len(infos):
                    raise ValueError("壓縮檔內有重複檔名")
                for info in infos:
                    path = PurePosixPath(info.filename)
                    if path.is_absolute() or ".." in path.parts or "\\" in info.orig_filename or "\x00" in info.orig_filename:
                        raise ValueError("壓縮檔內含不安全的路徑")
                    if stat.S_ISLNK(info.external_attr >> 16):
                        raise ValueError("壓縮檔不支援符號連結")
                    if info.is_dir() or "__MACOSX" in path.parts or path.suffix.lower() not in IMAGE_SUFFIXES:
                        continue
                    if info.flag_bits & 1:
                        raise ValueError("不支援加密的圖片壓縮檔")
                    expanded += info.file_size
                    if info.file_size > MAX_FRAME_BYTES or expanded > MAX_EXPANDED_BYTES:
                        raise ValueError("圖片解壓大小超過上限")
                    if len(entries) >= MAX_FILES:
                        raise ValueError("每份作品最多 500 張圖片，請分批製作")
                    target = output_dir / f"{len(entries):05}.jpg"
                    with archive.open(info) as stream:
                        data = stream.read(MAX_FRAME_BYTES + 1)
                    if len(data) > MAX_FRAME_BYTES:
                        raise ValueError("單張圖片超過大小上限")
                    prepare_image(data, target)
                    entries.append((info.filename, target))
        else:
            if source.stat().st_size > MAX_FRAME_BYTES:
                raise ValueError("單張圖片超過 64 MB，請縮小後再傳送")
            if len(entries) >= MAX_FILES:
                raise ValueError("每份作品最多 500 張圖片")
            target = output_dir / f"{len(entries):05}.jpg"
            prepare_image(source.read_bytes(), target)
            entries.append((name, target))
    if not entries:
        raise ValueError("沒有找到支援的圖片")
    return sorted(entries, key=lambda entry: natural_key(entry[0]))


def page_content(title, urls):
    nodes = [{"tag": "h3", "children": [title]}]
    nodes.extend({"tag": "img", "attrs": {"src": url}} for url in urls)
    content = json.dumps(nodes, ensure_ascii=False)
    if len(content.encode("utf-8")) > 62 * 1024:
        raise ValueError("圖片數量使頁面超過 Telegraph 上限，請分批製作")
    return content


def create_page(title, urls):
    import ehentai
    import httpx
    content = page_content(title, urls)
    with httpx.Client(**ehentai.http_client_kwargs(60)) as client:
        account = client.post(f"{ehentai.TELEGRAPH_API}/createAccount", data={"short_name": "Local files"}).json()
        if not account.get("ok"):
            raise ValueError("建立 Telegraph 帳號失敗")
        return ehentai._telegraph_post("createPage", access_token=account["result"]["access_token"],
            title=title, author_name="本機檔案", author_url="", content=content, timeout=60,
            http_client=client)


def result_text(title, url):
    return f'<b>{html.escape(title)}</b>\n\n[ <a href="{html.escape(url, quote=True)}">即時預覽</a> ] / [ 作者 ] / [ E站 ]'


@dataclass
class Batch:
    root: Path
    title: str = ""
    records: list = field(default_factory=list)
    pending: int = 0
    busy: bool = False
    creating_page: bool = False
    page_url: str = ""
    cover: Path | None = None
    total_bytes: int = 0
    reserved_bytes: int = 0
    touched: float = field(default_factory=time.monotonic)
    stop: threading.Event = field(default_factory=threading.Event)


def cleanup_batch(batch):
    # root 僅由 make_work_dir 建立，不使用上傳檔名或 ZIP 路徑。
    shutil.rmtree(batch.root, ignore_errors=True)


async def send_result(message, batch):
    # 使用壓縮後的第一張圖片；以檔案內容上傳，兼容 Docker／proot Bot API。
    with batch.cover.open("rb") as photo:
        await message.reply_photo(photo=photo, caption=result_text(batch.title, batch.page_url),
                                  parse_mode="HTML")


async def download_file(media, target, runtime):
    remote = await media.get_file()
    raw = remote.file_path or ""
    # PTB 將主機上不存在的容器絕對路徑誤當成下載相對路徑，補上 file/bot 前綴。
    if raw.startswith(("http://", "https://")):
        remote_path = unquote(urlsplit(raw).path)
        for prefix in ("/root/telegram-bot-api/", "/var/lib/telegram-bot-api/"):
            position = remote_path.find(prefix)
            if position >= 0:
                raw = remote_path[position:]
                break
    path = Path(unquote(urlsplit(raw).path)) if raw.startswith("file://") else Path(raw)
    if raw and not raw.startswith(("http://", "https://")):
        candidates = [path]
        # Termux 的 Bot API 位於 proot，getFile 的絕對路徑須映射到 rootfs。
        if raw.startswith("/root/telegram-bot-api/"):
            prefix = Path(os.getenv("PREFIX", "/data/data/com.termux/files/usr"))
            base = prefix / "var/lib/proot-distro"
            name = os.getenv("CHROOT_NAME", "botapi")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError("CHROOT_NAME 設定無效")
            relative = PurePosixPath(raw).relative_to("/")
            if ".." in relative.parts:
                raise ValueError("Bot API 回傳的檔案路徑無效")
            candidates.extend([base.joinpath("containers", name, "rootfs", *relative.parts),
                               base.joinpath("installed-rootfs", name, *relative.parts)])
        for candidate in candidates:
            if candidate.is_file():
                await asyncio.to_thread(shutil.copyfile, candidate, target)
                return
        if os.name == "nt" and raw.startswith("/var/lib/telegram-bot-api/"):
            # Windows 的具名 Docker volume 不在主機檔案系統中。
            if ".." in PurePosixPath(raw).parts:
                raise ValueError("Bot API 回傳的檔案路徑無效")
            result = await asyncio.to_thread(subprocess.run,
                ["docker", "cp", f"telegram-bot-api:{raw}", str(target)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
            if result.returncode:
                raise ValueError("無法從本機 Bot API 容器取得檔案，請確認 Docker 服務")
            return
    await remote.download_to_drive(custom_path=target)


def register(application, runtime):
    from telegram.ext import CommandHandler, MessageHandler, filters
    batches = {}

    def owner(update):
        message, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if not message or not user or not chat or not runtime["user_is_allowed"](user.id, chat.id, chat.type):
            return None
        return (chat.id, user.id)

    def sweep():
        for key, batch in list(batches.items()):
            if not batch.busy and not batch.pending and time.monotonic() - batch.touched > 86400:
                cleanup_batch(batches.pop(key))

    async def collect(update, context):
        message = update.effective_message
        # 保留原本「圖片說明中附作品連結」的解析行為。
        if any(runtime["detect_media_platform"](url) or runtime["ehentai"].is_gallery_url(url)
               for url in runtime["extract_urls"](message.caption or "")):
            return await runtime["handle_message"](update, context)
        key = owner(update)
        if key is None:
            return
        sweep()
        batch = batches.get(key)
        if batch and batch.busy:
            await message.reply_text("這份作品正在製作，請完成後再傳送新檔案。")
            return
        if not batch:
            if len(batches) >= 32:
                await message.reply_text("目前待製作作品較多，請稍後再試。")
                return
            batch = batches[key] = Batch(runtime["make_work_dir"]())
        media = message.document or message.photo[-1]
        name = getattr(media, "file_name", None) or f"圖片_{message.message_id}.jpg"
        name = name.replace("\\", "/").rsplit("/", 1)[-1]
        if not batch.title:
            batch.title = Path(name).stem or "未命名作品"
        size = getattr(media, "file_size", 0) or 0
        if batch.total_bytes + batch.reserved_bytes + size > MAX_INPUT_BYTES or len(batch.records) + batch.pending >= MAX_FILES:
            await message.reply_text("檔案超過 512 MB 或已達 500 個檔案上限，請分批製作。")
            return
        if shutil.disk_usage(batch.root).free < size + 128 * 1024 * 1024:
            await message.reply_text("暫存空間不足，請清理空間後再傳送。")
            return
        batch.pending += 1
        batch.reserved_bytes += size
        target = batch.root / f"input_{os.urandom(8).hex()}"
        status = None
        try:
            status = await message.reply_text(f"正在接收：{name}")
            await download_file(media, target, runtime)
            actual_size = target.stat().st_size
            if batch.total_bytes + actual_size > MAX_INPUT_BYTES:
                raise ValueError("下載檔案超過大小上限")
            batch.records.append((name, target))
            batch.total_bytes += actual_size
            batch.touched = time.monotonic()
        except Exception as exc:
            target.unlink(missing_ok=True)
            logger.warning("本機檔案接收失敗：%s", runtime["redact_secrets"](str(exc)))
            if status is not None:
                await status.edit_text("檔案接收失敗，請重新傳送或查看管理員日誌。")
        finally:
            batch.pending -= 1
            batch.reserved_bytes -= size
        if any(path == target for _, path in batch.records) and status is not None:
            await status.edit_text(f"已接收 {len(batch.records)} 個檔案。傳完後輸入 /publish 製作，或 /cancel_upload 取消。")

    async def cancel(update, context):
        key = owner(update)
        if key is None:
            return
        batch = batches.get(key)
        if not batch:
            return await update.effective_message.reply_text("沒有待製作的本機檔案。")
        if batch.pending:
            return await update.effective_message.reply_text("檔案仍在接收，請接收完成後再取消。")
        if batch.creating_page:
            return await update.effective_message.reply_text("Telegraph 頁面正在完成，請稍候。")
        batch.stop.set()
        if batch.busy:
            for entry in runtime["_pending_r2_decisions"].values():
                if entry["chat_id"] == key[0] and entry["user_id"] == key[1] and not entry["future"].done():
                    entry["future"].set_result("stop")
        else:
            cleanup_batch(batches.pop(key))
        await update.effective_message.reply_text("已取消本機檔案製作。")

    async def publish(update, context):
        key = owner(update)
        if key is None:
            return
        batch = batches.get(key)
        message = update.effective_message
        if not batch or not batch.records:
            return await message.reply_text("請先傳送 ZIP／CBZ、圖片或圖片文件，再輸入 /publish。")
        if batch.pending or batch.busy:
            return await message.reply_text("檔案仍在接收或製作中，請稍候。")
        with runtime["RequestGuard"](key[1]) as guard:
            if not guard.ok:
                return await message.reply_text(runtime["busy_message"](key[1]))
            batch.busy = True
            batch.stop.clear()
            acquired = False
            status = None
            try:
                status = await message.reply_text("正在等待製作並整理圖片……")
                await runtime["gallery_semaphore"].acquire()
                acquired = True
                if batch.stop.is_set():
                    raise ValueError("已停止製作")
                if batch.page_url:
                    await send_result(message, batch)
                    batches.pop(key, None)
                    cleanup_batch(batch)
                    await runtime["cleanup_request_messages"](status)
                    return
                import r2
                settings = r2.r2_settings()
                if settings is None:
                    raise ValueError("尚未設定 R2，無法製作本機檔案預覽")
                entries = await asyncio.to_thread(prepare_inputs, batch.records, batch.root / "prepared")
                batch.cover = entries[0][1]
                lookup_failures = 0
                while True:
                    if batch.stop.is_set():
                        raise ValueError("已停止製作")
                    try:
                        used, _ = await asyncio.to_thread(r2.bucket_usage, settings)
                        break
                    except Exception:
                        lookup_failures += 1
                        await status.edit_text(f"R2 用量讀取失敗（第 {lookup_failures}/3 次），正在重試……")
                        if lookup_failures >= 3:
                            choice = await runtime["request_r2_retry_decision"](status, key[1], len(entries),
                                failure_reason="用量讀取", task_name="本機檔案製作")
                            if choice == "stop":
                                raise ValueError("已停止製作")
                            lookup_failures = 0
                        else:
                            await asyncio.sleep(2 ** lookup_failures)
                urls = [None] * len(entries)
                pending = list(range(len(entries)))
                failures = 0
                while pending:
                    if batch.stop.is_set():
                        raise ValueError("已停止製作，沒有建立 Telegraph 頁面")
                    await status.edit_text(f"正在上傳 {len(pending)} 張 1280px 圖片到 R2……")
                    try:
                        report = await asyncio.to_thread(r2.upload_many, pending, settings=settings,
                            workers=runtime["R2_UPLOAD_WORKERS"], timeout=runtime["R2_UPLOAD_TIMEOUT"],
                            already_used=used, budget=r2.limit_bytes(), stop_event=batch.stop,
                            image_fetcher=lambda index: (entries[index][1].read_bytes(), "image/jpeg"))
                    except Exception as exc:
                        logger.warning("本機圖片 R2 上傳失敗：%s", runtime["redact_secrets"](str(exc)))
                        report = r2.UploadReport(total=len(pending), failed=len(pending),
                            results=[r2.UploadResult(index, error="上傳失敗") for index in pending])
                    used += report.bytes_sent
                    next_pending = []
                    for position, index in enumerate(pending):
                        result = report.results[position] if position < len(report.results) else None
                        if result is None or result.error or not result.public_url:
                            next_pending.append(index)
                        else:
                            urls[index] = result.public_url
                    pending = next_pending
                    if report.budget_stopped:
                        raise ValueError("R2 額度不足，已停止製作，沒有建立不完整頁面")
                    if pending:
                        failures += 1
                        await status.edit_text(f"R2 上傳失敗，尚有 {len(pending)} 張未完成（第 {failures}/3 次），正在重試。")
                        if failures >= 3:
                            choice = await runtime["request_r2_retry_decision"](status, key[1], len(pending),
                                failure_reason="本機圖片上傳", task_name="本機檔案製作")
                            if choice == "stop":
                                raise ValueError("已停止製作，沒有建立 Telegraph 頁面")
                            failures = 0
                        else:
                            await asyncio.sleep(2 ** failures)
                if batch.stop.is_set():
                    raise ValueError("已停止製作，沒有建立 Telegraph 頁面")
                if not all(urls):
                    raise ValueError("圖片上傳尚未完成，沒有建立不完整頁面")
                await status.edit_text("圖片已完成上傳，正在建立 Telegraph……")
                batch.creating_page = True
                page = await asyncio.to_thread(create_page, batch.title, urls)
                batch.page_url = page.url
                await send_result(message, batch)
                batches.pop(key, None)
                cleanup_batch(batch)
                await runtime["cleanup_request_messages"](status)
            except Exception as exc:
                logger.warning("本機檔案製作失敗：%s", runtime["redact_secrets"](str(exc)))
                if status is not None:
                    await status.edit_text(f"製作未完成：{runtime['redact_secrets'](str(exc))}\n可輸入 /publish 重試，或 /cancel_upload 取消。", reply_markup=None)
                if batch.stop.is_set():
                    batches.pop(key, None)
                    cleanup_batch(batch)
            finally:
                if acquired:
                    runtime["gallery_semaphore"].release()
                batch.busy = False
                batch.creating_page = False
                batch.touched = time.monotonic()

    image_filter = filters.PHOTO | filters.Document.IMAGE
    for extension in sorted(IMAGE_SUFFIXES | {".zip", ".cbz"}):
        image_filter |= filters.Document.FileExtension(extension.lstrip("."))
    application.add_handler(CommandHandler("publish", publish))
    application.add_handler(CommandHandler("cancel_upload", cancel))
    application.add_handler(MessageHandler(image_filter, collect))
    previous_shutdown = application.post_shutdown

    async def shutdown(app):
        for batch in batches.values():
            cleanup_batch(batch)
        batches.clear()
        if previous_shutdown is not None:
            await previous_shutdown(app)

    application.post_shutdown = shutdown
