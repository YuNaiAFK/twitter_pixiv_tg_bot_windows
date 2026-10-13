"""
Characterization tests: freeze the bot's behaviour before refactoring it.

Why this file exists
--------------------
The original suite (733 checks) was destroyed by a careless cleanup, and there was no
history to recover it from. Refactoring 4,600 lines without a net is how that becomes a
functional regression instead of just a lost file, so this rebuilds a net first.

It is deliberately a **characterization** suite: it asserts what the code does *today*,
for the parts that matter, so a refactor that changes behaviour fails loudly. It is not
an attempt to reproduce the old suite check for check.

Structure
---------
* Public surface: every configuration constant, every command handler, and the exact
  signature of the functions other code depends on. These are the things a "cleanup"
  most easily drops by accident.
* Behaviour: pure functions and small units, with inputs and expected outputs.
* Danger paths: the specific bugs already fixed in this project, so they cannot come
  back. Every one of these was a real defect that reached the user.
"""

from __future__ import annotations

import ast
import inspect
import os
import shutil
import sys
import tempfile
import time as _time
from pathlib import Path

import httpx as _httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "vendor"))
sys.path.insert(0, str(HERE))

import bot
import ehentai
import tag_translations

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition, detail: str = "") -> bool:
    ok = bool(condition)
    (PASSED if ok else FAILED).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}"
          f"{(' -> ' + str(detail)) if detail and not ok else ''}")
    return ok


print("=== 1. Public surface: configuration constants ===")
# A refactor that drops a constant used only from a rarely-taken branch is a latent
# crash, so the whole set is pinned.
EXPECTED_CONSTANTS = [
    "ADMIN_USER_IDS", "ALBUM_LIMIT", "ALLOWED_USER_IDS", "ANIMATION_EXTENSIONS",
    "ARCHIVE_PARALLEL_MIN_BYTES", "ATTACH_SOURCE_LINK", "AUTHORIZATION", "AUTH_FILE",
    "BASE_DIR", "BOT_TOKEN", "CACHE_DIR", "CACHE_FILE", "CACHE_MAX_ENTRIES",
    "CACHE_STALE_SECONDS", "CAPTION_LIMIT", "CHECK_COOKIES", "COOKIES_FILE",
    "DELETE_SOURCE_MESSAGE", "DELETE_STATUS_MESSAGE", "EHENTAI_ARCHIVE_WORKERS",
    "EHENTAI_COOKIES_FILE", "EHENTAI_DOWNLOAD_TIMEOUT_SECONDS",
    "EHENTAI_DOWNLOAD_WORKERS", "EHENTAI_NAMESPACE_LABELS", "EHENTAI_TIMEOUT_SECONDS",
    "EHENTAI_VIEWER_WORKERS", "EXPIRY_WARN_DAYS", "GALLERY_DL_CACHE_DIR",
    "GALLERY_DL_CONFIG", "GALLERY_URL_TTL_SECONDS", "GROUP_LOCKED_DEFAULT",
    "IMAGE_EXTENSIONS", "LOCAL_API_SERVER", "LOCKED_DEFAULT",
    "MAX_CONCURRENT_DOWNLOADS", "MAX_DOWNLOAD_SECONDS", "MAX_FILES",
    "MAX_UPLOAD_BYTES", "MAX_UPLOAD_MB", "MEDIA_EXTENSIONS", "PARSE_CACHE",
    "PHOTO_DOWNSCALE_MAX_EDGE", "PHOTO_DOWNSCALE_QUALITY", "PHOTO_LIMIT_BYTES",
    "PIXIV_COOKIES_FILE", "PIXIV_REFRESH_TOKEN", "REPLY_UNSUPPORTED_LINKS",
    "REQUEST_STALE_SECONDS", "RESTART_LOG_MAX_BYTES", "RESTART_STARTUP_DELAY_SECONDS",
    "SERVER_CAN_UPLOAD_BY_PATH", "SERVER_UPLOAD_LIMIT_BYTES", "SOURCE_LABEL",
    "SUPPORTED_SITES_NOTICE", "TELEGRAM_API_BASE", "TELEGRAM_LOCAL_FLAG",
    "TELEGRAPH_ALLOW_ADULT", "TELEGRAPH_AUTHOR", "TWITTER_COOKIES_FILE", "URL_RE",
    "USER_COOLDOWN_SECONDS", "VIDEO_EXTENSIONS", "WORK_DIR",
]
_missing = [c for c in EXPECTED_CONSTANTS if not hasattr(bot, c)]
check(f"all {len(EXPECTED_CONSTANTS)} configuration constants exist",
      not _missing, f"missing: {_missing}")

_SOURCE = (HERE / "bot.py").read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_TOP_LEVEL = {
    n.name for n in _TREE.body
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
}
check("bot.py has a substantial top-level surface",
      len(_TOP_LEVEL) >= 100, f"only {len(_TOP_LEVEL)} definitions")

print()
print("=== 2. Public surface: commands ===")
# Every command that existed must still be registered. A dropped CommandHandler is
# invisible until someone tries the command.
EXPECTED_COMMANDS = {
    "authorized", "cache", "cookies", "demote", "grant", "help", "id", "lock",
    "lockgroup", "promote", "restart", "revoke", "start", "status",
}
_found = set()
for _node in ast.walk(_TREE):
    if isinstance(_node, ast.Call) and getattr(_node.func, "id", "") == "CommandHandler":
        if _node.args and isinstance(_node.args[0], ast.Constant):
            _found.add(_node.args[0].value)
check("every command handler is still registered",
      EXPECTED_COMMANDS <= _found, f"missing: {sorted(EXPECTED_COMMANDS - _found)}")
check("no unexpected command was added or renamed",
      _found <= EXPECTED_COMMANDS, f"extra: {sorted(_found - EXPECTED_COMMANDS)}")

