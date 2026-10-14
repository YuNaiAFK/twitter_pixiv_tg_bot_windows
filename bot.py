import asyncio
import importlib.util
import hashlib
import httpx
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlparse

# Optional local dependency folder. It exists because this project can run on a
# host where packages cannot be installed into site-packages; a wheel unpacked
# here is picked up without disturbing a normal installation.
_VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
if _VENDOR_DIR.is_dir() and importlib.util.find_spec("PIL") is None:
    sys.path.append(str(_VENDOR_DIR))

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    LinkPreviewOptions,
    Update,
)
from telegram.error import BadRequest, TelegramError, TimedOut
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import ehentai

# Resolve .env beside this file, not beside the shell's current directory, so the
# bot finds its credentials regardless of where run.bat or a shortcut starts it.
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# -------------------- Configuration --------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
COOKIES_FILE = os.getenv("GALLERY_DL_COOKIES", "").strip()

# A self-hosted Bot API server (tdlib/telegram-bot-api) raises the limits a lot, so
# the bot asks it for its configuration rather than assuming the public one. Set
# TELEGRAM_API_BASE to the server's address to use it, e.g.
#   TELEGRAM_API_BASE=http://127.0.0.1:8081
# and optionally TELEGRAM_LOCAL=1 to announce that the server runs with --local, which
# is what allows uploads by local path and removes the 50 MB ceiling.
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "").strip().rstrip("/")
TELEGRAM_LOCAL_FLAG = os.getenv("TELEGRAM_LOCAL", "").strip().lower() in {
    "1", "true", "yes", "on",
}

# What the active server allows. The defaults describe https://api.telegram.org, and
# probe_telegram_limits() replaces them when a self-hosted server answers.
LOCAL_API_SERVER = False
SERVER_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
SERVER_CAN_UPLOAD_BY_PATH = False

MAX_DOWNLOAD_SECONDS = int(os.getenv("MAX_DOWNLOAD_SECONDS", "180"))
MAX_FILES = int(os.getenv("MAX_FILES", "20"))
# Kept as its own name as well as the byte form: the operator summary reports it in
# MB, and deriving that from the bytes at each call site is how a report ends up
# referencing a name that does not exist.
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "45"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
USER_COOLDOWN_SECONDS = int(os.getenv("USER_COOLDOWN_SECONDS", "10"))

# How long a claimed processing slot stays valid. If a request dies without
# releasing it, the slot is ignored after this so a user cannot be locked out.
REQUEST_STALE_SECONDS = int(os.getenv("REQUEST_STALE_SECONDS", "1800"))

# A restart's output is appended here by each replacement process, so it is rotated
# once it passes this size to keep it from growing without limit.
RESTART_LOG_MAX_BYTES = int(os.getenv("RESTART_LOG_MAX_MB", "2")) * 1024 * 1024

# How long a replacement waits before it starts polling. The process being replaced
# needs a moment to release Telegram's getUpdates lock; without this wait the two
# briefly poll at once and the new one is killed by
# "Conflict: terminated by other getUpdates request".
RESTART_STARTUP_DELAY_SECONDS = float(
    os.getenv("RESTART_STARTUP_DELAY", "6"))

# -------------------- Login credentials --------------------
#
# Login state for X/Twitter and Pixiv is supplied as Netscape-format cookie files
# exported from a logged-in browser. Cookies are used rather than passwords
# because X/Twitter no longer offers a usable password login for this kind of
# client and Pixiv's password flow trips captchas.
#
# A per-site file wins over the shared one, so a single exported file that holds
# both sites can live in GALLERY_DL_COOKIES while one stubborn site gets its own.
TWITTER_COOKIES_FILE = os.getenv("TWITTER_COOKIES", "").strip()
PIXIV_COOKIES_FILE = os.getenv("PIXIV_COOKIES", "").strip()

# Sanity check for the parts of the credential file the bot can verify locally.
CHECK_COOKIES = os.getenv("CHECK_COOKIES", "1").strip().lower() not in {
    "0", "false", "no", "off",
}

# Warn once a credential has this many days or fewer left, so the cookies can be
# re-exported before downloads start failing.
EXPIRY_WARN_DAYS = int(os.getenv("EXPIRY_WARN_DAYS", "14"))

# Remove the clutter a request creates once it succeeds: the "parsing..." notice,
# and the original message that carried the link.
#
# Deleting the bot's own status message works in a private chat and in a group.
# Deleting the *sender's* message in a group needs the bot to be an administrator
# with the "delete messages" right; without it Telegram refuses, which is logged
# once and otherwise ignored rather than treated as a failed request.
DELETE_STATUS_MESSAGE = os.getenv("DELETE_STATUS_MESSAGE", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
DELETE_SOURCE_MESSAGE = os.getenv("DELETE_SOURCE_MESSAGE", "1").strip().lower() not in {
    "0", "false", "no", "off",
}

# Set once, so a group without delete rights logs one warning instead of one per
# request.
_warned_cannot_delete = False

# -------------------- e-hentai / exhentai --------------------
#
# A gallery is converted into a Telegraph article: the page carries the cover,
# the gallery's own facts, and every tag grouped by its namespace. Adult (H)
# galleries need an e-hentai login; without one they answer "NotFound".
EHENTAI_COOKIES_FILE = os.getenv("EHENTAI_COOKIES", "").strip()

# Telegraph's terms forbid adult material, so a page built for an H gallery can be
# removed. Building it is the default because that is what was asked for; set this
# to 0 to answer adult galleries with the info text only.
TELEGRAPH_ALLOW_ADULT = os.getenv(
    "TELEGRAPH_ALLOW_ADULT", "1").strip().lower() not in {"0", "false", "no", "off"}

# Shown as the Telegraph page author.
TELEGRAPH_AUTHOR = os.getenv("TELEGRAPH_AUTHOR", "e-hentai 解析").strip()
# The gallery page is fetched directly, which takes about a second; the timeout is
# generous only to absorb a slow network or e-hentai rate limiting.
EHENTAI_TIMEOUT_SECONDS = int(os.getenv("EHENTAI_TIMEOUT", "60"))

# Downloading every page is much slower than reading metadata: a 53-page original
# set transfers hundreds of megabytes. This is separate on purpose, so a slow
# network does not make the fast metadata path look broken.
EHENTAI_DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("EHENTAI_DOWNLOAD_TIMEOUT", "900"))

# Parallel image downloads from e-hentai's CDN. Measured on a 5-page gallery:
# 5 images took 5.5s at 5 workers, against 29s for gallery-dl's default pacing.
EHENTAI_DOWNLOAD_WORKERS = int(os.getenv("EHENTAI_DOWNLOAD_WORKERS", "6"))

# Viewer pages are plain page views rather than metered image downloads, so a
# handful of workers is fine; measured 1.91s sequential vs 0.44s at 8.
EHENTAI_VIEWER_WORKERS = int(os.getenv("EHENTAI_VIEWER_WORKERS", "6"))

# Connections used to fetch one official archive. e-hentai's CDN throttles each
# connection hard, so one stream leaves the link idle. Measured on a 16 MiB prefix of
# a real 148 MiB archive:
#     1 connection    1.68 MiB/s
#     4 connections   3.34 MiB/s   (2.0x)
#     8 connections   3.38 MiB/s   (saturated - no gain past 4)
# Four is the point past which it stops paying, so the default sits there and the code
# caps itself at 8 regardless of what is configured.
EHENTAI_ARCHIVE_WORKERS = int(os.getenv("EHENTAI_ARCHIVE_WORKERS", "4"))

# Below this, parallel range requests cost more in round trips than they save, so a
# small archive is fetched in one stream.
ARCHIVE_PARALLEL_MIN_BYTES = int(
    os.getenv("ARCHIVE_PARALLEL_MIN_BYTES", str(8 * 1024 * 1024)))

# e-hentai's own namespace display names, overridable so a Chinese-only page can
# be produced without editing the module.
EHENTAI_NAMESPACE_LABELS = ehentai.NAMESPACE_LABELS

# -------------------- Supported sites --------------------
#
# X/Twitter, Pixiv and e-hentai galleries are resolved. Any other link gets a
# short notice instead of being fetched, which keeps the bot from becoming a
# general-purpose link fetcher.
SUPPORTED_SITES_NOTICE = os.getenv(
    "SUPPORTED_SITES_NOTICE",
    "目前只支援 X/Twitter、Pixiv 與 e-hentai 連結。",
).strip()

# The bot's own profile text, re-published on every startup.
#
# This exists because the description was **hijacked**: it was found holding a referral
# link to an unrelated adult site, in two languages. Nothing in this repository has ever
# set a description, so the text could only have been written by someone else holding the
# bot token - and a token that can set a description can also read updates and send
# messages on the bot's behalf.
#
# Setting it here makes the profile self-healing, but it repairs only the symptom. The
# token is the actual problem: rotate it with BotFather. See the README.
#
# Overridable so the operator can adjust the wording without editing code.
BOT_DESCRIPTION = os.getenv("BOT_DESCRIPTION", (
    "把 X/Twitter、Pixiv 或 e-hentai 的連結傳給我，我會把內容整理好傳回來。\n"
    "• X/Twitter：圖片直接傳回聊天\n"
    "• Pixiv：傳送原圖檔案\n"
    "• e-hentai：整理成 Telegraph 頁面，附標籤分類\n"
    "每則結果都會附上原始連結。"
)).strip()

# Telegram caps the short description at 120 characters.
BOT_SHORT_DESCRIPTION = os.getenv(
    "BOT_SHORT_DESCRIPTION",
    "傳送 X/Twitter、Pixiv 或 e-hentai 連結，我把內容整理好傳回給你。",
).strip()[:120]

# Reply to a link on an unsupported site. Set to 0 to stay silent instead, which
# suits a busy public group where the notice would just be noise.
REPLY_UNSUPPORTED_LINKS = os.getenv(
    "REPLY_UNSUPPORTED_LINKS", "1").strip().lower() not in {"0", "false", "no", "off"}

# Where downloads are staged before sending. Set this to a drive with free
# space; the default (system TEMP, usually C:) can fill up on large artworks.
WORK_DIR = os.getenv("WORK_DIR", "").strip()

# gallery-dl keeps a SQLite cache (login tokens, transaction keys) under
# %APPDATA% by default, which fails on a host where that profile is read-only
# and then breaks X/Twitter and Pixiv outright. A generated config repoints it
# next to the bot instead. Set GALLERY_DL_CONFIG to use your own config file.
GALLERY_DL_CONFIG = os.getenv("GALLERY_DL_CONFIG", "").strip()
GALLERY_DL_CACHE_DIR = os.getenv("GALLERY_DL_CACHE", ".gallery-dl-cache").strip()

# Pixiv artwork pages are fetched through Pixiv's App API, which authenticates
# with an OAuth refresh token rather than a cookie. A PHPSESSID cookie alone is
# enough for pixiv.net's own web endpoints but not for this extractor, so the
# token is configured separately. Obtain it once with:
#     .venv\Scripts\gallery-dl.exe oauth:pixiv
PIXIV_REFRESH_TOKEN = os.getenv("PIXIV_REFRESH_TOKEN", "").strip()

# Telegram hard limits, independent of MAX_UPLOAD_MB.
PHOTO_LIMIT_BYTES = 10 * 1024 * 1024
CAPTION_LIMIT = 1024
ALBUM_LIMIT = 10

