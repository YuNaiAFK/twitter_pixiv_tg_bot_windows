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
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    a login redirect, a removed gallery, or a refused host looks like.
    """
    # e-hentai answers **HTTP 200** with this 39-byte body when it refuses a host, which
    # is why it cannot be caught by a status check. Measured here: every gallery path
    # returned it, directly and through a proxy, while the front page still answered
    # normally. It is an access refusal, not a timeout, so retrying never helps - and
    # the generic "連結有誤或畫廊已被刪除" message would send the user looking for a
    # problem that is not there.
    stripped = text.strip()
    if len(stripped) < 200 and "key missing" in stripped.lower():
        raise EhentaiError(
            "e-hentai 拒絕了這台主機的請求（Key missing, or incorrect key provided.）。"
            "這通常是 IP 被暫時限制，或需要登入 Cookie。"
            "請確認 EHENTAI_COOKIES 是最新的，或稍後（可能數分鐘到數小時）再試。"
        )

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


def redact_url(url: str) -> str:
    """
    A URL safe to log: the path with credential-bearing query values removed.

    ``archiver.php?gid=..&token=..`` is the case this exists for - the token is a live
    e-hentai session value, and it was being written to the log on every retry. The path
    is kept because that is what makes a log line useful.
    """
    try:
        parsed = urllib.parse.urlsplit(url or "")
    except ValueError:
        return "<url>"
    path = parsed.path or "/"
    if not parsed.query:
        return path
    kept = []
    for part in parsed.query.split("&"):
        key, sep, _value = part.partition("=")
        if sep and key.lower() in {"token", "key", "auth", "password", "api_hash"}:
            kept.append(f"{key}=<REDACTED>")
        elif sep:
            kept.append(part)
    return f"{path}?{'&'.join(kept)}" if kept else path


def request_with_retry(client, method: str, url: str, *, attempts: int = 3,
                       delay: float = 3.0, **kwargs):
    """
    One e-hentai request, retried **lightly** on transient connection failures.

    This handles a bare

        httpx.ConnectTimeout: [WinError 10060]

    which is not a wrong URL, a missing cookie, or a deleted gallery, and used to surface
    as "處理時發生錯誤，請稍後再試" with a traceback for something retrying fixes.

    **The retry count is deliberately low, and this was learned the hard way.** An
    earlier version used five attempts with doubling backoff. Because the *whole reply*
    is not refused - the front page kept answering 200 while gallery paths were refused -
    those retries piled onto a host that was already unhappy. During testing that reached
    roughly 120 requests against e-hentai, and the host then started refusing gallery
    paths with

        HTTP 200, 39 bytes: "Key missing, or incorrect key provided."

    which persisted afterwards. **Retrying harder made the block worse, not better.**
    Three attempts with a short fixed delay is a compromise: enough to ride out a dropped
    connection, not enough to look like a flood.

    Only connection-level failures are retried. An HTTP status is a considered answer -
    404 means the gallery really is gone - and retrying it would repeat the refusal while
    making the user wait.

    Raises EhentaiError once the attempts are exhausted, so every caller reports the same
    thing rather than leaking httpx exception types to the user.
    """
    import httpx

    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            # The path only, not the whole URL. The archiver URL is
            # ``archiver.php?gid=<gid>&token=<session token>``, so logging it wrote a
            # live session token into the log on every retry.
            logger.warning("e-hentai 請求失敗（第 %s/%s 次，%s），%.0f 秒後重試：%s",
                           attempt, attempts, type(exc).__name__, delay,
                           redact_url(url))
            time.sleep(delay)
    raise EhentaiError(
        f"連線 e-hentai 逾時或中斷（已重試 {attempts} 次）："
        f"{type(last_exc).__name__}。e-hentai 有時會暫時限制連線，請稍後再試。"
    ) from last_exc


def proxy_url() -> str:
    """
    The proxy to send requests through, or "" for a direct connection.

    **This has to be passed explicitly, and that is the whole reason it exists.**
    httpx does *not* read the Windows registry proxy, so on this machine every request
    went out directly and timed out, while the same URL worked in a browser and worked
    from httpx the moment ``proxy=`` was given:

        httpx, no explicit proxy   -> ConnectTimeout
        httpx, proxy=...           -> HTTP 200

    An explicit environment variable wins, so this stays overridable. ``ALL_PROXY`` and
    friends are honoured because that is what a command-line user would set.
    """
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                 "ALL_PROXY", "all_proxy"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return _registry_proxy()


def _registry_proxy() -> str:
    """The Windows system proxy, in the form httpx accepts, or ""."""
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return ""
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except (OSError, ImportError):
        return ""

    server = (server or "").strip()
    if not server:
        return ""
    # ProxyServer is normally "host:port", but may be a per-scheme list such as
    # "http=host:port;https=host:port".
    if "=" in server:
        for part in server.split(";"):
            scheme, _, target = part.partition("=")
            if scheme.strip().lower() in {"https", "http"} and target.strip():
                server = target.strip()
                break
        else:
            return ""
    # A bare host:port is used as an HTTP proxy by httpx. The mixed port on this machine
    # speaks both HTTP CONNECT and SOCKS5, and httpx needs the socksio extra for the
    # latter, so the HTTP form is the one that works without another dependency.
    if "://" not in server:
        server = f"http://{server}"
    return server


def http_client_kwargs(timeout: float) -> dict:
    """Keyword arguments for an httpx client that honours the proxy."""
    kwargs: dict = {"timeout": timeout, "follow_redirects": True}
    proxy = proxy_url()
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


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

    client = http_client or httpx.Client(**http_client_kwargs(timeout))
    try:
        # Retried: the throttling described in request_with_retry hits this call too,
        # and it is the first request of every gallery action.
        response = request_with_retry(client, "GET", url, headers=headers)
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


# Domains whose cookies may be sent to e-hentai. Anything else in the file belongs to a
# different site and must not travel with these requests.
COOKIE_DOMAINS = ("e-hentai.org", "exhentai.org", "hath.network")

_cookie_header_cache: dict[tuple, tuple[float, int, str]] = {}
_cookie_header_lock = threading.Lock()


def cookies_header(path) -> str:
    """
    Build a Cookie header from a Netscape cookie file.

    **Memoised on (path, mtime, size)**, because this is called for every request that
    needs the cookie and the file is re-read and re-parsed each time otherwise. An
    archive download builds headers once per worker, and the parallel page fetch does it
    for every request, so the same few hundred bytes were being read from disk dozens of
    times per gallery. Measured at 57 us a call.

    The cache is keyed on the file's mtime and size rather than cleared on write, so a
    re-exported cookie file (which /cookies tells the operator to do) is picked up
    automatically instead of serving a stale header until restart. That correctness
    detail is the whole reason it is safe to cache at all.
    """
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        # Cannot stat it: fall through to the uncached path so the warning is emitted.
        return _read_cookie_header(path)

    with _cookie_header_lock:
        cached = _cookie_header_cache.get(key)
    if cached is not None:
        return cached[2]

    header = _read_cookie_header(path)
    with _cookie_header_lock:
        # Only the current key is kept: the file is a single config artefact, so older
        # revisions cannot be asked for again.
        _cookie_header_cache.clear()
        _cookie_header_cache[key] = (stat.st_mtime_ns, stat.st_size, header)
    return header


def _read_cookie_header(path) -> str:
    """
    Parse a Netscape cookie file into a Cookie header for **e-hentai only**.

    The domain is filtered, and that is a fix rather than tidiness. A cookie file may
    hold cookies for several sites - the one in this repository carried `.pixiv.net` and
    `.x.com` entries alongside the e-hentai ones - and this function used to send
    **every** cookie in the file to every e-hentai request:

        .e-hentai.org            8 cookies   sent
        .exhentai.org            7 cookies   sent
        .x.com                   7 cookies   LEAKED to e-hentai
        .pixiv.net               1 cookie    LEAKED to e-hentai

    That hands another site's session cookies to e-hentai, and it is the kind of thing
    that also makes a site unhappy with the request. Sending only the cookies that belong
    to the host is both correct and what the file format means.
    """
    pairs = []
    try:
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            raw = line.lstrip(" ")
            if raw.startswith("#HttpOnly_"):
                raw = raw[10:]
            elif not raw or raw[0] in ("#", "$"):
                continue
            fields = raw.split("\t")
            if len(fields) != 7:
                continue
            # A leading dot means "this domain and its subdomains", which is why the
            # comparison is a suffix test on the stripped domain.
            domain = fields[0].strip().lstrip(".").lower()
            if not domain:
                # A host-only cookie row with no domain: keep it, it belongs to nobody else.
                pairs.append(f"{fields[5]}={fields[6]}")
                continue
            if any(domain == allowed or domain.endswith("." + allowed)
                   for allowed in COOKIE_DOMAINS):
                pairs.append(f"{fields[5]}={fields[6]}")
    except OSError as exc:
        logger.warning("讀取 e-hentai cookie 檔案失敗：%s（%s）", path, exc)
    return "; ".join(pairs)


# -------------------- viewer pages (image URLs) --------------------

# Both hosts, because the domain used to be hard-coded to e-hentai.org and that made the
# whole viewer-page feature silently dead on exhentai (the 裡站):
#
#   e-hentai.org   https://e-hentai.org/s/...  -> 27 matches
#   exhentai.org   https://exhentai.org/s/...  ->  0 matches
#
# The failure surfaced as "讀不到畫廊的圖片清單。連結可能有誤，或畫廊已被刪除。" - the page
# was fine, the pattern simply never matched it.
_VIEWER_LINK_RE = re.compile(
    r'<a href="(https://(?:e-hentai\.org|exhentai\.org)/s/[^"]+)"')
_VIEWER_IMG_RE = re.compile(r'<img id="img" src="([^"]+)"')
_VIEWER_ORIGINAL_RE = re.compile(r'href="(https://[^"]*fullimg[^"]*)"')
# "Showing 1 - 20 of 27 images" - exhentai paginates at 20 per page while e-hentai
# returns everything at once, so the total has to be read and further pages followed or
# a 27-page gallery silently yields only 20.
_SHOWING_RE = re.compile(r"Showing\s+\d+\s*-\s*\d+\s+of\s+(\d+)", re.I)

# Hosts whose image URLs require the session, and therefore cannot be embedded anywhere
# else. Measured on a real gallery page:
#
#     <img id="img" src="https://<node>.hath.network/h/...">        public - 200 image/webp
#     <a href="https://exhentai.org/fullimg/3778922/1/xxx/001.jpg">  session - 302 to login
#
# The `/fullimg/` form is the **original** image. An anonymous request to it gets a 302 to
# the login page, so a Telegraph page embedding it shows a broken image on every page -
# which is exactly what was reported.
_SESSION_BOUND_IMAGE_HOSTS = ("e-hentai.org", "exhentai.org", "www.e-hentai.org",
                              "www.exhentai.org")


def is_session_bound_image(url: str) -> bool:
    """Whether an image URL needs the session and so cannot be embedded elsewhere."""
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        return False
    return host in _SESSION_BOUND_IMAGE_HOSTS


def _resolve_headers(cookies_path) -> dict:
    """
    Headers for resolving an original image: the session, and a referer the viewer uses.

    The referer matters here because ``/fullimg/`` is reached from a viewer page, and the
    redirect is what carries the signed target.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://e-hentai.org/",
    }
    if cookies_path is not None:
        headers["Cookie"] = cookies_header(cookies_path)
    return headers