print()
print("=== 3. Public surface: signatures other code depends on ===")
# Names and parameter lists, because a refactor that renames a parameter breaks callers
# that pass it by keyword.
EXPECTED_SIGNATURES = {
    "extract_urls": ["text"],
    "detect_media_platform": ["url"],
    "build_caption": ["source_url", "extra"],
    "normalize_cache_key": ["url"],
    "progress_bar": ["done", "total", "width"],
    "sanitize_filename": ["name", "fallback"],
    "build_source_keyboard": ["source_url"],
    "bot_was_mentioned": ["update", "context"],
    "commands_hint": [],
}
for _name, _params in EXPECTED_SIGNATURES.items():
    _fn = getattr(bot, _name, None)
    if _fn is None:
        check(f"{_name} exists", False, "missing")
        continue
    _actual = list(inspect.signature(_fn).parameters)
    # Only compare positionally-required names; optional extras are allowed but not
    # silently renamed.
    check(f"{_name} keeps its parameter names",
          _actual[:len(_params)] == _params, f"{_actual}")

print()
print("=== 4. Behaviour: link routing ===")
_urls = bot.extract_urls(
    "look https://www.pixiv.net/artworks/1 and https://x.com/a/status/2 thanks")
check("extract_urls finds both links in order",
      _urls == ["https://www.pixiv.net/artworks/1", "https://x.com/a/status/2"],
      str(_urls))
check("extract_urls ignores text without links",
      bot.extract_urls("i send no link") == [])
check("pixiv is recognised",
      bot.detect_media_platform("https://www.pixiv.net/artworks/1") == "pixiv")
check("x.com is recognised as twitter",
      bot.detect_media_platform("https://x.com/a/status/2") == "twitter")
check("twitter.com is recognised as twitter",
      bot.detect_media_platform("https://twitter.com/a/status/2") == "twitter")
check("an unrelated site is not a media platform",
      bot.detect_media_platform("https://example.com/x") is None)
check("an e-hentai gallery is recognised by url shape",
      ehentai.is_gallery_url("https://e-hentai.org/g/3672745/82d9d6d00e/"))
check("an e-hentai non-gallery is not",
      not ehentai.is_gallery_url("https://e-hentai.org/"))
check("an invite link is classified as invite",
      bot.classify_telegram_link("https://t.me/joinchat/abc") == "invite")

print()
print("=== 5. Behaviour: captions and keyboards ===")
_cap = bot.build_caption("https://x.com/a/status/2")
check("the caption carries a source label", "原始連結" in _cap, _cap[:60])
check("the caption carries the url", "x.com/a/status/2" in _cap, _cap[:60])
_cap2 = bot.build_caption("https://x.com/a/status/2", "extra note")
check("an extra note appears in the caption", "extra note" in _cap2, _cap2[:80])
_kb = bot.build_source_keyboard("https://www.pixiv.net/artworks/147895535")
check("a source keyboard is built", _kb is not None)
check("the keyboard is a single url button",
      _kb is not None and len(_kb.inline_keyboard) == 1
      and len(_kb.inline_keyboard[0]) == 1)
check("the button points at the source",
      _kb.inline_keyboard[0][0].url == "https://www.pixiv.net/artworks/147895535",
      str(_kb.inline_keyboard[0][0].url))
check("the button label fits Telegram's 64-char limit",
      len(_kb.inline_keyboard[0][0].text) <= 64,
      str(len(_kb.inline_keyboard[0][0].text)))
check("a non-http source yields no keyboard",
      bot.build_source_keyboard("not a url") is None)

print()
print("=== 6. Behaviour: formatting helpers ===")
check("progress renders 0%", bot.progress_bar(0, 10) == "░░░░░░░░░░ 0% (0/10)",
      bot.progress_bar(0, 10))
check("progress renders 50%", bot.progress_bar(5, 10) == "█████░░░░░ 50% (5/10)",
      bot.progress_bar(5, 10))
check("progress renders 100%",
      bot.progress_bar(10, 10) == "██████████ 100% (10/10)",
      bot.progress_bar(10, 10))
check("progress falls back to a page count when the total is unknown",
      bot.progress_bar(0, 0) == "0 頁" and bot.progress_bar(5, 0) == "5 頁",
      f"{bot.progress_bar(0, 0)!r} / {bot.progress_bar(5, 0)!r}")
check("progress clamps a done value above the total",
      bot.progress_bar(20, 10) == bot.progress_bar(10, 10),
      bot.progress_bar(20, 10))
check("progress clamps a negative done value",
      bot.progress_bar(-5, 10) == bot.progress_bar(0, 10),
      bot.progress_bar(-5, 10))
check("filesize is formatted in MiB",
      ehentai.format_filesize(155415196) == "148.2 MiB",
      ehentai.format_filesize(155415196))

print()
print("=== 7. Behaviour: filename safety ===")
_san = bot.sanitize_filename
check("windows-hostile characters are replaced",
      _san('a:b?c|d*e"f<g>h.zip') == "a_b_c_d_e_f_g_h.zip", _san('a:b?c|d*e"f<g>h.zip'))
check("path separators cannot escape the directory",
      "/" not in _san("../../etc/passwd.zip") and "\\" not in _san("..\\..\\x.zip"))
check("a reserved device name is avoided", _san("CON.zip").startswith("_"), _san("CON.zip"))
check("an empty name falls back", _san("   ") == "archive.zip", _san("   "))
check("a real e-hentai filename survives",
      _san("(C106) [MochiPeach] 猫耳 [中国翻訳]-1280x.zip")
      == "(C106) [MochiPeach] 猫耳 [中国翻訳]-1280x.zip")
check("an overlong name is shortened", len(_san("x" * 400 + ".zip")) <= 180,
      str(len(_san("x" * 400 + ".zip"))))

print()
print("=== 8. Behaviour: cache keys ===")
check("cache keys are lowercased and host-normalised",
      bot.normalize_cache_key("https://X.com/a/status/2?utm=1")
      == "https://x.com/a/status/2", bot.normalize_cache_key("https://X.com/a/status/2?utm=1"))
check("the same url normalises consistently",
      bot.normalize_cache_key("https://x.com/a/status/2")
      == bot.normalize_cache_key("https://x.com/a/status/2/"))

print()
print("=== 9. Behaviour: authorization semantics ===")
A = bot.Authorization
_priv = A(allowed=set(), admins={1000}, locked=True, group_locked=True)
check("an admin passes everywhere", _priv.is_allowed(1000, 999, "private"))
check("a stranger is refused in private when locked",
      not _priv.is_allowed(999, 999, "private"))
