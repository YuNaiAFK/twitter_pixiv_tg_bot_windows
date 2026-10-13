"""
e-hentai / exhentai gallery lookup.

The gallery page itself is the data source, fetched with one HTTP GET. That was a
deliberate choice after measuring the alternatives:

* ``gallery-dl -j`` walks **every image page** even when only metadata is wanted,
  which took 254 seconds on a 53-page gallery - unusable for a chat bot.
* ``gallery-dl -j`` with ``source: metadata`` is fast (about 1 second) because it
  calls e-hentai's gdata API once, but that API response has no category name, no
  language and no uploader, and it returns tags as ``namespace:tag`` strings
  without the per-namespace grouping.
* One GET of the gallery page returns in about 1.3 seconds and carries
  everything: title (English and Japanese), category, language, rating, page
  count, size, date, favourites, uploader, cover image, and the full tag list
  grouped by namespace.

The regexes below were each checked against real markup, because the obvious
patterns silently match the wrong thing - the cover appeared first as a small
placeholder image, and ``<div id="taglist">`` does not contain ``<tr>`` directly.
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Sequence

from tag_translations import translate_tag

logger = logging.getLogger("twitter_pixiv_bot.ehentai")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# gallery-dl reports the gallery's own content rating in "eh_category"; these are
# the values that denote adult material.
ADULT_CATEGORIES = {"H", "H-Site", "Doujinshi", "Manga", "Artist CG", "Game CG",
                    "Western", "Non-H"}
# "Non-H" is explicitly not adult, so it is removed again below.
ADULT_CATEGORIES.discard("Non-H")

# Human labels for the namespaces e-hentai uses.
NAMESPACE_LABELS = {
    "language": "語言 / Language",
    "artist": "繪師 / Artist",
    "group": "社團 / Group",
    "cosplayer": "Cosplayer",
    "character": "角色 / Character",
    "female": "女性 / Female",
    "male": "男性 / Male",
    "mixed": "混合 / Mixed",
    "other": "其他 / Other",
    "parody": "原作 / Parody",
    "reclass": "Reclass",
    "temp": "Temp",
}

# Order the groups are shown in: the ones readers look for first.
NAMESPACE_ORDER = [
    "language", "artist", "group", "parody", "character",
    "female", "male", "mixed", "other", "cosplayer", "reclass", "temp",
]

# --- gallery page markup -------------------------------------------------------
#
# The cover lives in an inline background style. Matching the first <img> instead
# picks up a small rating/placeholder graphic.
_COVER_RE = re.compile(r"background:transparent url\((https?://[^)]+)\)")
_TITLE_RE = re.compile(r'<h1 id="gn">(.*?)</h1>', re.S)
_TITLE_JPN_RE = re.compile(r'<h1 id="gj">(.*?)</h1>', re.S)
# <div id="gdc"><div class="cs ct9" ...>Non-H</div></div>
_CATEGORY_RE = re.compile(
    r'<div id="gdc">\s*<div class="cs ([a-z0-9]+)"[^>]*>(.*?)</div>', re.S)
_FACT_RE = re.compile(
    r'<td class="gdt1">(.*?):</td>\s*<td class="gdt2">(.*?)</td>', re.S)
_RATING_RE = re.compile(r'id="rating_label"[^>]*>\s*Average:\s*([\d.]+)', re.S)
# The tag table: rows live under id="taglist", and the namespace is a td.tc whose
# text ends with a colon.
_TAGLIST_RE = re.compile(r'id="taglist">(.*)</div>\s*</div>', re.S)
_TAGROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_TAGNS_RE = re.compile(r'<td class="tc">(.*?):</td>', re.S)
_TAGVAL_RE = re.compile(r'hentai\.org/tag/[^"]*"[^>]*>(.*?)</a>', re.S)

# e-hentai appends a wiki note to some tags, as a title attribute that survives
# tag-stripping, e.g. "japanese | definition revised please read the wiki". It is
# site boilerplate rather than part of the tag, so it is removed.
_TAG_NOTE_RE = re.compile(
    r"\s*\|\s*(?:definition revised|please read the wiki|see the wiki)"
    r"[^|]*$",
    re.IGNORECASE,
)


def clean_tag_value(value: str) -> str:
    """Strip e-hentai's wiki annotation from a tag value."""
    if not value:
        return value
    cleaned = _TAG_NOTE_RE.sub("", value).strip()
    return cleaned or value