def resolve_public_image_url(url: str, cookies_path=None, *, timeout: float = 30.0,
                             http_client=None) -> str | None:
    """
    Turn a session-bound image URL into the public CDN URL it redirects to, or None.

    **This is what makes 原圖 embeddable.** The original image is served from
    ``/fullimg/...``, which requires the session; an anonymous request gets a 302 to the
    login page, so a Telegraph page embedding it is broken on every page. The redirect
    target, however, is on ``hath.network`` and **is publicly fetchable** - measured
    anonymously: ``200 image/jpeg, 6121400B`` for one page.

    One request per image: the redirect is read without following it (``Location`` is the
    answer) and the target is confirmed with a **HEAD**, not a GET. That distinction is not
    cosmetic - measuring showed this CDN ignores a ``Range`` header and answers a ranged GET
    with the whole 6.1 MB file, so verifying with a GET would download every original.

    Returns None when the URL does not redirect, which the caller treats as "cannot embed".
    """
    import httpx

    client = http_client or httpx.Client(**http_client_kwargs(timeout))
    try:
        headers = _resolve_headers(cookies_path)
        response = client.get(url, headers=headers, follow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            return None
        target = (response.headers.get("location") or "").strip()
        if not target.startswith("http"):
            return None
        # Confirmed public with a HEAD; a GET here would transfer the whole file.
        try:
            probe = client.head(target, follow_redirects=True)
            if probe.status_code >= 400:
                logger.debug("原圖的公開網址回應 %s：%s", probe.status_code, target[:70])
                return None
        except Exception:  # noqa: BLE001
            # A CDN that refuses HEAD is not proof of a bad URL, and the redirect itself
            # already established the target. Kept rather than discarded.
            logger.debug("無法以 HEAD 確認原圖網址，仍採用：%s", target[:70])
        return target
    except Exception as exc:  # noqa: BLE001
        logger.debug("解析原圖公開網址失敗：%s（%s）", url[:70], exc)
        return None
    finally:
        if http_client is None:
            client.close()


def resolve_public_image_urls(urls: list[str], cookies_path=None, *,
                              workers: int = 6, timeout: float = 30.0,
                              progress=None) -> list[str]:
    """
    Resolve a whole gallery's session-bound originals to public URLs, keeping order.

    Each entry is either the resolved public URL or, when it could not be resolved, the
    original - so the list keeps its length and order and the caller can decide what to do
    with the failures rather than silently dropping pages.
    """
    import httpx
    from concurrent.futures import ThreadPoolExecutor

    if not urls:
        return []

    resolved: list[str | None] = [None] * len(urls)
    done = 0
    lock = threading.Lock()

    def one(item):
        index, url = item
        return index, resolve_public_image_url(url, cookies_path, timeout=timeout)

    with httpx.Client(**http_client_kwargs(timeout)) as client:
        def one_shared(item):
            index, url = item
            return index, resolve_public_image_url(url, cookies_path, timeout=timeout,
                                                   http_client=client)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for index, result in pool.map(one_shared, enumerate(urls)):
                with lock:
                    resolved[index] = result
                    done += 1
                    if progress:
                        progress(done, len(urls))

    return [r or u for r, u in zip(resolved, urls)]


def gallery_viewer_links(page_html: str) -> list[str]:
    """
    Every viewer-page link on a gallery page, in page order.

    The order matters: it is the gallery's own page order.

    **A gallery may be paginated, and this only reads the page it is given.**
    ``gallery_page_count`` reports how many pages the site says there are, and the caller
    follows the rest with ``?p=N``. e-hentai serves its whole grid in one response even
    for a 53-page gallery, but exhentai shows 20 at a time - so reading only the first
    page turned a 27-image gallery into a 20-image Telegraph page, silently.
    """
    return list(dict.fromkeys(_VIEWER_LINK_RE.findall(page_html)))


def gallery_page_count(page_html: str, links_on_this_page: int) -> int:
    """
    How many gallery pages the site says this gallery has, at least 1.

    Read from "Showing 1 - 20 of 27 images". Returns 1 when the text is absent or does not
    exceed what this page already holds, so a single-page gallery needs no extra request.
    """
    match = _SHOWING_RE.search(page_html or "")
    if not match:
        return 1
    try:
        total = int(match.group(1))
    except (TypeError, ValueError):
        return 1
    if total <= links_on_this_page:
        return 1
    # 20 per page as observed; derive it from this page rather than hard-coding, so a
    # different page size still divides correctly.
    per_page = max(1, links_on_this_page)
    return (total + per_page - 1) // per_page


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

    client = http_client or httpx.Client(**http_client_kwargs(timeout))
    try:
        response = client.get(url, headers=headers)
        if response.status_code >= 400:
            raise EhentaiError(f"e-hentai 回應 HTTP {response.status_code}。")
        links = gallery_viewer_links(response.text)
        if not links:
            raise EhentaiError(
                "讀不到畫廊的圖片清單。連結可能有誤，或畫廊已被刪除。"
            )

        # Follow ?p=1.. when the site says there are more pages than this one holds.
        # exhentai paginates at 20 while e-hentai returns everything at once.
        #
        # The cap is NOT applied while paging. Doing so looked equivalent and was not:
        # exhentai's page size is exactly 20, which is exactly MAX_FILES, so the first
        # page filled the budget and the loop broke immediately - the pagination code was
        # dead on arrival and a 27-image gallery stayed 20. Collect everything, then cap.
        pages = gallery_page_count(response.text, len(links))
        total_available = len(links)
        for page_index in range(1, pages):
            try:
                more = client.get(f"{url}?p={page_index}", headers=headers)
            except Exception as exc:  # noqa: BLE001
                logger.warning("讀取畫廊第 %s 頁失敗：%s", page_index + 1, exc)
                break
            if more.status_code >= 400:
                logger.warning("畫廊第 %s 頁回應 HTTP %s", page_index + 1, more.status_code)
                break
            extra = [link for link in gallery_viewer_links(more.text) if link not in links]
            if not extra:
                # Nothing new: stop rather than loop over identical pages.
                break
            links.extend(extra)
            logger.info("畫廊分頁：第 %s 頁補上 %s 張（共 %s）",
                        page_index + 1, len(extra), len(links))

        total_available = max(total_available, len(links))
        if total_available > max_images:
            # Say so rather than silently shipping a short gallery. Before this, a
            # 27-image gallery produced a 20-image Telegraph page and nothing anywhere
            # said 7 were missing.
            logger.warning("畫廊共 %s 張，超過單次上限 %s 張，只處理前 %s 張",
                           total_available, max_images, max_images)
            if progress is not None:
                try:
                    progress(0, max_images, total_available)
                except TypeError:
                    # Older callers take (done, total) only.
                    progress(0, max_images)

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
    """Raised when a Telegraph page cannot be created or edited."""


@dataclass
class TelegraphPage:
    """
    A published Telegraph page, and what a later edit of it needs.

    `access_token` belongs to the anonymous account that created the page and is returned by
    Telegraph exactly once, at creation - it cannot be fetched again, so it has to be carried
    around. `path` is the part of the URL after the domain and is what `editPage/{path}` needs.

    Returning the URL alone was the reason regenerating had to make a new page: without the token
    there is no way to touch the existing one. See edit_telegraph_page.
    """

    url: str
    path: str
    access_token: str

    def __str__(self) -> str:
        # So a stray f-string that expects the old return type still produces the URL rather than
        # a repr. The old signature returned a str and a caller that was missed would otherwise
        # show "TelegraphPage(url=...)" in a chat.
        return self.url


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


def _telegraph_content(gallery: EhentaiGallery, image_urls, embed_limit_kb: int) -> str:
    """The node tree for a gallery, serialised the way Telegraph wants it."""
    return json.dumps(
        build_telegraph_content(gallery, image_urls, embed_limit_kb=embed_limit_kb),
        ensure_ascii=False,
    )


def _telegraph_post(
    endpoint: str,
    *,
    access_token: str,
    title: str,
    author_name: str,
    author_url: str,
    content: str,
    timeout: float,
    http_client=None,
) -> TelegraphPage:
    """
    POST to createPage or editPage and return the resulting page.

    One helper for both because the request is identical apart from the endpoint, and a second
    copy is a second place for the field list to drift.
    """
    import httpx

    client = http_client or httpx.Client(**http_client_kwargs(timeout))
    try:
        data = {
            "access_token": access_token,
            "title": title[:256],
            "author_name": author_name[:128],
            "content": content,
            "return_content": "false",
        }
        if author_url:
            data["author_url"] = author_url[:512]

        page = client.post(f"{TELEGRAPH_API}/{endpoint}", data=data).json()
        if not page.get("ok"):
            raise TelegraphError(page.get("error") or "Telegraph 回應沒有 ok")
        result = page["result"]
        return TelegraphPage(
            url=result["url"],
            path=result["path"],
            access_token=access_token,
        )
    except TelegraphError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TelegraphError(f"Telegraph 請求失敗：{exc}") from exc
    finally:
        if http_client is None:
            client.close()


def create_telegraph_page(
    gallery: EhentaiGallery,
    image_urls: Sequence[str] | None = None,
    *,
    author_name: str = "",
    author_url: str = "",
    timeout: float = 60.0,
    embed_limit_kb: int = 62,
    http_client=None,
) -> TelegraphPage:
    """
    Publish a gallery to Telegraph and return the page.

    ``image_urls`` embeds the gallery's pages directly; Telegraph fetches them from e-hentai's own
    CDN, so the bot transfers no image bytes at all.

    An anonymous Telegraph account is created per page, which keeps the bot from depending on a
    token that has to be provisioned, and keeps each page independent: if one is removed, the
    others are unaffected.

    **The account's access token is part of the return value**, because edit_telegraph_page needs
    it and Telegraph never returns it again - which is why regenerating used to have to publish a
    second page at a different URL.
    """
    import httpx

    # Telegraph requires both names to be non-empty.
    resolved_author = (author_name or "e-hentai")

    client = http_client or httpx.Client(**http_client_kwargs(timeout))
    try:
        account = client.post(
            f"{TELEGRAPH_API}/createAccount",
            data={"short_name": resolved_author[:32]},
        ).json()
        if not account.get("ok"):
            raise TelegraphError(f"建立 Telegraph 帳號失敗：{account.get('error')}")
        token = account["result"]["access_token"]
    except TelegraphError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TelegraphError(f"Telegraph 請求失敗：{exc}") from exc
    finally:
        if http_client is None:
            client.close()

    return _telegraph_post(
        "createPage",
        access_token=token,
        title=(gallery.title or "Gallery"),
        author_name=resolved_author,
        author_url=author_url,
        content=_telegraph_content(gallery, image_urls, embed_limit_kb),
        timeout=timeout,
        http_client=http_client,
    )


@dataclass
class ImageCheck:
    """The outcome of checking one image URL."""

    url: str
    ok: bool
    reason: str = ""

    @property
    def host(self) -> str:
        try:
            return urllib.parse.urlsplit(self.url).netloc
        except ValueError:
            return ""


@dataclass
class ImageCheckReport:
    """
    What a check of a page's embedded images found.

    `broken` is the useful part: it names each URL that did not come back as a usable image, with
    the reason, so the chat can say how many and the log can say which.
    """

    total: int = 0
    checked: int = 0
    healthy: int = 0
    broken: list = field(default_factory=list)
    stopped_early: bool = False

    @property
    def ok(self) -> bool:
        """True when every URL that was checked came back as a usable image."""
        return not self.broken

    def summary(self) -> str:
        if not self.total:
            return "沒有可檢查的圖片網址"
        if not self.broken:
            if self.stopped_early:
                return f"已檢查 {self.checked} 張，全部正常"
            return f"檢查 {self.checked} 張圖片，全部正常"
        if self.stopped_early:
            # Saying "N of M" would be wrong - the rest were never looked at. What matters to the
            # reader is that the page is damaged and how much was confirmed before stopping.
            return (f"檢查 {self.checked} 張圖片，已發現 {len(self.broken)} 張有問題"
                    f"（尚有 {self.total - self.checked} 張未檢查）")
        return f"檢查 {self.checked} 張圖片，{len(self.broken)} 張有問題"


def _check_one_image(client, url: str, deep: bool) -> ImageCheck:
    """
    Request one image URL and decide whether it is usable.

    **What "usable" means, and why the status alone is not it.** A signed hath.network URL that has
    expired answers 403 or 404, which the status catches. But a node that is up and misconfigured
    can answer 200 with an HTML error page, or with an empty body - both of which draw a broken
    image in a browser and neither of which the status alone reveals. So the content type and the
    length are checked too.

    HEAD is used by default because these are images the bot does not otherwise need, and a ranged
    GET was measured downloading the whole file - these hosts ignore Range, so asking for the first
    kilobyte costs the whole image. ``deep`` fetches a bounded prefix instead, which catches a
    truncated or mislabelled body at the cost of one download per URL.
    """
    try:
        if deep:
            # Streamed so the connection can be abandoned after the first chunk rather than
            # reading an image the bot has no use for.
            with client.stream("GET", url) as response:
                status = response.status_code
                content_type = (response.headers.get("content-type") or "").lower()
                if status >= 400:
                    return ImageCheck(url, False, f"HTTP {status}")
                if content_type and not content_type.startswith("image/"):
                    return ImageCheck(url, False, f"不是圖片（{content_type.split(';')[0]}）")
                for chunk in response.iter_bytes(1024):
                    if not chunk:
                        return ImageCheck(url, False, "內容是空的")
                    if not _looks_like_an_image(chunk):
                        return ImageCheck(url, False, "內容不是圖片格式")
                    return ImageCheck(url, True)
                return ImageCheck(url, False, "內容是空的")
        else:
            response = client.head(url)
            status = response.status_code
            if status >= 400:
                return ImageCheck(url, False, f"HTTP {status}")
            content_type = (response.headers.get("content-type") or "").lower()
            if content_type and not content_type.startswith("image/"):
                # **Strict, and measured before being made strict.** Every working image from this
                # CDN answered with an image content type; an earlier version excused text/html on
                # the theory that some hosts only report the real type on GET - which was invented
                # rather than observed, and it meant a 200 carrying an HTML error page counted as
                # a working image. That is precisely the "damaged" case this is meant to catch.
                return ImageCheck(url, False, f"不是圖片（{content_type.split(';')[0]}）")
            length = response.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) <= 0:
                        return ImageCheck(url, False, "長度為 0")
                except ValueError:
                    pass
            return ImageCheck(url, True)
    except Exception as exc:  # noqa: BLE001
        # A timeout, a refused connection, a DNS failure: all mean the image does not load.
        return ImageCheck(url, False, f"{type(exc).__name__}")