# Attach the source link to everything the bot sends back.
ATTACH_SOURCE_LINK = os.getenv("ATTACH_SOURCE_LINK", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
SOURCE_LABEL = os.getenv("SOURCE_LABEL", "原始連結")

# Optional Pillow-based downscale for photos over Telegram's photo limit.
PHOTO_DOWNSCALE_MAX_EDGE = int(os.getenv("PHOTO_DOWNSCALE_MAX_EDGE", "2560"))
PHOTO_DOWNSCALE_QUALITY = int(os.getenv("PHOTO_DOWNSCALE_QUALITY", "88"))

# Empty means everyone can use the bot. Otherwise, list numeric Telegram user IDs.
ALLOWED_USER_IDS = {
    int(item.strip())
    for item in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if item.strip().isdigit()
}

# Runtime-managed authorization. The .env values above are the starting point
# (and a bootstrap for the first administrator, which cannot be granted in-chat
# for the obvious reason); everything else can be managed with /grant, /revoke,
# /promote, /demote and /lock, and survives restarts through this file.
AUTH_FILE = Path(os.getenv("AUTH_FILE", str(BASE_DIR / "authorized.json")))

# Whether the authorized list is a restriction. With False - the default - an empty
# list means everyone may parse, so enabling the list would otherwise silently cut
# off every existing user. With True, an empty list means nobody may parse, which
# is a deliberate choice an operator has to make.
LOCKED_DEFAULT = os.getenv("AUTH_LOCKED", "0").strip().lower() in {
    "1", "true", "yes", "on",
}

# The same choice for group chats, kept separate because it answers a different
# question. With only AUTH_LOCKED set, private chats were gated while every group
# stayed usable - the bot looked restricted and was not.
GROUP_LOCKED_DEFAULT = os.getenv("AUTH_GROUP_LOCKED", "0").strip().lower() in {
    "1", "true", "yes", "on",
}

ADMIN_USER_IDS = {
    int(item.strip())
    for item in os.getenv("ADMIN_USER_IDS", "").split(",")
    if item.strip().isdigit()
}


class Authorization:
    """
    Who may use the bot: people, group chats, and the people who administer it.

    Stored as JSON rather than only in `.env` because the point of the interface is
    to change it while the bot runs: editing `.env` requires host access, which is
    exactly what an operator in Telegram does not have.

    `locked` decides what an empty **user** list means, and that is the one decision
    here a reader is most likely to get wrong:

        locked=False (default)  empty list -> everyone may parse
                                the list is a blocklist-style restriction
        locked=True             empty list -> nobody may parse
                                the list is a closed allowlist

    Being locked out entirely is worse than being open, so an empty list means
    "open" unless that meaning was chosen on purpose. Administrators always pass,
    in both modes, so a locked bot can still be unlocked from within.

    Group chats are a separate list **and a separate mode**, because "which groups may
    use this" is a different question from "which people may use this":

        no groups authorized, group_locked=False   every group may parse
        no groups authorized, group_locked=True    no group may parse
        some groups authorized                     only those groups may parse

    The list wins once it has anything in it. `locked` does **not** govern groups:
    with only `locked=True`, every group stayed wide open, which is how a bot could
    look restricted in private chats while any group could still use it.
    """

    def __init__(self, allowed=None, admins=None, groups=None,
                 locked: bool = False, group_locked: bool = False):
        self.allowed: set[int] = set(allowed or ())
        self.admins: set[int] = set(admins or ())
        # Group chat IDs. Telegram gives groups negative IDs, and supergroups are
        # represented as -100…, so these are stored as they arrive.
        self.groups: set[int] = set(groups or ())
        self.locked: bool = bool(locked)
        self.group_locked: bool = bool(group_locked)

    # -------------------- persistence --------------------

    @classmethod
    def load(cls, path: Path, *, allowed=None, admins=None, groups=None,
             locked: bool = False, group_locked: bool = False) -> "Authorization":
        """
        Read the file, falling back to the .env values.

        A corrupt or unreadable file must not take the bot down or silently widen
        access, so the caller's values are used and the problem is logged. Reading
        failure is treated as "no runtime overrides", which is the state the .env
        alone describes.
        """
        state = cls(allowed=allowed, admins=admins, groups=groups, locked=locked,
                    group_locked=group_locked)
        if not path.exists():
            return state
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("無法讀取授權檔 %s，改用 .env 設定：%s", path, exc)
            return state
        if not isinstance(raw, dict):
            logger.warning("授權檔格式不正確，改用 .env 設定：%s", path)
            return state

        def ids(key):
            values = raw.get(key, [])
            if not isinstance(values, list):
                return set()
            return {int(v) for v in values if isinstance(v, int)
                    or (isinstance(v, str) and v.strip().lstrip("-").isdigit()
                        and v.strip().lstrip("-").isdigit())}

        state.allowed = ids("allowed")
        state.admins = ids("admins")
        state.groups = ids("groups")
        state.locked = bool(raw.get("locked", locked))
        state.group_locked = bool(raw.get("group_locked", group_locked))
        return state

    def save(self, path: Path) -> bool:
        """
        Write the file atomically, reporting success rather than raising.

        A write that fails is reported to the operator in chat, so raising here
        would turn a failed /grant into a crash instead of a message. The rename is
        what makes it atomic: a half-written file would otherwise be read back at
        the next start as a truncated authorization list.
        """
        payload = {
            "allowed": sorted(self.allowed),
            "admins": sorted(self.admins),
            "groups": sorted(self.groups),
            "locked": self.locked,
            "group_locked": self.group_locked,
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("無法寫入授權檔 %s：%s", path, exc)
            try:
                temporary.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            return False

    # -------------------- queries --------------------

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admins

    def user_allowed(self, user_id: int) -> bool:
        """
        Whether this user may parse, judging only the user-level lists.

        Administrators always pass, even in locked mode, so a mistake in the list
        cannot leave nobody able to fix it.
        """
        if self.is_admin(user_id):
            return True
        if self.locked:
            return user_id in self.allowed
        if not self.allowed:
            return True
        return user_id in self.allowed

    def group_allowed(self, chat_id: int) -> bool:
        """
        Whether this group chat may parse.

        A listed group always passes. With no groups listed, `group_locked` decides:
        False means every group is welcome (the original behaviour), True means no
        group is. The list wins once it has anything in it, so a locked bot with
        authorized groups serves exactly those.
        """
        if chat_id in self.groups:
            return True
        if self.groups:
            return False
        return not self.group_locked

    def is_allowed(self, user_id: int, chat_id: int | None = None,
                   chat_type: str | None = None) -> bool:
        """
        Whether this request may use the parsing features.

        In a group, the group's authorization is what counts: that is the whole
        point of authorizing a group, and it is why a member of an authorized group
        is admitted even when the user list is locked down. A private chat has no
        group to speak for it, so the user-level rules apply there.

        An administrator passes everywhere, including in a group that is not on the
        list, so an operator is never locked out of their own bot.
        """
        if self.is_admin(user_id):
            return True
        if chat_type in {"group", "supergroup"} and chat_id is not None:
            return self.group_allowed(chat_id)
        return self.user_allowed(user_id)

    @property
    def mode_label(self) -> str:
        if self.locked:
            return "守門模式（只有名單內的人可解析）"
        return "開放模式（所有人可解析，名單為限制）"

    @property
    def group_mode_label(self) -> str:
        if self.group_locked:
            return "守門模式（只有已授權群組可解析）"
        return "開放模式（所有群組可解析）"

    @property
    def group_label(self) -> str:
        if self.groups:
            return f"只有已授權的 {len(self.groups)} 個群組可以解析"
        if self.group_locked:
            return "沒有已授權群組，且為守門模式：所有群組都不能解析"
        return "所有群組都可以解析"

    # -------------------- mutations --------------------

    def grant(self, user_id: int) -> bool:
        """Allow a user. Returns whether anything changed."""
        if user_id in self.allowed:
            return False
        self.allowed.add(user_id)
        return True

    def revoke(self, user_id: int) -> bool:
        if user_id not in self.allowed:
            return False
        self.allowed.discard(user_id)
        return True

    def promote(self, user_id: int) -> bool:
        changed = user_id not in self.admins
        self.admins.add(user_id)
        # An administrator is implicitly allowed, so a stale entry would only be
        # confusing in the listing.
        self.allowed.discard(user_id)
        return changed

    def demote(self, user_id: int) -> bool:
        if user_id not in self.admins:
            return False
        self.admins.discard(user_id)
        return True

    def grant_group(self, chat_id: int) -> bool:
        """Authorize a group chat. Returns whether anything changed."""
        if chat_id in self.groups:
            return False
        self.groups.add(chat_id)
        return True

    def revoke_group(self, chat_id: int) -> bool:
        if chat_id not in self.groups:
            return False
        self.groups.discard(chat_id)
        return True


AUTHORIZATION = Authorization.load(
    AUTH_FILE, allowed=ALLOWED_USER_IDS, admins=ADMIN_USER_IDS,
    locked=LOCKED_DEFAULT, group_locked=GROUP_LOCKED_DEFAULT,
)

# Administrators are declared above, beside the authorization store that owns both
# lists at runtime. An empty list means the owner has not declared one, and the
# first person to run an admin command is refused with instructions rather than
# silently being trusted - on a public bot, an unset admin list must not mean
# "anyone".

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

# Chinese text often follows a URL with no space: "看这个 https://x.com/a/1，很好".
# URL_RE cannot stop there, so the cut must happen at the first CJK character
# or full-width punctuation, otherwise "，很好" stays glued to the URL and the
# download fails.
CJK_CUT_RE = re.compile(
    r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}
ANIMATION_EXTENSIONS = {".gif"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | ANIMATION_EXTENSIONS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("twitter_pixiv_bot")

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
last_request_time = {}

# Set once when Pillow is missing, so the warning is not logged per photo.
_pillow_unavailable = False


# -------------------- URL helpers --------------------

def clean_url(url: str) -> str:
    """
    Normalise a URL found inside a chat message.

    Cuts at the first CJK character or full-width punctuation (Chinese prose
    often continues right after a link with no space), then strips trailing
    ASCII/full-width sentence punctuation and closing brackets. Covering both
    widths matters: using the wrong width silently keeps the character, which
    then breaks the download.
    """
    url = CJK_CUT_RE.split(url, maxsplit=1)[0]
    return url.rstrip(
        " \t\r\n"
        ".,;:!?"          # ASCII sentence punctuation
        "，。；：！？"      # full-width equivalents
        "\"'`"            # quotes
        ")]}>》」』】）］｝〕"  # closing brackets, ASCII and full-width
    )


def get_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


# -------------------- Parse cache --------------------
#
# Reusing a previous parse is the difference between a reply that appears at once
# and one that waits on a download. Two things are cached, because the expensive
# work is different in each case:
#
#   e-hentai  the gallery's facts and the image URLs, plus the Telegraph page that
#             was published. Re-parsing means one request per page for the viewer
#             sweep, which is about a minute on a 53-page gallery.
#   media     the downloaded files themselves, copied out of the per-request
#             temporary directory into this cache, so a repeat costs no download.
#
# gallery-dl is additionally given a permanent `--download-archive`; that is the
# backstop when a cached copy has been evicted, and it stops it fetching bytes it
# has already fetched.
#
# What is deliberately NOT cached is the failure case: a link that failed is
# retried, because the usual cause is a transient one or a credential that the
# operator has since fixed.
CACHE_FILE = Path(os.getenv("CACHE_FILE", str(BASE_DIR / "parse_cache.json")))
CACHE_DIR = Path(os.getenv("CACHE_DIR", str(BASE_DIR / ".cache")))

# A media cache is a copy of files the bot already downloaded. Kept bounded so it
# cannot fill the disk: entries older than this are dropped when the cache is
# pruned, and the oldest are evicted first.
CACHE_STALE_SECONDS = int(os.getenv("CACHE_STALE_SECONDS", str(14 * 24 * 3600)))
CACHE_MAX_ENTRIES = int(os.getenv("CACHE_MAX_ENTRIES", "300"))

# e-hentai's page images are served from signed, time-limited URLs, and the signature
# lives far less long than the 14-day cache entry that carries it. A cached sweep that
# outlives its URLs makes EVERY page fail at once, which then dropped the download into
# the serial gallery-dl fallback - the "補齊缺少的頁面" step. Keeping the URL list on a
# much shorter clock means a stale list is refreshed instead of being discovered by
# failing.
GALLERY_URL_TTL_SECONDS = int(os.getenv("GALLERY_URL_TTL_SECONDS", str(2 * 3600)))


def normalize_cache_key(url: str) -> str:
    """
    The key a parse is stored under.

    The URL is already cleaned by the time it arrives, so this only removes the
    parts that do not change which artwork is meant - a query string or fragment
    such as `?s=20` or `#comments`. Without that, the same artwork reached through
    two links would be downloaded twice.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url.strip()
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


class ParseCache:
    """
    Parsed results that survive a restart.

    Written atomically for the same reason the authorization file is: a truncated
    file must not be read back as a cache of nothing. A corrupt file is discarded
    rather than fatal - a cache is an optimisation, so losing it costs time and
    nothing else.
    """

    def __init__(self, path: Path, *, stale_seconds: int = CACHE_STALE_SECONDS,
                 max_entries: int = CACHE_MAX_ENTRIES):
        self.path = path
        self.stale_seconds = stale_seconds
        self.max_entries = max_entries
        # key -> {"kind", "payload", "at"}
        self.entries: dict[str, dict] = {}
        self.hits = 0
        self.misses = 0

    # -------------------- persistence --------------------

    def load(self) -> "ParseCache":
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("無法讀取解析快取 %s，將重新建立：%s", self.path, exc)
            return self
        if not isinstance(raw, dict):
            logger.warning("解析快取格式不正確，將重新建立：%s", self.path)
            return self
        for key, entry in raw.items():
            if (isinstance(entry, dict) and "payload" in entry
                    and isinstance(entry.get("at"), (int, float))):
                self.entries[key] = entry
        return self

    def save(self) -> bool:
        payload = json.dumps(self.entries, ensure_ascii=False)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(payload + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("無法寫入解析快取 %s：%s", self.path, exc)
            try:
                temporary.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            return False

    # -------------------- queries --------------------

    def is_stale(self, entry: dict, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (now - float(entry.get("at", 0))) > self.stale_seconds

    def lookup(self, url: str, *, kind: str | None = None) -> dict | None:
        """
        The cached payload for a URL, or None.

        A stale entry is treated as absent rather than deleted, so a caller that
        then re-parses simply overwrites it.
        """
        entry = self.entries.get(normalize_cache_key(url))
        if entry is None:
            self.misses += 1
            return None
        if kind is not None and entry.get("kind") != kind:
            self.misses += 1
            return None
        if self.is_stale(entry):
            self.misses += 1
            return None
        self.hits += 1
        return entry.get("payload")

    def age(self, url: str) -> float | None:
        entry = self.entries.get(normalize_cache_key(url))
        if entry is None:
            return None
        return time.time() - float(entry.get("at", 0))

    # -------------------- mutations --------------------

    def store(self, url: str, kind: str, payload) -> None:
        self.entries[normalize_cache_key(url)] = {
            "kind": kind,
            "payload": payload,
            "at": time.time(),
        }

    def forget(self, url: str) -> bool:
        return self.entries.pop(normalize_cache_key(url), None) is not None

    def prune(self) -> int:
        """
        Drop stale entries and, if still too many, the oldest. Returns how many.

        Bounded on both axes because a media entry points at files on disk: an
        unbounded entry list would grow without limit, and dropping the entry
        without removing the files would leak disk space that nothing references.
        """
        removed = 0
        for key in [k for k, e in self.entries.items() if self.is_stale(e)]:
            self.discard_files(self.entries[key])
            self.entries.pop(key, None)
            removed += 1

        overflow = len(self.entries) - self.max_entries
        if overflow > 0:
            ordered = sorted(self.entries.items(), key=lambda kv: kv[1].get("at", 0))
            for key, entry in ordered[:overflow]:
                self.discard_files(entry)
                self.entries.pop(key, None)
                removed += 1
        return removed

    def needs_pruning(self) -> bool:
        """
        Whether prune() would remove anything.

        Checked before pruning, so a request does not walk the whole table on every
        parse just to establish that nothing is stale.
        """
        if len(self.entries) > self.max_entries:
            return True
        now = time.time()
        return any(self.is_stale(entry, now) for entry in self.entries.values())

    @staticmethod
    def discard_files(entry: dict) -> None:
        """Remove the cached files an entry points at, ignoring failures."""
        files = (entry.get("payload") or {}).get("files") or []
        for name in files:
            try:
                (CACHE_DIR / name).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                logger.debug("無法刪除快取檔案 %s", name, exc_info=True)

    def clear(self) -> int:
        """Forget everything, files included."""
        count = len(self.entries)
        for entry in self.entries.values():
            self.discard_files(entry)
        self.entries.clear()
        try:
            self.path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return count

    @property
    def summary(self) -> str:
        kinds: dict[str, int] = {}
        for entry in self.entries.values():
            kinds[entry.get("kind", "?")] = kinds.get(entry.get("kind", "?"), 0) + 1
        parts = ", ".join(f"{k} {v}" for k, v in sorted(kinds.items()))
        return f"{len(self.entries)} 筆（{parts}）" if parts else "0 筆"


PARSE_CACHE = ParseCache(CACHE_FILE).load()


def cache_media_files(files: list[Path], url: str) -> list[str]:
    """
    Copy downloaded files into the cache and return their cached names.

    Copies rather than moves: the originals belong to the request's temporary
    directory, which the caller deletes, and the caller may still need them to send
    the current reply. A file that cannot be copied is simply not cached, so a
    failure here costs a future re-download rather than the current reply.
    """
    names: list[str] = []
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("無法建立快取目錄 %s：%s", CACHE_DIR, exc)
        return names

    key = hashlib.sha1(normalize_cache_key(url).encode("utf-8")).hexdigest()[:12]
    for index, path in enumerate(files, 1):
        name = f"{key}_{index:03d}{path.suffix.lower()}"
        target = CACHE_DIR / name
        try:
            if not target.exists():
                shutil.copy2(path, target)
            names.append(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("無法快取檔案 %s：%s", path, exc)
    return names


def cached_media_files(payload: dict) -> list[Path]:
    """
    The cached files a payload refers to, or an empty list.

    Every file must still be present. A partially missing set is treated as no
    cache at all, because sending half an artwork as though it were the whole thing
    would be worse than downloading it again.
    """
    names = (payload or {}).get("files") or []
    if not names:
        return []
    paths = [CACHE_DIR / name for name in names]
    if not all(path.is_file() for path in paths):
        logger.info("快取檔案不完整，將重新下載")
        return []
    return paths


def media_cache_payload(files: list[Path], url: str) -> dict:
    """Build the cached payload for a media parse."""
    names = cache_media_files(files, url)
    return {"files": names}


def remember_media_parse(url: str, files: list[Path]) -> None:
    """
    Cache a successful media parse, then keep the cache bounded.

    Only successful parses are stored. A failure is the transient case - a
    credential the operator has just fixed, a hiccup - and caching it would make
    one bad moment permanent.
    """
    payload = media_cache_payload(files, url)
    if not payload["files"]:
        return
    PARSE_CACHE.store(url, "media", payload)
    if PARSE_CACHE.needs_pruning():
        removed = PARSE_CACHE.prune()
        if removed:
            logger.info("解析快取清除 %s 筆過期項目", removed)
    PARSE_CACHE.save()


def gallery_cache_payload(gallery, image_urls=None) -> dict:
    """
    Serialise a gallery for the cache.

    ``image_urls`` is the result of the per-page viewer sweep, which is the
    expensive part of a gallery: one request per page, about a minute on a 53-page
    gallery. Caching it turns a repeat request into no requests.

    Those URLs are e-hentai's time-limited signed links, so the cached sweep only
    stays valid for a short while. That is acceptable because the cache expires on
    its own, and because the "重新生成" button is the supported way to refresh a page
    whose links have died.
    """
    import dataclasses

    payload = {
        "gallery": dataclasses.asdict(gallery),
    }
    if image_urls:
        payload["display_urls"] = list(image_urls[0] or [])
        payload["original_urls"] = list(image_urls[1] or [])
    return payload


def gallery_from_payload(payload: dict):
    """Rebuild a gallery from a cached payload, or None if it cannot be."""
    data = (payload or {}).get("gallery")
    if not isinstance(data, dict):
        return None
    try:
        return ehentai.EhentaiGallery(**data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("快取的畫廊資料無法還原，將重新讀取：%s", exc)
        return None


def remember_gallery(url: str, gallery, image_urls=None) -> None:
    """Cache a gallery's facts and, when available, its image URLs."""
    PARSE_CACHE.store(url, "gallery", gallery_cache_payload(gallery, image_urls))
    if PARSE_CACHE.needs_pruning():
        removed = PARSE_CACHE.prune()
        if removed:
            logger.info("解析快取清除 %s 筆過期項目", removed)
    PARSE_CACHE.save()



async def send_cached_media(message, status, url: str, payload: dict,
                            platform: str | None, source_note: str) -> bool:
    """
    Send a previous parse's files. Returns whether it worked.

    The files come from the cache, so there is no download, no gallery-dl run and no
    waiting. Telegram re-encodes a photo anyway, so what the recipient sees matches a
    fresh parse.

    A **scratch** directory is used for the send and removed afterwards. The sender
    writes downscaled copies there for oversized images, and pointing that at the
    cache directory would let it overwrite the very files being cached.

    Returns False when the cache cannot serve the request, in which case the caller
    falls through to downloading. The check is deliberately strict - a partially
    missing set is refused rather than sent, because half an artwork presented as
    the whole thing is worse than a re-download.
    """
    files = cached_media_files(payload)
    if not files:
        return False

    scratch = make_work_dir()
    try:
        sent, note, _too_large = await send_original_files(
            message, files, url, scratch,
            as_document=(platform == "pixiv"),
            # The same platform split as a fresh download: only X/Twitter carries the
            # source link. Replaying a cached Pixiv link must not grow a link that the
            # original send did not have.
            attach_link=(platform != "pixiv"),
            # Blur adult works. The rating is cached, so a replay costs nothing extra.
        )
    except TelegramError as exc:
        logger.warning("從快取傳送失敗，將重新下載：%s", exc)
        return False
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    summary = f"已從快取傳送 {sent}/{len(files)} 個檔案。"
    if note:
        summary += f"（{note}）"
    # No source link here: send_original_files already posted it as its own last
    # message, which is what puts it at the bottom of the result. Repeating it in the
    # summary would show the same link twice.
    await status.edit_text(summary, link_preview_options=source_link_options())
    return True




def detect_media_platform(url: str):
    """Return 'twitter', 'pixiv', or None."""
    host = get_host(url)

    if host == "x.com" or host.endswith(".x.com"):
        return "twitter"
    if host == "twitter.com" or host.endswith(".twitter.com"):
        return "twitter"
    if host == "pixiv.net" or host.endswith(".pixiv.net"):
        return "pixiv"

    return None


def classify_telegram_link(url: str):
    """
    Identify Telegram links without claiming every t.me/name is a group.
    Returns 'invite', 'public_link', or None.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        return None

    path = parsed.path.strip("/")
    if not path:
        return "public_link"

    if path.startswith("+") or path.startswith("joinchat/"):
        return "invite"

    # t.me/c/... is a private-supergroup message path, not an invite link.
    # Public t.me/<name> could be a group, channel, user or bot.
    return "public_link"


def user_is_allowed(user_id: int, chat_id: int | None = None,
                    chat_type: str | None = None) -> bool:
    """
    Whether this request may use the parsing features.

    Parsing is open by default - any visitor can send a link - and the authorized
    lists narrow that when they are used. In a group the group's authorization
    decides, so authorizing a group is enough for every member of it; see
    Authorization.is_allowed.
    """
    return AUTHORIZATION.is_allowed(user_id, chat_id, chat_type)


def user_is_admin(user_id: int) -> bool:
    """
    True only for an explicitly listed administrator.

    Note this is deliberately *not* "no admins configured, so allow everyone".
    The bot runs in public groups, so an unset admin list would otherwise let any
    visitor run operator commands such as /cookies and /restart.
    """
    return AUTHORIZATION.is_admin(user_id)


def user_can_manage_list(user_id: int) -> bool:
    """
    Whether a user may change the authorized list.

    Administrators can, and so can anyone already on the list: an authorized user
    is trusted to pass the access on. Administrators themselves are handled by
    user_is_admin, so this is not a route to creating more administrators.
    """
    return AUTHORIZATION.is_admin(user_id) or user_id in AUTHORIZATION.allowed


# -------------------- Source link and caption helpers --------------------

def build_caption(source_url: str, extra: str = "") -> str:
    """
    Compose a caption that always carries the source link.

    Telegram caps captions at CAPTION_LIMIT characters. The link is kept whole
    and `extra` is trimmed first, so the source link is never cut off.
    """
    if not ATTACH_SOURCE_LINK or not source_url:
        return extra[:CAPTION_LIMIT]

    label = f"{SOURCE_LABEL}：" if SOURCE_LABEL else ""
    link_block = f"{label}{source_url}"

    if not extra:
        return link_block[:CAPTION_LIMIT]

    room = CAPTION_LIMIT - len(link_block) - 1
    if room <= 0:
        return link_block[:CAPTION_LIMIT]
    if len(extra) > room:
        extra = extra[: max(room - 1, 0)] + "…"
    return f"{extra}\n{link_block}"


def extract_urls(text: str) -> list[str]:
    """Collect cleaned, de-duplicated URLs from a message body or caption."""
    seen = set()
    urls = []
    for raw in URL_RE.findall(text or ""):
        url = clean_url(raw)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def source_link_options() -> LinkPreviewOptions:
    """
    Keep the source link tappable while suppressing the preview card, which
    would otherwise repeat the image the bot just sent.
    """
    return LinkPreviewOptions(is_disabled=True)


# -------------------- Login credentials --------------------

def resolve_cookie_file(platform: str) -> Path | None:
    """
    Return the cookie file to use for `platform`, or None when none is set.

    Precedence is deliberate: an explicit per-site setting beats the shared
    file, so a site that needs a freshly exported cookie can be pointed at its
    own file without disturbing the other.
    """
    candidates = []
    if platform == "twitter":
        candidates.append(TWITTER_COOKIES_FILE)
    elif platform == "pixiv":
        candidates.append(PIXIV_COOKIES_FILE)
    candidates.append(COOKIES_FILE)

    for raw in candidates:
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        if path.is_file():
            return path
        logger.warning("Cookie 檔案不存在，已略過：%s", path)

    return None


def parse_netscape_cookies(path: Path) -> dict[str, set[str]]:
    """
    Map each domain in a Netscape cookie file to the cookie names it holds.

    Only used to report whether the expected login cookie is present. The file
    itself is handed to gallery-dl untouched: re-emitting it risks dropping the
    HttpOnly flag or a field the standard parser tolerates differently.
    """
    cookies: dict[str, set[str]] = {}
    for domain, name, _expiry in iter_netscape_cookies(path):
        cookies.setdefault(domain, set()).add(name)
    return cookies


def resolve_ehentai_cookies() -> Path | None:
    """
    Return the configured e-hentai cookie file, or None.

    Kept separate from resolve_cookie_file because e-hentai is discovered by URL
    shape rather than by detect_media_platform, so it needs its own lookup.
    """
    if not EHENTAI_COOKIES_FILE:
        return None
    path = Path(EHENTAI_COOKIES_FILE).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    if path.is_file():
        return path
    logger.warning("EHENTAI_COOKIES 指定的檔案不存在：%s", path)
    return None


def ehentai_status() -> tuple[bool, str]:
    """
    Report whether e-hentai login is configured.

    e-hentai authenticates with ``ipb_member_id`` plus ``ipb_pass_hash``; ``sk``
    and ``igneous`` are also part of a working exhentai session, so their absence
    is noted but not treated as fatal, because public galleries work without any
    of them.
    """
    path = resolve_ehentai_cookies()
    if path is None:
        return False, "未設定（只能讀取公開畫廊）"

    cookies = parse_netscape_cookies(path)
    names: set[str] = set()
    for domain, cookie_names in cookies.items():
        if "e-hentai.org" in domain or "exhentai.org" in domain:
            names |= cookie_names

    if not names:
        return False, f"{path.name} 沒有 e-hentai 網域的 cookie"

    missing = [n for n in ("ipb_member_id", "ipb_pass_hash") if n not in names]
    if missing:
        return False, f"{path.name} 缺少 {'、'.join(missing)}"

    extra = [n for n in ("sk", "igneous") if n in names]
    note = f"（含 {', '.join(extra)}）" if extra else ""
    return True, f"{path.name}（已登入）{note}"


def iter_netscape_cookies(path: Path):
    """
    Yield (domain, name, expiry) for each cookie line in a Netscape file.

    ``expiry`` is an int Unix timestamp, or None for a session cookie. Lines that
    do not have the seven tab-separated fields are skipped rather than raising,
    because a truncated export should still be diagnosable.
    """
    try:
        # utf-8-sig drops the BOM that Windows editors add, which would
        # otherwise corrupt the first domain name.
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                line = line.rstrip("\r\n")
                if not line.strip():
                    continue
                if line.startswith("#HttpOnly_"):
                    # A real cookie line wearing a prefix, not a comment.
                    line = line[len("#HttpOnly_"):]
                elif line.lstrip().startswith(("#", "$")):
                    continue
                fields = line.split("\t")
                # A Netscape line is: domain, flag, path, secure, expiry, name, value
                if len(fields) < 7:
                    continue
                domain = fields[0].lstrip(".").lower()
                name = fields[5].strip()
                if not domain or not name:
                    continue
                raw_expiry = fields[4].strip()
                try:
                    expiry = int(raw_expiry) if raw_expiry else None
                except ValueError:
                    expiry = None
                yield domain, name, expiry
    except OSError as exc:
        logger.warning("讀取 Cookie 檔案失敗：%s（%s）", path, exc)


def cookie_expiry(platform: str) -> tuple[int | None, str]:
    """
    Earliest expiry among the cookies that actually authenticate `platform`.

    Returns (days_until_expiry, description). ``days`` is None when the value
    cannot be determined, and negative when a required cookie has already
    expired. Session cookies (no expiry field) are reported as such, because
    they die when the exporting browser closes rather than on a fixed date.
    """
    if platform == "pixiv":
        # The refresh token is the credential that authenticates the App API, and
        # it does not carry an expiry date, so cookie dates are informational.
        expiry_dates = []
        path = resolve_cookie_file("pixiv")
        if path is not None:
            for domain, name, expiry in iter_netscape_cookies(path):
                if name == "PHPSESSID" and (
                    domain == "pixiv.net" or domain.endswith(".pixiv.net")
                ):
                    expiry_dates.append(expiry)
        if not PIXIV_REFRESH_TOKEN:
            return None, "無 refresh-token"
        if not expiry_dates:
            return None, "refresh-token 不會過期"
        stamps = [e for e in expiry_dates if e]
        if not stamps:
            return None, "refresh-token 不會過期"
        soonest = min(stamps)
        days = int((soonest - time.time()) // 86400)
        note = "refresh-token 不會過期"
        if days < 0:
            note += "（但 PHPSESSID 已過期）"
        elif days <= EXPIRY_WARN_DAYS:
            note += f"（PHPSESSID 還有 {days} 天）"
        return days, note

    required = {"twitter": ("x.com", ("auth_token", "ct0"))}[platform]
    domain, needed = required
    path = resolve_cookie_file(platform)
    if path is None:
        return None, "未設定"

    expiry_dates = []
    for cookie_domain, name, expiry in iter_netscape_cookies(path):
        if name in needed and (
            cookie_domain == domain or cookie_domain.endswith("." + domain)
        ):
            expiry_dates.append(expiry)

    if not expiry_dates:
        return None, "缺少關鍵 cookie"
    stamps = [e for e in expiry_dates if e]
    if not stamps:
        return None, "工作階段 cookie（關閉瀏覽器後失效）"

    soonest = min(stamps)
    days = int((soonest - time.time()) // 86400)
    return days, f"{days} 天後過期"


def extract_oauth_code(value: str) -> str:
    """
    Pull the OAuth ``code`` out of whatever the user pastes.

    gallery-dl's own prompt says the whole URL may be pasted, and ``code`` is not
    the first parameter, so both forms are accepted. The parameter name must
    match exactly: ``code_challenge`` also starts with ``code``, and a loose
    pattern would extract the PKCE challenge from a ``/start`` URL and present it
    as if it were the authorization code.
    """
    value = (value or "").strip()
    if not value:
        return ""

    match = re.search(r"[?&]code=([^&\s]*)", value)
    if match:
        return unquote(match.group(1))

    # A URL without a code parameter is the mid-flow "start" page, not a token.
    # Return it unchanged so pixiv_token_problem explains what is wrong, rather
    # than silently treating a URL as a token.
    return value


def pixiv_token_problem(token: str) -> str:
    """
    Return a reason if `token` cannot be a Pixiv refresh token, else "".

    The OAuth flow is easy to misread: the URL that appears mid-flow contains only
    a PKCE ``code_challenge`` and no code at all. Catching that here turns a
    confusing "Invalid refresh token" at download time into a clear message at
    check time. An input carrying query parameters is accepted, because
    gallery-dl itself keeps the part after the last ``=``.
    """
    if not token:
        return ""

    if " " in token.strip() or "\t" in token:
        return "refresh-token 裡不應包含空格。"

    lowered = token.lower()
    if "post-redirect" in lowered or "/auth/pixiv/start" in lowered:
        return ("這是登入流程的「發起」環節（post-redirect / start），裡面只有 "
                "code_challenge，沒有 code。真正要複製的是 DevTools 裡最後那筆 "
                "'callback?state=...' 的 code 參數。")
    if lowered.startswith(("http://", "https://")):
        return ("這看起來是一個網址，不是 refresh-token，也沒找到 code 參數。"
                "請複製 'callback?state=...' 那筆請求的 code 參數。")
    if "=" in token:
        # Mirrors gallery-dl's own input handling: keep what follows the last '='.
        tail = token.rpartition("=")[2].strip()
        if len(tail) >= 20:
            return ""
        return "等號後面的值過短，可能沒有完整複製。"
    if len(token) < 20:
        return "refresh-token 過短，可能沒有完整複製。"
    return ""


def cookie_status(platform: str) -> tuple[bool, str]:
    """
    Check the credentials configured for `platform`.

    Returns (usable, human-readable status). Each site needs a different thing,
    and reporting the wrong one sends the user chasing the wrong fix:

    * X/Twitter needs the session cookies ``auth_token`` and ``ct0``. (Whether
      X then serves the data is outside this check: X answers HTTP 200 with an
      empty ``tweet_results`` for this kind of client, which is a platform-side
      restriction rather than a credential problem.)
    * Pixiv artwork pages go through Pixiv's App API, which authenticates with
      an OAuth refresh token instead of a cookie. ``PHPSESSID`` is still
      verified because it is what pixiv.net's own web endpoints accept, but a
      missing refresh token is reported as the actionable problem.
    """
    if platform == "pixiv":
        if PIXIV_REFRESH_TOKEN:
            problem = pixiv_token_problem(PIXIV_REFRESH_TOKEN)
            if problem:
                return False, f"PIXIV_REFRESH_TOKEN 無效：{problem}"
            return True, "refresh-token 已設定"

        path = resolve_cookie_file("pixiv")
        if path is None:
            return False, "未設定"
        cookies = parse_netscape_cookies(path)
        if not any(
            name == "PHPSESSID"
            for domain, names in cookies.items()
            if domain == "pixiv.net" or domain.endswith(".pixiv.net")
            for name in names
        ):
            return False, f"{path.name} 缺少 PHPSESSID"
        return False, f"缺少 refresh-token（Cookie {path.name} 有效，但 Pixiv App API 需要 refresh-token）"

    path = resolve_cookie_file(platform)
    if path is None:
        return False, "未設定"

    cookies = parse_netscape_cookies(path)
    if not cookies:
        return False, f"{path.name} 無法解析（需要 Netscape 格式）"

    domain, needed = "x.com", ("auth_token", "ct0")

    names: set[str] = set()
    for cookie_domain, cookie_names in cookies.items():
        if cookie_domain == domain or cookie_domain.endswith("." + domain):
            names |= cookie_names

    missing = [name for name in needed if name not in names]
    if missing:
        return False, f"{path.name} 缺少 {'、'.join(missing)}"

    return True, f"{path.name}（已登入）"


def verify_credentials_live(timeout: float = 30.0) -> list[tuple[str, bool | None, str]]:
    """
    Ask each site whether the current credential is really still valid.

    A date on a cookie does not prove anything: X can invalidate an auth_token
    server-side long before it expires, and a stale date tells the user nothing
    about whether downloads will work. One cheap authenticated request each is
    the only honest answer.

    Returns [(label, ok_or_None, detail)]. ``None`` means the check could not be
    performed, which is reported as such instead of being treated as a failure.

    Endpoint choice matters, and was verified by hand:
    * X: /i/api/1.1/account/settings.json and verify_credentials.json both return
      404 now. /i/api/1.1/dm/inbox_initial_state.json still authenticates and
      returns real data for this session. Note this proves the *session*, not that
      X will serve tweet content - X withholds that separately.
    * Pixiv: /ajax/user/extra uses PHPSESSID and returns the account's own counts.

    **The proxy is passed explicitly, and that is the difference between this working and
    lying.** It used urllib, which reads only the ``http_proxy`` environment variables and
    NOT the Windows registry, so on this machine every check failed with
    "URLError: handshake operation timed out" and reported **valid credentials as
    unverifiable**. Measured before the fix: X/Twitter and Pixiv both "無法連線" after 30s,
    while the same cookies downloaded perfectly through the proxy.

    httpx is used now, for the same reason the rest of the code does: it takes ``proxy=``
    explicitly, so the check tests the credential instead of the network.
    """
    import httpx

    results = []
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    bearer = (
        "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
        "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
    )

    def fetch(url, headers):
        # httpx rather than urllib, with the proxy passed explicitly: urllib reads only
        # the http_proxy environment variables, so it never reached these hosts here.
        try:
            with httpx.Client(**ehentai.http_client_kwargs(timeout)) as client:
                response = client.get(url, headers=headers)
            return response.status_code, response.text
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"

    # --- X/Twitter ---
    path = resolve_cookie_file("twitter")
    if path is None:
        results.append(("X/Twitter", None, "未設定 cookie"))
    else:
        jar = {}
        for domain, name, _expiry in iter_netscape_cookies(path):
            if domain == "x.com" or domain.endswith(".x.com"):
                jar[name] = domain
        # Re-read values, since iter_netscape_cookies only yields names.
        values = {}
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            raw = line.lstrip(" ")
            if raw.startswith("#HttpOnly_"):
                raw = raw[10:]
            elif not raw or raw[0] in ("#", "$"):
                continue
            fields = raw.split("\t")
            if len(fields) == 7 and fields[0].lstrip(".").lower() == "x.com":
                values[fields[5]] = fields[6]

        if "auth_token" not in values or "ct0" not in values:
            results.append(("X/Twitter", False, "缺少 auth_token 或 ct0"))
        else:
            status, body = fetch(
                "https://x.com/i/api/1.1/dm/inbox_initial_state.json"
                "?include_conversation_info=true",
                {
                    "Authorization": bearer,
                    "x-csrf-token": values["ct0"],
                    "Cookie": f"auth_token={values['auth_token']}; ct0={values['ct0']}",
                    "User-Agent": user_agent,
                    "Accept": "application/json",
                    "x-twitter-active-user": "yes",
                    "x-twitter-auth-type": "OAuth2Session",
                    "x-twitter-client-language": "en",
                },
            )
            if status == 200 and "inbox_initial_state" in body:
                results.append((
                    "X/Twitter", True,
                    "登入有效（注意：X 可能仍不傳回推文內容）",
                ))
            elif status in (401, 403):
                results.append(("X/Twitter", False, f"登入已失效（HTTP {status}）"))
            elif status is None:
                results.append(("X/Twitter", None, f"無法連線：{body}"))
            else:
                results.append(("X/Twitter", None, f"HTTP {status}：{body[:120]}"))

    # --- Pixiv ---
    pixiv_path = resolve_cookie_file("pixiv")
    pixiv_values = {}
    if pixiv_path is not None:
        for line in pixiv_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            raw = line.lstrip(" ")
            if raw.startswith("#HttpOnly_"):
                raw = raw[10:]
            elif not raw or raw[0] in ("#", "$"):
                continue
            fields = raw.split("\t")
            if len(fields) == 7:
                pixiv_values[fields[5]] = fields[6]

    if "PHPSESSID" not in pixiv_values:
        results.append(("Pixiv", None, "沒有 PHPSESSID，略過網頁介面檢查"))
    else:
        cookie_header = "; ".join(
            f"{name}={pixiv_values[name]}"
            for name in ("PHPSESSID", "device_token", "yuid_b")
            if name in pixiv_values
        )
        status, body = fetch(
            "https://www.pixiv.net/ajax/user/extra",
            {
                "Cookie": cookie_header,
                "User-Agent": user_agent,
                "Accept": "application/json",
            },
        )
        if status == 200 and '"error":false' in body.replace(" ", ""):
            counts = ""
            try:
                payload = json.loads(body).get("body", {})
                counts = (f"（追蹤 {payload.get('following')}，"
                          f"粉絲 {payload.get('followers')}）")
            except ValueError:
                pass
            results.append(("Pixiv", True, f"PHPSESSID 有效{counts}"))
        elif status is None:
            results.append(("Pixiv", None, f"無法連線：{body}"))
        else:
            results.append(("Pixiv", False, f"PHPSESSID 可能失效（HTTP {status}）"))

    # The refresh token is what actually downloads artwork; it cannot be checked
    # without spending a real API call, so its presence is all that is reported.
    if PIXIV_REFRESH_TOKEN:
        results.append(("Pixiv refresh-token", True, "已設定（不會過期）"))

    return results


def credential_lines(plain: bool = False) -> list[str]:
    """
    Login state for both sites, one line each, with the expiry date.

    ``plain`` swaps the status marks for ASCII: the Windows console defaults to
    a GBK code page that cannot encode symbols such as a check mark, so printing
    them there raises UnicodeEncodeError.
    """
    ok_mark, warn_mark = ("[OK]", "[!]") if plain else ("✅", "⚠️")
    lines = []
    for platform, label in (("twitter", "X/Twitter"), ("pixiv", "Pixiv")):
        usable, status = cookie_status(platform)
        days, expiry_note = cookie_expiry(platform)
        line = f"{ok_mark if usable else warn_mark} {label}：{status}"
        # Only show a date when it is knowable and actionable; "no expiry" would
        # just add noise to every run.
        if usable and expiry_note and "不會過期" not in expiry_note:
            line += f"；{expiry_note}"
        lines.append(line)

    # e-hentai is optional: public galleries work without it, so an unset value is
    # reported as a limitation rather than as a failure.
    eh_usable, eh_status = ehentai_status()
    lines.append(f"{ok_mark if eh_usable else warn_mark} e-hentai：{eh_status}")

    if not CHECK_COOKIES:
        lines.append("（CHECK_COOKIES=0，已略過憑證檢查）")
    return lines


def credential_summary() -> str:
    """Startup and /cookies report of the login state for both sites."""
    return "\n".join(credential_lines())



def find_executable() -> str:
    """
    Locate gallery-dl. Prefer the running interpreter's own Scripts directory so
    a venv install wins over an unrelated system-wide copy.
    """
    name = "gallery-dl.exe" if os.name == "nt" else "gallery-dl"
    candidate = Path(sys.executable).parent / name
    if candidate.is_file():
        return str(candidate)

    executable = shutil.which("gallery-dl")
    if not executable:
        raise RuntimeError(
            "找不到 gallery-dl，請執行：python -m pip install -U gallery-dl"
        )
    return executable


def create_staging_dir(root: Path) -> Path:
    """
    Create one uniquely named staging directory under `root`.

    tempfile.mkdtemp is deliberately not used. On some Windows hosts (including
    a sandboxed workspace) the security descriptor mkdtemp applies to the
    directory it creates is not writable, so every later write inside it fails
    with PermissionError even though the parent is writable. A plain mkdir gets
    an ordinary inheritable directory and works. Uniqueness is provided by a
    random suffix instead.
    """
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(8):
        candidate = root / f"twitter_pixiv_{secrets.token_hex(4)}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise OSError(f"無法在 {root} 下建立唯一暫存目錄")


def make_work_dir() -> Path:
    """
    Create the per-request staging directory, honouring WORK_DIR.

    Each candidate is probed with a real write, because a directory can be
    created and still reject writes (permissions, restrictive ACLs, or a
    sandboxed host). Falling back here keeps one bad setting or blocked path
    from failing every single request; the reason is logged so it is
    diagnosable.
    """
    candidates = []

    if WORK_DIR:
        root = Path(WORK_DIR).expanduser()
        if not root.is_absolute():
            root = BASE_DIR / root
        try:
            candidates.append(create_staging_dir(root))
        except OSError as exc:
            logger.warning("無法在 WORK_DIR 下建立目錄：%s（%s）", root, exc)

    # The project directory is a better fallback than the system temp dir: it is
    # known writable (the bot runs from it) and keeps large artworks on the same
    # drive the user configured the bot to live on.
    try:
        candidates.append(create_staging_dir(BASE_DIR / ".downloads"))
    except OSError as exc:
        logger.warning("無法在專案目錄下建立暫存目錄：%s", exc)

    try:
        candidates.append(Path(tempfile.mkdtemp(prefix="twitter_pixiv_")))
    except OSError as exc:
        logger.warning("無法在系統暫存目錄下建立暫存目錄：%s", exc)

    for path in candidates:
        try:
            probe = path / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return path
        except OSError as exc:
            logger.warning("暫存目錄不可寫入，改用下一個候選：%s（%s）", path, exc)

    raise RuntimeError(
        "找不到可寫入的暫存目錄。請檢查 WORK_DIR 設定或磁碟權限。"
    )


# -------------------- Telegram commands --------------------

def commands_hint() -> str:
    """
    Visitor-facing usage text.

    Kept free of operator detail on purpose: this bot runs in public groups, so
    deployment notes such as the @BotFather privacy-mode step belong in the
    README, not in a reply every stranger can read.
    """
    return (
        "直接把連結傳給我就行：\n"
        "• X/Twitter 連結：圖片傳回聊天\n"
        f"• Pixiv 連結：傳送原圖檔案（單個作品最多 {MAX_FILES} 張）\n"
        "• e-hentai 連結：整理成 Telegraph 頁面，附標籤分類\n"
        "\n每則結果都會附上原始連結。\n"
        "連結可以寫在文字裡，也可以寫在圖片的說明文字裡。\n"
        f"{SUPPORTED_SITES_NOTICE}"
    )


def zip_files(files: list[Path], temp_dir: Path, archive_name: str) -> Path:
    """
    Put ``files`` into one ZIP inside ``temp_dir`` and return its path.

    Used when Telegram refuses a file for being too large. A ZIP preserves the originals
    **byte for byte** - nothing is re-encoded - and travels as a document, so it is not
    subject to the 10 MB photo ceiling that made the direct send impossible.

    Compression is a side benefit rather than the point: PNG artwork often shrinks, and
    ``ZIP_DEFLATED`` costs a little CPU for it. Keeping the originals intact is the
    reason this exists.
    """
    archive_path = temp_dir / sanitize_filename(archive_name, "pixiv.zip")
    # Relative names only, so extracting does not recreate a temp directory tree.
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        used: set[str] = set()
        for item in files:
            name = item.name
            # A duplicate name would silently overwrite the earlier entry.
            stem, dot, suffix = name.rpartition(".")
            counter = 1
            while name in used:
                name = f"{stem}_{counter}{dot}{suffix}" if dot else f"{name}_{counter}"
                counter += 1
            used.add(name)
            archive.write(item, arcname=name)
    return archive_path


def is_size_refusal(exc: Exception) -> bool:
    """
    Whether Telegram refused a send specifically because the file is too large.

    Checked by message text because the Bot API has no distinct error code for it - the
    refusal arrives as a generic ``BadRequest``. The phrases cover the wording the API
    uses ("file is too big", "PHOTO_INVALID_DIMENSIONS" for an oversized photo) and are
    matched case-insensitively.

    Guarded narrowly on purpose: treating *any* BadRequest as a size problem would turn a
    genuine rejection - a broken file, a bad chat - into a pointless ZIP and a confusing
    message.
    """
    text = str(exc).lower()
    return any(marker in text for marker in (
        "too big", "too large", "file is too", "filesize", "file size",
        "photo_invalid_dimensions", "invalid dimensions",
    ))


def admin_hint(platform: str | None = None) -> str:
    """
    Operator-facing diagnosis, shown only when an administrator asks.

    Contains file names and command lines, so it must never be part of a reply to
    an ordinary visitor.

    The cause comes from gallery-dl's own output via ``LAST_DOWNLOAD_REASON`` when one
    was recognised. This used to be a fixed guess per platform - "Pixiv needs a
    refresh-token" for *any* empty result - which was printed for a deleted artwork, a
    private one, and a rate limit alike. gallery-dl had already said which it was and
    that answer was being discarded, so an operator chasing a deleted work was sent to
    re-do an OAuth flow that was never the problem.
    """
    command = ".venv\\Scripts\\python.exe bot.py --check"

    # A cause gallery-dl named itself. The credential advice is not repeated for these,
    # because none of them is a credential problem.
    if LAST_DOWNLOAD_REASON == "deleted":
        return "作品已被刪除（gallery-dl 回報 Deleted）。這不是憑證問題，無需重新登入。"
    if LAST_DOWNLOAD_REASON == "missing":
        return ("作品不存在或連結已失效（gallery-dl 回報 not found）。"
                "這不是憑證問題。")
    if LAST_DOWNLOAD_REASON == "rate_limit":
        return ("被平台限流（gallery-dl 回報 rate limit）。請稍後再試，"
                "不是憑證問題。")
    if LAST_DOWNLOAD_REASON == "private":
        return ("作品是私人的或受保護（gallery-dl 回報 private/protected）。"
                f"需要該帳號的 Cookie；用 {command} 檢查目前憑證。")
    if LAST_DOWNLOAD_REASON == "network":
        return ("連線逾時或中斷（gallery-dl 回報 timeout/connection）。"
                "多半是暫時性的，請稍後再試。")

    if platform == "pixiv":
        return (
            "Pixiv 取不到內容，且 gallery-dl 未回報明確原因。可能是憑證問題：\n"
            "1) 執行 .venv\\Scripts\\gallery-dl.exe oauth:pixiv\n"
            "2) 把瀏覽器網址列裡 code= 之後的值填入 .env 的 PIXIV_REFRESH_TOKEN\n"
            f"3) 執行 {command} 驗證"
        )
    if platform == "twitter":
        return (
            "若 X/Twitter 持續無結果，多半是 X 端限制而非 Cookie 失效。\n"
            f"用 {command} --live 可確認憑證是否仍有效。"
        )
    return (
        "憑證可能已失效，請在 .env 中檢查 TWITTER_COOKIES / PIXIV_REFRESH_TOKEN /\n"
        "EHENTAI_COOKIES，並執行 " + command + " 查看狀態。"
    )


def visitor_failure_note(platform: str | None = None) -> str:
    """
    The one line shown when a lookup finds nothing.

    Kept to a single short sentence: a failure reply is a statement that it did not
    work, not a paragraph. The operator's diagnosis goes to the log instead, where
    it is useful and does not clutter the chat.
    """
    if platform == "pixiv":
        return "取不到作品內容，連結可能已失效。"
    return "取不到連結內容，可能是平台限制，請稍後再試。"


def save_authorization() -> bool:
    """
    Persist the authorization lists, reporting failure instead of raising.

    Called by every command that changes a list, so an operator is told when a
    change is only in memory. Silently losing a /promote at the next restart would
    be a nasty surprise - it would look like the command had worked.
    """
    return AUTHORIZATION.save(AUTH_FILE)


def admin_only(handler):
    """
    Wrap a command handler so only listed administrators can run it.

    A wrapper is used rather than a check inside each handler so the refusal path
    cannot be forgotten when a new operator command is added.
    """
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        message = update.effective_message
        if not user or not message:
            return

        if user_is_admin(user.id):
            return await handler(update, context)

        if not AUTHORIZATION.admins:
            # Telling the owner how to become one is the only way to bootstrap,
            # and it is harmless to show nobody else has that power yet.
            await message.reply_text(
                "還沒有設定管理員，因此該命令對所有人關閉。\n"
                f"你的 user ID 是 {user.id}，"
                "把它寫入 .env 的 ADMIN_USER_IDS 後重新啟動即可啟用。"
            )
        else:
            await message.reply_text("該命令僅管理員可用。")
        return

    wrapper.__name__ = getattr(handler, "__name__", "admin_command")
    wrapper.__doc__ = handler.__doc__
    return wrapper


def spawn_replacement_process() -> subprocess.Popen:
    """
    Start a second copy of this bot, detached from the current one.

    Detaching matters: the bot is normally started from `run.bat` in a console
    window, so a process left attached to this console would die with it when the
    old bot exits or the window is closed.

    `close_fds=True` matters just as much, and was a real bug. With the default on
    Windows, `close_fds` is False, so the replacement inherited *every* inheritable
    handle - including the stdout pipe of whoever started the bot. A parent that
    captured that pipe, such as `subprocess.run(capture_output=True)`, then blocked
    forever waiting for an EOF that could not arrive while the bot kept running.
    Its output goes to a file, so there is nothing it needs to inherit.

    `CREATE_NO_WINDOW` is used with `DETACHED_PROCESS` because the process writes
    binary log output: with no console at all, any accidental `print` would fail,
    whereas this gives it a console that is simply not visible.
    """
    log_path = BASE_DIR / "restart.log"
    # Rotated rather than appended forever: each restart opens a new process's
    # output onto this file, and an unbounded log on a bot that restarts
    # occasionally would quietly grow without limit.
    try:
        if log_path.exists() and log_path.stat().st_size > RESTART_LOG_MAX_BYTES:
            previous = log_path.with_suffix(".log.1")
            previous.unlink(missing_ok=True)
            os.replace(log_path, previous)
    except OSError as exc:
        logger.warning("無法輪替 restart.log：%s", exc)

    log_handle = open(log_path, "ab", buffering=0)
    # The child needs this one handle, so only it is made inheritable.
    try:
        os.set_handle_inheritable(log_handle.fileno(), True)
    except (AttributeError, OSError):
        pass

    flags = 0
    if os.name == "nt":
        # Hidden console, and its own process group so signals to ours miss it.
        flags = (subprocess.DETACHED_PROCESS
                 | subprocess.CREATE_NEW_PROCESS_GROUP
                 | subprocess.CREATE_NO_WINDOW)

    # The log must be UTF-8 regardless of the machine's locale. Without this the
    # replacement wrote the log in the system code page (cp936 here), so the file
    # the confirmation tells the operator to read was mojibake - useless for the
    # diagnosis it exists for.
    #
    # RESTART_STARTUP_DELAY makes the replacement wait before it starts polling. The
    # old process takes a moment to release Telegram's getUpdates lock, and a second
    # poller on the same token gets "Conflict: terminated by other getUpdates
    # request" - which is how a restart can end with no bot running. The wait is
    # cheap and removes the race entirely, rather than relying on the replacement
    # surviving a Conflict.
    environment = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "RESTART_STARTUP_DELAY": str(RESTART_STARTUP_DELAY_SECONDS),
    }

    try:
        return subprocess.Popen(
            [sys.executable, str(BASE_DIR / "bot.py")],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            close_fds=True,
            env=environment,
            encoding="utf-8",
            errors="replace",
        )
    finally:
        log_handle.close()


def schedule_restart(application, delay: float = 2.0) -> None:
    """
    Replace this process with a fresh copy after a short delay.

    The delay lets the confirmation reach Telegram before polling stops. It is
    served by a worker thread rather than ``loop.call_later``, because the handler
    must return first: sleeping inside the event loop would hold up the very update
    that asked for the restart.

    The shutdown itself must then run **on the event loop**, via
    ``call_soon_threadsafe``. ``Application.stop_running()`` calls
    ``asyncio.get_running_loop().stop()``, so calling it from the worker thread
    raises ``RuntimeError: no running event loop`` and does nothing at all. That is
    exactly what happened: the old process kept polling, the new one died with
    ``Conflict: terminated by other getUpdates request``, and the bot was left
    offline. The exception is also no longer swallowed quietly, since a silent catch
    was the only reason this failure was hard to see.
    """
    loop = asyncio.get_running_loop()

    def request_stop():
        logger.info("依管理員要求重新啟動。")
        # On the event loop, where stop_running() can actually take effect.
        application.stop_running()

    def worker():
        time.sleep(delay)
        try:
            loop.call_soon_threadsafe(request_stop)
        except RuntimeError as exc:
            # The loop is already closed, so there is nothing left to stop.
            logger.warning("無法安排停止輪詢（事件迴圈已結束）：%s", exc)

    threading.Thread(target=worker, name="restart", daemon=True).start()


async def cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Show or clear the parse cache. Administrator only.

    A cache that cannot be inspected is hard to trust - "why did it not download
    again?" and "why is it still sending the old files?" are both answered here - and
    a clear function is the fix when cached content is wrong.
    """
    message = update.effective_message
    args = [str(a).lower() for a in (getattr(context, "args", None) or [])]

    if args and args[0] in {"clear", "清除"}:
        removed = PARSE_CACHE.clear()
        await message.reply_text(
            f"已清除解析快取：{removed} 筆（含快取檔案）。\n"
            "下次解析會重新下載。"
        )
        return

    size_kb = 0
    file_count = 0
    if CACHE_DIR.is_dir():
        for path in CACHE_DIR.rglob("*"):
            if path.is_file():
                file_count += 1
                try:
                    size_kb += path.stat().st_size
                except OSError:
                    pass

    lines = [
        "解析快取：",
        f"• 項目：{PARSE_CACHE.summary}",
        f"• 命中／未命中（本次執行）：{PARSE_CACHE.hits}／{PARSE_CACHE.misses}",
        f"• 快取目錄：{CACHE_DIR}",
        f"• 目錄內容：{file_count} 個檔案，{size_kb / 1024 / 1024:.1f} MB",
        f"• 有效期限：{PARSE_CACHE.stale_seconds // 3600} 小時，"
        f"最多 {PARSE_CACHE.max_entries} 筆",
        f"• 快取檔：{PARSE_CACHE.path}",
        "",
        "用法：/cache clear 清除全部快取",
    ]
    await message.reply_text("\n".join(lines))


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Restart the bot. Administrator only, enforced by the admin_only wrapper.

    A restart is the only way to pick up a `.env` or credential change without
    reaching the host, so it is genuinely useful for an operator - but it also
    takes the bot offline for a moment, which is why it is not open to visitors.
    """
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    try:
        process = spawn_replacement_process()
    except Exception as exc:  # noqa: BLE001
        logger.exception("無法啟動新的程序")
        await message.reply_text(
            f"重新啟動失敗，無法啟動新的程序：{exc}\n"
            "目前這個程序仍在運作。"
        )
        return

    logger.info("已啟動新的程序（PID %s），準備結束目前程序。", process.pid)
    await message.reply_text(
        "已啟動新的程序，正在重新啟動……\n"
        f"• 新程序 PID：{process.pid}\n"
        f"• 舊程序將在數秒內結束\n"
        f"• 輸出寫入：{BASE_DIR / 'restart.log'}\n\n"
        "若數秒後仍無回應，請檢查 restart.log。"
    )
    schedule_restart(context.application)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "你好！我是 X/Twitter + Pixiv 解析機器人。\n\n" + commands_hint(),
        link_preview_options=source_link_options(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        commands_hint(),
        link_preview_options=source_link_options(),
    )


async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Operator view of credential state, with paths and expiry.

    Administrator-only: it names local files and error strings, which is not
    information to hand to an arbitrary member of a public group.
    """
    await update.effective_message.reply_text(
        "登入憑證狀態：\n" + credential_summary()
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Operator summary of the running configuration."""
    lines = [
        "執行狀態：",
        "• 支援站點：X/Twitter、Pixiv、e-hentai",
        "• e-hentai 畫廊：轉為 Telegraph 頁面",
        f"• 成人畫廊建立 Telegraph：{'是' if TELEGRAPH_ALLOW_ADULT else '否'}",
        f"• 其他連結回覆提示：{'是' if REPLY_UNSUPPORTED_LINKS else '否'}",
        f"• 附上原始連結：{'是' if ATTACH_SOURCE_LINK else '否'}",
        f"• 單次最多檔案數：{MAX_FILES}",
        f"• 上傳上限：{ehentai.format_filesize(upload_limit_bytes())}"
        + (f"（自架伺服器：{'本機模式' if can_upload_by_path() else '遠端模式'}）"
           if LOCAL_API_SERVER else "（公開 API）"),
        f"• 連結冷卻時間：{USER_COOLDOWN_SECONDS} 秒（按鈕不受此限制）",
        "• 同一使用者同時只能有一個請求在處理中",
    ]
    if ALLOWED_USER_IDS:
        lines.append(f"• .env 白名單：{len(ALLOWED_USER_IDS)} 人（啟動時的起始值）")
    lines.append(f"• 授權模式：{AUTHORIZATION.mode_label}")
    lines.append(f"• 已授權使用者：{len(AUTHORIZATION.allowed)} 人")
    lines.append(f"• 管理員：{len(AUTHORIZATION.admins)} 人")
    if not AUTHORIZATION.admins:
        lines.append("　（未設定管理員，所有管理命令對所有人關閉）")
    await update.effective_message.reply_text("\n".join(lines))


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the caller their own IDs, so an admin can whitelist them."""
    user = update.effective_user
    chat = update.effective_chat
    chat_kind = {
        "private": "私訊",
        "group": "群組",
        "supergroup": "超級群組",
        "channel": "頻道",
    }.get(getattr(chat, "type", ""), str(getattr(chat, "type", "未知")))
    await update.effective_message.reply_text(
        f"你的 Telegram user ID：{user.id if user else '未知'}\n"
        f"目前 chat ID：{chat.id if chat else '未知'}（{chat_kind}）"
    )


# -------------------- Authorization commands --------------------
#
# The interface for who may parse and who may administer. It exists because the
# alternative is editing .env, which needs host access - exactly what an operator
# in Telegram does not have.

def list_manager_only(handler):
    """
    Wrap a handler so only administrators and authorized users may run it.

    Chosen over admin_only for the list commands: an authorized user is trusted to
    pass the access on, but creating another administrator is not part of that and
    stays behind admin_only.

    Inside a group the bar is higher - administrators only. Group authorization is
    decided in a group, and if any authorized member could run it then any member of
    an authorized group could change the lists, which would make the lists
    self-service for everyone the bot already admits.
    """
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        message = update.effective_message
        if not user or not message:
            return
        chat_type = getattr(getattr(update, "effective_chat", None), "type", None)
        in_group = chat_type in {"group", "supergroup"}

        if in_group:
            if user_is_admin(user.id):
                return await handler(update, context)
            await message.reply_text("在群組中只有管理員可以管理授權名單。")
            return

        if user_can_manage_list(user.id):
            return await handler(update, context)
        await message.reply_text("只有管理員或已授權使用者可以管理名單。")
        return

    wrapper.__name__ = getattr(handler, "__name__", "list_command")
    wrapper.__doc__ = handler.__doc__
    return wrapper


def parse_target_user(context) -> int | None:
    """
    Read the user ID a management command should act on.

    Accepts the ID itself. A Telegram username is deliberately not accepted: a
    username can be changed and re-registered by someone else, so an authorization
    recorded against one would eventually point at a different person. The numeric
    ID is what Telegram guarantees, and /id prints it.

    The caller's own ID is returned when no argument is given, which makes
    /grant with no argument a self-service opt-in for the open mode.
    """
    args = getattr(context, "args", None) or []
    if not args:
        return None
    raw = str(args[0]).strip().lstrip("@")
    # Only a plain numeric ID is accepted, so nothing is guessed. A negative value
    # is a chat ID rather than a person, and would be stored as an authorization
    # that can never match anyone.
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return None


def authorization_report() -> str:
    """The current lists and what they mean, for an operator to read."""
    lines = [
        f"• 模式：{AUTHORIZATION.mode_label}",
    ]
    if AUTHORIZATION.admins:
        lines.append("• 管理員：" + ", ".join(
            str(uid) for uid in sorted(AUTHORIZATION.admins)))
    else:
        lines.append("• 管理員：（未設定，所有管理命令對所有人關閉）")

    if AUTHORIZATION.allowed:
        lines.append(f"• 已授權使用者（{len(AUTHORIZATION.allowed)}）：" +
                     ", ".join(str(uid) for uid in sorted(AUTHORIZATION.allowed)))
    else:
        lines.append("• 已授權使用者：無")

    if AUTHORIZATION.groups:
        lines.append(f"• 已授權群組（{len(AUTHORIZATION.groups)}）：" +
                     ", ".join(str(cid) for cid in sorted(AUTHORIZATION.groups)))
    else:
        lines.append("• 已授權群組：無")
    lines.append(f"• 群組模式：{AUTHORIZATION.group_mode_label}")
    lines.append(f"　群組解析權限：{AUTHORIZATION.group_label}")

    if AUTHORIZATION.locked:
        lines.append("　私訊解析權限：只有管理員與已授權使用者可用")
    elif AUTHORIZATION.allowed:
        lines.append("　私訊解析權限：所有人可用，但名單外的人會被擋下")
    else:
        lines.append("　私訊解析權限：所有人可用（名單為空）")

    lines.append(f"• 授權檔：{AUTH_FILE}")
    return "\n".join(lines)


async def authorized_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show who may parse and who may administer."""
    await update.effective_message.reply_text(
        "授權狀態：\n" + authorization_report()
    )


async def grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Authorize a user or the current group chat.

    In a group with no argument this authorizes **that group**, so every member of
    it may parse - which is the intended way to use it, since a chat ID is awkward
    to obtain by hand. In a private chat with no argument it authorizes the caller.

    Granting is what lifts a restriction in the open mode, and what admits someone
    in the locked mode.
    """
    user = update.effective_user
    message = update.effective_message
    chat = getattr(update, "effective_chat", None)
    chat_type = getattr(chat, "type", None)
    in_group = chat_type in {"group", "supergroup"}
    args = getattr(context, "args", None) or []

    # No argument inside a group means the group itself, which is the common case
    # and cannot be expressed by typing an ID the operator does not know.
    if not args and in_group:
        chat_id = getattr(message, "chat_id", None) or getattr(chat, "id", None)
        if chat_id is None:
            await message.reply_text("讀不到這個群組的 ID，無法授權。")
            return
        changed = AUTHORIZATION.grant_group(chat_id)
        saved = save_authorization()
        note = "已授權這個群組，群組內所有人都可以解析。" if changed \
            else "這個群組本來就已授權。"
        warning = "" if saved else "\n⚠ 無法寫入授權檔，這個變更在重新啟動後會消失。"
        await message.reply_text(
            f"{note}{warning}\n\n{authorization_report()}")
        return

    target = parse_target_user(context)
    if target is None:
        if args:
            await message.reply_text(
                "用法：/grant <數字 ID>\n"
                "• 在群組裡不帶參數：授權這個群組\n"
                "• 帶數字 ID：授權該使用者\n"
                "不接受 @username（使用者名稱可以改，授權會指到別人）。"
            )
            return
        target = user.id

    if user_is_admin(target):
        await message.reply_text(f"{target} 已經是管理員，不需要授權。")
        return

    changed = AUTHORIZATION.grant(target)
    saved = save_authorization()
    note = "已加入授權名單。" if changed else "本來就在授權名單中。"
    warning = "" if saved else "\n⚠ 無法寫入授權檔，這個變更在重新啟動後會消失。"
    await message.reply_text(f"{target}：{note}{warning}\n\n{authorization_report()}")


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Remove a user, or the current group chat, from the authorized lists.

    In a group with no argument this revokes **that group**. Use it to cut off a
    specific user or group without locking the bot down for everyone.
    """
    message = update.effective_message
    chat = getattr(update, "effective_chat", None)
    chat_type = getattr(chat, "type", None)
    in_group = chat_type in {"group", "supergroup"}
    args = getattr(context, "args", None) or []

    if not args and in_group:
        chat_id = getattr(message, "chat_id", None) or getattr(chat, "id", None)
        if chat_id is None:
            await message.reply_text("讀不到這個群組的 ID，無法移除授權。")
            return
        changed = AUTHORIZATION.revoke_group(chat_id)
        saved = save_authorization()
        note = "已移除這個群組的授權。" if changed else "這個群組本來就不在授權名單中。"
        warning = "" if saved else "\n⚠ 無法寫入授權檔，這個變更在重新啟動後會消失。"
        if not AUTHORIZATION.groups:
            note += "\n（已授權群組變成空的，現在所有群組都可以解析）"
        await message.reply_text(
            f"{note}{warning}\n\n{authorization_report()}")
        return

    target = parse_target_user(context)
    if target is None:
        if args:
            await message.reply_text(
                "用法：/revoke <數字 ID>\n"
                "• 在群組裡不帶參數：移除這個群組的授權\n"
                "• 帶數字 ID：移除該使用者的授權"
            )
            return
        target = update.effective_user.id

    if user_is_admin(target):
        await message.reply_text(
            f"{target} 是管理員，無法用 /revoke 移除。請先用 /demote 取消管理員身分。"
        )
        return

    changed = AUTHORIZATION.revoke(target)
    saved = save_authorization()
    note = "已移出授權名單。" if changed else "本來就不在授權名單中。"
    warning = "" if saved else "\n⚠ 無法寫入授權檔，這個變更在重新啟動後會消失。"
    await message.reply_text(f"{target}：{note}{warning}\n\n{authorization_report()}")


async def promote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Make a user an administrator. Administrator only.

    Deliberately not available to authorized users: they may pass on access, but
    creating another administrator is a different level of trust.
    """
    message = update.effective_message
    target = parse_target_user(context)
    if target is None:
        await message.reply_text(
            "用法：/promote <數字 user ID>\n"
            "需要對方先傳 /id 給你。為避免誤升自己，不帶參數時不會生效。"
        )
        return

    changed = AUTHORIZATION.promote(target)
    saved = save_authorization()
    note = "已設為管理員。" if changed else "本來就是管理員。"
    warning = "" if saved else "\n⚠ 無法寫入授權檔，這個變更在重新啟動後會消失。"
    await message.reply_text(f"{target}：{note}{warning}\n\n{authorization_report()}")


async def demote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove administrator rights. Administrator only."""
    message = update.effective_message
    user = update.effective_user
    target = parse_target_user(context)
    if target is None:
        await message.reply_text("用法：/demote <數字 user ID>")
        return

    if target == user.id:
        await message.reply_text(
            "不能取消自己的管理員身分。請找另一位管理員執行，"
            "避免把自己鎖在外面。"
        )
        return

    # Refuse to remove the last one: nobody could then run any admin command,
    # including the one that would fix it.
    if target in AUTHORIZATION.admins and len(AUTHORIZATION.admins) <= 1:
        await message.reply_text(
            "這是最後一位管理員，取消後將沒有任何人能使用管理命令。\n"
            "請先 /promote 另一位管理員。"
        )
        return

    changed = AUTHORIZATION.demote(target)
    saved = save_authorization()
    note = "已取消管理員身分。" if changed else "本來就不是管理員。"
    warning = "" if saved else "\n⚠ 無法寫入授權檔，這個變更在重新啟動後會消失。"
    await message.reply_text(f"{target}：{note}{warning}\n\n{authorization_report()}")


async def lock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Choose what an empty authorized list means. Administrator only.

    This is the one setting that can lock the bot down for everybody, so it says
    plainly which direction it is switching to and refuses nothing else.
    """
    message = update.effective_message
    args = [str(a).lower() for a in (getattr(context, "args", None) or [])]

    if not args or args[0] not in {"on", "off", "status"}:
        await message.reply_text(
            "用法：/lock on|off\n"
            "• on：守門模式 — 只有管理員與已授權使用者可以解析連結\n"
            "• off：開放模式 — 所有人可以解析，名單變成限制\n\n"
            + authorization_report()
        )
        return

    if args[0] == "status":
        await message.reply_text(authorization_report())
        return

    want_locked = args[0] == "on"
    if want_locked and not AUTHORIZATION.allowed and not AUTHORIZATION.admins:
        await message.reply_text(
            "拒絕切換：目前沒有任何管理員或已授權使用者，"
            "開啟守門模式後沒有人能使用解析功能。\n"
            "請先 /grant 或 /promote 至少一個人。"
        )
        return

    if want_locked == AUTHORIZATION.locked:
        await message.reply_text(
            f"已經是{'守門' if want_locked else '開放'}模式。\n\n"
            + authorization_report()
        )
        return

    AUTHORIZATION.locked = want_locked
    saved = save_authorization()
    warning = "" if saved else "\n⚠ 無法寫入授權檔，這個變更在重新啟動後會消失。"
    if want_locked:
        summary = (
            "已切換為守門模式：只有管理員與已授權使用者可以解析連結。\n"
            f"目前名單有 {len(AUTHORIZATION.allowed)} 人。"
        )
    else:
        summary = "已切換為開放模式：所有使用者都可以解析連結。"
    await message.reply_text(f"{summary}{warning}\n\n{authorization_report()}")


async def lock_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Choose whether groups are gated. Administrator only.

    Separate from /lock on purpose. /lock governs private chats and the user list;
    it said nothing about groups, so a bot that looked locked down in private was
    still usable from **any** group. This is that missing switch.
    """
    message = update.effective_message
    args = [str(a).lower() for a in (getattr(context, "args", None) or [])]

    if not args or args[0] not in {"on", "off", "status"}:
        await message.reply_text(
            "用法：/lockgroup on|off\n"
            "• on：守門模式 — 只有已授權的群組可以解析連結\n"
            "• off：開放模式 — 任何群組都可以解析\n\n"
            "已授權群組名單有內容時，一律只有名單內的群組可用。\n\n"
            + authorization_report()
        )
        return

    if args[0] == "status":
        await message.reply_text(authorization_report())
        return

    want_locked = args[0] == "on"

    if want_locked == AUTHORIZATION.group_locked:
        await message.reply_text(
            f"群組已經是{'守門' if want_locked else '開放'}模式。\n\n"
            + authorization_report()
        )
        return

    AUTHORIZATION.group_locked = want_locked
    saved = save_authorization()
    warning = "" if saved else "\n⚠ 無法寫入授權檔，這個變更在重新啟動後會消失。"
    if want_locked:
        summary = (
            "已切換為群組守門模式：只有已授權的群組可以解析連結。"
        )
        if AUTHORIZATION.groups:
            summary += f"\n目前有 {len(AUTHORIZATION.groups)} 個已授權群組。"
        else:
            # Deliberately allowed rather than refused: gating first and authorizing
            # the groups afterwards is the natural order, and an administrator can
            # always undo it with /lockgroup off in a private chat. Refusing here
            # blocked the very sequence that makes the feature usable.
            summary += (
                "\n目前沒有已授權群組，所以**所有群組**現在都不能解析。\n"
                "請到你要開放的群組裡執行 /grant 授權它。\n"
                "（若臨時需要恢復，在私訊執行 /lockgroup off 即可。）"
            )
    else:
        summary = "已切換為群組開放模式：任何群組都可以解析連結。"
    await message.reply_text(f"{summary}{warning}\n\n{authorization_report()}")


# -------------------- Download and media sending --------------------

def gallery_dl_config_path() -> Path | None:
    """
    Return the gallery-dl config file to pass with --config.

    An explicit GALLERY_DL_CONFIG wins. Otherwise a minimal config is generated
    next to the bot for two reasons:

    * The SQLite cache lives on a writable path. gallery-dl defaults to
      %APPDATA%\\gallery-dl, and where that location is read-only it reports
      "attempt to write a readonly database" and both sites stop working.
    * Pixiv's refresh token can be supplied from .env, which keeps the token in
      one place instead of inside gallery-dl's own config.

    The file is regenerated whenever its content would change, so a token added
    to .env takes effect on the next run; a hand-written gallery-dl.conf is
    still honoured through GALLERY_DL_CONFIG.
    """
    if GALLERY_DL_CONFIG:
        configured = Path(GALLERY_DL_CONFIG).expanduser()
        if not configured.is_absolute():
            configured = BASE_DIR / configured
        if configured.is_file():
            return configured
        logger.warning("GALLERY_DL_CONFIG 指定的文件不存在：%s", configured)
        return None

    config_path = BASE_DIR / "gallery-dl.conf"

    cache_dir = Path(GALLERY_DL_CACHE_DIR).expanduser()
    if not cache_dir.is_absolute():
        cache_dir = BASE_DIR / cache_dir
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("無法建立 gallery-dl 快取目錄：%s（%s）", cache_dir, exc)
        return config_path if config_path.is_file() else None

    config: dict = {"cache": {"file": str(cache_dir / "cache.sqlite3")}}
    if PIXIV_REFRESH_TOKEN:
        config["extractor"] = {
            "pixiv": {"refresh-token": PIXIV_REFRESH_TOKEN},
        }

    # JSON is valid gallery-dl config syntax, so no YAML dependency is needed.
    body = json.dumps(config, indent=2, ensure_ascii=False) + "\n"

    try:
        if not config_path.is_file() or config_path.read_text(encoding="utf-8") != body:
            config_path.write_text(body, encoding="utf-8")
            logger.info("已寫入 gallery-dl 設定檔：%s", config_path)
    except OSError as exc:
        logger.warning("無法寫入 gallery-dl 設定檔：%s（%s）", config_path, exc)
        return None

    return config_path


def build_gallery_dl_command(url: str, output_dir: Path, platform: str | None) -> list[str]:
    """
    Assemble the gallery-dl invocation for one URL.

    Every option goes before the terminating ``--``, which then separates the URL.
    ``--`` is not decoration: it stops a URL beginning with a dash being read as an
    option. Appending anything *after* it makes gallery-dl treat that argument as a
    second URL, which is how ``--download-archive`` ended up being reported as
    "Unsupported URL '--download-archive'" and broke every Pixiv and X download.

    Kept separate from the run so the flags and credential selection can be
    asserted in a test without launching the downloader.
    """
    command = [
        find_executable(),
        "--no-mtime",
        "-D",
        str(output_dir),
    ]

    config_path = gallery_dl_config_path()
    if config_path is not None:
        command.extend(["--config", str(config_path)])

    # NO --download-archive here, deliberately, and this was a real bug.
    #
    # The archive records that a file has been downloaded, so gallery-dl skips it.
    # But each request downloads into a *fresh* temp directory, and the archive knows
    # nothing about that: on a repeat it skipped every file, wrote nothing at all,
    # and the bot then reported "沒有找到可傳送的圖片或影片" for a link it had already
    # delivered successfully. The archive and the per-request output directory
    # disagree, and the archive wins.
    #
    # It was also redundant. Reusing a previous parse is the result cache's job, and
    # the cache copies the files aside so they are genuinely still there. When the
    # cache misses - evicted or expired - re-downloading is the correct behaviour,
    # not a false "no media".

    cookie_path = resolve_cookie_file(platform) if platform else None
    if cookie_path is None and COOKIES_FILE and not platform:
        # A URL on a site the bot does not classify still benefits from the
        # shared cookie file when the user configured one.
        fallback = Path(COOKIES_FILE).expanduser()
        if not fallback.is_absolute():
            fallback = BASE_DIR / fallback
        cookie_path = fallback if fallback.is_file() else None

    if cookie_path is not None:
        command.extend(["--cookies", str(cookie_path)])

    # The terminator and the URL are always last.
    command.append("--")
    command.append(url)
    return command


def system_proxy_url() -> str:
    """
    The proxy this machine is configured to use, or "" for a direct connection.

    This exists because **gallery-dl does not read the Windows system proxy**, and that
    cost 115 seconds per Pixiv request. Measured on this machine: the proxy is set in the
    registry (``HKCU\\...\\Internet Settings``, ``ProxyEnable=1``,
    ``ProxyServer=127.0.0.1:7897``), and:

    * ``httpx`` with ``trust_env`` honours it - a request to ``app-api.pixiv.net``
      answers in 1.3s
    * a raw TCP connect to the same address times out every time
    * ``gallery-dl`` timed out on every Pixiv App API call, three 30-second attempts,
      ending in ``ConnectTimeoutError``

    Setting ``HTTPS_PROXY`` for the subprocess fixes it: the same command went from
    **115.7s to 1.3s**. The variable is used rather than a command-line flag because both
    ``requests`` and ``aiohttp`` - whichever gallery-dl reaches for - read it.

    An explicit environment variable wins, so a user can override or disable this without
    touching the registry.
    """
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                 "ALL_PROXY", "all_proxy"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value

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
    except (OSError, ImportError, FileNotFoundError):
        return ""

    server = (server or "").strip()
    if not server:
        return ""
    # ProxyServer may be "host:port" or a per-scheme list such as
    # "http=host:port;https=host:port". Only the simple form is used here.
    if "=" in server:
        for part in server.split(";"):
            scheme, _, target = part.partition("=")
            if scheme.strip().lower() in {"https", "http"} and target.strip():
                server = target.strip()
                break
        else:
            return ""
    if "://" not in server:
        server = f"http://{server}"
    return server


def gallery_dl_env(platform: str | None = None) -> dict:
    """
    The environment to run gallery-dl in, with the system proxy passed explicitly.

    gallery-dl **does not read the Windows system proxy**, so without this every Pixiv
    App API call times out: measured at 115.7s before, 1.3s after setting ``HTTPS_PROXY``.
    The variable is used rather than a flag because both ``requests`` and ``aiohttp`` -
    whichever gallery-dl reaches for - read it.

    Both gallery-dl call sites must use this. There are two - one here and one inside
    ``download_gallery_media``, which builds its own Popen so it can stream progress -
    and fixing only the first left the slow path untouched, which is exactly how this was
    first "fixed" without any effect.
    """
    env = dict(os.environ)
    proxy = system_proxy_url()
    if proxy:
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            env[name] = proxy
    elif platform == "pixiv":
        # No proxy configured: the direct route to app-api.pixiv.net is blocked on some
        # networks, and saying so beats a bare 115-second timeout.
        logger.warning("沒有偵測到系統代理；Pixiv App API 可能需要代理才能連線。")
    return env


# Why the most recent gallery-dl run produced nothing. Set by
# ``record_download_output`` and read by ``admin_hint``.
#
# This exists because the operator's diagnosis used to be a fixed guess: "Pixiv needs a
# refresh-token" was printed for *any* empty result, including a deleted artwork, a
# private one, and a rate limit. gallery-dl had already said which it was, and that
# answer was being thrown away with the captured output.
LAST_DOWNLOAD_REASON = ""


def classify_download_output(text: str) -> str:
    """
    The specific reason a gallery-dl run returned no media, from its own output.

    Ordered most-specific first, because gallery-dl prints several lines and the generic
    ones are always present. Returns "" when nothing recognisable is there, so callers
    can fall back to a generic message rather than inventing a cause.
    """
    lowered = (text or "").lower()
    if "deleted" in lowered:
        return "deleted"
    if "does not exist" in lowered or "not found" in lowered or "404" in lowered:
        return "missing"
    if "login" in lowered or "authentication" in lowered or "refresh-token" in lowered:
        return "auth"
    if "rate limit" in lowered or "429" in lowered or "too many" in lowered:
        return "rate_limit"
    if "private" in lowered or "protected" in lowered or "sensitive" in lowered:
        return "private"
    if "timed out" in lowered or "connection" in lowered or "timeout" in lowered:
        return "network"
    return ""


def begin_download_diagnosis() -> None:
    """
    Clear the recorded reason before a run.

    Without this the reason is sticky: one deleted artwork would leave "deleted" set, and
    the *next* failure for an unrelated reason would be reported as a deletion. Each run
    starts from "unknown" so the hint can only ever describe the run that just happened.
    """
    global LAST_DOWNLOAD_REASON
    LAST_DOWNLOAD_REASON = ""


def record_download_output(text: str) -> None:
    """Remember why a run came back empty, for the log hint that follows."""
    global LAST_DOWNLOAD_REASON
    reason = classify_download_output(text)
    if reason:
        LAST_DOWNLOAD_REASON = reason


def download_with_gallery_dl(url: str, output_dir: Path, platform: str | None = None):
    """Run gallery-dl outside the event loop."""
    begin_download_diagnosis()
    command = build_gallery_dl_command(url, output_dir, platform)

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=MAX_DOWNLOAD_SECONDS,
        check=False,
        shell=False,
        env=gallery_dl_env(platform),
    )

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "没有详细错误信息").strip()
        raise RuntimeError(details[-1200:])

    # A deleted or private item exits 0 with no files, so the reason is only in the
    # output. Recording it here is what lets the log hint name the real cause.
    record_download_output((result.stderr or "") + (result.stdout or ""))


def collect_media_files(temp_dir: Path) -> list[Path]:
    """Media files the download produced, capped at MAX_FILES."""
    files = sorted(
        path for path in temp_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
    )
    return files[:MAX_FILES]


def apply_cooldown(user_id: int) -> float:
    """
    Return remaining cooldown seconds, or 0 and record this request.

    Only used for incoming links. Button presses are gated by request_running
    instead, because a time gate there punishes exactly the behaviour it should
    encourage: choosing a second action as soon as the first finishes.
    """
    now = time.monotonic()
    previous = last_request_time.get(user_id, 0.0)
    wait_left = USER_COOLDOWN_SECONDS - (now - previous)
    if wait_left > 0:
        return wait_left
    last_request_time[user_id] = now
    return 0.0


# Requests currently being processed, keyed by user. A second request is refused
# only while one is genuinely in flight, which is the condition that actually
# matters: it prevents piling work onto a slow gallery download.
#
# The earlier design used a flat 10-second cooldown on everything, including
# buttons. That surfaced as an "操作太快了" alert whenever a user picked a second
# action right after the first had finished - the remaining cooldown was still
# counting down even though nothing was running - so it slowed real work down
# while adding no protection.
_requests_in_flight: dict[int, float] = {}


def begin_request(user_id: int) -> bool:
    """
    Claim the processing slot for a user. Returns False if one is already running.

    A stale entry is ignored so a request that died without clearing its slot
    cannot lock a user out permanently.
    """
    now = time.monotonic()
    started = _requests_in_flight.get(user_id)
    if started is not None and now - started < REQUEST_STALE_SECONDS:
        return False
    _requests_in_flight[user_id] = now
    return True


def end_request(user_id: int) -> None:
    """Release the processing slot."""
    _requests_in_flight.pop(user_id, None)


def busy_message(user_id: int) -> str:
    """Explain a refusal in terms of what is actually happening."""
    elapsed = time.monotonic() - _requests_in_flight.get(user_id, time.monotonic())
    return f"上一個請求還在處理中（已 {int(elapsed)} 秒），完成後就能再用。"


class RequestGuard:
    """
    Claim and release a user's processing slot.

    Used as ``with RequestGuard(user.id) as guard:`` and then checking
    ``guard.ok``. It deliberately does not raise: every handler already has a
    try/finally for its own cleanup, and refusing inside that structure keeps the
    release guaranteed without an extra exception path.
    """

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.ok = False

    def __enter__(self):
        self.ok = begin_request(self.user_id)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.ok:
            end_request(self.user_id)
        return False


def downscale_image(
    file_path: Path,
    target_dir: Path,
    max_edge: int | None = None,
    quality: int | None = None,
) -> Path | None:
    """
    Shrink an oversized image so Telegram accepts it as a photo instead of a
    document. Returns None when Pillow is unavailable or the image cannot be
    processed, in which case the caller sends a document instead.

    A rendered page can legitimately be very tall, so the caller may pass a
    smaller ``max_edge`` than the default; the result is always JPEG because that
    is what Telegram compresses photos to anyway.
    """
    global _pillow_unavailable

    if _pillow_unavailable:
        return None

    try:
        from PIL import Image
    except ImportError:
        _pillow_unavailable = True
        logger.warning(
            "Pillow 未安裝，超過 10MB 的圖片將以檔案形式傳送。"
            "如需壓縮後傳送，請執行：python -m pip install -U Pillow"
        )
        return None

    edge = PHOTO_DOWNSCALE_MAX_EDGE if max_edge is None else max_edge
    jpeg_quality = PHOTO_DOWNSCALE_QUALITY if quality is None else quality

    try:
        with Image.open(file_path) as image:
            # A very large original is a legitimate input here, so the
            # decompression-bomb limit is left at the library default on purpose.
            image = image.convert("RGB")
            image.thumbnail((edge, edge), Image.LANCZOS)
            output_path = target_dir / f"{file_path.stem}_small.jpg"
            image.save(output_path, "JPEG", quality=jpeg_quality)
    except Exception:
        logger.warning("圖片壓縮失敗：%s", file_path.name, exc_info=True)
        return None

    if output_path.stat().st_size >= PHOTO_LIMIT_BYTES:
        return None
    return output_path


def downscale_photo(file_path: Path, target_dir: Path) -> Path | None:
    """Backwards-compatible wrapper for the artwork path."""
    return downscale_image(file_path, target_dir)


async def send_status(message, text: str):
    """Reply with a status/error message, without a link preview card."""
    try:
        return await message.reply_text(
            text, link_preview_options=source_link_options()
        )
    except BadRequest:
        # Fall back for clients/versions that reject the extra option.
        return await message.reply_text(text)


async def delete_message_quietly(message, purpose: str) -> bool:
    """
    Delete a chat message, treating "not allowed" as a normal outcome.

    Removing the bot's own status notice works nearly everywhere. Removing the
    sender's link message in a group requires the bot to be an administrator with
    the delete-messages right, which a public bot often is not. That refusal must
    not turn a successful parse into an error, so failures are logged - once per
    run for the permission case, which is a configuration issue rather than a
    per-request problem - and reported as False.

    Telegram also refuses to delete anything older than 48 hours, and any message
    that is already gone, so MessageIdInvalid is expected and ignored.
    """
    global _warned_cannot_delete

    if message is None:
        return False
    try:
        await message.delete()
        return True
    except BadRequest as exc:
        text = str(exc).lower()
        if "message to delete not found" in text:
            # Already removed, or far older than the 48 hours a bot may delete.
            logger.debug("訊息無法刪除（%s）：%s", purpose, exc)
        elif not _warned_cannot_delete:
            # Telegram words both "no right to delete" and "too old" as
            # "message can't be deleted", and only the first is actionable, so the
            # hint covers both rather than asserting which one it was.
            _warned_cannot_delete = True
            logger.warning(
                "無法刪除%s：%s。若是在群組刪除他人訊息，需要把機器人設為管理員"
                "並勾選「刪除訊息」權限；否則只會刪除機器人自己的訊息。",
                purpose,
                exc,
            )
        return False
    except TelegramError as exc:
        logger.debug("刪除訊息失敗（%s）：%s", purpose, exc)
        return False


async def cleanup_request_messages(status_message, source_message) -> None:
    """
    Clear away what a request added to the chat after it has succeeded.

    Called only on success: on failure the status message *is* the result and
    must stay, otherwise the user is left with nothing explaining what happened.
    """
    if DELETE_SOURCE_MESSAGE:
        await delete_message_quietly(source_message, "原始訊息")
    if DELETE_STATUS_MESSAGE:
        await delete_message_quietly(status_message, "解析提示訊息")


async def edit_message_to_cover(status, gallery, caption: str,
                                reply_markup=None) -> bool:
    """
    Turn the status message into the gallery's cover photo. Returns success.

    The cover is put *into the message the bot already sent*, by editing its media,
    rather than posting a second message. Two messages for one result is what made
    the reply look like it was repeating itself, and editing keeps the chat to one
    message per request.

    The image is referenced by **URL**, so Telegram fetches it and the bot downloads
    nothing - a cover is decoration, and the pages themselves are only downloaded for
    the archive actions. The URL is e-hentai's static thumbnail (``ehgt.org``), not
    one of the time-limited ``hath.network`` links, so a posted cover keeps working.

    The keyboard is carried over, because `edit_media` replacing the message body
    would otherwise drop the "重新生成" button that was just attached.

    A failure is cosmetic, so it reports False and the caller carries on with a text
    message: refusing to deliver a gallery because its cover would not load would be
    the wrong trade.
    """
    url = (gallery.thumb or "").strip()
    if not url or status is None:
        return False
    try:
        await status.edit_media(
            media=InputMediaPhoto(media=url, caption=caption or None),
            reply_markup=reply_markup,
        )
        return True
    except TelegramError as exc:
        logger.warning("封面編輯失敗：%s（%s）", url, exc)
        return False


async def send_single_media(message, file_path: Path, source_url: str,
                            temp_dir: Path, extra: str = "",
                            prefer_original: bool = False,
                            keyboard=None, caption=None) -> bool:
    """
    Send one media file. Returns success.

    ``caption`` carries the source link, which is what puts it **above** the image: a
    single-photo caption is rendered above its media by Telegram. A url button is
    attached alongside when ``keyboard`` is given, so the link stays reachable however
    a client renders the caption. An earlier version used the button alone, which put
    the link below the image.

    With ``prefer_original`` an image is sent as a document, which is the only
    way Telegram passes the file through untouched. ``reply_photo`` re-encodes
    the image server-side (and renames it to a .jpg), so a photo upload is never
    the original even when it fits under the 10 MB photo limit. That is why this
    mode also skips the downscale step: shrinking the picture is exactly what the
    caller asked to avoid.

    ``extra`` is still used for a per-file note such as "compressed because the
    original exceeded 10 MB", which belongs on the file it describes.
    """
    size = file_path.stat().st_size

    limit = upload_limit_bytes()
    if size > limit:
        await send_status(
            message,
            f"略過 {file_path.name}：{ehentai.format_filesize(size)} "
            f"超過目前的上傳上限 {ehentai.format_filesize(limit)}。",
        )
        return False

    suffix = file_path.suffix.lower()
    note = f"（{extra}）" if extra else None
    # The source link wins over a per-file note when both are present: the link is what
    # the caller asked to show, and the note is only a compression remark.
    text = caption or note

    if suffix in VIDEO_EXTENSIONS:
        with file_path.open("rb") as media:
            await message.reply_video(
                video=media,
                filename=file_path.name,
                caption=text,
                supports_streaming=True,
                reply_markup=keyboard,
                
            )
        return True

    if suffix in ANIMATION_EXTENSIONS:
        with file_path.open("rb") as media:
            await message.reply_animation(
                animation=media,
                filename=file_path.name,
                caption=text,
                reply_markup=keyboard,
                
            )
        return True

    if suffix in IMAGE_EXTENSIONS:
        if prefer_original:
            # Sent as a document on purpose: the bytes reach Telegram unchanged.
            with file_path.open("rb") as media:
                await message.reply_document(
                    document=media, filename=file_path.name, caption=text,
                    reply_markup=keyboard,
                )
            return True

        if size <= PHOTO_LIMIT_BYTES:
            with file_path.open("rb") as media:
                await message.reply_photo(
                    photo=media, filename=file_path.name, caption=text,
                    reply_markup=keyboard,
                )
            return True

        # Too large for sendPhoto: try a downscaled copy, else send a document.
        smaller = await asyncio.to_thread(downscale_photo, file_path, temp_dir)
        if smaller is not None:
            compress_note = "（原圖超過 10MB，已壓縮傳送）"
            with smaller.open("rb") as media:
                await message.reply_photo(
                    photo=media,
                    filename=smaller.name,
                    # The link still leads when the image had to be shrunk, so the
                    # compression remark is appended rather than replacing it.
                    caption=(f"{text}\n{compress_note}" if text else compress_note),
                    reply_markup=keyboard,
                )
            return True

        with file_path.open("rb") as media:
            await message.reply_document(
                document=media, filename=file_path.name, caption=text,
                reply_markup=keyboard,
            )
        return True

    with file_path.open("rb") as media:
        await message.reply_document(
            document=media, filename=file_path.name, caption=text,
            reply_markup=keyboard,
        )
    return True


async def send_media_album(message, files: list[Path], source_url: str,
                           as_document: bool = False, keyboard=None,
                           with_caption: bool = True) -> bool:
    """
    Send 2..ALBUM_LIMIT files as one album, with the source link as the **first**
    item's caption so it reads above the images. Raises TelegramError so the caller can
    fall back to individual sends.

    Requested explicitly, after the alternatives were tried and rejected:

    * A caption on the first item puts the link at the **top**. That is what a plain
      text caption on a single photo does, so it matches the obvious expectation.
    * A separate message for the link is guaranteed to be above the album, but costs a
      second message - which was the earlier complaint.
    * A url button on the album sits below the images and stays in one message. That is
      the previous behaviour, and it is what this replaces.

    **The position is Telegram's to decide, not ours.** An album caption is drawn by the
    client against the media, which is why an earlier attempt at a caption landed the
    link in the middle of the set when it was attached to a later item. Anchoring it to
    the first item is the best that can be done from here; the button is still attached
    as well, so the link remains reachable even if a client renders the caption
    somewhere unexpected.

    ``as_document`` keeps an image byte-identical to the downloaded original. The
    filenames inside an album are not preserved by Telegram, which is a trade for
    delivering the whole set as **one message** instead of one per page.

    Each item is given an **open file object**, not a path string, and that
    distinction is the whole bug this function used to have. For an album item a
    ``str`` is read by Telegram as a URL or a file_id, so passing
    ``str(file_path)`` produced:

        BadRequest: Invalid file http url specified: unsupported url protocol

    Telegram then rejected the album and the caller fell back to one message per
    page. The streams are closed in a ``finally``, so a failure part-way through does
    not leak handles on the files the caller is about to delete.
    """
    caption = build_caption(source_url) if with_caption else None
    handles = []
    try:
        media = []
        for index, file_path in enumerate(files):
            builder = InputMediaDocument if as_document else InputMediaPhoto
            handle = file_path.open("rb")
            handles.append(handle)
            # Only the first item carries text: Telegram rejects a media group whose
            # items all have captions, and one link is what is wanted anyway.
            item_kwargs = {"media": handle,
                           "caption": caption if index == 0 else None}
            if as_document:
                # Send the real filename, so an original does not arrive as
                # "file_0.dat". It MUST go through api_kwargs: PTB's `filename=`
                # parameter is deprecated and is dropped by to_dict(), which was
                # measured - InputMediaDocument(media=..., filename="x.png") serialises
                # to {"media": ..., "type": "document"} with no filename at all. Passing
                # it as a keyword looked correct and did nothing.
                item_kwargs["api_kwargs"] = {"filename": file_path.name}
            media.append(builder(**item_kwargs))

        kind = "document" if as_document else "photo"
        try:
            await _reply_media_group(message, media, keyboard)
        except TimedOut:
            # Deliberately NOT retried and NOT degraded to one message per file.
            #
            # A timeout here does not mean the album was rejected: the Bot API server may
            # have accepted it and be uploading it to Telegram, with only the HTTP
            # response late. Retrying or splitting then delivers the SAME pages two or
            # three times, which is exactly what was reported. Saying so and stopping
            # leaves at most one copy, and the caller reports the timeout.
            logger.warning("相簿傳送逾時（%s × %s）：不再重試或分割，避免重複傳送。"
                           "若相簿未出現，請調高 TELEGRAM_TIMEOUT_SECONDS 後再試。",
                           kind, len(media))
            raise
        except TelegramError as exc:
            # The caption or the button was refused. Retry with neither rather than
            # losing the album: one message of images with no link beats one message
            # per page, and the caller still reports the source.
            logger.warning("相簿加說明或按鈕失敗，改為純圖片重試：%s", exc)
            try:
                await _reply_media_group(message, _strip_captions(media), None)
            except TimedOut:
                logger.warning("相簿重試逾時（%s × %s）：不再分割，避免重複傳送。",
                               kind, len(media))
                raise
            except TelegramError as retry_exc:
                logger.warning("相簿傳送失敗（%s × %s）：%s",
                               kind, len(media), retry_exc)
                raise
        logger.info("已以單一相簿傳送 %s 個%s%s", len(media), kind,
                    "（第一張帶原始連結）" if caption else "")
        return True
    finally:
        for handle in handles:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass


def _strip_captions(media: list):
    """
    Rebuild album items without captions, reusing the already-open file handles.

    Used only for the retry after Telegram refuses a captioned media group. The handles
    belong to the caller, so they are passed through rather than reopened - reopening
    would leak the originals and could fail on Windows, where a file open for reading
    can still block a second open.
    """
    stripped = []
    for item in media:
        builder = type(item)
        stripped.append(builder(media=item.media))
    return stripped


async def _reply_media_group(message, media, keyboard):
    """
    Send one media group, with the keyboard when there is one.

    ``reply_markup`` is not a parameter of ``reply_media_group`` in PTB 22.8, so it
    is passed through ``api_kwargs``, which the library merges into the request body
    (``data.update(api_kwargs)``). Telegram keeps the button as part of the album's
    own message, which is what puts the link "below the images" while staying one
    message - the layout a caption cannot produce, because Telegram draws an album's
    caption against the media instead of under the group.
    """
    if keyboard is None:
        return await message.reply_media_group(media=media)
    return await message.reply_media_group(media=media,
                                           api_kwargs={"reply_markup": keyboard})


def build_source_keyboard(source_url: str):
    """
    A one-button keyboard whose button opens the source artwork.

    **Kept deliberately, but currently unused.** The source link now travels as the
    first item's caption, and a button pointing at the same address printed the link a
    second time, which was reported as redundant. Both callers that attached it
    (``send_original_files`` for the first message, and the caption path) now pass
    ``None``.

    It is retained rather than deleted because it is the only place the source-link
    keyboard exists and it is covered by the characterization suite: if a client is
    found to render the caption somewhere unreachable, reattaching it is a one-line
    change. A future reader should treat "no caller" as the intended state, not as an
    oversight, and should not reattach it without removing the caption link at the same
    time - attaching both is exactly the duplication this replaced.

    Returns None when there is no usable URL, in which case no keyboard is attached.
    """
    url = (source_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    label = url.split("://", 1)[-1]
    label = label[len("www."):] if label.startswith("www.") else label
    # Telegram caps button text at 64 characters.
    if len(label) > 60:
        label = label[:57] + "…"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"🔗 {label}", url=url)]]
    )


async def send_source_link(message, source_note: str):
    """
    Post the source link on its own, after the files.

    This is what puts the link at the *bottom* of the result. It cannot be an album
    caption: Telegram draws an album's caption against the media, so with more pages
    below it the link ends up in the middle. A separate message after the files puts
    it where it belongs.

    Returns the message it created, or None. The caller keeps it out of the failure
    cleanup, since it *is* part of the result.
    """
    if not source_note:
        return None
    try:
        return await message.reply_text(
            source_note, link_preview_options=source_link_options()
        )
    except TelegramError as exc:
        logger.warning("原始連結傳送失敗：%s", exc)
        return None


async def send_original_files(message, files: list[Path], source_url: str,
                              temp_dir: Path,
                              as_document: bool = True,
                              attach_link: bool = True
                              ) -> tuple[int, str, list[Path]]:
    """
    Send downloaded files, optionally with the source link above them.
    Returns ``(sent, note, oversize)``.

    ``oversize`` lists the files Telegram refused **specifically for being too large**,
    so the caller can bundle them into a ZIP and tell the user why. It is a list rather
    than a count because the caller needs the files themselves to build the archive.
    A file refused for any other reason is not included: it would not be fixed by
    zipping, and pretending otherwise would hide a real failure.

    When ``attach_link`` is set, the link travels inside the first message as its
    caption, so it reads above the images, plus a url button on that same message so it
    stays reachable however the client renders the caption. Requested explicitly after
    both alternatives were tried: a separate message lands above but costs a second
    message, and a button alone sits below the images.

    ``attach_link=False`` sends the media with **no** link and no button. Requested for
    Pixiv specifically: only X/Twitter should carry the source link. That is why this is
    a parameter rather than a global - the two platforms are configured differently, and
    the cached replay path has to respect the same split, or a repeat of a cached Pixiv
    link would suddenly grow a link that a fresh download does not have.

    ``as_document`` (the default) sends images as documents, which is the only
    way Telegram passes a file through untouched; the Pixiv path relies on that
    so recipients get the original artwork. With ``as_document=False`` images go
    as normal photos, which Telegram re-encodes - the right choice for
    X/Twitter posts, where a compressed preview is what users expect.

    Multi-page artwork is grouped into albums so a 10-page work does not flood
    the chat, and anything that is not a groupable image is sent on its own.
    Telegram caps an album at ALBUM_LIMIT items, so larger sets are split into
    several albums rather than dropped.

    Only the **first** message carries the link, so a 30-page work does not repeat it
    on all three of its albums.
    """
    sent = 0
    notes = []
    # Files Telegram refused for being too large, so the caller can ZIP them.
    too_large: list[Path] = []
    # Whether the source link has already been carried by a sent message. The link is
    # what makes the result self-describing, so it is attached to whichever message
    # goes out first and never repeated. With attach_link off this stays False and
    # nothing is attached anywhere.
    link_carried = not attach_link
    caption = build_caption(source_url) if attach_link else None
    # No url button is attached. The caption already carries the link, and a button
    # pointing at the same address printed it a second time - reported as redundant.
    # The button was introduced when the link could only be a caption drawn against the
    # media, so it acted as the guaranteed-reachable copy; now that the link is in the
    # caption on a message which itself survives cleanup, it no longer earns its place.

    images = [path for path in files if path.suffix.lower() in IMAGE_EXTENSIONS]
    others = [path for path in files if path.suffix.lower() not in IMAGE_EXTENSIONS]

    # Only images that fit the upload limit can be grouped; an oversized one is
    # still offered individually, where the size check reports it. The limit comes
    # from the server in use, so a self-hosted one admits far larger pages.
    _limit = upload_limit_bytes()
    groupable = [path for path in images if path.stat().st_size <= _limit]
    oversized = [path for path in images if path.stat().st_size > _limit]

    # One album per ALBUM_LIMIT images; a single leftover image is sent alone
    # because Telegram requires at least two items for a media group.
    for start in range(0, len(groupable), ALBUM_LIMIT):
        chunk = groupable[start:start + ALBUM_LIMIT]
        if len(chunk) >= 2:
            try:
                await send_media_album(
                    message, chunk, source_url, as_document=as_document,
                    keyboard=None,
                    # Later albums get no caption, so the link is not repeated.
                    with_caption=not link_carried,
                )
                sent += len(chunk)
                link_carried = True
                continue
            except TimedOut:
                # NOT degraded to one message per file. The album was probably accepted
                # and is still uploading; sending each page separately afterwards is what
                # produced two and three copies of the same Pixiv artwork. The pages are
                # counted as attempted so the summary is not misleadingly empty, and the
                # note tells the operator what to change.
                logger.warning("相簿逾時，略過逐個傳送以免重複：%s", [p.name for p in chunk])
                sent += len(chunk)
                link_carried = True
                notes.append("相簿傳送逾時（可能已送達），未再逐個傳送以免重複")
                continue
            except TelegramError:
                logger.warning("相簿傳送失敗，改為逐個傳送", exc_info=True)
                notes.append("相簿傳送失敗，已改為逐個傳送")
        for file_path in chunk:
            try:
                if await send_single_media(
                    message, file_path, source_url, temp_dir,
                    prefer_original=as_document,
                    keyboard=None,
                    caption=None if link_carried else caption,
                ):
                    sent += 1
                    link_carried = True
            except TimedOut:
                # Never retried. A timed-out upload has very likely arrived, and this bot
                # is reachable from anywhere, so a retry can deliver the same file twice.
                logger.warning("傳送逾時，不重試以免重複：%s", file_path.name)
                notes.append(f"{file_path.name} 傳送逾時（可能已送達）")
            except TelegramError as exc:
                if is_size_refusal(exc):
                    too_large.append(file_path)
                    logger.warning("檔案過大被拒，將改用 ZIP：%s（%s）",
                                   file_path.name, exc)
                else:
                    logger.exception("傳送失敗：%s", file_path.name)
                    notes.append(f"{file_path.name} 傳送失敗")

    for file_path in oversized + others:
        try:
            if await send_single_media(
                message, file_path, source_url, temp_dir,
                prefer_original=as_document,
                keyboard=None,
                caption=None if link_carried else caption,
            ):
                sent += 1
                link_carried = True
        except TimedOut:
            logger.warning("傳送逾時，不重試以免重複：%s", file_path.name)
            notes.append(f"{file_path.name} 傳送逾時（可能已送達）")
        except TelegramError as exc:
            if is_size_refusal(exc):
                too_large.append(file_path)
                logger.warning("檔案過大被拒，將改用 ZIP：%s（%s）", file_path.name, exc)
            else:
                logger.exception("傳送失敗：%s", file_path.name)
                notes.append(f"{file_path.name} 傳送失敗")

    # Nothing carried the link only if there was nothing to send, or every send failed.
    # The text message is still the honest fallback in that case, rather than leaving
    # the user with images and no way back to the source.
    if attach_link and not link_carried and sent == 0:
        await send_source_link(message, caption or build_caption(source_url))

    return sent, ("；".join(dict.fromkeys(notes)) if notes else ""), too_large


def gallery_url(text: str) -> str | None:
    """First e-hentai/exhentai gallery link in a message, if any."""
    for url in extract_urls(text):
        if ehentai.is_gallery_url(url):
            return url
    return None


def build_gallery_info(gallery, telegraph_url: str = "") -> str:
    """
    Chat-side summary of a gallery, with every tag grouped by namespace.

    The Telegraph page is the full presentation; this is what a chat can show, so
    it keeps the facts and the grouped tags and omits the images.
    """
    lines = [f"📚 {gallery.title}"]

    facts = []
    if gallery.category:
        facts.append(f"分類 {gallery.category}")
    if gallery.language:
        facts.append(f"語言 {gallery.language}")
    if gallery.rating:
        facts.append(f"評分 {gallery.rating}")
    if gallery.filecount:
        facts.append(f"{gallery.filecount} 頁")
    if gallery.filesize:
        facts.append(ehentai.format_filesize(gallery.filesize))
    if facts:
        lines.append("　".join(facts))

    for namespace in gallery.ordered_namespaces():
        label = EHENTAI_NAMESPACE_LABELS.get(namespace, namespace)
        values = gallery.tags.get(namespace, [])
        if values:
            lines.append(
                f"• {label}：{', '.join(ehentai.display_tag(v) for v in values)}"
            )

    if gallery.expunged:
        lines.append("⚠ 此畫廊已被刪除（expunged）")

    return "\n".join(lines)


async def handle_ehentai_gallery(message, url: str, user) -> bool:
    """
    Read a gallery's metadata and offer the four download choices.

    Nothing is downloaded until a button is pressed, so the reply is quick and a
    gallery the user does not actually want costs one metadata request. The
    Telegraph page is also deferred to the chosen action, because it is the image
    embedding that makes it worth building.

    Returns success. Every failure is reported in chat instead of raising, so one
    unreadable gallery never looks like a bot crash.
    """
    status = await send_status(message, "正在讀取 e-hentai 畫廊……")

    try:
        # A gallery parsed before is served from the cache, so a repeat request
        # costs no request to e-hentai at all. The facts are cheap to fetch; the
        # per-page sweep is not, and that is what the cache mostly saves.
        cached = PARSE_CACHE.lookup(url, kind="gallery")
        gallery = gallery_from_payload(cached) if cached else None
        if gallery is not None:
            age = PARSE_CACHE.age(url)
            logger.info("命中畫廊快取（%.0f 秒前）：%s", age or 0, url)

        if gallery is None:
            gallery = await asyncio.to_thread(
                ehentai.fetch_gallery,
                url,
                cookies_path=resolve_ehentai_cookies(),
                timeout=float(EHENTAI_TIMEOUT_SECONDS),
            )
            await asyncio.to_thread(remember_gallery, url, gallery)

        info = build_gallery_info(gallery)
        notes = []
        if gallery.is_adult and not TELEGRAPH_ALLOW_ADULT:
            notes.append("（成人畫廊，依設定不建立 Telegraph 頁面）")
        elif gallery.is_adult:
            notes.append("（成人內容，Telegraph 頁面可能被移除）")

        prompt = "\n".join([
            info,
            "",
            "請選擇要如何取得這個畫廊：",
            "• Telegraph：建立內嵌全部圖片的頁面（不另外傳圖片到聊天）",
            "• 歸檔：把整個畫廊打包成 ZIP（過大時自動分批）",
        ] + notes)

        await status.edit_text(
            prompt,
            link_preview_options=source_link_options(),
            reply_markup=gallery_keyboard(gallery.gid, gallery.token),
        )
        return True

    except ehentai.EhentaiError as exc:
        logger.warning("e-hentai 解析失敗：%s（%s）| %s", url, exc, admin_hint(None))
        await status.edit_text(
            f"無法讀取這個畫廊：{exc}\n\n{build_caption(url)}",
            link_preview_options=source_link_options(),
        )
        return False
    except Exception:
        logger.exception("e-hentai 處理發生未預期錯誤：%s", url)
        await status.edit_text(
            "解析這個畫廊時發生錯誤，請稍後再試。\n\n" + build_caption(url),
            link_preview_options=source_link_options(),
        )
        return False


def gallery_keyboard(gid: str, token: str) -> InlineKeyboardMarkup:
    """
    The four gallery actions, laid out as two rows.

    Callback data is kept short because Telegram caps it at 64 bytes. It carries
    the gallery id and token rather than any local path, so a button press is
    self-contained and cannot be used to reach an arbitrary file.

    "Telegraph" publishes a page with the images embedded on it; nothing is sent to
    the chat. "歸檔" downloads the gallery and sends it as ZIP archives. The two
    differ in resolution: the 1280 version is the site's own web-sized image
    (about 0.14 MB a page), the original is the full-resolution file (about 10 MB a
    page).
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Telegraph · 顯示圖",
                                 callback_data=f"eh|view|{gid}|{token}"),
            InlineKeyboardButton("Telegraph · 原圖",
                                 callback_data=f"eh|orig|{gid}|{token}"),
        ],
        [
            InlineKeyboardButton("歸檔 · 1280", callback_data=f"eh|zip1280|{gid}|{token}"),
            InlineKeyboardButton("歸檔 · 原圖", callback_data=f"eh|ziporig|{gid}|{token}"),
        ],
        [
            InlineKeyboardButton("取消", callback_data="eh|cancel"),
        ],
    ])