_GID_RE = re.compile(r"/g/(\d+)/")
_TOKEN_RE = re.compile(r"/g/\d+/([0-9a-f]+)")

_SIZE_RE = re.compile(r"([\d.]+)\s*([KMG]?i?B)", re.I)
_SIZE_FACTORS = {
    "B": 1, "KB": 1000, "KIB": 1024,
    "MB": 1000 ** 2, "MIB": 1024 ** 2,
    "GB": 1000 ** 3, "GIB": 1024 ** 3,
}


class EhentaiError(RuntimeError):
    """Raised when the gallery cannot be read."""


@dataclass
class EhentaiGallery:
    """One gallery, reduced to what a Telegraph page needs."""

    url: str
    gid: str = ""
    token: str = ""
    title: str = ""
    title_jpn: str = ""
    category: str = ""
    language: str = ""
    rating: str = ""
    rating_count: str = ""
    filecount: str = ""
    filesize: int = 0
    uploader: str = ""
    date: str = ""
    favorites: str = ""
    thumb: str = ""
    expunged: bool = False
    # namespace -> [values], e.g. {"artist": ["momozu komamochi"]}
    tags: dict[str, list[str]] = field(default_factory=dict)

    @property
    def is_adult(self) -> bool:
        return (self.category or "").strip() in ADULT_CATEGORIES

    @property
    def page_url(self) -> str:
        if self.gid and self.token:
            return f"https://e-hentai.org/g/{self.gid}/{self.token}/"
        return self.url

    def tag_summary(self) -> str:
        """One-line tag list, for a chat message or a log line."""
        parts = []
        for namespace in NAMESPACE_ORDER:
            values = self.tags.get(namespace)
            if values:
                parts.append(f"{namespace}:{', '.join(values)}")
        return " | ".join(parts)

    def ordered_namespaces(self) -> list[str]:
        """Namespaces present on this gallery, in display order."""
        known = [n for n in NAMESPACE_ORDER if self.tags.get(n)]
        extra = sorted(n for n in self.tags if n not in NAMESPACE_ORDER and self.tags[n])
        return known + extra


def format_filesize(size: int) -> str:
    """Render a byte count the way e-hentai does, e.g. 148.5 MiB."""
    if not size:
        return ""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


def is_gallery_url(url: str) -> bool:
    """True for an e-hentai/exhentai gallery URL, false for its other pages."""
    return bool(re.match(
        r"^https?://(?:www\.)?(?:e-hentai|exhentai)\.org/g/\d+/[0-9a-f]+",
        (url or "").strip(), re.IGNORECASE,
    ))


def display_tag(value: str) -> str:
    """
    Render one tag as Traditional Chinese, keeping the English when it differs.

    Proper nouns - artist, group, character and parody names - have no Chinese
    form, so they pass through unchanged and are shown alone rather than as
    "name (name)".
    """
    translated = translate_tag(value)
    if translated == value:
        return value
    return f"{translated}（{value}）"


def _strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value or "").replace("&nbsp;", " ").strip()