check("a stranger is refused in a group when group_locked",
      not _priv.is_allowed(999, -1009999, "supergroup"))
_open = A(allowed=set(), admins={1000}, locked=True, group_locked=False)
check("with groups open, a stranger may use a group",
      _open.is_allowed(999, -1009999, "supergroup"))
_listed = A(allowed=set(), admins={1000}, groups={-1001234}, group_locked=False)
check("a listed group passes", _listed.is_allowed(999, -1001234, "supergroup"))
check("an unlisted group is refused once a list exists",
      not _listed.is_allowed(999, -1009999, "supergroup"))
check("an authorized user passes in private",
      A(allowed={999}, admins={1000}, locked=True).is_allowed(999, 999, "private"))
check("the group mode label reports gated",
      "守門" in A(group_locked=True).group_mode_label)
check("the mode label reports gated", "守門" in A(locked=True).mode_label)

print()
print("=== 10. Danger paths: bugs that already reached the user ===")
# (a) An album item must be an uploaded file, never a path string. Telegram reads a str
#     as a URL, and "file:///C:/..." produced:
#       BadRequest: Invalid file http url specified: unsupported url protocol
#     which silently degraded every artwork to one message per page.
_album_src = inspect.getsource(bot.send_media_album)
check("album items are opened as files, not passed as path strings",
      "file_path.open(\"rb\")" in _album_src and "media=str(file_path)" not in _album_src,
      _album_src[:300])
check("album streams are closed in a finally",
      "handle.close()" in _album_src and "finally:" in _album_src)
check("a refused caption or button still retries the album",
      "改為純圖片重試" in _album_src,
      "a refused reply_markup or caption would lose the whole album")
check("the retry strips captions rather than reopening the files",
      "_strip_captions" in _album_src and "item.media" in inspect.getsource(
          bot._strip_captions),
      "reopening would leak the original handles and can fail on Windows")
# Layout, requested explicitly: the source link is the FIRST item's caption, so it
# reads above the images, with a url button alongside so it stays reachable.
check("the link is the first album item's caption",
      "caption if index == 0 else None" in _album_src, _album_src[:400])
check("later albums do not repeat the link",
      "with_caption" in inspect.signature(bot.send_media_album).parameters,
      "a 30-page work would carry the same link on every album")
check("a single send also carries the link as a caption",
      "caption" in inspect.signature(bot.send_single_media).parameters,
      "a single page would show no link above its image")
check("single-send callers actually pass the caption",
      "caption=None if link_carried else caption"
      in inspect.getsource(bot.send_original_files),
      "the parameter would exist but never be used")
check("no separate link message is posted when files were sent",
      "if attach_link and not link_carried and sent == 0" in inspect.getsource(
          bot.send_original_files),
      "the link would appear twice, in two messages")

# Requested: only X/Twitter carries the source link; Pixiv shows the images alone.
_attach = inspect.signature(bot.send_original_files).parameters
check("attach_link is a per-call option, not a global",
      "attach_link" in _attach,
      "the two platforms could not be configured differently")
_pixiv_src = inspect.getsource(bot.handle_message)
check("the Pixiv branch sends no link",
      "attach_link=False" in _pixiv_src, _pixiv_src[-600:])
check("the X/Twitter branch sends the link",
      "attach_link=True" in _pixiv_src, _pixiv_src[-600:])
# The cached replay must follow the same split, or a repeat of a cached Pixiv link
# would grow a link the original send did not have.
_cache_src = inspect.getsource(bot.send_cached_media)
check("the cached replay splits by platform too",
      'attach_link=(platform != "pixiv")' in _cache_src, _cache_src[:400])
check("attaching nothing suppresses the button as well",
      "link_carried = not attach_link" in inspect.getsource(
          bot.send_original_files),
      "a link-less send would still carry a source button")

# Reported as redundant: the link showed twice, as caption text and as a url button
# pointing at the same address. Only the caption remains.
_orig_src = inspect.getsource(bot.send_original_files)
check("no keyboard is attached to any media message",
      "keyboard=None," in _orig_src and "build_source_keyboard" not in _orig_src,
      "the button would duplicate the caption link")
check("the source-link keyboard has no caller",
      _SOURCE.count("build_source_keyboard(") == 1,
      "a caller was reattached; that duplicates the caption link unless the caption "
      "is removed at the same time")
check("the unused keyboard is documented as intentionally unused",
      "currently unused" in inspect.getsource(bot.build_source_keyboard),
      "a future reader would treat it as an oversight")
check("the keyboard still builds correctly if reattached",
      bot.build_source_keyboard("https://x.com/a/status/2") is not None and
      bot.build_source_keyboard("nope") is None)


def _local_server_urls(monkeypatch_base: str):
    """Evaluate the two URL helpers as if TELEGRAM_API_BASE were the given value."""
    saved = bot.TELEGRAM_API_BASE
    try:
        bot.TELEGRAM_API_BASE = monkeypatch_base
        return bot.telegram_server_root(), bot.telegram_base_url(), \
            bot.telegram_api_url("getMe")
    finally:
        bot.TELEGRAM_API_BASE = saved


# Reported from a real boot: passing the bare server root to PTB produced
#   InvalidURL: Invalid port: '80818693732536:AAEDcYYcEbQ08xurHFD_jFzGV311JsivHag'
# because python-telegram-bot parses base_url with `return value + token` - it APPENDS
# the token, so the value must already end with "/bot". The token was concatenated onto
# the port, so httpx read it as part of the port number.
_root, _base, _api = _local_server_urls("http://127.0.0.1:8081")
check("base_url ends with /bot, which PTB requires",
      _base == "http://127.0.0.1:8081/bot",
      f"PTB appends the token, so this must end with /bot; got {_base!r}")
check("the raw root carries no /bot of its own",
      _root == "http://127.0.0.1:8081",
      "a caller building its own URL would then double the /bot segment")
check("the method URL has exactly one /bot segment",
      _api == f"http://127.0.0.1:8081/bot{bot.BOT_TOKEN}/getMe",
      _api.replace(bot.BOT_TOKEN, "<TOKEN>"))
