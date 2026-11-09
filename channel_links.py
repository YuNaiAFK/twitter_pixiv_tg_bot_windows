"""以 bot 可用的 MTProto 方法，唯讀比對目標頻道的歷史連結。"""
import asyncio
import os
import re
from urllib.parse import urlsplit, unquote


class ChannelCheckError(RuntimeError):
    pass


def normalize_link(value):
    value = value.rstrip(".,!?;:)]}>，。！？；：）】」』")
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.scheme.lower() not in {"http", "https"} or not host:
        return ""
    path = unquote(parts.path).rstrip("/")
    if host in {"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com"}:
        match = re.search(r"/status/(\d+)", path)
        if match:
            return "twitter:" + match[1]
    if host in {"e-hentai.org", "exhentai.org"}:
        match = re.fullmatch(r"/g/(\d+)/([a-fA-F0-9]+)", path)
        if match:
            return "eh:" + match[1] + ":" + match[2].lower()
    return host + path + (("?" + parts.query) if parts.query else "")


def message_links(message):
    text = next((value for name in ("message", "text", "caption")
                 if isinstance(value := getattr(message, name, None), str) and value), "")
    links = set(re.findall(r"https?://[^\s<>\"']+", text, re.I))
    entities = tuple(getattr(message, "entities", None) or ()) + tuple(getattr(message, "caption_entities", None) or ())
    for entity in entities:
        if getattr(entity, "url", None):
            links.add(entity.url)
        elif str(getattr(entity, "type", "")).lower() == "url" or type(entity).__name__ == "MessageEntityUrl":
            raw = text.encode("utf-16-le")
            links.add(raw[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le"))
    return {key for value in links if (key := normalize_link(value))}


async def latest_message_id(client, peer):
    from telethon import functions, types
    info = (await client(functions.channels.GetFullChannelRequest(peer))).full_chat
    top = max(info.read_inbox_max_id, info.read_outbox_max_id)
    pts = 1
    while True:
        result = await client(functions.updates.GetChannelDifferenceRequest(
            peer, types.ChannelMessagesFilterEmpty(), pts=pts, limit=100, force=True))
        dialog = getattr(result, "dialog", None)
        if dialog is not None:
            return max(top, dialog.top_message)
        messages = getattr(result, "new_messages", ())
        top = max([top] + [m.id for m in messages])
        for update in getattr(result, "other_updates", ()):
            if type(update).__name__ == "UpdateDeleteChannelMessages":
                top = max([top] + update.messages)
        if result.final:
            return top
        if result.pts <= pts:
            raise ChannelCheckError("頻道更新進度無法確認")
        pts = result.pts


async def scan_links(client, peer, links):
    top = await latest_message_id(client, peer)
    for end in range(top, 0, -100):
        if end != top:
            await asyncio.sleep(1)
        messages = await client.get_messages(peer, ids=list(range(max(1, end - 99), end + 1)))
        for message in messages:
            if message is not None and links.intersection(message_links(message)):
                return message.id
    return None


class ChannelLinkChecker:
    def __init__(self, session_dir, proxy_url=""):
        self.session_dir, self.proxy_url = session_dir, proxy_url

    async def __call__(self, channel_id, from_chat_id, message_ids, source_url=""):
        from telethon import TelegramClient
        from telegram_documents import _send_lock, proxy_settings
        api_id = os.getenv("TELEGRAM_API_ID", "").strip()
        api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not api_id.isdigit() or not api_hash or not token:
            raise ChannelCheckError("需要 TELEGRAM_API_ID、TELEGRAM_API_HASH 與 BOT_TOKEN")
        async with _send_lock:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            client = TelegramClient(str(self.session_dir / "documents"), int(api_id), api_hash,
                proxy=proxy_settings(self.proxy_url), request_retries=1, connection_retries=1,
                auto_reconnect=False, flood_sleep_threshold=60, raise_last_call_error=True)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await client.sign_in(bot_token=token)
                me = await client.get_me()
                if not me or not me.bot or me.id != int(token.split(":", 1)[0]):
                    raise ChannelCheckError("MTProto 工作階段與 bot 身分不一致")
                links = {normalize_link(source_url)} - {""}
                for start in range(0, len(message_ids), 100):
                    messages = await client.get_messages(from_chat_id, ids=message_ids[start:start + 100])
                    if any(message is None for message in messages) or len(messages) != len(message_ids[start:start + 100]):
                        raise ChannelCheckError("無法讀取完整解析結果")
                    for message in messages:
                        links.update(message_links(message))
                if not links:
                    raise ChannelCheckError("解析結果沒有可比對的連結")
                peer = await client.get_input_entity(channel_id)
                return await scan_links(client, peer, links)
            except ChannelCheckError:
                raise
            except Exception as exc:
                raise ChannelCheckError(type(exc).__name__) from exc
            finally:
                await client.disconnect()
                for path in self.session_dir.glob("documents.session*"):
                    path.chmod(0o600)
