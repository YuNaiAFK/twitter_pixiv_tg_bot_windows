"""依完整畫廊識別排除已確認廣告頁；不推測其他作品的頁碼。"""
from channel_links import normalize_link

RULES = {
    "eh:2051305:d1969d0ee2": (36, 41),
    "eh:2000612:54a53baa2d": (38, 42),
}


def known_rule(url):
    return normalize_link(url) in RULES


def has_external_ads(gallery):
    tags = getattr(gallery, "tags", {}) or {}
    values = tags.get("other", [])
    return any(str(value).split("|", 1)[0].strip().casefold().removeprefix("other:")
               in {"extraneous ads", "外部廣告", "外部广告"} for value in values)


def rule_version(url, gallery=None):
    span = RULES.get(normalize_link(url)) if has_external_ads(gallery) else None
    return f"pages:{span[0]}-{span[1]}:v2" if span else ""


def filter_pages(url, images, gallery=None):
    span = RULES.get(normalize_link(url)) if has_external_ads(gallery) else None
    if not span:
        return list(images)
    return [image for page, image in enumerate(images, 1) if not span[0] <= page <= span[1]]