check("a trailing slash in the setting cannot break the /bot suffix",
      _local_server_urls("http://127.0.0.1:8081/")[1] == "http://127.0.0.1:8081/bot",
      "a trailing slash would give //bot and a wrong path")
check("no local server configured still means the public API",
      _local_server_urls("")[1] == "",
      "base_url() must stay falsy so main() does not call base_url('')")

# Pinned against the installed library: PTB parses base_url with `value + token`, so a
# future release that switches to a {token} placeholder would make the "/bot" suffix
# wrong. This fails loudly at that point instead of at the next boot.
from telegram import Bot as _Bot  # noqa: E402

check("PTB really does append the token to base_url",
      _Bot(bot.BOT_TOKEN, base_url="http://127.0.0.1:9999/bot").base_url
      == f"http://127.0.0.1:9999/bot{bot.BOT_TOKEN}",
      "if this fails, PTB changed how base_url is parsed and the /bot suffix must change "
      "with it")


# A single ConnectTimeout on the archiver POST used to escape to the generic handler and
# surface as "處理時發生錯誤，請稍後再試" with a full traceback. e-hentai is throttled on
# this network, so a transient failure is expected rather than exceptional.
_arch_src = inspect.getsource(bot.resolve_official_archive)
check("the archiver POST is retried",
      'request_with_retry(client, "POST"' in _arch_src,
      "a transient connection failure would fail the whole request")
check("the archive probe is retried too",
      'request_with_retry(\n            client, "GET", download_url' in _arch_src,
      "the ranged probe is as exposed to the throttling as the POST")
# The throttle hits the gallery page as well, and that is the FIRST request of every
# gallery action - retrying only the archiver would still fail the common path.
check("the gallery fetch is retried too",
      'request_with_retry(client, "GET", url'
      in inspect.getsource(ehentai.fetch_gallery),
      "the throttle would fail the first request of every gallery action")
check("there is exactly one retry implementation",
      not hasattr(bot, "_archiver_request")
      and hasattr(ehentai, "request_with_retry"),
      "two copies of the retry policy would drift apart")
# Retrying harder made things worse in practice: five attempts with doubling backoff
# reached roughly 120 requests against e-hentai during testing, after which the host
# began refusing gallery paths entirely. The count is pinned low deliberately.
_retry_params = inspect.signature(ehentai.request_with_retry).parameters
check("e-hentai retries stay light",
      2 <= _retry_params["attempts"].default <= 3,
      f"attempts={_retry_params['attempts'].default}; heavy retrying worsened the block")
check("e-hentai retries use a fixed delay, not doubling backoff",
      "delay *= 2" not in inspect.getsource(ehentai.request_with_retry),
      "doubling backoff multiplied load on an already-unhappy host")

# The cookie reader sent EVERY cookie in the file to e-hentai, including the .x.com and
# .pixiv.net entries a multi-site cookie export carries - another site's session cookies
# handed to e-hentai. Measured before the fix: 1707 chars, 8 foreign cookies.
_cookie_work = Path(tempfile.mkdtemp(prefix="dsh-cookies-"))
try:
    _cf = _cookie_work / "mixed.txt"
    _cf.write_text(
        "# Netscape HTTP Cookie File\n"
        ".e-hentai.org\tTRUE\t/\tTRUE\t0\tipb_member_id\t12345\n"
        ".e-hentai.org\tTRUE\t/\tTRUE\t0\tipb_pass_hash\tabcdef\n"
        ".exhentai.org\tTRUE\t/\tTRUE\t0\tigneous\txyz\n"
        ".x.com\tTRUE\t/\tTRUE\t0\tauth_token\tSECRET_TWITTER\n"
        ".pixiv.net\tTRUE\t/\tTRUE\t0\tPHPSESSID\tSECRET_PIXIV\n"
        "note-this-is-not-a-cookie-line\n",
        encoding="utf-8")
    _hdr = ehentai.cookies_header(_cf)
    check("e-hentai cookies are sent",
          "ipb_member_id=12345" in _hdr and "igneous=xyz" in _hdr,
          _hdr[:80])
    check("another site's cookies are NOT sent to e-hentai",
          "SECRET_TWITTER" not in _hdr and "SECRET_PIXIV" not in _hdr,
          f"leaked: {_hdr[:120]}")
    check("a malformed line is skipped rather than crashing",
          isinstance(_hdr, str) and "note-this-is-not-a-cookie-line" not in _hdr)
    check("the allowed domains are declared once",
          ehentai.COOKIE_DOMAINS == ("e-hentai.org", "exhentai.org", "hath.network"),
          ehentai.COOKIE_DOMAINS)
    # A leading-dot domain must match its own subdomains, or forums.exhentai.org cookies
    # would be dropped.
    _sub = _cookie_work / "sub.txt"
    _sub.write_text(".forums.e-hentai.org\tTRUE\t/\tTRUE\t0\tf\t1\n"
                    "e-hentai.org\tTRUE\t/\tTRUE\t0\thostonly\t2\n", encoding="utf-8")
    _shdr = ehentai.cookies_header(_sub)
    check("subdomain and host-only cookies are both kept",
          "f=1" in _shdr and "hostonly=2" in _shdr, _shdr)
finally:
    shutil.rmtree(_cookie_work, ignore_errors=True)
    # The memo cache is keyed on path, so a removed temp file cannot poison later reads.
    ehentai._cookie_header_cache.clear()
# e-hentai polices gallery-page access hard, so an archive click must not spend a /g/
# request: the gid and token it needs are already in the cached gallery. Measured: with a
# warm cache the only request is POST archiver.php?gid=..&token=.., and fetch_gallery is
# never called.
check("an archive click does not re-fetch the gallery page",
      "resolve_official_archive" in inspect.getsource(bot.run_gallery_archive)
      and "fetch_gallery" not in inspect.getsource(bot.run_gallery_archive),
      "a /g/ request per archive click is exactly what e-hentai polices")
check("the button handler reuses the cached gallery",
      "PARSE_CACHE.lookup(gallery_url" in inspect.getsource(bot.run_gallery_action)
      and "if gallery is None" in inspect.getsource(bot.run_gallery_action),
      "the gallery facts must come from cache when present, not a fresh fetch")

