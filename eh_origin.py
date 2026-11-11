"""依 Telegram 提供的來源識別，略過已發布／既有 bot 的 EH 訊息。"""
from exviewer_import import BOT_USERNAME


def should_ignore(message, runtime):
    channels = set()
    for name in ("EH_CHANNEL_PUBLISHER", "CHANNEL_PUBLISHER"):
        publisher = runtime.get(name)
        if publisher is not None:
            channels.update(publisher.channels)
    origin = getattr(message, "forward_origin", None)
    chats = (getattr(message, "chat", None), getattr(message, "sender_chat", None),
             getattr(origin, "chat", None), getattr(origin, "sender_chat", None),
             getattr(message, "forward_from_chat", None))
    if any(getattr(chat, "id", None) in channels for chat in chats if chat is not None):
        return True
    users = (getattr(message, "from_user", None), getattr(message, "via_bot", None),
             getattr(origin, "sender_user", None), getattr(message, "forward_from", None))
    return any(getattr(user, "is_bot", False) and
               (getattr(user, "username", "") or "").casefold() == BOT_USERNAME.casefold()
               for user in users if user is not None)
