"""使用 MTProto 保留 GIF 檔名，並以一般文件 MIME 傳送。"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, unquote

_send_lock = asyncio.Lock()
logger = logging.getLogger(__name__)


def document_key(path, bot_id):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return hashlib.sha256(f"{bot_id}:{path.name}:{digest.hexdigest()}".encode()).hexdigest()


def read_cache(path):
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data["files"] if isinstance(data, dict) and data.get("version") == 1 and isinstance(data.get("files"), dict) else {}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def save_cache(path, files):
    partial = None
    try:
        while len(files) > 1024:
            files.pop(next(iter(files)))
        fd, name = tempfile.mkstemp(prefix=".documents-", dir=path.parent)
        partial = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "files": files}, stream)
        partial.chmod(0o600)
        os.replace(partial, path)
    except OSError:
        logger.warning("MTProto 文件快取寫入失敗；訊息已送出，未重送")
    finally:
        if partial is not None:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass


def cached_media(entry):
    from telethon.tl.types import InputDocument, InputMediaDocument
    try:
        return InputMediaDocument(InputDocument(int(entry["id"]), int(entry["access_hash"]),
                                               base64.b64decode(entry["reference"], validate=True)))
    except (ValueError, KeyError, TypeError):
        return None


def document_entry(document, chat_id, message_id):
    return {"id": document.id, "access_hash": document.access_hash,
            "reference": base64.b64encode(document.file_reference).decode(),
            "chat_id": chat_id, "message_id": message_id, "updated": time.time()}


def upload_media(uploaded, filename):
    from telethon.tl.types import InputMediaUploadedDocument, DocumentAttributeFilename
    return InputMediaUploadedDocument(
        file=uploaded, mime_type="application/octet-stream", force_file=True,
        attributes=[DocumentAttributeFilename(filename)],
    )


def proxy_settings(url):
    if not url:
        return None
    parsed = urlsplit(url)
    schemes = {"http": "http", "https": "http", "socks5": "socks5", "socks5h": "socks5"}
    if parsed.scheme not in schemes or not parsed.hostname or not parsed.port:
        raise ValueError("MTProto 代理必須是含連接埠的 HTTP 或 SOCKS5 網址")
    return dict(proxy_type=schemes[parsed.scheme], addr=parsed.hostname, port=parsed.port,
                rdns=True, username=unquote(parsed.username) if parsed.username else None,
                password=unquote(parsed.password) if parsed.password else None)


async def send_gif_document(path: Path, *, chat_id: int, reply_to: int | None,
                            caption: str | None, session_dir: Path, proxy_url=""):
    from telethon import TelegramClient
    from telethon.tl.functions.messages import SendMediaRequest
    from telethon.tl.types import InputReplyToMessage, DocumentAttributeAnimated
    from telethon.errors import FileReferenceExpiredError, FileReferenceInvalidError, FileReferenceEmptyError, RPCError

    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not api_id.isdigit() or not api_hash or not token:
        raise ValueError("Pixiv GIF 文件模式需要 TELEGRAM_API_ID、TELEGRAM_API_HASH 與 BOT_TOKEN")
    async with _send_lock:
        session_dir.mkdir(parents=True, exist_ok=True)
        session_dir.chmod(0o700)
        client = TelegramClient(str(session_dir / "documents"), int(api_id), api_hash,
                                # 初次登入可能需要切換 Telegram 資料中心；傳送沿用同一 random_id 去重。
                                proxy=proxy_settings(proxy_url), request_retries=2,
                                connection_retries=1, auto_reconnect=False,
                                flood_sleep_threshold=0, raise_last_call_error=True)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.sign_in(bot_token=token)
            me = await client.get_me()
            if not me or not me.bot or me.id != int(token.split(":", 1)[0]):
                raise ValueError("MTProto 工作階段與目前 bot 身分不一致")
            peer = await client.get_input_entity(chat_id)
            cache_path = session_dir / "documents-cache.json"
            files = await asyncio.to_thread(read_cache, cache_path)
            key = await asyncio.to_thread(document_key, path, me.id)
            entry = files.get(key)
            media = cached_media(entry) if isinstance(entry, dict) else None
            random_id = secrets.randbits(63)

            async def send(media):
                return await client(SendMediaRequest(
                    peer=peer, media=media, message=caption or "", random_id=random_id,
                    reply_to=InputReplyToMessage(reply_to_msg_id=reply_to) if reply_to else None,
                ))

            reference_errors = (FileReferenceExpiredError, FileReferenceInvalidError, FileReferenceEmptyError)
            result = None
            if media is not None:
                try:
                    result = await send(media)
                    logger.info("命中 MTProto GIF 文件快取，略過上傳")
                except reference_errors:
                    # 僅在 Telegram 明確拒絕參照時重試；逾時或其他錯誤不重送。
                    refreshed = None
                    try:
                        original = await client.get_messages(entry["chat_id"], ids=entry["message_id"])
                        doc = getattr(getattr(original, "media", None), "document", None)
                        if doc is not None and doc.mime_type == "application/octet-stream" and not any(
                                isinstance(attr, DocumentAttributeAnimated) for attr in doc.attributes):
                            refreshed = cached_media(document_entry(doc, entry["chat_id"], entry["message_id"]))
                    except (RPCError, KeyError, TypeError, ValueError):
                        pass
                    if refreshed is not None:
                        try:
                            result = await send(refreshed)
                            logger.info("MTProto 文件參照已更新，略過上傳")
                        except reference_errors:
                            pass
            if result is None:
                uploaded = await client.upload_file(str(path), file_name=path.name)
                result = await send(upload_media(uploaded, path.name))
            for update in getattr(result, "updates", ()):
                message = getattr(update, "message", None)
                document = getattr(getattr(message, "media", None), "document", None)
                if document is not None:
                    if document.mime_type != "application/octet-stream" or any(
                            isinstance(attr, DocumentAttributeAnimated) for attr in document.attributes):
                        raise RuntimeError("訊息已送出，但 Telegram 未保留一般文件類型；未再重送")
                    files.pop(key, None)
                    files[key] = document_entry(document, chat_id, message.id)
                    await asyncio.to_thread(save_cache, cache_path, files)
                    return message.id
            raise RuntimeError("訊息可能已送出，但未取得文件回傳資料；未再重送")
        finally:
            await client.disconnect()
            for session_file in session_dir.glob("documents.session*"):
                session_file.chmod(0o600)