# The operator hint used to be a fixed per-platform guess: "Pixiv needs a refresh-token"
# for ANY empty result, including a deleted artwork. gallery-dl had already said which it
# was and that answer was discarded with the captured output - so an operator was sent to
# redo an OAuth flow that was never the problem.
for _text, _expected in (
        ("[pixiv][warning] 150822697: Deleted", "deleted"),
        ("[twitter][error] 404 Not Found", "missing"),
        ("[pixiv][error] AuthenticationError: refresh-token required", "auth"),
        ("[twitter][error] rate limit exceeded 429", "rate_limit"),
        ("[pixiv][error] this work is private", "private"),
        ("[twitter][error] ConnectTimeoutError: timed out", "network"),
        ("nothing recognisable here", ""),
):
    check(f"gallery-dl output classifies as {_expected or 'unknown'}",
          bot.classify_download_output(_text) == _expected,
          f"{_text[:44]!r} -> {bot.classify_download_output(_text)!r}")

# A sticky reason would mislabel the NEXT unrelated failure as the previous one's cause.
_prev_reason = bot.LAST_DOWNLOAD_REASON
try:
    bot.LAST_DOWNLOAD_REASON = "deleted"
    bot.begin_download_diagnosis()
    check("each download starts with no recorded reason",
          bot.LAST_DOWNLOAD_REASON == "",
          "a stale reason would be reported as this run's cause")
    bot.record_download_output("[pixiv][warning] 1: Deleted")
    check("a recognised reason is recorded",
          bot.LAST_DOWNLOAD_REASON == "deleted", bot.LAST_DOWNLOAD_REASON)
finally:
    bot.LAST_DOWNLOAD_REASON = _prev_reason

_saved_reason = bot.LAST_DOWNLOAD_REASON
try:
    bot.LAST_DOWNLOAD_REASON = "deleted"
    _hint_deleted = bot.admin_hint("pixiv")
    bot.LAST_DOWNLOAD_REASON = ""
    _hint_unknown = bot.admin_hint("pixiv")
finally:
    bot.LAST_DOWNLOAD_REASON = _saved_reason
check("the deleted hint does not send the operator to redo OAuth",
      "被刪除" in _hint_deleted and "oauth:pixiv" not in _hint_deleted,
      _hint_deleted[:70])
check("the unknown case still offers the credential check",
      "oauth:pixiv" in _hint_unknown,
      "with no recognised cause the credential advice remains the useful default")

# Reported: after a timeout the bot delivered the same Pixiv artwork two and three times.
# A timed-out media group has very likely been accepted - with a self-hosted server the
# upload to Telegram continues after the HTTP request ends - so retrying or degrading to
# one message per file delivers duplicates. Both call sites must treat TimedOut as final.
_album_src2 = inspect.getsource(bot.send_media_album)
check("an album timeout is not retried",
      "except TimedOut" in _album_src2
      and _album_src2.index("except TimedOut") < _album_src2.index("except TelegramError"),
      "TimedOut subclasses TelegramError, so order decides: retrying duplicates the album")
_orig_src2 = inspect.getsource(bot.send_original_files)
check("a timed-out album is not degraded to one message per file",
      "except TimedOut" in _orig_src2
      and "略過逐個傳送以免重複" in _orig_src2,
      "splitting after a timeout is what produced 2-3 copies of the same work")
check("a timed-out single send is not retried either",
      _orig_src2.count("except TimedOut") >= 2,
      "the same reasoning applies to a single file: a public bot can redeliver twice")
# The underlying cause: PTB defaults are read/write 5s, pool 1s, media write 20s, and a
# self-hosted server needs longer because it relays the upload to Telegram itself.
_main_src = inspect.getsource(bot.main)
check("the request timeouts are raised above PTB's 5s defaults",
      "media_write_timeout" in _main_src and "TELEGRAM_TIMEOUT_SECONDS" in _main_src,
      "a 5s read timeout is what fired in the first place")
_httpx_sig = inspect.signature(__import__("telegram.request",
                                          fromlist=["HTTPXRequest"]).HTTPXRequest.__init__)
check("PTB really does default those timeouts low (pinned)",
      _httpx_sig.parameters["read_timeout"].default == 5.0
      and _httpx_sig.parameters["media_write_timeout"].default == 20.0,
      "if this fails, PTB changed its defaults and the override may be unnecessary")

# A second duplication path, in the cache branch: falling through after a failed cached
# send downloads the artwork again and delivers it a second time.
_handle_src2 = inspect.getsource(bot.handle_message)
_cache_block = _handle_src2[_handle_src2.index("if cached_media is not None"):]
_cache_block = _cache_block[:_cache_block.index("logger.info(\"快取無法使用")]
check("a timed-out cached send does not fall through to re-download",
      "except TimedOut" in _cache_block
      and "return" in _cache_block.split("except TimedOut")[1]
                                .split("except TelegramError")[0],
      "falling through downloads the same work again and sends it twice")
check("the cached-send timeout handler comes before the generic one",
      _cache_block.index("except TimedOut") < _cache_block.index("except TelegramError"),
      "TimedOut subclasses TelegramError, so order decides this")

_album_src3 = inspect.getsource(bot.send_media_album)

from telegram import InputMediaDocument as _IMD2  # noqa: E402

check("album documents carry their real filename via api_kwargs",
      'item_kwargs["api_kwargs"] = {"filename": file_path.name}' in _album_src3,
      "a document album would deliver originals under a generic name")
check("PTB drops the filename named parameter (pinned)",
      "filename" not in _IMD2(media="x", filename="n.png").to_dict(),
      "if PTB starts honouring filename=, api_kwargs need not carry it")
check("api_kwargs is the route that survives serialisation",
      _IMD2(media="x", api_kwargs={"filename": "n.png"}).to_dict().get("filename") == "n.png")

# Telegram refuses an oversized file with a generic BadRequest and no distinct code, so
# the fallback matches on wording - narrowly, because treating any refusal as a size
# problem would turn a broken file into a pointless ZIP and a confusing message.
from telegram.error import BadRequest as _BadRequest  # noqa: E402