def _looks_like_an_image(chunk: bytes) -> bool:
    """
    Whether these leading bytes are a known image format.

    Checked by magic number rather than by the content type, because the content type is what the
    server claims and the magic number is what the bytes are. JPEG, PNG, GIF, WebP and AVIF are
    what e-hentai's CDN serves.
    """
    if len(chunk) < 12:
        return False
    if chunk.startswith(b"\xff\xd8\xff"):                      # JPEG
        return True
    if chunk.startswith(b"\x89PNG\r\n\x1a\n"):               # PNG
        return True
    if chunk.startswith(b"GIF87a") or chunk.startswith(b"GIF89a"):
        return True
    if chunk[:4] == b"RIFF" and chunk[8:12] == b"WEBP":           # WebP
        return True
    if chunk[4:12] in (b"ftypavif", b"ftypavis"):                 # AVIF
        return True
    return False


def check_image_urls(
    urls: Sequence[str],
    *,
    workers: int = 24,
    timeout: float = 12.0,
    deep: bool = False,
    limit: int = 0,
    stop_after: int = 0,
    progress=None,
) -> ImageCheckReport:
    """
    Check whether every embedded image URL still loads, and returns an image when it does.

    **Every URL, not a sample.** This is what "detect images that fail to load or are damaged"
    means, and a sample cannot answer it: the previous version called a page healthy as soon as one
    of three URLs loaded, so a page with almost everything broken was reported as fine.

    Concurrent because two hundred sequential requests would take minutes, and HEAD requests to a
    CDN are cheap. ``progress`` is called with (done, total) if given.

    ``limit`` caps how many are checked, for a caller that wants a quick opinion rather than a
    guarantee. Zero means all of them.

    ``stop_after`` halts once that many broken URLs have been found. The caller that only needs to
    decide "is this page damaged" gets its answer in the time it takes to find the first one -
    measured at a few seconds on a page whose signatures had all expired. The report records that
    it stopped early, so its counts are not read as the whole picture.
    """
    import httpx

    candidates = [u for u in urls if u]
    if limit and limit > 0:
        candidates = candidates[:limit]
    report = ImageCheckReport(total=len(candidates))
    if not candidates:
        return report

    client = httpx.Client(**http_client_kwargs(timeout))
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(_check_one_image, client, url, deep): url
                       for url in candidates}
            seen = 0
            for future in as_completed(futures):
                seen += 1
                report.checked = seen
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    result = ImageCheck(futures[future], False, f"{type(exc).__name__}")
                if result.ok:
                    report.healthy += 1
                else:
                    report.broken.append(result)
                if progress is not None:
                    try:
                        progress(seen, len(candidates))
                    except Exception:  # noqa: BLE001
                        pass
                if stop_after and len(report.broken) >= stop_after:
                    # **The answer is already known.** One broken image is enough to justify a
                    # repair, so the rest would be requests made for a number nobody acts on. The
                    # futures are cancelled by leaving the with-block, which shuts the pool down.
                    report.stopped_early = seen < len(candidates)
                    break
    finally:
        client.close()
    return report


