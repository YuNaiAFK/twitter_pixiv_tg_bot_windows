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
import shutil
import sys
import tempfile
from pathlib import Path

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
check("a refused button retries the album without it",
      "改為無按鈕重試" in _album_src, "a refused reply_markup would lose the album")
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
check("a link-less message returns before replying",
      "if not urls:" in _handle_src and "return" in _handle_src.split("if not urls:")[1][:400])

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