for _msg, _want, _label in (
        ("Bad Request: file is too big", True, "too big"),
        ("Bad Request: file size exceeds limit", True, "size exceeds"),
        ("Bad Request: PHOTO_INVALID_DIMENSIONS", True, "photo dimensions"),
        ("Bad Request: chat not found", False, "a missing chat is not a size problem"),
        ("Bad Request: wrong file identifier", False, "a bad file id is not size"),
        ("Bad Request: file must be non-empty", False, "an empty file is not size"),
):
    check(f"size refusal: {_label}",
          bot.is_size_refusal(_BadRequest(_msg)) is _want,
          f"{_msg!r} -> {bot.is_size_refusal(_BadRequest(_msg))}")

# The ZIP exists so nothing is re-encoded: it must carry the originals byte for byte.
_arch_work = Path(tempfile.mkdtemp(prefix="dsh-archive-"))
try:
    import hashlib as _hashlib
    import zipfile as _zipfile

    _a = _arch_work / "o_p0.png"
    _a.write_bytes(b"\x89PNG\r\n\x1a\n" + b"first" * 2000)
    _b = _arch_work / "o_p1.png"
    _b.write_bytes(b"\x89PNG\r\n\x1a\n" + b"second" * 2000)

    _arc = bot.zip_files([_a, _b], _arch_work, "pixiv_150824857.zip")
    with _zipfile.ZipFile(_arc) as _zf:
        check("the archive holds the originals byte for byte",
              all(_hashlib.sha256(_zf.read(_n)).hexdigest()
                  == _hashlib.sha256(_p.read_bytes()).hexdigest()
                  for _n, _p in (("o_p0.png", _a), ("o_p1.png", _b))),
              "a re-encoded archive would defeat its whole purpose")
        check("the archive keeps the real filenames",
              set(_zf.namelist()) == {"o_p0.png", "o_p1.png"}, _zf.namelist())
        check("the archive is not corrupt", _zf.testzip() is None)

    # A duplicate name must not silently overwrite the earlier entry.
    _dupdir = _arch_work / "sub"
    _dupdir.mkdir()
    _dup = _dupdir / "o_p0.png"
    _dup.write_bytes(b"different")
    with _zipfile.ZipFile(bot.zip_files([_a, _dup], _arch_work, "dup.zip")) as _zf:
        check("a duplicate filename does not overwrite an entry",
              len(_zf.namelist()) == 2 and len(set(_zf.namelist())) == 2,
              _zf.namelist())
finally:
    shutil.rmtree(_arch_work, ignore_errors=True)

_handle_src3 = inspect.getsource(bot.handle_message)
check("the ZIP fallback explains itself to the user",
      "無法直接傳送" in _handle_src3 and "下載後解壓縮" in _handle_src3,
      "a silent archive would look like a failure")
check("the message gives the size, so the wait makes sense",
      "total_mb" in _handle_src3,
      "the user needs to know how big it is before waiting")
check("the fallback says the originals are inside",
      "不重新編碼" in _handle_src3,
      "otherwise the user cannot tell the ZIP is lossless")
check("the fallback triggers only on a size refusal",
      "if oversize:" in _handle_src3,
      "zipping after an unrelated failure hides the real error")
check("the archive is sent as a document, not a photo",
      "prefer_original=True" in _handle_src3,
      "a photo upload would re-encode and hit the 10 MB ceiling")
check("the original is attempted first, before any ZIP",
      _handle_src3.index("await send_original_files(") < _handle_src3.index("zip_files"),
      "zipping first would defeat the request for original files")

# The refusal is HTTP 200 with a tiny body, so a status check cannot see it and the
# generic "gallery deleted" message would be actively misleading.
_REFUSAL = "Key missing, or incorrect key provided."
try:
    ehentai.parse_gallery_page(_REFUSAL, "https://e-hentai.org/g/1/abc/")
    check("the 200-with-Key-missing refusal is reported as a refusal", False,
          "it parsed as a gallery instead of raising")
except ehentai.EhentaiError as _exc:
    check("the 200-with-Key-missing refusal is reported as a refusal",
          "拒絕" in str(_exc) and "Key missing" in str(_exc),
          f"message was {str(_exc)[:70]}")
try:
    ehentai.parse_gallery_page("<html><body>not a gallery</body></html>",
                               "https://e-hentai.org/g/1/abc/")
except ehentai.EhentaiError as _exc:
    check("a short non-refusal page keeps the generic message",
          "拒絕" not in str(_exc),
          "the refusal detector misfires on unrelated short pages")

# socks5_transport.py exists to test whether a proxy escapes the e-hentai refusal. It
# does not - the exit node is refused too - so it must stay unwired. Someone finding the
# module and importing it would trade a timeout for a refusal and think it was a fix.
check("the SOCKS5 transport is deliberately not wired into the bot",
      "socks5_transport" not in Path(HERE / "bot.py").read_text(encoding="utf-8")
      and "socks5_transport" not in Path(HERE / "ehentai.py").read_text(encoding="utf-8"),
      "importing it would route e-hentai through a proxy whose exit node is also refused")
check("the unwired transport still compiles and exposes its builder",
      (HERE / "socks5_transport.py").exists()
      and hasattr(__import__("socks5_transport"), "build_socks5_http_client"),
      "it is kept as a working reference, not dead weight")


# gallery-dl does NOT read the Windows system proxy, and that made every Pixiv App API
# call time out three times over: measured 115.7s before, 1.3s after HTTPS_PROXY was set.
# Both gallery-dl call sites must pass it.
check("gallery-dl is given the system proxy",
      "system_proxy_url" in inspect.getsource(bot.gallery_dl_env)
      and "HTTPS_PROXY" in inspect.getsource(bot.gallery_dl_env),
      "gallery-dl would ignore the registry proxy and Pixiv would time out")
check("both gallery-dl call sites use it",
      inspect.getsource(bot.download_with_gallery_dl).count("gallery_dl_env(") == 1
      and inspect.getsource(bot.download_gallery_media).count("gallery_dl_env(") == 1,
      "fixing only one leaves the slow path untouched - which happened once already")
check("the streaming call site passes a real env, not a bare Popen",
      "env=gallery_dl_env(" in inspect.getsource(bot.download_gallery_media),
      "the progress-streaming Popen builds its own command and needs its own env")