def parse_gallery_page(text: str, url: str) -> EhentaiGallery:
    """
    Parse a gallery page into an EhentaiGallery.

    Raises EhentaiError when the page is not a gallery page at all, which is what
    a login redirect or a removed gallery looks like.
    """
    title_match = _TITLE_RE.search(text)
    if not title_match:
        if "login" in text.lower() and "ipb_member_id" in text:
            raise EhentaiError(
                "這個畫廊需要 e-hentai 登入。請設定 EHENTAI_COOKIES 後再試。"
            )
        raise EhentaiError(
            "讀不到畫廊內容。可能是連結有誤、畫廊已被刪除，"
            "或該畫廊需要 e-hentai 登入（成人畫廊）。"
        )

    title_jpn_match = _TITLE_JPN_RE.search(text)
    cover_match = _COVER_RE.search(text)
    category_match = _CATEGORY_RE.search(text)

    facts = {
        _strip_tags(label): _strip_tags(value)
        for label, value in _FACT_RE.findall(text)
    }

    tags: dict[str, list[str]] = {}
    taglist = _TAGLIST_RE.search(text)
    if taglist:
        for row in _TAGROW_RE.findall(taglist.group(1)):
            namespace_match = _TAGNS_RE.search(row)
            if not namespace_match:
                continue
            namespace = _strip_tags(namespace_match.group(1)).lower()
            values = [
                clean_tag_value(html.unescape(_strip_tags(value)))
                for value in _TAGVAL_RE.findall(row)
            ]
            bucket = tags.setdefault(namespace, [])
            for value in values:
                if value and value not in bucket:
                    bucket.append(value)

    size_match = _SIZE_RE.search(facts.get("File Size", ""))
    filesize = 0
    if size_match:
        filesize = int(float(size_match.group(1))
                       * _SIZE_FACTORS.get(size_match.group(2).upper(), 1))

    length_match = re.match(r"(\d+)", facts.get("Length", ""))
    rating_match = _RATING_RE.search(text)

    gid_match = _GID_RE.search(url)
    token_match = _TOKEN_RE.search(url)

    return EhentaiGallery(
        url=url,
        gid=gid_match.group(1) if gid_match else "",
        token=token_match.group(1) if token_match else "",
        title=html.unescape(_strip_tags(title_match.group(1))),
        title_jpn=(html.unescape(_strip_tags(title_jpn_match.group(1)))
                   if title_jpn_match else ""),
        category=_strip_tags(category_match.group(2)) if category_match else "",
        language=facts.get("Language", ""),
        rating=rating_match.group(1) if rating_match else "",
        filecount=length_match.group(1) if length_match else "",
        filesize=filesize,
        uploader="",
        date=facts.get("Posted", ""),
        favorites=(facts.get("Favorites", "").replace("times", "").strip()
                   if facts.get("Favorites") else ""),
        thumb=(cover_match.group(1).strip() if cover_match else ""),
        expunged=facts.get("Visible", "").strip().lower() == "no",
        tags=tags,
    )


def fetch_gallery(url: str, *, cookies_path=None, timeout: float = 60.0,
                  http_client=None) -> EhentaiGallery:
    """
    Read one gallery with a single HTTP request.

    Synchronous on purpose: the caller runs it in a worker thread, matching how
    the rest of the bot dispatches blocking work.
    """
    import httpx

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookies_path is not None:
        headers["Cookie"] = cookies_header(cookies_path)

    client = http_client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        response = client.get(url, headers=headers)
        if response.status_code == 404:
            raise EhentaiError(
                "找不到這個畫廊。可能是連結有誤或畫廊已被刪除。"
            )
        if response.status_code >= 400:
            raise EhentaiError(f"e-hentai 回應 HTTP {response.status_code}。")
        return parse_gallery_page(response.text, url)
    except EhentaiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise EhentaiError(f"讀取畫廊失敗：{exc}") from exc
    finally:
        if http_client is None:
            client.close()


def cookies_header(path) -> str:
    """Build a Cookie header from a Netscape cookie file."""
    pairs = []
    try:
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            raw = line.lstrip(" ")
            if raw.startswith("#HttpOnly_"):
                raw = raw[10:]
            elif not raw or raw[0] in ("#", "$"):
                continue
            fields = raw.split("\t")
            if len(fields) == 7:
                pairs.append(f"{fields[5]}={fields[6]}")
    except OSError as exc:
        logger.warning("讀取 e-hentai cookie 檔案失敗：%s（%s）", path, exc)
    return "; ".join(pairs)


# -------------------- viewer pages (image URLs) --------------------

_VIEWER_LINK_RE = re.compile(r'<a href="(https://e-hentai\.org/s/[^"]+)"')
_VIEWER_IMG_RE = re.compile(r'<img id="img" src="([^"]+)"')
_VIEWER_ORIGINAL_RE = re.compile(r'href="(https://[^"]*fullimg[^"]*)"')


def gallery_viewer_links(page_html: str) -> list[str]:
    """
    Every viewer-page link on a gallery page, in page order.

    e-hentai lists the whole grid on one page even for a 53-page gallery, so no
    paging requests are needed. The order matters: it is the gallery's own page
    order.
    """
    return list(dict.fromkeys(_VIEWER_LINK_RE.findall(page_html)))


