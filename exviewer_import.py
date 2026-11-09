"""只查詢 exviewer 的既有 inline 結果，不送出使用者訊息或觸發下載。"""
import asyncio
from dataclasses import dataclass
import logging
import os
from urllib.parse import urlsplit, parse_qs

from channel_links import normalize_link, message_links
from telegram import MessageEntity, LinkPreviewOptions

logger = logging.getLogger(__name__)
_user_lock = asyncio.Lock()
BOT_USERNAME = "exviewer_bot"


@dataclass
class ExistingResult:
    text: str
    entities: list
    preview_url: str


def preview_url(entities):
    for entity in entities:
        url = getattr(entity, "url", None)
        if not url:
            continue
        parts = urlsplit(url)
        if parts.hostname == "t.me" and parts.path == "/iv":
            url = (parse_qs(parts.query).get("url") or [""])[0]
            parts = urlsplit(url)
        if parts.scheme in {"http", "https"} and parts.hostname == "telegra.ph":
            return url
    return ""


def existing_result(results, source_url):
    key = normalize_link(source_url)
    if not key.startswith("eh:"):
        return None
    mapping = {"MessageEntityCode": "code", "MessageEntityHashtag": "hashtag",
        "MessageEntityTextUrl": "text_link", "MessageEntityUrl": "url", "MessageEntityBold": "bold",
        "MessageEntityItalic": "italic", "MessageEntityUnderline": "underline",
        "MessageEntityStrike": "strikethrough", "MessageEntitySpoiler": "spoiler",
        "MessageEntityPre": "pre", "MessageEntityBlockquote": "blockquote", "MessageEntityMention": "mention"}
    for item in results:
        result = getattr(item, "result", item)
        payload = getattr(result, "send_message", None)
        if type(payload).__name__ != "BotInlineMessageText" or key not in message_links(payload):
            continue
        text = payload.message
        if not text or len(text) > 4096:
            continue
        preview = preview_url(payload.entities or [])
        if not preview:
            continue
        converted = []
        for entity in payload.entities or []:
            kind = mapping.get(type(entity).__name__)
            if kind is None:
                continue
            kwargs = {}
            if kind == "text_link":
                kwargs["url"] = entity.url
            if kind == "pre":
                kwargs["language"] = getattr(entity, "language", "")
            converted.append(MessageEntity(kind, entity.offset, entity.length, **kwargs))
        return ExistingResult(text, converted, preview)
    return None


async def lookup(source_url, session_dir, proxy_url=""):
    # 沒有登入時維持原本 EH 解析，不影響既有安裝。
    if os.getenv("EXVIEWER_ENABLED", "1") == "0" or not (session_dir / "exviewer-user.session").is_file():
        return None
    from telethon import TelegramClient
    from telegram_documents import proxy_settings
    api_id, api_hash = os.getenv("TELEGRAM_API_ID", ""), os.getenv("TELEGRAM_API_HASH", "")
    if not api_id.isdigit() or not api_hash:
        return None
    try:
        async with asyncio.timeout(25), _user_lock:
            client = TelegramClient(str(session_dir / "exviewer-user"), int(api_id), api_hash,
                proxy=proxy_settings(proxy_url), request_retries=1, connection_retries=1,
                auto_reconnect=False, flood_sleep_threshold=0, raise_last_call_error=True)
            try:
                await client.connect()
                me = await client.get_me()
                if not me or me.bot:
                    logger.warning("exviewer 使用者工作階段尚未登入；沿用本機 EH 解析")
                    return None
                results = await client.inline_query(BOT_USERNAME, source_url)
                return existing_result(results, source_url)
            finally:
                await client.disconnect()
                for path in session_dir.glob("exviewer-user.session*"):
                    path.chmod(0o600)
    except Exception as exc:
        logger.warning("exviewer 查詢未完成（%s）；沿用本機 EH 解析", type(exc).__name__)
        return None


async def reuse(message, status, source_url, user_id, runtime):
    if message.chat.type != "private" or not runtime["user_is_admin"](user_id):
        return False
    result = await lookup(source_url, runtime["BASE_DIR"] / ".mtproto", runtime["ehentai"].proxy_url())
    if result is None:
        return False
    # 由本 bot 發送同一份文字與 entities，不建立轉發標頭或 via_bot 標記。
    await status.edit_text(result.text, entities=result.entities, parse_mode=None,
        link_preview_options=LinkPreviewOptions(url=result.preview_url, prefer_large_media=True, show_above_text=True),
        reply_markup=None)
    logger.info("已沿用 exviewer 的既有 EH 預覽結果")
    from channel_publish import offer_eh_publish
    await offer_eh_publish(message, status, user_id, runtime.get("EH_CHANNEL_PUBLISHER"), source_url=source_url)
    return True