_saved_proxy_env = {k: os.environ.get(k) for k in
                    ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                     "ALL_PROXY", "all_proxy")}
try:
    for _k in _saved_proxy_env:
        os.environ.pop(_k, None)
    os.environ["HTTPS_PROXY"] = "http://example.invalid:1"
    check("an explicit environment variable wins over the registry",
          bot.system_proxy_url() == "http://example.invalid:1",
          f"got {bot.system_proxy_url()!r}")
finally:
    for _k, _v in _saved_proxy_env.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v

check("system_proxy_url returns a usable URL or nothing",
      bot.system_proxy_url() == "" or "://" in bot.system_proxy_url(),
      f"got {bot.system_proxy_url()!r}")

_saved_sleep = _time.sleep
_time.sleep = lambda _s: None
try:
    _attempts = {"n": 0}

    class _AlwaysTimeouts:
        def request(self, method, url, **kwargs):
            _attempts["n"] += 1
            raise _httpx.ConnectTimeout("simulated")

    try:
        ehentai.request_with_retry(_AlwaysTimeouts(), "POST", "https://e-hentai.org/x",
                              attempts=3, data={})
        check("a persistent connection failure is retried then reported", False,
              "it returned instead of raising")
    except bot.ehentai.EhentaiError as exc:
        check("a persistent connection failure is retried then reported",
              _attempts["n"] == 3 and "重試 3 次" in str(exc),
              f"attempts={_attempts['n']}, message={str(exc)[:60]}")

    # An HTTP status is a considered answer: 404 means the archiver refused, and
    # retrying would repeat the refusal while making the user wait.
    _status_attempts = {"n": 0}

    class _Always404:
        def request(self, method, url, **kwargs):
            _status_attempts["n"] += 1
            return _httpx.Response(404, text="no",
                                   request=_httpx.Request(method, url))

    _resp = ehentai.request_with_retry(_Always404(), "POST", "https://e-hentai.org/x", data={})
    check("an HTTP status is handed back, not retried",
          _status_attempts["n"] == 1 and _resp.status_code == 404,
          f"attempts={_status_attempts['n']}")

    # A failure that clears is a slow success, which is the point of retrying at all.
    _flaky = {"n": 0}

    class _FailsOnce:
        def request(self, method, url, **kwargs):
            _flaky["n"] += 1
            if _flaky["n"] == 1:
                raise _httpx.ReadTimeout("slow")
            return _httpx.Response(200, text="ok",
                                   request=_httpx.Request(method, url))

    check("a transient failure that clears becomes a success",
          ehentai.request_with_retry(_FailsOnce(), "POST", "https://e-hentai.org/x",
                               data={}).status_code == 200
          and _flaky["n"] == 2,
          f"attempts={_flaky['n']}")
finally:
    _time.sleep = _saved_sleep
check("reply_markup is sent through api_kwargs",
      "api_kwargs" in "".join(inspect.getsource(bot._reply_media_group)),
      "PTB 22.8 has no reply_markup parameter on reply_media_group")

# (b) A message with no link must never be answered, and authorization must be checked
#     only after a link is found - otherwise a plain remark drew a rejection.
_handle_src = inspect.getsource(bot.handle_message)
_urls_pos = _handle_src.index("extract_urls(text)")
_auth_pos = _handle_src.index("user_is_allowed(")
check("a link is looked for before authorization",
      _urls_pos < _auth_pos, "authorization would answer link-less messages")
# Reported as the bot being triggered by someone else's conversation. The fix is that a
# link-less message is now answered **unconditionally never** - no mention heuristic, no
# reply-to-bot exception. Both of those were guesses about intent, and a wrong guess is
# worse than silence; /help covers the legitimate case explicitly.
check("a link-less message never draws a reply",
      "if not urls:" in _handle_src
      and "commands_hint()" not in _handle_src,
      "a mention or reply-to-bot heuristic could still answer an unrelated message")