def fetch_gallery_images(
    url: str,
    *,
    cookies_path=None,
    timeout: float = 60.0,
    http_client=None,
    max_images: int = 500,
    progress=None,
    workers: int = 6,
) -> tuple[list[str], list[str]]:
    """
    Return (display_urls, original_urls) for every page of a gallery.

    One gallery request plus one viewer request per page. The display URL is the
    resized JPEG the site itself serves; the original URL is the full-resolution
    file. Neither needs the bot to download any image bytes, which is what makes
    embedding them in a Telegraph page cheap.

    The two lists are kept the same length and in page order by falling back to
    the display URL wherever a page has no original. Neither list is complete on
    its own: a cover page often has no download link at all, so returning the raw
    originals would silently publish a shorter page than the gallery has pages.

    Viewer pages are fetched with bounded parallelism. Measured on a 5-page
    gallery: 1.91s sequentially, 0.44s at 8 concurrent. Six workers is the default
    because the gain is mostly captured by then while the load on e-hentai stays
    modest; these are plain page views, not the image downloads the site meters.

    ``progress``, when given, is called as ``progress(done, total)`` after each
    page so a caller can report incremental status.
    """
    import httpx

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookies_path is not None:
        headers["Cookie"] = cookies_header(cookies_path)

    client = http_client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        response = client.get(url, headers=headers)
        if response.status_code >= 400:
            raise EhentaiError(f"e-hentai 回應 HTTP {response.status_code}。")
        links = gallery_viewer_links(response.text)
        if not links:
            raise EhentaiError(
                "讀不到畫廊的圖片清單。連結可能有誤，或畫廊已被刪除。"
            )
        links = links[:max_images]

        # Collected per page first, then aligned, so a page without an original
        # cannot shift the pairing of the pages after it.
        per_page: list[tuple[str | None, str | None]] = [None] * len(links)
        done = 0
        lock = threading.Lock()

        def fetch_viewer(item):
            index, link = item
            try:
                viewer = client.get(link, headers=headers)
            except Exception as exc:  # noqa: BLE001
                logger.warning("讀取頁面失敗：%s（%s）", link, exc)
                return index, None, None
            if viewer.status_code >= 400:
                logger.warning("頁面回應 HTTP %s：%s", viewer.status_code, link)
                return index, None, None
            display = _VIEWER_IMG_RE.search(viewer.text)
            original = _VIEWER_ORIGINAL_RE.search(viewer.text)
            return (index,
                    display.group(1) if display else None,
                    original.group(1) if original else None)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for index, display, original in pool.map(
                    fetch_viewer, enumerate(links)):
                with lock:
                    per_page[index] = (display, original)
                    done += 1
                    if progress:
                        progress(done, len(links))

        display_urls = [entry[0] for entry in per_page if entry and entry[0]]
        if not display_urls:
            raise EhentaiError("沒有取得任何圖片。可能需要 e-hentai 登入。")

        # Same length and order as display_urls: where there is no original, the
        # display image stands in so the page keeps every page of the gallery.
        original_urls = [
            (entry[1] or entry[0]) for entry in per_page if entry and entry[0]
        ]
        return display_urls, original_urls
    finally:
        if http_client is None:
            client.close()


def gallery_image_urls(url: str, *, cookies_path=None, timeout: float = 120.0,
                       workers: int = 6, progress=None) -> tuple[list[str], list[str]]:
    """Thin alias kept so callers read as one operation."""
    return fetch_gallery_images(
        url, cookies_path=cookies_path, timeout=timeout,
        workers=workers, progress=progress,
    )


# -------------------- Telegraph --------------------

TELEGRAPH_API = "https://api.telegra.ph"


class TelegraphError(RuntimeError):
    """Raised when a Telegraph page cannot be created."""


def _node(tag: str, children, attrs: dict | None = None) -> dict:
    node: dict = {"tag": tag, "children": children}
    if attrs:
        node["attrs"] = attrs
    return node


def _text(value: str) -> str:
    return "" if value is None else str(value)