def telegraph_page_images(path: str, *, timeout: float = 20.0, http_client=None) -> list[str]:
    """
    The image URLs a published Telegraph page embeds, in page order.

    **Read from the page, not from anything the bot stored.** The cache keeps a handful of sampled
    URLs per page, which cannot answer "are this page's images broken" - and a stored list can
    drift from what was actually published. The page's own <img> tags are what a reader's browser
    will try to load, so those are what to check.

    Returns an empty list when the page cannot be read, which the caller must treat as "cannot
    confirm" rather than "healthy" - see check_image_urls.

    Only absolute http(s) sources are returned: the page also references scripts and an image
    proxy, and those are not the gallery's images.
    """
    import httpx

    if not path:
        return []

    client = http_client or httpx.Client(**http_client_kwargs(timeout))
    owns_client = http_client is None
    try:
        response = client.get(f"https://telegra.ph/{path.lstrip('/')}")
        if response.status_code >= 400:
            return []
        page_html = response.text
    except Exception as exc:  # noqa: BLE001
        logger.warning("讀取 Telegraph 頁面失敗（%s）：%s", path, exc)
        return []
    finally:
        if owns_client:
            client.close()

    found: list[str] = []
    seen = set()
    for match in re.finditer(r'<img\b[^>]*?\bsrc=["\']([^"\']+)["\']', page_html, re.I):
        src = html.unescape(match.group(1)).strip()
        if not src.lower().startswith(("http://", "https://")):
            continue
        # The page includes Telegram's own image proxy in some renderings; it is not the gallery.
        if "t.me/" in src:
            continue
        if src not in seen:
            seen.add(src)
            found.append(src)
    return found