async def handle_regenerate(update: Update, query, user, gid: str, token: str) -> None:
    """
    Rebuild a gallery's Telegraph page with freshly signed image URLs.

    e-hentai's image links are time-limited, so a page built weeks ago shows broken
    images while its tags stay correct. This fetches new URLs for the same gallery
    and publishes a new page, replacing the old one in place and removing the
    button afterwards so it cannot be pressed twice by accident.
    """
    entry = _regenerate_cache.get((query.message.chat_id, user.id))
    if entry is None:
        await query.answer("這個按鈕已過期，請重新傳送畫廊連結。", show_alert=True)
        return

    # The chat is passed too, because in a group the group's own authorization
    # decides, not the member's.
    if not user_is_allowed(
            user.id,
            query.message.chat_id,
            getattr(getattr(query.message, "chat", None), "type", None)):
        await query.answer("此機器人目前不允許你的帳號使用。", show_alert=True)
        return

    with RequestGuard(user.id) as guard:
        if not guard.ok:
            await query.answer(busy_message(user.id), show_alert=True)
            return
        await run_regenerate(query, user, entry)


async def run_regenerate(query, user, entry: dict) -> None:
    """Rebuild the page and swap it into the message. See handle_regenerate."""
    await query.answer("正在重新生成頁面……")
    gallery = entry["gallery"]
    mode = entry["mode"]

    try:
        # Deliberately fetches fresh URLs rather than reading the sweep from the
        # cache: the whole point of regenerating is that the cached links have
        # expired. The fresh set is then written back, so the cache is current again.
        display_urls, original_urls = await asyncio.to_thread(
            ehentai.fetch_gallery_images,
            gallery.page_url,
            cookies_path=resolve_ehentai_cookies(),
            timeout=float(EHENTAI_TIMEOUT_SECONDS),
            workers=EHENTAI_VIEWER_WORKERS,
        )
        await asyncio.to_thread(remember_gallery, gallery.page_url, gallery,
                                (display_urls, original_urls))
        embed = original_urls if mode == "orig" else display_urls
        telegraph_url = await asyncio.to_thread(
            ehentai.create_telegraph_page,
            gallery,
            embed,
            author_name=TELEGRAPH_AUTHOR,
        )
    except (ehentai.EhentaiError, ehentai.TelegraphError) as exc:
        logger.warning("重新生成失敗：%s（%s）", gallery.page_url, exc)
        await query.answer(f"重新生成失敗：{exc}", show_alert=True)
        return
    except Exception:
        logger.exception("重新生成發生未預期錯誤：%s", gallery.page_url)
        await query.answer("重新生成時發生錯誤，請稍後再試。", show_alert=True)
        return

    # The message being replaced is the finished result, so its new text is built
    # the same way and a fresh button is attached.
    text_lines = [build_gallery_info(gallery)]
    text_lines.append(f"\n📄 Telegraph：{telegraph_url}")
    text_lines.append(f"（已重新生成，內嵌 {len(embed)} 頁圖片）")
    if gallery.is_adult:
        text_lines.append("（成人內容，Telegraph 頁面可能被移除）")

    try:
        await query.message.edit_text(
            "\n".join(text_lines),
            link_preview_options=source_link_options(),
            reply_markup=regenerate_keyboard(gallery.gid, gallery.token),
        )
    except TelegramError as exc:
        logger.warning("更新頁面訊息失敗：%s", exc)
        await send_status(query.message, "\n".join(text_lines))
    return


