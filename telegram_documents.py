"""使用 MTProto 保留 GIF 檔名，並以一般文件 MIME 傳送。"""
import asyncio
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit, unquote

_send_lock = asyncio.Lock()


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
            uploaded = await client.upload_file(str(path), file_name=path.name)
            result = await client(SendMediaRequest(
                peer=peer, media=upload_media(uploaded, path.name), message=caption or "",
                random_id=secrets.randbits(63),
                reply_to=InputReplyToMessage(reply_to_msg_id=reply_to) if reply_to else None,
            ))
            for update in getattr(result, "updates", ()):
                message = getattr(update, "message", None)
                document = getattr(getattr(message, "media", None), "document", None)
                if document is not None:
                    if document.mime_type != "application/octet-stream" or any(
                            isinstance(attr, DocumentAttributeAnimated) for attr in document.attributes):
                        raise RuntimeError("訊息已送出，但 Telegram 未保留一般文件類型；未再重送")
                    return message.id
            raise RuntimeError("訊息可能已送出，但未取得文件回傳資料；未再重送")
        finally:
            await client.disconnect()
            for session_file in session_dir.glob("documents.session*"):
                session_file.chmod(0o600)