_after_urls = _handle_src.split("if not urls:")[1]
# The branch body is a comment block followed by a bare return, so the first statement
# after the colon is the return - asserting on the raw slice would have matched the
# comment instead and passed vacuously.
_branch_body = [ln.strip() for ln in _after_urls.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
check("the link-less branch's first statement is a bare return",
      _branch_body and _branch_body[0] == "return",
      f"first statement is {_branch_body[0] if _branch_body else '(none)'}")
check("no mention check gates a reply any more",
      "bot_was_mentioned" not in _handle_src,
      "a heuristic on 'was I addressed' can misfire, which is what was reported")

# (c) Only the bot's OWN username counts as a mention. A mention entity alone says only
#     that a username was written, so replying to @someone picked up the bot.
_mention_src = inspect.getsource(bot.bot_was_mentioned)
check("a mention is matched against the bot's own username",
      "mentioned == bot_username" in _mention_src, _mention_src[:300])
check("a mention of another user does not count",
      '!= "mention"' in _mention_src or '== "mention"' in _mention_src)
# Verified against the real source: `if not bot_username:` is immediately followed by a
# comment and `return False`. Looking only right after the colon had matched the comment
# instead of the statement, so the assertion is written against the statement.
check("no username means no mention is claimed",
      "if not bot_username:" in _mention_src
      and "return False" in _mention_src.split("if not bot_username:")[1][:200],
      "assuming a mention without knowing our name would misfire")

# (d) 歸檔 must use e-hentai's official archiver, not a page download.
_archive_src = inspect.getsource(bot.run_gallery_archive)
check("the archive asks the official archiver",
      "resolve_official_archive" in _archive_src, _archive_src[:200])
check("the archive no longer sweeps pages",
      "gallery_page_urls(" not in _archive_src, "the archive still sweeps pages")
check("the archive no longer zips pages itself",
      "create_single_zip" not in _archive_src, _archive_src[:200])

# (e) The H@H downloader needs a running H@H client, so it must not be used.
_resolve_src = inspect.getsource(bot.resolve_official_archive)
check("the H@H form is deliberately not used",
      "hathdl_xres" not in _resolve_src, "H@H requires a client that is not running")
check("the waiting page is converted to the real download url",
      "?start=1" in _resolve_src, "the /2 page returns HTML, not the zip")

# (f) The stop flag must never be cleared, and every phase must honour it.
check("nothing clears the stop flag",
      "stop_event.clear()" not in _SOURCE,
      "a clear() erases a stop that arrived between phases")
_pages_src = inspect.getsource(bot.download_gallery_pages)
check("a parallel download registers a stoppable flag",
      "register_running_stop(" in _pages_src,
      "the button answered 'nothing running' while the download continued")
check("a process-less stop sets the flag instead of killing",
      "process is None" in inspect.getsource(bot.stop_running_download))
check("the result is re-checked after the fallback",
      "stop_event.is_set()" in _pages_src, "a stopped fallback still packaged and sent")
_direct_src = inspect.getsource(bot.download_gallery_direct)
check("the parallel fetch re-checks the flag after each request",
      _direct_src.count("stopped()") >= 3, "a late result would still be written")

# (g) A stale signed URL list must be re-swept, not discovered by failing.
check("the url list has a shorter clock than the cache entry",
      bot.GALLERY_URL_TTL_SECONDS < bot.CACHE_STALE_SECONDS,
      f"{bot.GALLERY_URL_TTL_SECONDS} vs {bot.CACHE_STALE_SECONDS}")
check("the url cache can be forced to re-sweep",
      "force" in inspect.signature(bot.gallery_page_urls).parameters)

# (h) Progress must appear at once and must never claim completion falsely.
_prog_src = inspect.getsource(bot.run_with_progress)
check("progress is rendered before the work starts",
      "render(0, total)" in _prog_src,
      "the bar appeared only after the first byte, leaving the message frozen")
check("the false 100% fallback is gone",
      "latest[0] or total" not in _prog_src.split('"""')[2],
      "a failed download was displayed as complete")
check("completion is gated on progress having been seen",
      "seen_progress" in _prog_src)

# (i) The official archive must be one file, and a total failure must raise.
_par_src = inspect.getsource(bot._download_archive_parallel)
check("a fully failed parallel download raises rather than returning 0",
      "沒有任何分段下載成功" in _par_src,
      "a 0-byte archive would be handed to Telegram")
check("each parallel worker uses its own client",
      _par_src.count("httpx.Client") == 1,
      "a shared client would serialise on its connection pool")

# (j) The Bot API server limits must be probed, not assumed.
check("the upload ceiling is probed rather than assumed",
      "probe_telegram_limits" in _SOURCE)
check("path uploads are only claimed with --local",
      "TELEGRAM_LOCAL_FLAG" in inspect.getsource(bot.probe_telegram_limits))

print()
print("=== 11. Behaviour: parse cache round trip ===")
_work = Path(tempfile.mkdtemp(prefix="dsh_char_"))
try:
    _cache = bot.ParseCache(_work / "c.json", stale_seconds=3600, max_entries=10)
    _cache.store("https://x.com/a/status/2", "media", {"files": ["a.jpg"]})
    check("a stored entry is returned",
          _cache.lookup("https://x.com/a/status/2", kind="media")
          == {"files": ["a.jpg"]})
    check("a lookup respects the kind",
          _cache.lookup("https://x.com/a/status/2", kind="gallery") is None)
    check("the entry records its age", _cache.age("https://x.com/a/status/2") is not None)
    check("a miss returns None", _cache.lookup("https://x.com/nope") is None)
    check("the kind filter is not confused by a normalised key",
          _cache.lookup("https://X.com/a/status/2/", kind="media") is not None)
finally:
    shutil.rmtree(_work, ignore_errors=True)

print()
print("=== 12. Behaviour: authorization file round trip ===")
_work = Path(tempfile.mkdtemp(prefix="dsh_char_"))
_saved_file = bot.AUTH_FILE
_saved_auth = bot.AUTHORIZATION
try:
    bot.AUTH_FILE = _work / "authorized.json"
    bot.AUTHORIZATION = bot.Authorization(allowed={5}, admins={1}, groups={-1001},
                                          locked=True, group_locked=True)
    check("saving the authorization file succeeds", bot.save_authorization())
    _reloaded = bot.Authorization.load(bot.AUTH_FILE, allowed=set(), admins=set())
    check("users survive a reload", _reloaded.allowed == {5}, str(_reloaded.allowed))
    check("admins survive a reload", _reloaded.admins == {1}, str(_reloaded.admins))
    check("groups survive a reload", _reloaded.groups == {-1001}, str(_reloaded.groups))
    check("the private mode survives a reload", _reloaded.locked is True)
    check("the group mode survives a reload", _reloaded.group_locked is True)
    # A corrupt file must not take the bot down or silently widen access.
    bot.AUTH_FILE.write_text("{not json", encoding="utf-8")
    _broken = bot.Authorization.load(bot.AUTH_FILE, allowed=set(), admins={9})
    check("a corrupt authorization file falls back to the defaults",
          _broken.admins == {9} and _broken.allowed == set(),
          f"{_broken.admins} / {_broken.allowed}")
finally:
    bot.AUTH_FILE = _saved_file
    bot.AUTHORIZATION = _saved_auth
    shutil.rmtree(_work, ignore_errors=True)

print()
print("=== 13. Behaviour: translations still resolve ===")
check("the translation table is populated", len(tag_translations.TAG_TRANSLATIONS) > 100,
      str(len(tag_translations.TAG_TRANSLATIONS)))
check("a known tag translates", bool(tag_translations.translate_tag("sole female")))
check("an unknown tag is returned unchanged",
      tag_translations.translate_tag("zzz-not-a-tag") == "zzz-not-a-tag",
      tag_translations.translate_tag("zzz-not-a-tag"))

print()
print("=== 14. Behaviour: official archive filename safety ===")
check("an official filename is sanitised before use",
      "sanitize_filename(" in _archive_src, "a Windows-hostile title would raise OSError")
check("the size limit comes from the server",
      "upload_limit_bytes()" in _archive_src, _archive_src[:200])

print()
print(f"=== RESULT: {len(PASSED)} passed, {len(FAILED)} failed ===")
for _name in FAILED:
    print("  FAILED:", _name)
raise SystemExit(1 if FAILED else 0)