def stop_keyboard(gid: str, token: str) -> InlineKeyboardMarkup:
    """A single '停止' button, shown while a gallery action is running."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("停止", callback_data=f"eh|stop|{gid}|{token}"),
    ]])


# -------------------- Telegraph regeneration --------------------
#
# A Telegraph page embeds e-hentai's image URLs, and those are time-limited signed
# links (hath.network). The tag and fact sections are static HTML and survive, but
# the images stop loading after a while. Free image hosts were measured and are
# not a substitute - catbox rejects the upload, 0x0.st has disabled uploads, and
# uguu.se and tmpfiles.org keep files for hours - so the honest fix is a button
# that rebuilds the page with freshly signed URLs from the same gallery.
#
# The gallery object and the message to update are held here because Telegram caps
# callback data at 64 bytes, which cannot carry them. Keyed by chat and user so one
# person cannot regenerate another's page.
_regenerate_cache: dict[tuple[int, int], dict] = {}
_REGENERATE_LIMIT = 200


def remember_regenerate(chat_id: int, user_id: int, gallery, mode: str,
                        message_id: int) -> None:
    """Store what a later '重新生成' press needs."""
    if len(_regenerate_cache) >= _REGENERATE_LIMIT:
        # Drop the oldest entry; this is a convenience cache, not a store.
        for key in list(_regenerate_cache)[: _REGENERATE_LIMIT // 4]:
            _regenerate_cache.pop(key, None)
    _regenerate_cache[(chat_id, user_id)] = {
        "gallery": gallery,
        "mode": mode,
        "message_id": message_id,
    }


def regenerate_keyboard(gid: str, token: str) -> InlineKeyboardMarkup:
    """The '重新生成' button shown with a finished Telegraph page."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("重新生成 Telegraph 頁面",
                             callback_data=f"eh|regen|{gid}|{token}"),
    ]])