def images_are_still_available(
    urls: Sequence[str],
    *,
    sample: int = 3,
    timeout: float = 15.0,
    http_client=None,
) -> bool:
    """
    A quick opinion: whether at least one of a few URLs still loads.

    Kept for the cheap path, where the question is only "is this page obviously dead". It is **not**
    the check behind "detect images that fail to load or are damaged" - use check_image_urls for
    that, which examines every URL and what came back.

    Returns False when there is nothing to check, which is the safe answer.
    """
    import httpx

    candidates = [u for u in urls if u]
    if not candidates:
        return False

    if len(candidates) <= sample:
        chosen = candidates
    else:
        step = max(1, len(candidates) // sample)
        chosen = [candidates[i] for i in range(0, len(candidates), step)][:sample]

    client = http_client or httpx.Client(**http_client_kwargs(timeout))
    owns_client = http_client is None
    try:
        for url in chosen:
            try:
                response = client.head(url)
                if response.status_code < 400:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False
    finally:
        if owns_client:
            client.close()


def edit_telegraph_page(
    page: TelegraphPage,
    gallery: EhentaiGallery,
    image_urls: Sequence[str] | None = None,
    *,
    author_url: str = "",
    timeout: float = 60.0,
    embed_limit_kb: int = 62,
    http_client=None,
) -> TelegraphPage:
    """
    Rewrite an existing Telegraph page in place, at the same URL.

    This is what 重新生成 should always have done. The page keeps its address, so a link already
    shared still works and now shows working images - which is the entire point of regenerating,
    since e-hentai's image URLs are time-limited and that is what breaks.

    ``author_name`` is not a parameter: the page already has one and Telegraph takes it from the
    page when it is omitted, so it cannot be changed here by accident.
    """
    if not page.path or not page.access_token:
        raise TelegraphError("這個頁面沒有可用的編輯憑證，無法保留原網址")

    return _telegraph_post(
        f"editPage/{page.path}",
        access_token=page.access_token,
        title=(gallery.title or "Gallery"),
        author_name="",
        author_url=author_url,
        content=_telegraph_content(gallery, image_urls, embed_limit_kb),
        timeout=timeout,
        http_client=http_client,
    )