def build_telegraph_content(
    gallery: EhentaiGallery,
    image_urls: Sequence[str] | None = None,
    *,
    embed_limit_kb: int = 62,
) -> list[dict]:
    """
    Build the Telegraph node tree for a gallery.

    Layout: the gallery's facts, one section per tag namespace, then the images, then
    a link back to the gallery. Tags come before the images because they are what a
    reader scans, and a long image list would otherwise push them off the page.

    The cover is deliberately **not** included. It is shown in the chat message
    instead, and repeating it at the top of the page only used up content budget that
    the actual pages need.

    ``embed_limit_kb`` guards Telegraph's 64 KB content cap. A gallery with
    hundreds of pages can exceed it, and Telegraph rejects the whole page rather
    than trimming it, so images past the budget are skipped and the page says so.
    """
    content: list[dict] = []

    content.append(_node("h3", [_text(gallery.title or "未命名畫廊")]))
    if gallery.title_jpn and gallery.title_jpn != gallery.title:
        content.append(_node("p", [_node("em", [_text(gallery.title_jpn)])]))

    facts = []
    if gallery.category:
        facts.append(("分類", gallery.category))
    if gallery.language:
        facts.append(("語言", gallery.language))
    if gallery.rating:
        facts.append(("評分", gallery.rating))
    if gallery.filecount:
        facts.append(("頁數", gallery.filecount))
    if gallery.filesize:
        facts.append(("大小", format_filesize(gallery.filesize)))
    if gallery.uploader:
        facts.append(("上傳者", gallery.uploader))
    if gallery.date:
        facts.append(("上傳日期", gallery.date))
    if gallery.favorites:
        facts.append(("收藏數", gallery.favorites))

    if facts:
        children = []
        for index, (label, value) in enumerate(facts):
            if index:
                children.append(_node("br", []))
            children.append(_node("b", [f"{label}："]))
            children.append(_text(value))
        content.append(_node("p", children))

    if gallery.expunged:
        content.append(_node("p", [_node("b", ["⚠ 此畫廊已被刪除（expunged）"])]))

    for namespace in gallery.ordered_namespaces():
        label = NAMESPACE_LABELS.get(namespace, namespace.title())
        values = gallery.tags.get(namespace, [])
        if not values:
            continue
        content.append(_node("h4", [label]))
        content.append(_node("ul", [
            _node("li", [_text(display_tag(value))]) for value in values
        ]))

    if image_urls:
        content.append(_node("h4", [f"圖片（共 {len(image_urls)} 頁）"]))
        budget = embed_limit_kb * 1024
        used = len(json.dumps(content, ensure_ascii=False))
        embedded = 0
        for url in image_urls:
            node = _node("img", [], {"src": url})
            size = len(json.dumps(node, ensure_ascii=False))
            if used + size > budget:
                break
            content.append(node)
            used += size
            embedded += 1
        if embedded < len(image_urls):
            content.append(_node("p", [_node("em", [
                f"（因 Telegraph 內容上限，僅嵌入前 {embedded} 頁，"
                f"其餘請見原畫廊）"
            ])]))

    content.append(_node("hr", []))
    content.append(_node("p", [
        _node("a", ["在 e-hentai 開啟原畫廊"], {"href": gallery.page_url}),
    ]))

    return content


def create_telegraph_page(
    gallery: EhentaiGallery,
    image_urls: Sequence[str] | None = None,
    *,
    author_name: str = "",
    author_url: str = "",
    timeout: float = 60.0,
    embed_limit_kb: int = 62,
    http_client=None,
) -> str:
    """
    Publish a gallery to Telegraph and return the page URL.

    ``image_urls`` embeds the gallery's pages directly; Telegraph fetches them
    from e-hentai's own CDN, so the bot transfers no image bytes at all.

    An anonymous Telegraph account is created per call, which keeps the bot from
    depending on a token that has to be provisioned, and keeps each page
    independent: if one is removed, the others are unaffected.
    """
    import httpx

    # Telegraph requires both names to be non-empty.
    resolved_author = (author_name or "e-hentai")[:128]

    client = http_client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        account = client.post(
            f"{TELEGRAPH_API}/createAccount",
            data={"short_name": resolved_author[:32]},
        ).json()
        if not account.get("ok"):
            raise TelegraphError(f"建立 Telegraph 帳號失敗：{account.get('error')}")
        token = account["result"]["access_token"]

        data = {
            "access_token": token,
            "title": (gallery.title or "Gallery")[:256],
            "author_name": resolved_author,
            "content": json.dumps(
                build_telegraph_content(gallery, image_urls, embed_limit_kb=embed_limit_kb),
                ensure_ascii=False,
            ),
            "return_content": "false",
        }
        if author_url:
            data["author_url"] = author_url[:512]

        page = client.post(f"{TELEGRAPH_API}/createPage", data=data).json()
        if not page.get("ok"):
            raise TelegraphError(f"建立 Telegraph 頁面失敗：{page.get('error')}")
        return page["result"]["url"]
    except TelegraphError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TelegraphError(f"Telegraph 請求失敗：{exc}") from exc
    finally:
        if http_client is None:
            client.close()