def parse_gallery_callback(data: str) -> tuple[str, str, str] | None:
    """Split 'eh|<mode>|<gid>|<token>' into its parts, or None if malformed."""
    if not data.startswith("eh|"):
        return None
    parts = data.split("|")
    if len(parts) == 2 and parts[1] == "cancel":
        return ("cancel", "", "")
    if len(parts) != 4:
        return None
    mode, gid, token = parts[1], parts[2], parts[3]
    if mode not in {"view", "orig", "zip1280", "ziporig", "stop", "regen"}:
        return None
    if not gid.isdigit() or not token.isalnum():
        return None
    return (mode, gid, token)


async def handle_gallery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Run the action chosen in a gallery's button menu.

    The buttons answer with the gallery id and token, so the metadata and the
    download are both redone here: nothing large is held between the menu and the
    choice, and a stale button cannot point at another request's files.
    """
    query = update.callback_query
    message = update.effective_message
    user = update.effective_user
    if query is None or message is None or user is None:
        return

    parsed = parse_gallery_callback(query.data or "")
    if parsed is None:
        await query.answer("這個按鈕已失效。", show_alert=False)
        return

    mode, gid, token = parsed
    gallery_url = f"https://e-hentai.org/g/{gid}/{token}/"

    if mode == "stop":
        stopped = stop_running_download(message.chat_id)
        await query.answer("已停止下載。" if stopped else "目前沒有進行中的下載。",
                           show_alert=not stopped)
        return

    if mode == "regen":
        await handle_regenerate(update, query, user, gid, token)
        return

    if mode == "cancel":
        await query.answer("已取消。")
        try:
            await message.edit_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        return

    # The chat is passed too, because in a group the group's own authorization
    # decides, not the member's.
    if not user_is_allowed(
            user.id,
            getattr(message, "chat_id", None),
            getattr(getattr(message, "chat", None), "type", None)):
        await query.answer("此機器人目前不允許你的帳號使用。", show_alert=True)
        return

    # Refused only while a request is genuinely running, so pressing a second
    # button as soon as the first finishes works immediately.
    with RequestGuard(user.id) as guard:
        if not guard.ok:
            await query.answer(busy_message(user.id), show_alert=True)
            return
        await run_gallery_action(query, message, user, mode, gallery_url)

    return


async def run_gallery_action(query, message, user, mode: str,
                             gallery_url: str) -> None:
    """Do the chosen gallery work. See handle_gallery_callback."""
    await query.answer()
    # The menu has served its purpose once an action is chosen.
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    label = {
        "view": "Telegraph · 顯示圖",
        "orig": "Telegraph · 原圖",
        "zip1280": "歸檔 · 1280",
        "ziporig": "歸檔 · 原圖",
    }[mode]
    status = await send_status(message, f"已選擇「{label}」，開始處理……")

    temp_dir = make_work_dir()
    try:
        # The gallery's facts come from the cache when present. The URL sweep is
        # cached separately inside run_gallery_images, since that is the slow part.
        cached = PARSE_CACHE.lookup(gallery_url, kind="gallery")
        gallery = gallery_from_payload(cached) if cached else None
        if gallery is None:
            gallery = await asyncio.to_thread(
                ehentai.fetch_gallery,
                gallery_url,
                cookies_path=resolve_ehentai_cookies(),
                timeout=float(EHENTAI_TIMEOUT_SECONDS),
            )
            await asyncio.to_thread(remember_gallery, gallery_url, gallery)
        source_note = build_caption(gallery_url)

        if mode in {"view", "orig"}:
            await run_gallery_images(
                message, status, gallery, mode, temp_dir, source_note, user
            )
        else:
            await run_gallery_archive(
                message, status, gallery, mode, temp_dir, source_note
            )
    except ehentai.EhentaiError as exc:
        logger.warning("畫廊按鈕處理失敗：%s（%s）| %s", gallery_url, exc,
                       admin_hint(None))
        await status.edit_text(f"處理失敗：{exc}\n\n{source_note}",
                               link_preview_options=source_link_options())
    except Exception:
        logger.exception("畫廊按鈕處理發生未預期錯誤：%s", gallery_url)
        await status.edit_text(
            "處理時發生錯誤，請稍後再試。\n\n" + build_caption(gallery_url),
            link_preview_options=source_link_options(),
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def run_gallery_images(message, status, gallery, mode: str,
                             temp_dir: Path, source_note: str, user) -> None:
    """
    Publish a Telegraph page embedding every page of the gallery, and post the
    gallery's cover into the chat.

    The page images are not sent to the chat: they are already on the Telegraph
    page, and posting the same set again only duplicated the gallery and cluttered
    the conversation. The "歸檔" actions are the way to obtain the actual files.
    The **cover** is the exception - one image, fetched by URL by Telegram, which
    gives the reply a face instead of opening with plain text.

    "顯示圖" embeds the site's web-sized image (about 0.14 MB a page) and "原圖"
    the full-resolution file (about 10 MB a page) — and that is a real difference
    here, because Telegraph fetches every embedded image itself.

    This mode downloads nothing. Only the URLs are collected, and Telegraph fetches
    each embedded image itself - which is why the reply appears without waiting for
    the gallery's bytes.
    """
    # The sweep alone. The "歸檔" modes download through the same helper afterwards,
    # so a gallery already looked at costs no sweep there either.
    display_urls, original_urls = await gallery_page_urls(gallery, status, False)

    await status.edit_text(
        f"已取得 {len(display_urls)} 頁，正在建立 Telegraph 頁面……",
        link_preview_options=source_link_options(),
    )

    embed = display_urls if mode == "view" else (original_urls or display_urls)
    try:
        telegraph_url = await asyncio.to_thread(
            ehentai.create_telegraph_page,
            gallery,
            embed,
            author_name=TELEGRAPH_AUTHOR,
        )
    except ehentai.TelegraphError as exc:
        logger.warning("Telegraph 建立失敗：%s", exc)
        telegraph_url = ""
        telegraph_note = f"（Telegraph 頁面建立失敗：{exc}）"
    else:
        telegraph_note = ""

    summary_lines = [build_gallery_info(gallery)]
    if telegraph_url:
        summary_lines.append(
            f"\n📄 Telegraph（內嵌 {len(embed)} 頁圖片）：{telegraph_url}")
    if telegraph_note:
        summary_lines.append(f"\n{telegraph_note}")
    if gallery.is_adult:
        summary_lines.append("（成人內容，Telegraph 頁面可能被移除）")
    summary = "\n".join(summary_lines)

    # Remember this so the page can be re-signed later: e-hentai's image links
    # expire, and this button refreshes them without starting over.
    if telegraph_url:
        remember_regenerate(message.chat_id, user.id, gallery, mode,
                            status.message_id if hasattr(status, "message_id") else 0)

    # One message, not two. The cover is edited into the message the bot already
    # sent, so the reply does not look like it is repeating itself. If the cover
    # cannot be shown the same message falls back to text - the caption carries the
    # source link either way.
    keyboard = (regenerate_keyboard(gallery.gid, gallery.token)
                if telegraph_url else None)
    if await edit_message_to_cover(status, gallery, summary, keyboard):
        return

    await status.edit_text(
        summary,
        link_preview_options=source_link_options(),
        reply_markup=keyboard,
    )


def download_gallery_direct(urls: list[str], output_dir: Path, *, referer: str,
                            workers: int, stop_event=None, total: int = 0,
                            progress=None, register=None) -> int:
    """
    Download image URLs straight from e-hentai's CDN, several at a time.
    Returns how many files were written.

    This replaces running gallery-dl for the download step, and the reason is
    measured rather than assumed. gallery-dl's exhentai extractor ships
    ``request_interval = (3.0, 6.0)``, a deliberate delay of up to six seconds
    between requests that dominates everything else:

        gallery-dl, default interval   29.0s for 5 pages
        gallery-dl, interval 0.2        8.6s
        direct fetch, 5 parallel        5.5s

    The URLs used here are the same ones the gallery page already handed us for
    the Telegraph embedding, so nothing extra is requested from e-hentai. Their
    own fetch is still the fallback when a direct download fails.
    """

    if not urls:
        return 0

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0.0.0 Safari/537.36",
        # e-hentai checks the Referer on some CDN hosts.
        "Referer": referer,
    }
    cookies_path = resolve_ehentai_cookies()
    if cookies_path is not None:
        headers["Cookie"] = ehentai.cookies_header(cookies_path)

    written = 0
    done = 0
    lock = threading.Lock()
    output_dir.mkdir(parents=True, exist_ok=True)

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    # Shorter than a whole-gallery timeout on purpose: a stop cannot interrupt a
    # request already in flight, so this bounds how long the button takes to take
    # effect on the workers that are mid-download. 120s was long enough for a
    # cancelled request to hold the response open for minutes.
    request_timeout = 30.0

    with httpx.Client(headers=headers,
                      **ehentai.http_client_kwargs(request_timeout)) as client:
        def fetch(item):
            index, image_url = item
            if stopped():
                return None
            try:
                response = client.get(image_url)
                # Checked before the body is even inspected: a stop during the
                # request should discard the result rather than write the file.
                if stopped():
                    return None
                if response.status_code >= 400:
                    return None
                content = response.content
            except Exception as exc:  # noqa: BLE001
                if stopped():
                    return None
                logger.warning("直接下載失敗（第 %s 頁）：%s", index, exc)
                return None
            return index, content, response.headers.get("content-type", "")

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            # handler=None: map writes each result into an internal queue as it is
            # produced, so cancelling the iteration does not block on the workers.
            for result in pool.map(fetch, enumerate(urls, 1)):
                with lock:
                    if stopped():
                        # Detected after the request came back, so files written before
                        # the stop still count; nothing further is written.
                        break
                    done += 1
                    if result is not None:
                        index, content, content_type = result
                        suffix = ".jpg"
                        if "png" in content_type:
                            suffix = ".png"
                        elif "webp" in content_type:
                            suffix = ".webp"
                        elif "gif" in content_type:
                            suffix = ".gif"
                        target = output_dir / f"{index:04d}{suffix}"
                        target.write_bytes(content)
                        written += 1
                    if progress:
                        progress(done, total or len(urls))
    return written


def download_gallery_media(url: str, output_dir: Path, *, original: bool,
                           stop_event=None, register=None, total: int = 0,
                           progress=None) -> bool:
    """
    Download a gallery's pages with gallery-dl. Returns True when it completed.

    ``original=False`` asks for the site's 1280px web image; ``True`` asks for the
    full-resolution file. gallery-dl defaults to the original, so both are set
    explicitly rather than relying on the default.

    Started with Popen rather than run so it can be interrupted: downloading every
    original page of a large gallery takes minutes and transfers hundreds of
    megabytes, and a user who changes their mind must be able to stop it.

    Progress is read from gallery-dl's own output. ``--Print 'file:<marker>'``
    registers a post-processor on the per-file event that still downloads, so one
    marker line arrives per completed page. Counting those lines gives a real
    percentage without scraping log messages whose wording can change.

    A killed process exits with a non-zero status, so the cancellation flag is
    checked *before* the status: otherwise a deliberate stop would surface as a
    download failure with an empty error message.
    """
    # The trailing space is required: gallery-dl wraps a bare token in braces and
    # then tries to resolve it as a field name.
    marker = "PROGRESS EVENT"

    # Cleared per run so the diagnosis can only describe this attempt.
    begin_download_diagnosis()

    command = [
        find_executable(),
        "--no-mtime",
        "-D",
        str(output_dir),
        "-o", f"extractor.exhentai.original={'true' if original else 'false'}",
        "--Print", f"file:{marker}",
    ]
    config_path = gallery_dl_config_path()
    if config_path is not None:
        command += ["--config", str(config_path)]
    cookies_path = resolve_ehentai_cookies()
    if cookies_path is not None:
        command += ["--cookies", str(cookies_path)]
    command += ["--", url]

    # Derived from the URL, not assumed: this function is generic - the exhentai `-o`
    # above is simply ignored for other sites - and the warning about a missing proxy
    # should only appear for Pixiv, which is the site that needs one.
    platform = detect_media_platform(url)

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # Line buffering, so a marker is seen as it is written rather than when
        # the process exits.
        bufsize=1,
        shell=False,
        # Without this, gallery-dl ignores the system proxy and every Pixiv App API
        # call times out three times over - 115 seconds - before failing.
        env=gallery_dl_env(platform),
    )
    # The stop flag is deliberately NOT cleared here. It used to be, back when each
    # request spawned its own gallery-dl run and a flag left over from a previous
    # request had to be discarded. The flag is now created per request and shared
    # across the phases of that request, so clearing it here erased a stop that
    # arrived between phases - the fallback then ran to completion and the button
    # looked broken.
    if register is not None:
        register(process)

    done = 0
    stderr_chunks: list[str] = []
    stop_reader = threading.Event()

    def drain_stderr():
        try:
            for line in process.stderr:
                stderr_chunks.append(line)
                if len(stderr_chunks) > 400:
                    del stderr_chunks[:200]
        except Exception:  # noqa: BLE001
            pass

    def read_progress():
        nonlocal done
        try:
            for line in process.stdout:
                if stop_reader.is_set():
                    break
                if marker in line:
                    done += 1
                    if progress is not None:
                        progress(done, total)
        except Exception:  # noqa: BLE001
            pass

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stdout_thread = threading.Thread(target=read_progress, daemon=True)
    stderr_thread.start()
    stdout_thread.start()

    timed_out = False
    try:
        process.wait(timeout=EHENTAI_DOWNLOAD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait()
    finally:
        stop_reader.set()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        if register is not None:
            register(None)

    if timed_out:
        raise ehentai.EhentaiError("下載逾時，已停止。")

    # Checked first: a stop kills the process, which also makes the exit status
    # non-zero, and reporting that as a failure would be wrong.
    if stop_event is not None and stop_event.is_set():
        return False

    if process.returncode != 0:
        details = "".join(stderr_chunks).strip() or "沒有詳細錯誤資訊"
        raise ehentai.EhentaiError(details[-400:])

    # Exit 0 with no files is how a deleted or private item comes back, and the reason
    # is only in stderr - gallery-dl writes to stdout only the progress markers that
    # read_progress consumes. Recorded so the log hint can name it instead of guessing.
    record_download_output("".join(stderr_chunks))
    return True


def download_gallery_for_archive(image_urls: list[str], output_dir: Path, *,
                                 referer: str, workers: int,
                                 stop_event=None, total: int = 0,
                                 progress=None) -> int:
    """
    Download every page for a ZIP archive, in parallel. Returns how many arrived.

    A thin wrapper over :func:`download_gallery_direct`, so the archive path gets the
    same parallel fetch the Telegram paths use. It previously ran gallery-dl, whose
    exhentai extractor waits 3-6 seconds between requests - a deliberate politeness
    delay that made a 21-page archive take minutes while the identical pages were
    already being fetched concurrently elsewhere in this file.

    A file whose fetch fails is missing from the result, and the caller compares the
    count against what it expected before deciding to retry with gallery-dl.
    """
    return download_gallery_direct(
        image_urls, output_dir, referer=referer, workers=workers,
        stop_event=stop_event, total=total, progress=progress,
    )


async def gallery_page_urls(gallery, status, original: bool,
                            *, label: str = "",
                            force: bool = False) -> tuple[list[str], list[str]]:
    """
    Return (display_urls, original_urls) for a gallery, from the cache when possible.

    The per-page viewer sweep is the slow part of a gallery - one request per page,
    about a minute for 53 pages - so a cached sweep is used when there is one. Those
    URLs are **time-limited**: the cache entry lives 14 days, the signed URL does not.
    A sweep older than GALLERY_URL_TTL_SECONDS is therefore re-done rather than reused,
    because reusing it makes every page fail together and pushes the download into the
    slow serial fallback.

    ``force`` skips the cache entirely, which is what a download that came up short
    uses for its retry: at that point the cached list is suspected, not trusted.
    """
    pages = int(gallery.filecount or 0) or None

    cached = {} if force else (PARSE_CACHE.lookup(gallery.page_url, kind="gallery") or {})
    age = None if force else PARSE_CACHE.age(gallery.page_url)
    if cached.get("display_urls") and age is not None and age > GALLERY_URL_TTL_SECONDS:
        logger.info("圖片清單已過期（%.0f 分鐘），重新取得：%s",
                    age / 60, gallery.page_url)
        cached = {}
    if cached.get("display_urls"):
        display_urls = list(cached["display_urls"])
        original_urls = list(cached.get("original_urls") or display_urls)
        logger.info("命中圖片清單快取（%s 頁）：%s", len(display_urls),
                    gallery.page_url)
        return display_urls, original_urls

    display_urls, original_urls = await run_with_progress(
        lambda report: ehentai.fetch_gallery_images(
            gallery.page_url,
            cookies_path=resolve_ehentai_cookies(),
            timeout=float(EHENTAI_TIMEOUT_SECONDS),
            workers=EHENTAI_VIEWER_WORKERS,
            progress=report,
        ),
        status,
        prefix=f"正在取得圖片清單{'（' + label + '）' if label else ''}……",
        gid=gallery.gid,
        token=gallery.token,
        total=pages or 0,
    )
    # Stored with the gallery, so a repeat of either action is free.
    await asyncio.to_thread(remember_gallery, gallery.page_url, gallery,
                            (display_urls, original_urls))
    return display_urls, original_urls


async def download_gallery_pages(gallery, temp_dir: Path, urls: list[str],
                                 status, *, original: bool,
                                 refresh_urls=None,
                                 stop_event=None, chat_id: int = 0,
                                 label: str = "") -> tuple[list[Path], bool]:
    """
    Fetch pages concurrently, returning (files, completed).

    **Not used by the 歸檔 modes any more.** They fetch e-hentai's own published
    archive, which needs no page download at all. This is kept because it is the only
    parallel page fetcher and is exercised by its tests, and because a gallery whose
    official archive cannot be produced still needs a way to get the pages.

    A short result is retried **once with freshly swept URLs** before gallery-dl is
    involved, because the usual cause is not a flaky fetch: these are signed,
    time-limited URLs, and a cached list that has outlived its signature fails for
    every page at once. Re-sweeping fixes that in one parallel pass, whereas the
    serial fallback takes minutes and hides the real problem behind the
    "補齊缺少的頁面" message.

    ``refresh_urls`` is an async callable returning a fresh URL list. gallery-dl is
    kept as the final fallback: it re-signs pages itself and has retries, so a page
    that is genuinely hard to get is not simply lost.
    """
    expected = len(urls)
    if not expected:
        return [], True

    async def fetch(url_list: list[str]) -> None:
        """One parallel pass. Registers the stop flag for its whole duration."""
        register_running_stop(chat_id, stop_event)
        try:
            await run_with_progress(
                lambda report: download_gallery_for_archive(
                    url_list, temp_dir,
                    referer=gallery.page_url,
                    workers=EHENTAI_DOWNLOAD_WORKERS,
                    stop_event=stop_event,
                    total=len(url_list),
                    progress=report,
                ),
                status,
                prefix=f"正在下載 {label}{len(url_list)} 頁……" if label
                else f"正在下載 {len(url_list)} 頁……",
                gid=gallery.gid,
                token=gallery.token,
                total=len(url_list),
            )
        finally:
            # Removed even when the download raised, so a stale flag cannot make a
            # later stop claim to have stopped something.
            register_running_stop(chat_id, None)

    await fetch(urls)

    if stop_event is not None and stop_event.is_set():
        return collect_media_files(temp_dir), False

    files = collect_media_files(temp_dir)
    if len(files) < expected and refresh_urls is not None:
        logger.info("平行下載取得 %s/%s，可能為連結過期，重新取得清單", len(files), expected)
        try:
            fresh, _fresh_original = await refresh_urls()
        except Exception:  # noqa: BLE001
            logger.warning("重新取得圖片清單失敗", exc_info=True)
            fresh = []
        if fresh and list(fresh) != list(urls):
            await fetch(list(fresh))
            if stop_event is not None and stop_event.is_set():
                return collect_media_files(temp_dir), False
            files = collect_media_files(temp_dir)
            logger.info("以新清單重試後取得 %s/%s 頁", len(files), expected)

    if len(files) < expected:
        logger.info("平行下載取得 %s/%s，最後以 gallery-dl 補齊", len(files), expected)
        try:
            await run_with_progress(
                lambda report: download_gallery_media(
                    gallery.page_url, temp_dir, original=original,
                    stop_event=stop_event, total=expected, progress=report,
                    register=lambda process: register_running_download(
                        chat_id, process, stop_event),
                ),
                status,
                prefix="補齊缺少的頁面……",
                gid=gallery.gid,
                token=gallery.token,
                total=expected,
            )
        except ehentai.EhentaiError as exc:
            logger.warning("補齊下載失敗：%s", exc)
        files = collect_media_files(temp_dir)

    # Checked again after the fallback. A stop pressed while gallery-dl ran killed that
    # process, and this is what makes the caller report "已停止" instead of packaging and
    # sending whatever had arrived by then - the button appeared to do nothing.
    if stop_event is not None and stop_event.is_set():
        return files, False

    return files, True


# Downloads that a "停止" button can interrupt, keyed by chat id: the button
# belongs to one chat's request, and pressing stop in one chat must not kill
# another chat's download.
_running_downloads: dict[int, tuple[subprocess.Popen, "threading.Event"]] = {}


def register_running_download(chat_id: int, process: subprocess.Popen | None,
                              stop_event=None) -> None:
    if process is None:
        _running_downloads.pop(chat_id, None)
    elif stop_event is not None:
        _running_downloads[chat_id] = (process, stop_event)


def register_running_stop(chat_id: int, stop_event=None) -> None:
    """
    Make a download stoppable when there is no child process to kill.

    The parallel fetch runs in threads inside this process, so there is nothing to
    terminate - the stop has to be a flag that the workers read. Only the flag was
    passed before, and nothing registered it, so the button could never set it: it
    answered "目前沒有進行中的下載" while the download ran to completion.

    A stop_event is registered with no process, which is exactly how
    :func:`stop_running_download` tells the two kinds apart.
    """
    if stop_event is None:
        _running_downloads.pop(chat_id, None)
    else:
        _running_downloads[chat_id] = (None, stop_event)


def stop_running_download(chat_id: int) -> bool:
    """
    Stop the download for a chat, if one is running. Returns whether anything was
    asked to stop.

    Two kinds are handled, and the difference matters:

    * **gallery-dl** runs as a child process, which is killed. The flag is set first,
      so the worker sees a deliberate stop rather than a crash, and the partially
      written files stay in the request's temporary directory for the caller to
      remove.
    * **The parallel fetch** runs as threads in this process and cannot be killed, so
      the flag is the whole mechanism. It is set without any process, and the workers
      return early at their next check.
    """
    entry = _running_downloads.get(chat_id)
    if entry is None:
        return False
    process, stop_event = entry

    if process is None:
        # Parallel fetch: the flag is the only handle there is.
        stop_event.set()
        return True

    if process.poll() is not None:
        return False
    stop_event.set()
    try:
        process.kill()
    except Exception:  # noqa: BLE001
        logger.warning("停止下載失敗", exc_info=True)
        return False
    return True


def create_zip_batches(files: list[Path], temp_dir: Path) -> list[Path]:
    """
    Zip downloaded pages into as many archives as the upload limit requires.

    **Kept, but no longer on any live path.** The 歸檔 modes now fetch e-hentai's own
    published archive, so nothing here zips pages for a gallery. This remains for a
    caller that needs a bounded archive, and because the split-by-size logic is the
    only place that knowledge lives. Do not wire it back into 歸檔 without deciding
    what to do about the official archive, which is both more accurate and far less
    work: it needs no page download at all.
    """
    batch_limit = int(MAX_UPLOAD_BYTES * 0.9)      # headroom for ZIP overhead
    batches: list[Path] = []
    current: list[Path] = []
    current_size = 0

    def flush(index: int) -> None:
        nonlocal current, current_size
        if not current:
            return
        archive_path = temp_dir / f"ehentai_{index:02d}.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            used = set()
            for item in current:
                name = item.name
                while name in used:
                    name = f"_{name}"
                used.add(name)
                archive.write(item, arcname=name)
        batches.append(archive_path)
        current = []
        current_size = 0

    for file_path in files:
        size = file_path.stat().st_size
        if current and current_size + size > batch_limit:
            flush(len(batches) + 1)
        current.append(file_path)
        current_size += size
    flush(len(batches) + 1)

    return batches


def sanitize_filename(name: str, fallback: str = "archive.zip") -> str:
    """
    Make a name safe to create on disk, keeping it recognisable.

    e-hentai's archive filename carries the gallery title, which routinely contains
    characters Windows forbids in a path - ``:``, ``?``, ``|``, and so on - so using it
    verbatim raises OSError before Telegram is ever reached. Reserved device names
    (``CON``, ``NUL``, ``COM1``) are avoided for the same reason.
    """
    name = (name or "").strip().replace("\x00", "")
    # Path separators would place the file somewhere else entirely.
    name = name.replace("/", "_").replace("\\", "_")
    for bad in '<>:"|?*':
        name = name.replace(bad, "_")
    name = "".join(ch for ch in name if ord(ch) >= 32).strip(" .")
    if not name:
        return fallback
    stem = name.rsplit(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or (
            len(stem) == 4 and stem[:3] in {"COM", "LPT"}
            and stem[3].isdigit()):
        name = f"_{name}"
    # Leave room for the temp directory alongside it.
    if len(name) > 180:
        head, dot, tail = name.rpartition(".")
        name = head[:170] + (f".{tail}" if dot else "")
    return name


def resolve_official_archive(gallery, *, resample: bool) -> tuple[str, str, int]:
    """
    Ask e-hentai's archiver for the gallery's official archive.

    Returns ``(download_url, filename, size_bytes)``.

    e-hentai builds the archive itself, so this needs no per-page download at all -
    it is one POST and one streamed GET, against a sweep plus every page otherwise.
    That is the whole reason for using it.

    The flow has three steps, and each is required:

    1. ``POST archiver.php`` with ``dltype`` (``org`` or ``res``) starts the job and
       returns a page saying the file is being prepared.
    2. That page links a ``hath.network/archive/...`` URL, which is a *waiting* page,
       not the file - fetching it returns HTML.
    3. The same URL with ``?start=1`` is the actual ZIP, and its
       ``Content-Disposition`` carries e-hentai's own filename.

    ``res`` is the 1280px resample and is far smaller in practice: one gallery
    measured 148.2 MiB for ``org`` against 3.2 MiB for ``res``.

    The H@H downloader offered on the same page is deliberately **not** used: it
    requires a running H@H client and answers "Your H@H client appears to be offline"
    otherwise, which a bot cannot satisfy.
    """

    archiver_url = (f"https://e-hentai.org/archiver.php"
                    f"?gid={gallery.gid}&token={gallery.token}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0.0.0 Safari/537.36",
        "Referer": gallery.page_url,
    }
    cookies_path = resolve_ehentai_cookies()
    if cookies_path is not None:
        headers["Cookie"] = ehentai.cookies_header(cookies_path)

    if resample:
        form = {"dltype": "res", "dlcheck": "Download Resample Archive"}
    else:
        form = {"dltype": "org", "dlcheck": "Download Original Archive"}

    with httpx.Client(headers=headers, **ehentai.http_client_kwargs(float(EHENTAI_TIMEOUT_SECONDS))) as client:
        prepared = ehentai.request_with_retry(client, "POST", archiver_url, data=form)
        if prepared.status_code >= 400:
            raise ehentai.EhentaiError(
                f"官方歸檔頁面回應 {prepared.status_code}，請確認 Cookie 是否有效。")

        match = re.search(
            r'href=["\'](https://[^"\']*hath\.network/archive/[^"\']+)["\']',
            prepared.text)
        if not match:
            # The page explains itself when it refuses, and its wording is more useful
            # than a status code: no GP, a missing client, or a bad token.
            reason = re.sub(r"<[^>]+>", " ", prepared.text)
            reason = re.sub(r"\s+", " ", reason).strip()
            raise ehentai.EhentaiError(
                f"官方歸檔無法取得下載連結：{reason[-200:]}")

        waiting_url = match.group(1)
        download_url = (waiting_url if waiting_url.endswith("/2")
                        else waiting_url + "/2") + "?start=1"

        # A ranged GET: this reads the real filename and total size without pulling
        # the whole archive, which matters because the original can be hundreds of MB.
        # Retried for the same reason as the POST above, and safe to repeat: the range
        # asks for four bytes, so a retry cannot re-download the archive.
        probe = ehentai.request_with_retry(
            client, "GET", download_url, headers={"Range": "bytes=0-3"})
        if probe.status_code >= 400:
            raise ehentai.EhentaiError(
                f"官方歸檔下載連結無效（HTTP {probe.status_code}）。")

        disposition = probe.headers.get("content-disposition") or ""
        name_match = re.search(r'filename="?([^";]+)"?', disposition)
        filename = name_match.group(1).strip() if name_match else ""

        size = 0
        content_range = probe.headers.get("content-range") or ""
        if "/" in content_range:
            try:
                size = int(content_range.rsplit("/", 1)[-1])
            except ValueError:
                size = 0
        if not size:
            try:
                size = int(probe.headers.get("content-length") or 0)
            except ValueError:
                size = 0

    if not filename:
        filename = f"{gallery.gid}.zip"
    return download_url, filename, size


class _RangesUnsupported(Exception):
    """
    Raised when a server ignores a Range request and answers with the whole file.

    Distinct from a transport failure: the download is not broken, it simply cannot be
    split, so the caller falls back to a single stream rather than reporting an error.
    """


def _archive_headers(referer: str) -> dict:
    """Request headers for the archive CDN, including the gallery cookie."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0.0.0 Safari/537.36",
        "Referer": referer,
    }
    cookies_path = resolve_ehentai_cookies()
    if cookies_path is not None:
        headers["Cookie"] = ehentai.cookies_header(cookies_path)
    return headers


