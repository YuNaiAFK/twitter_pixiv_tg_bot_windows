"""以 bot 可用的 MTProto 方法，唯讀比對目標頻道的歷史連結。"""
import asyncio
import os
import json
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


async def indexed_links(client, peer, links, path):
    from telethon import functions, types
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
        if index.get("version") != 1:
            index = None
    except (FileNotFoundError, ValueError):
        index = None
    while True:
        if index is None:
            # 先記錄掃描前的 pts；掃描期間的變動會在下方增量同步補齊。
            info = (await client(functions.channels.GetFullChannelRequest(peer))).full_chat
            pts = info.pts
            top = await latest_message_id(client, peer)
            index = {"version": 1, "pts": pts, "messages": {}}
            for end in range(top, 0, -100):
                if end != top:
                    await asyncio.sleep(1)
                batch = await client.get_messages(peer, ids=list(range(max(1, end-99), end+1)))
                for message in batch:
                    if message is not None:
                        found = message_links(message)
                        if found:
                            index["messages"][str(message.id)] = sorted(found)
        result = await client(functions.updates.GetChannelDifferenceRequest(
            peer, types.ChannelMessagesFilterEmpty(), pts=index["pts"], limit=100, force=True))
        if getattr(result, "dialog", None) is not None:
            # Telegram 已無法提供完整增量：重建，而非拿不完整快取放行。
            index = None
            continue
        for message in getattr(result, "new_messages", ()):
            index["messages"].pop(str(message.id), None)
            found = message_links(message)
            if found:
                index["messages"][str(message.id)] = sorted(found)
        for update in getattr(result, "other_updates", ()):
            name = type(update).__name__
            if name == "UpdateDeleteChannelMessages":
                for message_id in update.messages:
                    index["messages"].pop(str(message_id), None)
            elif name in {"UpdateEditChannelMessage", "UpdateNewChannelMessage"}:
                message = update.message
                index["messages"].pop(str(message.id), None)
                found = message_links(message)
                if found:
                    index["messages"][str(message.id)] = sorted(found)
        previous = index["pts"]
        index["pts"] = result.pts
        if result.final:
            break
        if result.pts <= previous:
            raise ChannelCheckError("頻道增量進度無法確認")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return next((int(mid) for mid, stored in index["messages"].items() if links.intersection(stored)), None)


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
                return await indexed_links(client, peer, links, self.session_dir / f"channel-links-{me.id}-{channel_id}.json")
            except ChannelCheckError:
                raise
            except Exception as exc:
                raise ChannelCheckError(type(exc).__name__) from exc
            finally:
                await client.disconnect()
                for path in self.session_dir.glob("documents.session*"):
                    path.chmod(0o600)
