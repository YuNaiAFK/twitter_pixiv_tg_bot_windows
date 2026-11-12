"""依完整畫廊識別排除已確認廣告頁；不推測其他作品的頁碼。"""
from channel_links import normalize_link

RULES = {
    "eh:2051305:d1969d0ee2": (36, 41),
    "eh:2000612:54a53baa2d": (38, 42),
}


def rule_version(url):
    span = RULES.get(normalize_link(url))
    return f"pages:{span[0]}-{span[1]}:v1" if span else ""


def filter_pages(url, images):
    span = RULES.get(normalize_link(url))
    if not span:
        return list(images)
    return [image for page, image in enumerate(images, 1) if not span[0] <= page <= span[1]]