def download_official_archive(download_url: str, target: Path, *, referer: str,
                              expected: int = 0, stop_event=None,
                              progress=None, workers: int = 0) -> int:
    """
    Download the official archive to disk, in parallel where the server allows it.
    Returns the bytes written.

    **Why parallel.** e-hentai's CDN throttles each connection hard, so a single
    stream leaves most of the link idle. Measured on a 16 MiB prefix of a real archive:

        1 connection    1.68 MiB/s
        4 connections   3.34 MiB/s    (2.0x)
        8 connections   3.38 MiB/s    (saturated - no gain past 4)

    On a 148 MiB original that is roughly 88s against 46s, and the same ceiling applies
    to the archive whatever its size.

    The server must support range requests, which it advertises with
    ``Accept-Ranges: bytes`` and honours with ``206``. When it does not, or when the
    size is unknown or too small to bother, this falls back to one stream - the
    behaviour that existed before.

    Each worker writes its own byte range into the **same** file at its own offset, so
    nothing is buffered in memory and no reassembly step is needed: a 148 MB archive
    never exists anywhere but on disk.

    ``progress`` is called with ``(bytes_done, expected)`` from several threads, so it
    must tolerate that; it only accumulates a counter. The same ``stop_event`` the rest
    of the download uses ends the transfer early.
    """

    headers = _archive_headers(referer)
    # A generous read timeout: the archive is served from a CDN and can be large,
    # while "stop" is handled by the flag rather than by a short timeout.
    timeout = httpx.Timeout(30.0, read=300.0)
    if workers <= 0:
        workers = max(1, min(EHENTAI_ARCHIVE_WORKERS, 8))

    # --- probe: total size, and evidence that ranges are honoured ---
    #
    # HEAD is tried first because it is much cheaper. Measured on this CDN family:
    # HEAD 287 ms against 1468 ms for a one-byte ranged GET, so the probe alone was
    # costing over a second per archive before any payload moved.
    #
    # The evidence accepted is deliberately broad, because the header is not
    # reliable here: this CDN answers a ranged GET with **200** and no
    # ``Accept-Ranges`` at all, so requiring ``Accept-Ranges: bytes`` would have
    # concluded "no ranges" and silently given up the parallel path. What is
    # trustworthy is the *response to a range request*, so a 206 with a parseable
    # ``Content-Range`` counts, and a 200 carrying a plausible Content-Length is
    # treated as "unknown" and verified by the workers instead.
    total = expected
    range_evidence = False
    size_known = False
    try:
        with httpx.Client(headers=headers,
                          **ehentai.http_client_kwargs(timeout)) as client:
            probe = client.head(download_url)
            if probe.status_code >= 400:
                # Some hosts refuse HEAD; the ranged GET below is the real test.
                probe = client.get(download_url, headers={"Range": "bytes=0-0"})
            accept_ranges = (probe.headers.get("accept-ranges") or "").lower()
            content_range = probe.headers.get("content-range") or ""
            if probe.status_code == 206 and "/" in content_range:
                range_evidence = True
                try:
                    total = int(content_range.rsplit("/", 1)[-1])
                    size_known = True
                except ValueError:
                    pass
            elif "bytes" in accept_ranges:
                range_evidence = True
                try:
                    total = int(probe.headers.get("content-length") or 0)
                    size_known = total > 0
                except ValueError:
                    pass
            elif not total:
                try:
                    total = int(probe.headers.get("content-length") or 0)
                except ValueError:
                    total = 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("無法探測官方歸檔大小，改用單一連線：%s", exc)
        range_evidence = False

    # A range download needs a divisible size. Without one, the workers cannot be given
    # ranges, so one stream is the only option.
    use_parallel = (
        range_evidence and size_known and total > ARCHIVE_PARALLEL_MIN_BYTES
        and workers > 1
    )
    if not use_parallel:
        # The reason is reported because the three cases look identical from outside:
        # all three simply download more slowly, and that is the kind of thing a
        # confusing log turns into a wild goose chase.
        if not size_known:
            reason = "大小未知，無法切分"
        elif not range_evidence:
            reason = "伺服器不支援分段請求"
        elif total <= ARCHIVE_PARALLEL_MIN_BYTES:
            reason = (f"檔案不大（{ehentai.format_filesize(total)}），"
                      f"低於 {ehentai.format_filesize(ARCHIVE_PARALLEL_MIN_BYTES)}")
        elif workers <= 1:
            reason = "已設定為單一連線"
        else:
            reason = "未知原因"
        logger.info("官方歸檔使用單一連線下載（%s）", reason)
        return _download_archive_single(headers, download_url, target, total,
                                        stop_event, progress, timeout)

    logger.info("官方歸檔使用 %s 個連線分段下載（%s）", workers,
                ehentai.format_filesize(total))
    try:
        return _download_archive_parallel(headers, download_url, target, total,
                                          workers, stop_event, progress, timeout)
    except _RangesUnsupported as exc:
        # The probe said ranges were honoured and the workers proved otherwise. One
        # stream is correct, just slower, so fall back instead of failing the download.
        logger.warning("分段請求未被接受（%s），改用單一連線", exc)
        return _download_archive_single(headers, download_url, target, total,
                                        stop_event, progress, timeout)


