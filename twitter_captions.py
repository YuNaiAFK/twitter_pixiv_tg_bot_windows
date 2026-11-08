"""使用 Telegram 文字實體呈現來源連結，不改變其他解析的文字格式。"""
from telegram import MessageEntity


def linked_caption(text, source_url):
    if not text or source_url not in text:
        return text, None
    position = text.index(source_url)
    start = text.rfind("\n", 0, position) + 1
    label = text[start:position].rstrip("：: ") or "原始連結"
    rendered = text[:start] + label + text[position + len(source_url):]
    offset = len(rendered[:start].encode("utf-16-le")) // 2
    length = len(label.encode("utf-16-le")) // 2
    return rendered, [MessageEntity("text_link", offset, length, url=source_url)]