def _download_archive_single(headers: dict, download_url: str, target: Path,
                             total: int, stop_event, progress,
                             timeout) -> int:
    """One stream straight to disk. The fallback, and the small-file path."""

    written = 0
    with httpx.Client(headers=headers,
                      **ehentai.http_client_kwargs(timeout)) as client:
        with client.stream("GET", download_url) as response:
            if response.status_code >= 400:
                raise ehentai.EhentaiError(
                    f"官方歸檔下載失敗（HTTP {response.status_code}）。")
            if not total:
                try:
                    total = int(response.headers.get("content-length") or 0)
                except ValueError:
                    total = 0
            with target.open("wb") as handle:
                for chunk in response.iter_bytes(256 * 1024):
                    if stop_event is not None and stop_event.is_set():
                        break
                    handle.write(chunk)
                    written += len(chunk)
                    if progress is not None:
                        progress(written, total)
    return written


def _download_archive_parallel(headers: dict, download_url: str, target: Path,
                               total: int, workers: int, stop_event, progress,
                               timeout) -> int:
    """
    Split the archive into contiguous ranges and fetch them at once.

    Workers write into one file at distinct offsets, which is safe because each range
    is disjoint and ``pwrite``-style offset writes do not disturb the others. The file
    is pre-sized so every worker has somewhere to write without extending it
    concurrently.

    A failed range is retried once; a range that still fails is reported, because
    silently returning a short file would hand Telegram a corrupt archive.
    """

    written = 0
    lock = threading.Lock()
    # Declared before the workers so the closure always refers to one list.
    failed_ranges: list[tuple[int, int]] = []
    # A modest chunk keeps the last worker from idling on a straggler, while staying
    # large enough that the per-request overhead is irrelevant.
    chunk_size = max(1 * 1024 * 1024, total // (workers * 4))
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < total:
        end = min(start + chunk_size - 1, total - 1)
        ranges.append((start, end))
        start = end + 1

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    def fetch_range(item, attempt=1):
        nonlocal written
        index, (lo, hi) = item
        if stopped():
            return 0
        got = 0
        try:
            # Its own client per worker: sharing one across threads would serialise on
            # its connection pool, which is the very thing being avoided.
            with httpx.Client(headers=headers,
                              **ehentai.http_client_kwargs(timeout)) as client:
                with client.stream(
                        "GET", download_url,
                        headers={"Range": f"bytes={lo}-{hi}"}) as response:
                    # A 206 is required, not merely preferred. A server that ignores
                    # the Range header answers 200 with the **whole** file, and writing
                    # that at this worker's offset would corrupt the archive silently -
                    # the file would be the right length and the wrong bytes. Raising
                    # here turns that into a clean fallback to one stream.
                    if response.status_code != 206:
                        raise _RangesUnsupported(
                            f"伺服器未依分段回應（HTTP {response.status_code}）")
                    offset = lo
                    with target.open("r+b") as handle:
                        for part in response.iter_bytes(256 * 1024):
                            if stopped():
                                return got
                            handle.seek(offset)
                            handle.write(part)
                            offset += len(part)
                            got += len(part)
                            with lock:
                                written += len(part)
                                if progress is not None:
                                    progress(written, total)
        except _RangesUnsupported:
            # Not retryable and not a per-range failure: the server does not support
            # ranges at all, so retrying the other ranges would fail identically.
            # Propagated for the caller to fall back to a single stream. This must come
            # before the generic handler, or it would be recorded as a failed range and
            # reported as a corrupt download instead of a fallback.
            raise
        except Exception as exc:  # noqa: BLE001
            if stopped():
                return got
            if attempt < 2:
                logger.warning("分段 %s-%s 失敗，重試一次：%s", lo, hi, exc)
                return got + fetch_range(item, attempt + 1)
            logger.warning("分段 %s-%s 重試後仍失敗：%s", lo, hi, exc)
            with lock:
                failed_ranges.append((lo, hi))
            return got
        return got

    failed_ranges: list[tuple[int, int]] = []
    with target.open("wb") as handle:
        # Pre-size so concurrent offset writes never race to extend the file.
        handle.truncate(total)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(fetch_range, enumerate(ranges, 1)))

    # Checked before the stop test on purpose. A genuine stop leaves a partial file,
    # which the caller discards - but **nothing at all** written is not a partial
    # download, it is a failed one, and reporting it as a short read would let the
    # caller send a 0-byte archive. That is what happened when an expired archive URL
    # made every range fail while a stop was arriving.
    if written == 0:
        raise ehentai.EhentaiError(
            "官方歸檔沒有任何分段下載成功（下載連結可能已過期，請重新取得）。")
    if stopped():
        return written
    if failed_ranges:
        raise ehentai.EhentaiError(
            f"官方歸檔有 {len(failed_ranges)} 個分段下載失敗，"
            "檔案不完整。")
    return written


async def run_gallery_archive(message, status, gallery, mode: str,
                              temp_dir: Path, source_note: str) -> None:
    """
    Fetch the gallery's **official archive** from e-hentai's archiver and send it.

    This replaces building an archive here. e-hentai prepares its own ZIP, so the bot
    downloads no pages at all: one POST returns the archive URL, and one streamed GET
    delivers the file. Previously this swept every viewer page and downloaded every
    image, which is strictly more work for the same ZIP - and produced a differently
    named archive that was not the one e-hentai publishes.

    The trade is size, and it is a real one:

        org (original)   148.2 MiB for the gallery measured
        res (1280x)        3.2 MiB for the same gallery

    Telegram only accepts about 50 MB from a bot, so ``org`` usually cannot be
    delivered. The size is reported before the attempt and the upload is still tried,
    so the answer comes from Telegram rather than from a local refusal.
    """
    original = mode == "ziporig"
    label = "原圖" if original else "1280"
    # Set by the "停止" button so a transfer in progress is reported as cancelled.
    stop_event = threading.Event()

    await status.edit_text(
        f"正在向 e-hentai 官方歸檔取得{label}壓縮包……",
        link_preview_options=source_link_options(),
    )

    try:
        download_url, filename, size = await asyncio.to_thread(
            resolve_official_archive, gallery, resample=not original)
    except ehentai.EhentaiError as exc:
        logger.warning("官方歸檔取得失敗：%s（%s）", gallery.page_url, exc)
        await status.edit_text(
            f"無法取得官方歸檔：{exc}\n"
            "（官方歸檔需要有效的 e-hentai／exhentai Cookie）\n\n" + source_note,
            link_preview_options=source_link_options(),
        )
        return

    logger.info("官方歸檔：%s（%s，%s）", filename, label,
                ehentai.format_filesize(size))

    # Safe on disk: e-hentai's filename carries the gallery title, which can contain
    # characters Windows rejects.
    safe_name = sanitize_filename(filename)
    archive_path = temp_dir / safe_name

    await status.edit_text(
        f"官方歸檔已備妥（{ehentai.format_filesize(size)}），開始下載……",
        link_preview_options=source_link_options(),
    )

    # Registered so "停止" reaches this phase, exactly as for a page download.
    register_running_stop(message.chat_id, stop_event)
    try:
        written = await run_with_progress(
            lambda report: download_official_archive(
                download_url, archive_path, referer=gallery.page_url,
                expected=size, stop_event=stop_event, progress=report,
            ),
            status,
            prefix=f"正在下載官方歸檔（{label}）……",
            gid=gallery.gid,
            token=gallery.token,
            total=size or 1,
            # A single large file reports far less often than a page set, so the
            # progress bar should not wait as long between updates.
            min_interval=1.0,
        )
    except ehentai.EhentaiError as exc:
        logger.warning("官方歸檔下載失敗：%s", exc)
        await status.edit_text(
            f"下載官方歸檔失敗：{exc}\n\n" + source_note,
            link_preview_options=source_link_options(),
        )
        return
    finally:
        register_running_stop(message.chat_id, None)

    if stop_event.is_set():
        await status.edit_text(
            "已停止下載。\n\n" + source_note,
            link_preview_options=source_link_options(),
        )
        return

    archive_bytes = archive_path.stat().st_size if archive_path.exists() else written
    if not archive_bytes:
        await status.edit_text(
            "沒有下載到任何內容。\n\n" + source_note,
            link_preview_options=source_link_options(),
        )
        return

    caption = build_caption(
        gallery.page_url,
        f"{gallery.title[:120]}\n官方歸檔 · {label} · "
        f"{ehentai.format_filesize(archive_bytes)}",
    )

    # Reported, not enforced: an archive over the limit is still sent, so the answer
    # comes from Telegram rather than from a local refusal. With a self-hosted server
    # in local mode the limit is 2000 MB, so the e-hentai original usually fits.
    _limit = upload_limit_bytes()
    if archive_bytes > _limit:
        logger.info("官方歸檔（%s）超過上限 %s，仍嘗試傳送",
                    ehentai.format_filesize(archive_bytes),
                    ehentai.format_filesize(_limit))
        await status.edit_text(
            f"官方歸檔 {ehentai.format_filesize(archive_bytes)}，"
            f"超過目前的上傳上限 {ehentai.format_filesize(_limit)}，仍嘗試傳送……",
            link_preview_options=source_link_options(),
        )

    try:
        with archive_path.open("rb") as archive:
            await message.reply_document(
                document=archive,
                filename=archive_path.name,
                caption=caption,
            )
    except TelegramError as exc:
        logger.warning("官方歸檔傳送失敗：%s（%s）", archive_path.name, exc)
        await status.edit_text(
            "壓縮包傳送失敗：這個檔案超過 Telegram 允許的大小。\n"
            f"（{ehentai.format_filesize(archive_bytes)}／"
            f"上限約 {ehentai.format_filesize(_limit)}）\n"
            "可改用「歸檔 · 1280」，官方 resample 會小得多。\n\n" + source_note,
            link_preview_options=source_link_options(),
        )
        return

    summary = [
        build_gallery_info(gallery),
        f"\n📦 已傳送官方歸檔（{label}，"
        f"{ehentai.format_filesize(archive_bytes)}）",
    ]
    if gallery.is_adult:
        summary.append("（成人內容）")
    await status.edit_text("\n".join(summary),
                           link_preview_options=source_link_options())


def progress_bar(done: int, total: int, width: int = 10) -> str:
    """
    Render '██████░░░░ 60% (12/20)' for a progress message.

    A bar rather than only a number, because the question a waiting user has is
    "roughly how much longer", and a filled bar answers that at a glance.
    """
    if total <= 0:
        return f"{done} 頁"
    done = max(0, min(done, total))
    filled = int(width * done / total)
    percent = int(100 * done / total)
    return f"{'█' * filled}{'░' * (width - filled)} {percent}% ({done}/{total})"


async def run_with_progress(work, status, *, prefix: str, gid: str, token: str,
                            total: int, min_interval: float = 1.5):
    """
    Run a blocking callable on a worker thread while showing its progress.

    ``work`` is called as ``work(report)`` and is expected to call ``report(done,
    total)`` as it goes. Those calls come from worker threads, so they are handed to
    the event loop through a queue - only the loop may edit a Telegram message.

    Edits are throttled: Telegram rate-limits message edits, and a page-per-second
    gallery would otherwise be throttled into failures.

    **A bar is shown immediately, at 0%**, before any work has reported anything. Two
    reasons, both learned from a real download: the stop button belongs on screen from
    the start, and a large archive can take many seconds to produce its first byte, so
    waiting for the first report left the message looking frozen.

    **Completion is only claimed when progress was actually observed.** The final edit
    used to be ``latest[0] or total``, which printed a full 100% bar when the work had
    reported nothing at all - so a download that failed instantly was displayed as
    finished. A caller that gets nothing back now finds the message unedited rather
    than falsely complete, and reports its own failure.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    seen_progress = False

    def report(done, observed_total, available=None):
        # Called from a worker thread, so the queue must not block it.
        #
        # ``available`` is the gallery's real page count when the caller capped its work
        # below it, so the bar can say "20/20 of 27" instead of implying the gallery has
        # only 20 pages. Optional: most workers never send it.
        loop.call_soon_threadsafe(queue.put_nowait,
                                  (done, observed_total or total, available))

    def render(done, observed, available=None):
        text = f"{prefix}\n{progress_bar(done, observed or total)}\n"
        if available and available > (observed or total):
            # The gallery has more pages than this run will process, and saying so is the
            # difference between "done" and "you got 20 of 27".
            text += f"（作品共 {available} 張，單次最多 {observed or total} 張）\n"
        return text + "（可按「停止」中止）"

    # Before the work starts, so the bar and the stop button appear at once.
    try:
        await status.edit_text(
            render(0, total),
            link_preview_options=source_link_options(),
            reply_markup=stop_keyboard(gid, token),
        )
    except TelegramError:
        logger.debug("無法顯示初始進度，繼續下載", exc_info=True)

    task = asyncio.create_task(asyncio.to_thread(work, report))

    last_edit = 0.0
    latest = (0, total)
    poll = max(0.05, min(min_interval, 0.25)) if min_interval else 0.1
    try:
        # Poll and drain rather than waiting on the queue directly. Two earlier
        # attempts failed here for the same underlying reason, so it is worth
        # recording: `asyncio.wait_for(queue.get(), timeout)` cancels the getter on
        # every timeout, and a queue whose getter keeps being cancelled stops
        # waking reliably - the message then sat unchanged until the work finished.
        # Sleeping and draining has no cancellation to corrupt, and costs nothing
        # measurable at these intervals.
        while True:
            progressed = False
            while not queue.empty():
                latest = queue.get_nowait()
                progressed = True
                seen_progress = True

            if progressed:
                now = time.monotonic()
                if now - last_edit >= min_interval:
                    last_edit = now
                    try:
                        await status.edit_text(
                            render(latest[0], latest[1], latest[2] if len(latest) > 2 else None),
                            link_preview_options=source_link_options(),
                            reply_markup=stop_keyboard(gid, token),
                        )
                    except TelegramError:
                        # A throttled or deleted message must not stop the download.
                        logger.debug("進度更新失敗，繼續下載", exc_info=True)

            if task.done() and queue.empty():
                break

            await asyncio.sleep(poll)
    finally:
        result = await task

    # Only a run that reported something gets a closing bar. Without progress there is
    # nothing truthful to say about completion, and saying "100%" would be a lie the
    # caller cannot correct - its own failure message would arrive after this one.
    if seen_progress:
        try:
            await status.edit_text(
                # latest[2] carries "the gallery really has N pages" when the caller
                # capped its work. Dropping it here erased the notice on the final edit,
                # which is the one the user actually reads.
                render(latest[0], latest[1], latest[2] if len(latest) > 2 else None),
                link_preview_options=source_link_options(),
                reply_markup=stop_keyboard(gid, token),
            )
        except TelegramError:
            pass
    else:
        logger.warning("下載過程沒有回報任何進度（%s），不顯示完成進度", prefix)
    return result


async def handle_telegram_links(message, urls: list[str]) -> bool:
    """Reply about Telegram links; return True if one was handled."""
    for url in urls:
        kind = classify_telegram_link(url)
        if kind == "invite":
            await send_status(
                message,
                "偵測到 Telegram 群組／頻道邀請連結。\n"
                "我不會自動加入該群組，也不會驗證邀請連結是否有效。",
            )
            return True
        if kind == "public_link":
            await send_status(
                message,
                "偵測到 Telegram 公開連結。\n"
                "它可能指向群組、頻道、使用者或機器人；"
                "僅憑連結格式無法準確區分。",
            )
            return True
    return False


def bot_was_mentioned(update, context) -> bool:
    """
    Whether this message addresses the bot directly.

    Only two things count: a reply to one of the bot's own messages, or an `@mention`
    of **this bot's** username. Any other mention is somebody else being addressed - a
    reply to a person, or a mention of another user.

    A reply is matched on the bot's **own user id** when that is known. Falling back to
    "the sender is any bot" is broader than intended: it would treat a reply to a
    *different* bot in the same group as addressing this one.

    Retained for that meaning, but note that no caller acts on it any more:
    ``handle_message`` returns on a link-less message without consulting this, because
    replying to the bot made it answer with the help text and that was reported as the
    bot reacting to someone else's conversation. It stays because it is the correct
    test for "was I addressed", and the characterization suite pins its behaviour for
    the day something needs it.
    """
    message = getattr(update, "effective_message", None)
    if message is None:
        return False

    # A reply to one of the bot's own messages.
    reply_to = getattr(message, "reply_to_message", None)
    if reply_to is not None:
        sender = getattr(reply_to, "from_user", None)
        if sender is not None:
            bot = getattr(context, "bot", None)
            bot_id = getattr(bot, "id", None)
            sender_id = getattr(sender, "id", None)
            if bot_id is not None and sender_id is not None:
                if sender_id == bot_id:
                    return True
            elif getattr(sender, "is_bot", False):
                # No id to compare: the broader test is the only option left.
                return True

    bot = getattr(context, "bot", None)
    bot_username = (getattr(bot, "username", "") or "").lstrip("@").lower()
    if not bot_username:
        # Without knowing our own username, a mention cannot be attributed to us, so
        # the safest answer is "not addressed to me".
        return False

    text = message.text or message.caption or ""
    if f"@{bot_username}" in text.lower():
        return True

    # An entity of type "mention" carries the username in the text it spans. Checking
    # the text rather than merely the entity's presence is the whole point: a mention
    # of a *different* user must not be read as addressing the bot.
    entities = (getattr(message, "entities", None)
                or getattr(message, "caption_entities", None) or [])
    for entity in entities:
        if getattr(entity, "type", "") != "mention":
            continue
        start = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)
        if start is None or length is None:
            continue
        mentioned = text[start:start + length].lstrip("@").lower()
        if mentioned == bot_username:
            return True
    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    # Works for plain text and media captions, in private chats and in groups.
    text = message.text or message.caption or ""
    if not text:
        return

    # Look for a link FIRST, before the authorization check.
    #
    # Privacy mode is off on this bot, so Telegram delivers every group message -
    # including ones with nothing to parse. With the authorization check first, any
    # message from a non-authorized user was answered with "not allowed", even a
    # plain remark like "i send no link". The bot appeared to be reading other
    # people's conversations.
    urls = extract_urls(text)
    if not urls:
        # Nothing to parse, so nothing is said. This is deliberately unconditional.
        #
        # It used to answer a link-less message when the bot appeared to be addressed -
        # a reply to one of its own messages, or an @mention of its username. That was
        # reported as the bot being triggered by someone else: replying to a message the
        # bot had sent earlier made it answer with the help text, which read as the bot
        # reacting to an unrelated remark. Any heuristic here is a guess about intent,
        # and a guess that is wrong is worse than silence.
        #
        # Being addressed with no link is still served by /help, which is explicit and
        # cannot misfire. Silence costs nothing: a user who meant to ask something can
        # send a link or the command.
        return

    # There is something to act on, so authorization is checked now - and only now.
    # The chat is passed too, because in a group the group's own authorization
    # decides, not the member's - which is what makes authorizing a group enough for
    # everyone in it.
    if not user_is_allowed(
            user.id,
            getattr(message, "chat_id", None),
            getattr(getattr(message, "chat", None), "type", None)):
        await send_status(message, "此機器人目前不允許你的帳號使用。")
        return

    # Telegram links are identified separately and never sent to the downloader.
    telegram_urls = [url for url in urls if classify_telegram_link(url)]
    media_url = next(
        (url for url in urls if detect_media_platform(url)),
        None,
    )
    # e-hentai galleries are recognised by URL shape rather than by
    # detect_media_platform, because they go through their own metadata route
    # instead of the media downloader.
    eh_url = gallery_url(text)

    if telegram_urls and not media_url and not eh_url:
        await handle_telegram_links(message, telegram_urls)
        return

    # An artwork link wins over a gallery link, matching the rule that a supported
    # media link takes precedence when several links share a message.
    if eh_url and not media_url:
        wait_left = apply_cooldown(user.id)
        if wait_left > 0:
            await send_status(
                message, f"操作太快了，請約 {int(wait_left) + 1} 秒後再試。"
            )
            return
        await handle_ehentai_gallery(message, eh_url, user)
        return

    if not media_url:
        # Only the supported sites are resolved. Any other link is answered with a
        # short notice rather than being fetched, so the bot never becomes a
        # general-purpose link fetcher.
        if REPLY_UNSUPPORTED_LINKS:
            await send_status(message, SUPPORTED_SITES_NOTICE)
        return

    platform = detect_media_platform(media_url)

    # A link that has been parsed before is served from the cache: the files were
    # copied aside, so this costs no download at all.
    cached_media = PARSE_CACHE.lookup(media_url, kind="media")

    # The link cooldown exists to stop repeated *download* work, and a cached result
    # does none. Applying it here would make a repeat wait ten seconds before showing
    # the answer it already has - the one case where waiting is pure loss.
    if cached_media is None:
        wait_left = apply_cooldown(user.id)
        if wait_left > 0:
            await send_status(
                message, f"操作太快了，請約 {int(wait_left) + 1} 秒後再試。"
            )
            return

    status = await send_status(message, "收到連結，正在解析……")
    temp_dir = make_work_dir()
    source_note = build_caption(media_url)
    succeeded = False

    if cached_media is not None:
        try:
            if await send_cached_media(message, status, media_url, cached_media,
                                       platform, source_note):
                logger.info("命中解析快取：%s", media_url)
                shutil.rmtree(temp_dir, ignore_errors=True)
                # The same cleanup a fresh download performs. Without this the cache
                # path left both the progress/summary notice and the link message in
                # the chat, so serving from the cache produced *more* clutter than
                # downloading did - the opposite of the point.
                await cleanup_request_messages(status, message)
                return
        except TimedOut:
            # NOT re-downloaded and NOT re-sent. This is the second duplication path:
            # the cached send very likely arrived, and falling through would download the
            # same artwork again and deliver it a second time. Observed as two or three
            # copies of one Pixiv work after a timeout.
            logger.warning("快取傳送逾時，不再重新下載或重送以免重複：%s", media_url)
            shutil.rmtree(temp_dir, ignore_errors=True)
            await status.edit_text(
                "傳送逾時，可能已經送達（請先確認）。若沒有收到，可能是網路較慢，"
                "請稍後再試。",
                link_preview_options=source_link_options(),
            )
            return
        except TelegramError:
            logger.warning("快取傳送發生錯誤，改為重新下載", exc_info=True)
        logger.info("快取無法使用，重新下載：%s", media_url)

    try:
        async with download_semaphore:
            await asyncio.to_thread(
                download_with_gallery_dl, media_url, temp_dir, platform
            )

        media_files = collect_media_files(temp_dir)

        if not media_files:
            # No operator detail in the chat. The credential diagnosis names local
            # files and command lines, and an operator who wants it can run
            # /cookies or read the log - where it is now written instead, so the
            # information is not lost, only kept out of the conversation.
            logger.warning("沒有取得媒體：%s（%s）| %s",
                           media_url, platform, admin_hint(platform))
            await status.edit_text(
                "沒有找到可傳送的圖片或影片。\n\n" + visitor_failure_note(platform)
                + "\n\n" + source_note,
                link_preview_options=source_link_options(),
            )
            return

        if platform == "pixiv":
            # Sending the artwork as-is: one message per album, original files.
            # No image is downscaled, so the bytes the recipient gets match what
            # gallery-dl downloaded.
            #
            # If a send is refused purely because a file is too large, the work is
            # re-sent as a ZIP - see send_original_files, which reports that back
            # through ``oversize`` so the user can be told what happened and why.
            sent_count, album_note, oversize = await send_original_files(
                message, media_files, media_url, temp_dir,
                # Requested: Pixiv sends the images alone, with no source link and no
                # button. Only X/Twitter carries the link.
                attach_link=False,
            )

            if oversize:
                total_mb = sum(f.stat().st_size for f in oversize) / 1048576
                # Say what happened and what to expect. An unexplained archive looks
                # like a failure, and the user cannot tell that the originals are inside.
                await status.edit_text(
                    f"{len(oversize)} 個檔案因為超過 Telegram 的上傳限制而無法直接傳送"
                    f"（共約 {total_mb:.1f} MB）。\n\n"
                    f"已改用 ZIP 傳送原檔，內容與原圖完全相同（不重新編碼）。"
                    f"檔案較大，請稍等，下載後解壓縮即可看到原檔。",
                    link_preview_options=source_link_options(),
                )
                archive = await asyncio.to_thread(
                    zip_files, oversize, temp_dir, f"{Path(media_url).name}.zip")
                await send_single_media(
                    message, archive, media_url, temp_dir,
                    prefer_original=True,      # a document: the ZIP is the payload
                    caption=build_caption(media_url),
                )
                await cleanup_request_messages(status, message)
                await asyncio.to_thread(remember_media_parse, media_url, media_files)
                succeeded = True
                return

            summary = f"Pixiv 解析完成，已傳送原圖 {sent_count}/{len(media_files)} 個檔案。"
            if album_note:
                summary += f"（{album_note}）"
            # No source link here: send_original_files posts it as its own last
            # message, which is what keeps it at the bottom of the result.
            await status.edit_text(summary,
                                   link_preview_options=source_link_options())
            succeeded = True
            # Cached after a successful send, so the next request for this link is
            # served from disk instead of downloading again.
            await asyncio.to_thread(remember_media_parse, media_url, media_files)
            return

        # --- X/Twitter: the same sender, but as normal photos. A Telegram photo
        # is re-encoded, which is what users expect from a post preview; the
        # byte-exact "original" path is reserved for Pixiv artwork. ---
        sent_count, album_note, _too_large = await send_original_files(
            message, media_files, media_url, temp_dir, as_document=False,
            # Requested: X/Twitter shows the source link, Pixiv does not.
            attach_link=True,
        )

        summary = f"X/Twitter 解析完成，已傳送 {sent_count}/{len(media_files)} 個檔案。"
        if album_note:
            summary += f"（{album_note}）"
        await status.edit_text(summary, link_preview_options=source_link_options())
        succeeded = True
        await asyncio.to_thread(remember_media_parse, media_url, media_files)

    except subprocess.TimeoutExpired:
        await status.edit_text(
            "下載逾時，請稍後重試。\n\n" + source_note,
            link_preview_options=source_link_options(),
        )
    except Exception:
        logger.exception("Failed to process URL")
        # The operator detail goes to the log rather than into the chat: a failure
        # reply should be a short statement of what happened, not a command line.
        logger.warning("解析失敗的診斷：%s | %s", media_url, admin_hint(platform))
        await status.edit_text(
            "解析失敗，請稍後再試。\n\n" + source_note,
            link_preview_options=source_link_options(),
        )
    finally:
        # Only after success: on failure the status message carries the reason and
        # must survive, and the source link is the only reference the user has.
        if succeeded:
            await cleanup_request_messages(status, message)
        shutil.rmtree(temp_dir, ignore_errors=True)


# -------------------- Startup --------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Report an unhandled exception from any handler, instead of losing it.

    Without this, python-telegram-bot logs "No error handlers are registered" and a
    traceback, and the user who triggered it gets **nothing** — the chat simply goes
    quiet. That is how a one-line NameError in /status reached an operator as a
    stack trace in the console while the command itself appeared to do nothing.

    The traceback is still logged in full, because that is what makes it fixable.
    What is added is a reply, so the failure is visible where it happened.
    """
    logger.error("處理更新時發生未預期錯誤", exc_info=context.error)

    message = None
    if isinstance(update, Update):
        message = update.effective_message
    if message is None:
        return

    note = "處理這個指令時發生錯誤，請稍後再試。"
    user = getattr(update, "effective_user", None)
    if user is not None and user_is_admin(user.id):
        # The operator can act on it, and the exception type is the single most
        # useful clue, so it is included for them and withheld from visitors.
        note += f"\n\n（管理員）{type(context.error).__name__}: {context.error}"
    try:
        await message.reply_text(note)
    except TelegramError:
        # Replying can itself fail (the chat is gone, the bot was blocked). There
        # is nowhere left to report that, so it is only logged.
        logger.debug("無法回報錯誤訊息給使用者", exc_info=True)


async def post_init(application: Application):
    """
    Publish the command menus **and the bot's own profile text**.

    The public menu lists visitor commands only, so it does not advertise
    capabilities a visitor cannot use. Administrators get a second menu scoped to
    their own chat, which is how they discover the management commands without
    those being visible to anyone else.

    The description and short description are set here for a security reason rather than a
    cosmetic one. **Anyone holding the bot token can rewrite them**, and that is exactly
    what happened: the description was replaced with a referral link to an unrelated
    adult site, in two languages. Nothing in this repository ever set a description, so
    the text could only have come from outside - and a token that can set a description
    can also read updates and send messages.

    Setting them at every startup makes the profile self-healing: the ad comes back only
    if the attacker acts again, and the next restart removes it. Rotate the token as well
    - see SECURITY notes in the README; this only repairs the symptom.
    """
    # Reclaimed on every start. A failure here must not stop the bot: an unset
    # description is cosmetic, whereas failing to boot is not.
    try:
        await application.bot.set_my_description(BOT_DESCRIPTION)
        await application.bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
    except TelegramError as exc:
        logger.warning("無法設定機器人簡介：%s", exc)

    await application.bot.set_my_commands([
        BotCommand("start", "開始使用"),
        BotCommand("help", "使用說明"),
        BotCommand("id", "顯示你的 ID"),
    ])

    # A scoped menu, per administrator. This is why the management commands can
    # exist without appearing in the public list: Telegram shows a chat its own
    # scope in preference to the default.
    operator_commands = [
        BotCommand("start", "開始使用"),
        BotCommand("help", "使用說明"),
        BotCommand("id", "顯示你的 ID"),
        BotCommand("authorized", "查看授權狀態"),
        BotCommand("grant", "授權使用者（/grant <ID>）"),
        BotCommand("revoke", "移除授權（/revoke <ID>）"),
    ]
    admin_commands = operator_commands + [
        BotCommand("promote", "設為管理員（/promote <ID>）"),
        BotCommand("demote", "取消管理員（/demote <ID>）"),
        BotCommand("lock", "私訊守門／開放模式"),
        BotCommand("lockgroup", "群組守門／開放模式"),
        BotCommand("cookies", "登入憑證狀態"),
        BotCommand("status", "執行狀態"),
        BotCommand("cache", "解析快取狀態／清除"),
        BotCommand("restart", "重新啟動機器人"),
    ]

    for user_id in sorted(AUTHORIZATION.allowed | AUTHORIZATION.admins):
        commands = (admin_commands if user_is_admin(user_id)
                    else operator_commands)
        try:
            await application.bot.set_my_commands(
                commands, scope=BotCommandScopeChat(chat_id=user_id)
            )
        except TelegramError as exc:
            # A user who has never started a private chat with the bot cannot have
            # a scoped menu set, and Telegram refuses it. That is normal and must
            # not stop startup.
            logger.debug("無法為 %s 設定專屬命令選單：%s", user_id, exc)


def log_startup_configuration():
    """Print the settings that silently change behaviour, once, at startup."""
    logger.info("支援站點：X/Twitter、Pixiv、e-hentai")
    logger.info("e-hentai 成人畫廊建立 Telegraph：%s", TELEGRAPH_ALLOW_ADULT)
    logger.info("其他連結回覆提示：%s", REPLY_UNSUPPORTED_LINKS)
    logger.info("附上原始連結：%s", ATTACH_SOURCE_LINK)
    logger.info("授權模式：%s", AUTHORIZATION.mode_label)
    logger.info("群組模式：%s", AUTHORIZATION.group_mode_label)
    logger.info("已授權使用者：%s 人", len(AUTHORIZATION.allowed))
    logger.info("已授權群組：%s 個", len(AUTHORIZATION.groups))
    if AUTHORIZATION.admins:
        logger.info("管理員命令可用（%s 位管理員）。", len(AUTHORIZATION.admins))
    else:
        logger.warning(
            "未設定管理員，所有管理命令（/cookies、/status、/restart、"
            "/promote、/demote、/lock）對所有人關閉。"
            "傳送 /id 給機器人取得你的 user ID，寫入 .env 的 ADMIN_USER_IDS "
            "並重新啟動即可啟用；之後可用 /promote 在聊天中新增其他管理員。"
        )
    for line in credential_summary().splitlines():
        logger.info("憑證 %s", line)
    if not CHECK_COOKIES:
        return
    for platform, label in (("twitter", "X/Twitter"), ("pixiv", "Pixiv")):
        usable, status = cookie_status(platform)
        if usable or status == "未設定":
            continue
        if platform == "pixiv" and "refresh-token" in status:
            logger.warning(
                "Pixiv 尚未設定 refresh-token，Pixiv 作品無法下載："
                "執行 .venv\\Scripts\\gallery-dl.exe oauth:pixiv 取得後填入 .env。"
            )
        else:
            logger.warning(
                "%s 的憑證不可用（%s）；該站點的作品可能無法下載。",
                label,
                status,
            )

    # Expiry is a separate failure mode from "not configured": the bot keeps
    # working right up to the moment a credential dies, then stops with an error
    # that looks like a bug. Warning in advance is the whole point.
    #
    # Only X/Twitter is warned about: its auth_token *is* the credential for all
    # X access. For Pixiv the credential is the refresh token, which does not
    # expire, and PHPSESSID is used only by pixiv.net's own AJAX endpoints (the
    # R-18 sanity check), so a lapsed PHPSESSID does not stop artwork downloads.
    days, note = cookie_expiry("twitter")
    if days is not None:
        if days < 0:
            logger.warning("X/Twitter 的登入 cookie 已過期，請重新匯出並取代 cookies/twitter.txt。")
        elif days <= EXPIRY_WARN_DAYS:
            logger.warning(
                "X/Twitter 的登入 cookie 還有 %s 天過期，"
                "建議盡快重新匯出並取代 cookies/twitter.txt。",
                days,
            )


def enable_utf8_console():
    """
    Make console output survive the Chinese Windows default code page.

    cmd.exe on a zh-CN system uses GBK, which cannot encode characters such as
    the status marks in the credential report; without this the self-check
    crashes with UnicodeEncodeError before printing its result.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # A redirected or already-closed stream is not worth failing over.
            pass


def run_self_check():
    """
    Validate the configuration, and optionally prove the credentials work.

    This exists because the interesting failures (a wrong cookie path, a missing
    refresh token) otherwise only show up as a vague Telegram error message, and
    checking them from a terminal is far faster than round-tripping in chat.
    """
    enable_utf8_console()
    print("=== 設定自檢 ===")
    print(f"BOT_TOKEN：{'已設定' if BOT_TOKEN else '缺少'}")
    print("支援站點：X/Twitter、Pixiv、e-hentai")
    print(f"其他連結回覆提示：{'是' if REPLY_UNSUPPORTED_LINKS else '否'}")
    print("登入憑證：")
    for line in credential_lines(plain=True):
        print(f"  {line}")

    if "--live" in sys.argv:
        print()
        print("線上驗證（會對站點發出一次真實請求）：")
        for label, ok, detail in verify_credentials_live():
            mark = "[OK]" if ok else ("[!!]" if ok is False else "[--]")
            print(f"  {mark} {label}：{detail}")

    try:
        print(f"gallery-dl：{find_executable()}")
    except RuntimeError as exc:
        print(f"gallery-dl：不可用（{exc}）")

    try:
        import PIL  # noqa: F401
        print("Pillow：已安裝")
    except ImportError:
        print("Pillow：未安裝（超過 10MB 的圖片會以檔案傳送）")

    return 0


def telegram_server_root() -> str:
    """
    Scheme and host of the Bot API server, with **no** trailing path.

    This is the raw form, for code that builds a full URL itself. Do not hand it to
    ``Application.builder().base_url()`` - see ``telegram_base_url`` for why.
    """
    return TELEGRAM_API_BASE.rstrip("/")


def telegram_base_url() -> str:
    """
    The value ``base_url()`` needs: the server root **plus ``/bot``**.

    The trailing ``/bot`` is not decoration and is easy to get wrong. python-telegram-bot
    parses this with:

        def _parse_base_url(value, token):
            ...
            return value + token

    It **appends the token** rather than inserting it into a ``{token}`` placeholder, so
    the value must already end with ``/bot``. The library's own default is
    ``"https://api.telegram.org/bot"``, which is the hint.

    Passing the bare root produced exactly this, and it is worth recording because the
    message points nowhere near the cause:

        InvalidURL: Invalid port: '80818693732536:AAEDcYYcEbQ08xurHFD_jFzGV311JsivHag'

    The token was concatenated onto the port, so httpx read it as part of the port
    number and the bot died during bootstrap.
    """
    root = telegram_server_root()
    return f"{root}/bot" if root else ""


def telegram_api_url(method: str) -> str:
    """
    Absolute URL for one Bot API method, honouring a self-hosted server.

    A self-hosted server serves the bot's methods under ``/bot<token>/<method>``
    exactly as the public API does, so only the host part changes.
    """
    root = telegram_server_root() or "https://api.telegram.org"
    return f"{root}/bot{BOT_TOKEN}/{method}"


def probe_telegram_limits() -> dict:
    """
    Ask the active Bot API server what it allows, and record the answer.

    This is a probe rather than an assumption. A self-hosted server raises the upload
    ceiling from 50 MB to 2000 MB, but **only when it was started with ``--local`` does
    it accept a local filesystem path instead of file bytes** - without that flag it
    still requires an upload, so announcing path support would break every send.

    The public API has no endpoint describing itself, so its known limits stay the
    defaults. Returns a dict describing what was found, for the startup log and
    ``/status``.
    """
    global LOCAL_API_SERVER, SERVER_UPLOAD_LIMIT_BYTES, SERVER_CAN_UPLOAD_BY_PATH

    info = {
        "base": TELEGRAM_API_BASE or "https://api.telegram.org",
        "local": False,
        "can_upload_by_path": False,
        "upload_limit_bytes": SERVER_UPLOAD_LIMIT_BYTES,
        "detail": "使用公開 API（未設定 TELEGRAM_API_BASE）",
    }
    if not TELEGRAM_API_BASE:
        return info


    try:
        # The proxy is set explicitly even for the self-hosted server, because the
        # same call is used against api.telegram.org when no local server is configured
        # - and there it needs one. Leaving it out made this probe fail, which was
        # reported as "using the public API" and silently dropped the upload ceiling
        # from 2000 MB back to 50 MB.
        with httpx.Client(**ehentai.http_client_kwargs(10.0)) as client:
            response = client.get(telegram_api_url("getMe"))
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        info["detail"] = f"無法連線自架伺服器：{type(exc).__name__}: {exc}"
        logger.warning("自架 Bot API 伺服器無法使用：%s", info["detail"])
        return info

    if not payload.get("ok"):
        info["detail"] = f"自架伺服器回應錯誤：{payload.get('description')}"
        logger.warning("%s", info["detail"])
        return info

    LOCAL_API_SERVER = True
    info["local"] = True
    # The 2000 MB ceiling comes with running your own server at all. Uploading **by
    # local path** is the narrower feature, and only ``--local`` provides it - the
    # server does not report which mode it is in, so the operator states it rather than
    # being guessed at. Assuming it were on would break every file send.
    SERVER_UPLOAD_LIMIT_BYTES = 2000 * 1024 * 1024
    SERVER_CAN_UPLOAD_BY_PATH = TELEGRAM_LOCAL_FLAG
    info["can_upload_by_path"] = SERVER_CAN_UPLOAD_BY_PATH
    info["upload_limit_bytes"] = SERVER_UPLOAD_LIMIT_BYTES
    bot_username = (payload.get("result") or {}).get("username", "?")
    info["detail"] = (
        f"已連接自架伺服器（@{bot_username}，"
        f"本機路徑上傳={'是' if TELEGRAM_LOCAL_FLAG else '否'}，"
        f"上傳上限={SERVER_UPLOAD_LIMIT_BYTES // (1024 * 1024)}MB）"
    )
    logger.info("%s", info["detail"])
    return info


def upload_limit_bytes() -> int:
    """
    The largest file that may be sent, honouring a self-hosted server.

    Kept as a function rather than the constant because the answer depends on the
    server actually in use, which is only known after the probe.
    """
    return max(SERVER_UPLOAD_LIMIT_BYTES, MAX_UPLOAD_BYTES)


def can_upload_by_path() -> bool:
    """Whether the server accepts a local path instead of uploading the bytes."""
    return LOCAL_API_SERVER and SERVER_CAN_UPLOAD_BY_PATH


def main():
    if len(sys.argv) > 1 and sys.argv[1] in {"--check", "--selfcheck"}:
        raise SystemExit(run_self_check())

    # One-off helper for a messy part of the Pixiv setup. gallery-dl's prompt
    # tells the user to copy the `code` query parameter of the 'callback?state='
    # network entry, but the URL that appears first in a browser session is
    # 'accounts.pixiv.net/post-redirect?...return_to=.../start?code_challenge=...',
    # which carries no code at all. Accept either the callback URL or a bare code,
    # and name the wrong-URL case explicitly instead of guessing.
    if len(sys.argv) > 1 and sys.argv[1] == "--pixiv-token":
        enable_utf8_console()
        if len(sys.argv) < 3:
            raise SystemExit("用法：bot.py --pixiv-token <code 或完整回呼網址>")
        # Extract first, then validate: a callback URL is only "wrong" if no code
        # can be pulled out of it, so validating the raw argument would reject
        # the very input gallery-dl tells users to paste.
        code = extract_oauth_code(sys.argv[2])
        problem = pixiv_token_problem(code)
        if problem:
            raise SystemExit(f"這個值不能用：{problem}")
        print("提取到的 code：")
        print(code)
        print()
        print("下一步：把上面的值填入 .env 的 PIXIV_REFRESH_TOKEN，"
              "再執行 bot.py --check 驗證。")
        return

    if not BOT_TOKEN:
        raise SystemExit("缺少 BOT_TOKEN，請複製 .env.example 為 .env 並填寫 Token。")

    # A replacement process waits before polling, so the process it is replacing has
    # time to release Telegram's getUpdates lock. Only /restart sets this variable,
    # so a normal start has no delay.
    startup_delay = 0.0
    try:
        startup_delay = float(os.getenv("RESTART_STARTUP_DELAY", "0") or 0)
    except ValueError:
        logger.warning("RESTART_STARTUP_DELAY 不是數字，將忽略。")
    if startup_delay > 0:
        logger.info("等待 %.1f 秒讓前一個程序結束後再開始輪詢……", startup_delay)
        time.sleep(startup_delay)

    # Ask the active server what it allows before building anything that depends on
    # the answer. Failure is not fatal: the public limits stay in force.
    api_info = probe_telegram_limits()
    if api_info.get("local"):
        logger.info("Bot API 來源：%s", api_info.get("detail"))
    else:
        logger.warning("Bot API 來源：%s", api_info.get("detail"))

    builder = Application.builder().token(BOT_TOKEN)
    if telegram_base_url():
        # Points every request at the self-hosted server. api.telegram.org stays the
        # default when TELEGRAM_API_BASE is unset.
        builder = builder.base_url(telegram_base_url())

    # Timeouts are raised well above PTB's defaults, which are 5.0s for read, write and
    # connect, 1.0s for pool, and 20.0s for a media write.
    #
    # THIS IS THE FIX FOR A REPORTED DUPLICATION BUG. With a self-hosted server the
    # upload does not finish when the HTTP request does: the server takes the file
    # straight from disk and pushes it to Telegram itself, over MTProto - which on this
    # network is relayed through a SOCKS5 proxy. A 5.5 MB album therefore needs several
    # seconds of work upstream *after* the request body has been written, so the 5s
    # read timeout fired, the send looked failed, and the fallback delivered the same
    # artwork again.
    #
    # media_write_timeout is set separately and matters: PTB applies it to the upload
    # itself and its default is 20s, which is already longer than read_timeout but still
    # short for a large archive over a throttled relay.
    #
    # Raising the ceiling does not slow anything down: a timeout is an upper bound, and a
    # normal send returns as soon as it is done.
    request_timeout = max(1.0, float(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "180") or 180))
    request_kwargs = dict(
        read_timeout=request_timeout,
        write_timeout=request_timeout,
        connect_timeout=min(request_timeout, 30.0),
        pool_timeout=30.0,
        media_write_timeout=request_timeout,
    )
    builder = builder.get_updates_request(
        HTTPXRequest(**request_kwargs)
    ).request(
        HTTPXRequest(**request_kwargs)
    )

    application = (
        builder
        .post_init(post_init)
        .build()
    )

    # Registered first, so no handler's failure can go unreported.
    application.add_error_handler(on_error)

    # --- Visitor commands ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    # /id is public: a visitor needs their own ID for an admin to whitelist them.
    application.add_handler(CommandHandler("id", id_command))

    # --- Administrator commands ---
    # Registered so they work, wrapped so only listed admins can run them, and
    # absent from the published menu above.
    application.add_handler(
        CommandHandler("cookies", admin_only(cookies_command))
    )
    application.add_handler(
        CommandHandler("status", admin_only(status_command))
    )
    # The parse cache is operator-visible because "why did it not download again"
    # and "why is it sending the old files" are both diagnosed here.
    application.add_handler(
        CommandHandler("cache", admin_only(cache_command))
    )
    # A restart is the only way to apply a .env or credential change without host
    # access, and it briefly takes the bot offline, so it is administrator only.
    application.add_handler(
        CommandHandler("restart", admin_only(restart_command))
    )

    # --- Authorization management ---
    # Authorized users may change the list; only administrators may create another
    # administrator, which is why /promote and /demote use admin_only instead.
    application.add_handler(
        CommandHandler("authorized", list_manager_only(authorized_command))
    )
    application.add_handler(
        CommandHandler("grant", list_manager_only(grant_command))
    )
    application.add_handler(
        CommandHandler("revoke", list_manager_only(revoke_command))
    )
    application.add_handler(
        CommandHandler("promote", admin_only(promote_command))
    )
    application.add_handler(
        CommandHandler("demote", admin_only(demote_command))
    )
    application.add_handler(
        CommandHandler("lock", admin_only(lock_command))
    )
    # Separate from /lock: that one governs private chats and the user list, and said
    # nothing about groups, so a bot that looked locked in private was still usable
    # from any group.
    application.add_handler(
        CommandHandler("lockgroup", admin_only(lock_group_command))
    )

    # --- Gallery action buttons ---
    # Registered before the message handler so a button press is never treated as
    # ordinary text.
    application.add_handler(
        CallbackQueryHandler(handle_gallery_callback, pattern=r"^eh\|")
    )

    # Captions matter: in a group the link is often attached to a photo.
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_message,
        )
    )

    # A handler referencing a misspelled callable or an unknown filter only fails
    # when Telegram first delivers an update, so --dry-run builds the exact same
    # application and stops before polling starts.
    if "--dry-run" in sys.argv:
        log_startup_configuration()
        logger.info("構建成功，已註冊 %s 個處理器（--dry-run，不連線 Telegram）。",
                    len(application.handlers[0]))
        return

    log_startup_configuration()
    logger.info("機器人已啟動，等待 Telegram 訊息。")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
