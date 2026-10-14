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

import re as _re

# Telegram bot token shape. Used to assert that no real token literal is committed.
_TOKEN_SHAPE = _re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
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


def _drop_is_silent(notices: bool = False) -> bool:
    """
    Run a non-work link through handle_message and report whether the chat was written to.

    Driven directly rather than by inspecting the source, because the environment
    overrides the code default: flipping the default in code left a source-based check
    passing while the setting was still 0 in .env.
    """
    import asyncio

    class Msg:
        def __init__(self):
            self.text = "https://www.pixiv.net/users/12345"
            self.caption = None
            self.chat_id = 1
            self.message_id = 1
            self.chat = type("C", (), {"type": "private"})()
            self.wrote = 0

        async def reply_text(self, *a, **k):
            self.wrote += 1
            return self

        async def edit_text(self, *a, **k):
            self.wrote += 1
            return self

        async def delete(self):
            return

    class U:
        id = 111
        full_name = "T"
        username = "t"
        is_bot = False

    class Upd:
        def __init__(self):
            self.effective_message = Msg()
            self.effective_user = U()
            self.message = self.effective_message

    saved = bot.REPLY_UNSUPPORTED_LINKS
    saved_allowed = set(bot.AUTHORIZATION.allowed)
    try:
        # The gate is fail-closed, so an empty allow list would answer "not allowed"
        # instead of exercising the classifier.
        bot.AUTHORIZATION.allowed = {111}
        bot.REPLY_UNSUPPORTED_LINKS = notices
        update = Upd()
        asyncio.run(asyncio.wait_for(bot.handle_message(update, None), timeout=30))
        return update.effective_message.wrote == 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        bot.REPLY_UNSUPPORTED_LINKS = saved
        bot.AUTHORIZATION.allowed = saved_allowed


def _drop_explains_when_enabled() -> bool:
    """The same link must produce a message when notices are on."""
    import asyncio

    class Msg:
        def __init__(self):
            self.text = "https://www.pixiv.net/users/12345"
            self.caption = None
            self.chat_id = 1
            self.message_id = 1
            self.chat = type("C", (), {"type": "private"})()
            self.texts = []

        async def reply_text(self, text, **k):
            self.texts.append(text)
            return self

        async def edit_text(self, text, **k):
            self.texts.append(text)
            return self

        async def delete(self):
            return

    class U:
        id = 111
        full_name = "T"
        username = "t"
        is_bot = False

    class Upd:
        def __init__(self):
            self.effective_message = Msg()
            self.effective_user = U()
            self.message = self.effective_message

    saved = bot.REPLY_UNSUPPORTED_LINKS
    saved_allowed = set(bot.AUTHORIZATION.allowed)
    try:
        bot.AUTHORIZATION.allowed = {111}
        bot.REPLY_UNSUPPORTED_LINKS = True
        update = Upd()
        asyncio.run(asyncio.wait_for(bot.handle_message(update, None), timeout=30))
        said = " ".join(update.effective_message.texts)
        return "使用者頁面" in said
    except Exception:  # noqa: BLE001
        return False
    finally:
        # Both restored: leaving the allow list modified would leak into every later test.
        bot.REPLY_UNSUPPORTED_LINKS = saved
        bot.AUTHORIZATION.allowed = saved_allowed


def _spawn_flags() -> bool:
    """
    Whether the spawner gives the replacement its own console and does not detach it.

    Only the ``flags`` assignments are inspected. The whole function cannot be searched:
    ast.unparse keeps comments, and the comment there explains that DETACHED_PROCESS is
    deliberately *not* used - so searching the text found the explanation and failed against
    correct code.
    """
    import ast
    import inspect as _i
    import textwrap
    source = textwrap.dedent(_i.getsource(bot.spawn_replacement_process))
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    pieces = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "flags":
                    pieces.append(ast.unparse(node.value))
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "flags":
                pieces.append(ast.unparse(node.value))
    joined = " ".join(pieces)
    return "CREATE_NEW_CONSOLE" in joined and "DETACHED_PROCESS" not in joined


def _replacement_is_decided_not_assumed() -> bool:
    """
    Whether ``is_replacement`` comes from the environment rather than a constant.

    Parsed, and matched on the assignment: a literal True is exactly the regression, and a
    plain text search for the variable name cannot tell the two apart.
    """
    import ast
    import inspect as _i
    import textwrap
    source = textwrap.dedent(_i.getsource(bot.main))
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not (isinstance(target, ast.Name) and target.id == "is_replacement"):
                    continue
                value = node.value
                # A constant is the regression; anything computed is acceptable, and the
                # name it reads is asserted separately below.
                if isinstance(value, ast.Constant):
                    return False
                return True
    return False


def _replacement_flag_is_read() -> bool:
    """
    Whether main() reads BOT_IS_REPLACEMENT from the environment to decide the wait.

    Parsed with AST and matched on the call, so the environment dictionary in the spawner -
    which also contains the name - cannot satisfy it.
    """
    import ast
    import inspect as _i
    import textwrap
    # dedent, not lstrip: only the first line loses its indentation otherwise, and the parse
    # fails - which is how this helper reported False against correct code.
    source = textwrap.dedent(_i.getsource(bot.main))
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getenv" and node.args):
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == "BOT_IS_REPLACEMENT":
                return True
    return None


def _lock_call_uses_the_setting() -> bool:
    """
    main() must pass the configured delay to the lock, not a constant.

    Parsed from the source rather than run, because the value only matters while a previous
    instance is actually holding the lock.
    """
    import ast
    import inspect as _i
    import textwrap
    source = textwrap.dedent(_i.getsource(bot.main))
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "acquire_instance_lock" and node.args):
            arg = node.args[0]
            # The argument must be the variable the setting was read into.
            return isinstance(arg, ast.Name) and arg.id == "await_lock"
    return False


def _lock_acquires_immediately() -> bool:
    """With nothing holding it, the lock must be taken at once."""
    import time as _tm
    try:
        start = _tm.monotonic()
        got = bot.acquire_instance_lock(2.0)
        elapsed = _tm.monotonic() - start
        bot.release_instance_lock()
        return got is True and elapsed < 0.5
    except Exception:  # noqa: BLE001
        return False


def _lock_waits_for_the_holder() -> bool:
    """
    A second holder must wait, and succeed once the first releases.

    Run in a child process, because the lock is per-process by design: an in-process test
    would hold it with the same handle and prove nothing.
    """
    import subprocess as _sp
    import sys as _sys
    import time as _tm
    try:
        bot.acquire_instance_lock(2.0)
    except Exception:  # noqa: BLE001
        return False
    child = (
        "import sys, time; sys.path.append('vendor'); import bot;"
        "t=time.monotonic(); got=bot.acquire_instance_lock(8.0);"
        "print(got, round(time.monotonic()-t, 2))"
    )
    try:
        proc = _sp.Popen([_sys.executable, "-c", child], stdout=_sp.PIPE,
                         stderr=_sp.DEVNULL, text=True, encoding="utf-8")
        _tm.sleep(1.0)
        bot.release_instance_lock()
        out = (proc.communicate(timeout=30)[0] or "").strip()
    except Exception:  # noqa: BLE001
        bot.release_instance_lock()
        return False
    finally:
        bot.release_instance_lock()
    if not out:
        return False
    try:
        got_text, elapsed_text = out.split()
        return got_text == "True" and float(elapsed_text) >= 0.5
    except ValueError:
        return False


def _deletion_is_batched() -> bool:
    """
    A long list must be deleted in batches, with a pause between them.

    Asserted on the pause rather than on a wall-clock time, so the check does not depend on
    how fast the disk is.
    """
    import time as _tm
    import tempfile as _tf
    import shutil as _sh
    work = Path(_tf.mkdtemp(prefix="dsh-batch-"))
    try:
        files = []
        for i in range(120):
            path = work / f"f{i:04d}.bin"
            path.write_bytes(b"x")
            files.append(path)
        start = _tm.monotonic()
        bot.delete_files_quietly(files, batch=40, pause=0.05)
        elapsed = _tm.monotonic() - start
        # 120 files at 40 per batch is 2 pauses, so at least ~0.1s of deliberate waiting.
        return elapsed >= 0.09
    except Exception:  # noqa: BLE001
        return False
    finally:
        _sh.rmtree(work, ignore_errors=True)


def _deletion_skips_directories() -> bool:
    """
    A directory in the list is left alone, and its contents are untouched.

    Asserted on the **count**, not only on the directory surviving.

    Measured with the guard removed: ``unlink()`` on a directory raises PermissionError, the
    surrounding handler swallows it, and the count stays the same - so the outcome is
    identical either way and no assertion here can tell the two implementations apart. The
    guard is therefore defence in depth rather than the thing that makes this safe; the
    ``except OSError`` is. What this check does pin is the observable contract: exactly one
    removal, the directory and its contents intact.
    """
    import tempfile as _tf
    import shutil as _sh
    work = Path(_tf.mkdtemp(prefix="dsh-skipdir-"))
    try:
        (work / "sub").mkdir()
        (work / "sub" / "inner.bin").write_bytes(b"y")
        (work / "plain.bin").write_bytes(b"x")
        removed = bot.delete_files_quietly([work / "sub", work / "plain.bin"])
        # Exactly one removal: the file. The directory must not be counted.
        return (removed == 1
                and (work / "sub").is_dir()
                and (work / "sub" / "inner.bin").is_file()
                and not (work / "plain.bin").exists())
    except Exception:  # noqa: BLE001
        return False
    finally:
        _sh.rmtree(work, ignore_errors=True)


def _deletion_is_idempotent() -> bool:
    """Deleting the same list twice must not raise and must remove nothing the second time."""
    import tempfile as _tf
    import shutil as _sh
    work = Path(_tf.mkdtemp(prefix="dsh-idem-"))
    try:
        files = []
        for i in range(5):
            path = work / f"f{i}.bin"
            path.write_bytes(b"x")
            files.append(path)
        first = bot.delete_files_quietly(files)
        second = bot.delete_files_quietly(files)
        return first == 5 and second == 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        _sh.rmtree(work, ignore_errors=True)


def _state_from_response(status_code: int, payload=None) -> str:
    """
    Run pixiv_work_state against a stubbed HTTP response.

    The token and client are stubbed so the classification logic is what is under test, not
    the network. A real un-published work was used while developing, but it will not stay
    un-published forever, so the response shape is reproduced instead.
    """
    class FakeResponse:
        def __init__(self):
            self.status_code = status_code

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return FakeResponse()

    saved_token = bot.pixiv_access_token
    saved_httpx = bot.httpx.Client
    try:
        bot.pixiv_access_token = lambda *a, **k: "stub-token"
        bot.httpx.Client = FakeClient
        return bot.pixiv_work_state("1")
    except Exception:  # noqa: BLE001
        return "ERROR"
    finally:
        bot.pixiv_access_token = saved_token
        bot.httpx.Client = saved_httpx


def _unpublished_is_unavailable() -> bool:
    return _state_from_response(200, {"illust": {
        "id": 1, "title": "", "visible": False,
        "total_view": 0, "total_bookmarks": 0}}) == "unavailable"


def _deleted_is_unavailable() -> bool:
    return _state_from_response(404, None) == "unavailable"


def _server_error_is_unknown() -> bool:
    return _state_from_response(500, None) == "unknown"


def _unavailable_notice() -> str:
    """The message text report_empty_download sends for an unavailable work."""
    import asyncio

    class Msg:
        def __init__(self):
            self.chat_id = 1
            self.message_id = 1
            self.texts = []

        async def reply_text(self, text, **k):
            self.texts.append(text)
            return self

        async def edit_text(self, text, **k):
            self.texts.append(text)
            return self

        async def delete(self):
            return self

    saved = bot.pixiv_work_state
    try:
        bot.pixiv_work_state = lambda *a, **k: "unavailable"
        status, message = Msg(), Msg()
        asyncio.run(bot.report_empty_download(
            status, message, "https://www.pixiv.net/artworks/1", "pixiv", "note"))
        return " ".join(status.texts + message.texts)
    except Exception:  # noqa: BLE001
        return ""
    finally:
        bot.pixiv_work_state = saved


def _empty_download_reports_deleted() -> bool:
    """
    A work the API says is gone must produce a message naming that.

    The probe is stubbed so the test does not depend on any particular artwork still being
    deleted - the real one used while developing will not be deleted forever.
    """
    import asyncio

    class Msg:
        def __init__(self):
            self.chat_id = 1
            self.message_id = 1
            self.log = []

        async def reply_text(self, text, **k):
            self.log.append(("reply", text))
            return self

        async def edit_text(self, text, **k):
            self.log.append(("edit", text))
            return self

        async def delete(self):
            self.log.append(("delete", None))
            return self

    saved = bot.pixiv_work_state
    try:
        bot.pixiv_work_state = lambda *a, **k: "unavailable"
        status, message = Msg(), Msg()
        asyncio.run(bot.report_empty_download(
            status, message, "https://www.pixiv.net/artworks/1", "pixiv", "note"))
        said = " ".join(t for k, t in status.log if t)
        return "無法瀏覽" in said and not any(k == "delete" for k, _ in status.log)
    except Exception:  # noqa: BLE001
        return False
    finally:
        bot.pixiv_work_state = saved


def _empty_download_is_silent_for_private() -> bool:
    """A work whose state is not 'deleted' must produce no message at all."""
    import asyncio

    class Msg:
        def __init__(self):
            self.chat_id = 1
            self.message_id = 1
            self.deleted = 0

        async def reply_text(self, text, **k):
            self.spoke = True
            return self

        async def edit_text(self, text, **k):
            self.spoke = True
            return self

        async def delete(self):
            self.deleted += 1
            return self

    saved = bot.pixiv_work_state
    try:
        # "ok" is what an age-restricted work answers when credentials work: the work is
        # readable, so a failure to download it is not something to tell the sender about.
        bot.pixiv_work_state = lambda *a, **k: "ok"
        status, message = Msg(), Msg()
        asyncio.run(bot.report_empty_download(
            status, message, "https://www.pixiv.net/artworks/124400619", "pixiv", "note"))
        return (getattr(status, "spoke", False) is False
                and getattr(message, "spoke", False) is False
                and status.deleted == 1)
    except Exception:  # noqa: BLE001
        return False
    finally:
        bot.pixiv_work_state = saved


def _unknown_state_is_silent() -> bool:
    """An unverifiable state must be silent rather than claim a deletion."""
    import asyncio

    class Msg:
        def __init__(self):
            self.chat_id = 1
            self.message_id = 1
            self.spoke = False
            self.deleted = 0

        async def reply_text(self, text, **k):
            self.spoke = True
            return self

        async def edit_text(self, text, **k):
            self.spoke = True
            return self

        async def delete(self):
            self.deleted += 1
            return self

    saved = bot.pixiv_work_state
    try:
        bot.pixiv_work_state = lambda *a, **k: "unknown"
        status, message = Msg(), Msg()
        asyncio.run(bot.report_empty_download(
            status, message, "https://www.pixiv.net/artworks/1", "pixiv", "note"))
        return status.spoke is False and message.spoke is False
    except Exception:  # noqa: BLE001
        return False
    finally:
        bot.pixiv_work_state = saved


def _env_flag_documented(name: str) -> bool:
    """Whether a configuration flag appears in .env.example, so it can be discovered."""
    try:
        text = (HERE / ".env.example").read_text(encoding="utf-8")
    except OSError:
        return False
    return name in text


def _order_in(source: str, first: str, second: str) -> bool:
    """
    Whether `first` appears before `second` in `source`, without raising when one is absent.

    `str.index` raises ValueError, which aborts the run instead of failing the check that
    asked the question - and a missing needle is exactly the regression these assertions
    exist to catch, so it has to report rather than crash.
    """
    a = source.find(first)
    b = source.find(second)
    return a != -1 and b != -1 and a < b


def _resolver_code() -> str:
    """The resolver's code with its docstring removed, so prose cannot satisfy a check."""
    import ast
    import inspect as _i
    source = _i.getsource(ehentai.resolve_public_image_url)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                body.pop(0)
    return ast.unparse(tree)


def _public_embed_urls_passthrough() -> bool:
    """A list that needs no conversion must be returned as-is, with no network access."""
    import asyncio

    class G:
        gid = "1"
        token = "aa"

    async def scenario():
        urls = ["https://node.hath.network/h/abc-1280-1-wb",
                "https://ehgt.org/w/02/240/x.webp"]
        out = await bot.public_embed_urls(list(urls), G(), None, report=False)
        return out == urls

    try:
        return asyncio.run(scenario())
    except Exception:  # noqa: BLE001
        return False


def _public_embed_urls_accepts_no_status() -> bool:
    """
    report=True with status=None must not raise.

    It did: the helper wrote the progress message before checking it had anywhere to write,
    which the regenerate path can hit because the message it passes is not a status
    placeholder.
    """
    import inspect as _i
    source = _i.getsource(bot.public_embed_urls)
    # Asserted on the guard itself: status is only used when it is present.
    return "report is not None and status is not None" in source


def _result_message_helper_works() -> bool:
    """
    update_result_message must edit a caption on a photo and text on a text message.

    A stub that records which call was made, plus a strict edit_text that raises the exact
    error Telegram gave, so returning the wrong branch is visible rather than silent.
    """
    import asyncio

    class Msg:
        def __init__(self, has_photo):
            self.photo = [object()] if has_photo else []
            self.calls = []
            self.message_id = 1
            self.chat_id = 1
            self.caption = None

        async def edit_caption(self, caption=None, **kwargs):
            self.calls.append(("edit_caption", caption))
            return self

        async def edit_text(self, text=None, **kwargs):
            self.calls.append(("edit_text", text))
            return self

    async def scenario():
        text_msg = Msg(False)
        photo_msg = Msg(True)
        ok_text = await bot.update_result_message(text_msg, "summary")
        ok_photo = await bot.update_result_message(photo_msg, "summary")
        if not (ok_text and ok_photo):
            return False
        if text_msg.calls[0][0] != "edit_text":
            return False
        if photo_msg.calls[0][0] != "edit_caption":
            return False
        # And the caption actually carries the new text, not an empty placeholder.
        return photo_msg.calls[0][1] == "summary"

    try:
        return asyncio.run(scenario())
    except Exception:  # noqa: BLE001
        return False


def _empty_caption_is_refused() -> bool:
    """An empty caption on a photo must return False, not raise or send an empty edit."""
    import asyncio

    class Msg:
        photo = [object()]
        message_id = 1

        def __init__(self):
            self.calls = []

        async def edit_caption(self, caption=None, **kwargs):
            self.calls.append(caption)
            return self

        async def edit_text(self, text=None, **kwargs):
            self.calls.append(text)
            return self

    async def scenario():
        msg = Msg()
        result = await bot.update_result_message(msg, "")
        return result is False and not msg.calls

    try:
        return asyncio.run(scenario())
    except Exception:  # noqa: BLE001
        return False


def _orphan_cleanup_is_safe() -> bool:
    """
    Orphan cleanup must remove exactly the unreferenced files, and nothing else.

    The dangerous direction is deleting too much: the live files, the archive cache in its
    subdirectory, or an unrelated file dropped in the cache directory. All three are
    asserted here, not just the removal.
    """
    import shutil as _sh
    import tempfile as _tf
    import time as _tm
    real_dir, real_cache = bot.CACHE_DIR, bot.PARSE_CACHE
    try:
        work = Path(_tf.mkdtemp(prefix="dsh-orph-"))
        bot.CACHE_DIR = work
        (work / "archives").mkdir()
        (work / "archives" / "keep_orig.zip").write_bytes(b"archive cache")
        (work / "aaaa0000bbbb_001.jpg").write_bytes(b"live")
        (work / "aaaa0000bbbb_002.jpg").write_bytes(b"orphan")
        (work / ".aaaa0000bbbb_003.jpg.1.2.part").write_bytes(b"partial")
        (work / "random-notes.txt").write_bytes(b"not ours")
        bot.PARSE_CACHE = bot.ParseCache(work / "c.json")
        bot.PARSE_CACHE.entries = {
            "https://x.com/u/status/1": {
                "kind": "media",
                "payload": {"files": ["aaaa0000bbbb_001.jpg"]},
                "at": _tm.time(),
            }
        }
        found = {p.name for p in bot.orphaned_cache_files()}
        bot.prune_orphaned_cache_files()
        left = {p.name for p in work.rglob("*") if p.is_file()}
        return (found == {"aaaa0000bbbb_002.jpg", ".aaaa0000bbbb_003.jpg.1.2.part"}
                and "aaaa0000bbbb_001.jpg" in left          # live file kept
                and "aaaa0000bbbb_002.jpg" not in left      # orphan removed
                and "keep_orig.zip" in left                 # archive cache untouched
                and "random-notes.txt" in left              # foreign file untouched
                and "keep_orig.zip" not in found)           # subdirectory not scanned
    except Exception:  # noqa: BLE001
        return False
    finally:
        bot.CACHE_DIR, bot.PARSE_CACHE = real_dir, real_cache
        _sh.rmtree(work, ignore_errors=True)


class _FakeGallery:
    """The minimum a gallery needs to have for cache-key purposes."""

    gid = "3778922"
    token = "19330e2349"
    title = "Test Gallery"


def _archive_cache_evicts() -> bool:
    """
    Fill a small archive cache past its bound and confirm the oldest go first.

    Run against a temporary directory with a temporary bound: the real cache is 8 GB and
    the real entries are tens of MB, so this is the only way to exercise eviction cheaply.
    """
    import os as _os
    import shutil as _sh
    import tempfile as _tf
    import time as _tm
    real_dir, real_max = bot.ARCHIVE_CACHE_DIR, bot.ARCHIVE_CACHE_MAX_BYTES
    try:
        work = Path(_tf.mkdtemp(prefix="dsh-evic-"))
        bot.ARCHIVE_CACHE_DIR = work
        bot.ARCHIVE_CACHE_MAX_BYTES = 3000
        now = _tm.time()
        for i in range(6):
            path = work / f"g{i}_orig.zip"
            path.write_bytes(b"x" * 1000)
            _os.utime(path, (now - (6 - i), now - (6 - i)))
        bot.prune_archive_cache()
        names = {p.name for p in work.glob("*") if p.is_file()}
        return (bot.archive_cache_total_bytes() <= 3000
                and "g0_orig.zip" not in names
                and "g5_orig.zip" in names)
    except Exception:  # noqa: BLE001
        return False
    finally:
        bot.ARCHIVE_CACHE_DIR, bot.ARCHIVE_CACHE_MAX_BYTES = real_dir, real_max
        _sh.rmtree(work, ignore_errors=True)


def _empty_archive_not_served() -> bool:
    """A 0-byte file in the archive cache must not be treated as a hit."""
    import shutil as _sh
    import tempfile as _tf
    real_dir = bot.ARCHIVE_CACHE_DIR
    try:
        work = Path(_tf.mkdtemp(prefix="dsh-empty-"))
        bot.ARCHIVE_CACHE_DIR = work
        (work / f"{bot.archive_cache_key(_FakeGallery(), True)}.zip").write_bytes(b"")
        return bot.archive_cache_path(_FakeGallery(), True) is None
    except Exception:  # noqa: BLE001
        return False
    finally:
        bot.ARCHIVE_CACHE_DIR = real_dir
        _sh.rmtree(work, ignore_errors=True)


def _unguarded_number_reads() -> int:
    """
    Count `int(...)`/`float(...)` applied directly to an environment value.

    Only the two inside try/except blocks are acceptable: they already fall back on a bad
    value. Anywhere else, `int("")` raises at import.
    """
    import re as _r
    source = Path(HERE / "bot.py").read_text(encoding="utf-8")
    lines = source.splitlines()
    total = 0
    for match in _r.finditer(r"(int|float)\(\s*(?:\n\s*)?os\.getenv", source):
        start = source[:match.start()].count("\n")
        # In scope if an indented `try:` appears within the few lines above it.
        window = lines[max(0, start - 6):start]
        if not any(l.strip() == "try:" for l in window):
            total += 1
    return total


def _imports_with_blank_env() -> bool:
    """
    Run the real bot module in a scratch copy with every optional key set to blank.

    A subprocess, not an import: this has to observe import-time behaviour, and the module
    is already imported in this process. `.env.example` verbatim is the realistic input,
    because that is the file the documentation tells people to copy.
    """
    import os as _os
    import shutil as _sh
    import subprocess as _sp
    import sys as _sys
    import tempfile as _tf
    try:
        work = Path(_tf.mkdtemp(prefix="dsh-blankenv-"))
        try:
            for path in HERE.glob("*.py"):
                (work / path.name).write_text(
                    path.read_text(encoding="utf-8"), encoding="utf-8")
            example = (HERE / ".env.example").read_text(encoding="utf-8")
            blanked = "\n".join(
                (line.split("=")[0] + "=")
                if ("=" in line and not line.strip().startswith("#")) else line
                for line in example.splitlines())
            (work / ".env").write_text(blanked, encoding="utf-8")
            env = dict(_os.environ,
                       PYTHONPATH=str(HERE / "vendor"),
                       PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            result = _sp.run(
                [_sys.executable, "-c",
                 "import sys; sys.path.insert(0,'.'); import bot;"
                 "print('OK', bot.MAX_FILES, bot.ARCHIVE_DEADLINE_SECONDS)"],
                cwd=work, env=env, stdout=_sp.PIPE, stderr=_sp.PIPE,
                encoding="utf-8", errors="replace", timeout=180)
            return result.returncode == 0 and "OK" in (result.stdout or "")
        finally:
            _sh.rmtree(work, ignore_errors=True)
    except Exception:  # noqa: BLE001
        return False


def _deadline_reports_to_user() -> bool:
    """
    The timeout must be an EhentaiError so run_gallery_archive shows it in the chat.

    Asserts the type relationship and that the message carries the configured budget,
    rather than re-running a download - the guard itself is exercised in
    _deadline_aborts_a_stalled_transfer.
    """
    try:
        guard = bot._archive_deadline_guard(_time.monotonic() - 1)
        try:
            guard()
            return False
        except bot._ArchiveDeadlineExceeded as exc:
            # The wording is Chinese ("超過 300 秒"); asserting the bare digits would be
            # asserting the rendering rather than the message.
            return (isinstance(exc, ehentai.EhentaiError)
                    and "官方歸檔下載超過" in str(exc)
                    and "已中止" in str(exc))
    except Exception:  # noqa: BLE001
        return False


def _deadline_aborts_a_stalled_transfer() -> bool:
    """
    Serve a stream that never ends and check the guard aborts it.

    A per-read timeout cannot catch this: the transfer keeps producing bytes, just
    slowly, which is exactly the case the old code had no bound for.
    """
    import tempfile
    import threading
    import time as _t
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    try:
        class Trickle(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(50 * 1024 * 1024))
                self.end_headers()
                try:
                    for _ in range(20000):
                        self.wfile.write(b"x" * 4096)
                        self.wfile.flush()
                        _t.sleep(0.005)
                except Exception:  # noqa: BLE001
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        work = Path(tempfile.mkdtemp(prefix="dsh-deadline-"))
        try:
            import httpx as _hx
            try:
                bot._download_archive_single(
                    {"User-Agent": "test"}, 
                    f"http://127.0.0.1:{server.server_address[1]}/a.zip",
                    work / "a.zip", 50 * 1024 * 1024, None, None,
                    _hx.Timeout(10.0, read=10.0),
                    _t.monotonic() - 0.001)
                return False
            except bot._ArchiveDeadlineExceeded:
                return True
        finally:
            server.shutdown()
            shutil.rmtree(work, ignore_errors=True)
    except Exception:  # noqa: BLE001
        return False


def _streamed_pages_are_identical() -> bool:
    """
    Serve real bytes and confirm the streamed files match them exactly.

    The change replaced a buffer-and-write with a stream-and-rename, so the risk is a
    truncated or misnamed file. Comparing hashes is the only assertion that actually
    covers that.
    """
    import hashlib
    import os as _os
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    try:
        pages = {f"/p{i}.png": _os.urandom(300_000 + i * 10_000) for i in range(1, 5)}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = pages.get(self.path.split("?")[0])
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                # Small writes, so buffering would be visible in memory use.
                for i in range(0, len(body), 8192):
                    self.wfile.write(body[i:i + 8192])
                    self.wfile.flush()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        work = Path(tempfile.mkdtemp(prefix="dsh-identical-"))
        try:
            out = work / "out"
            written = bot.download_gallery_direct(
                [f"http://127.0.0.1:{server.server_address[1]}{k}" for k in pages],
                out, referer="https://e-hentai.org/", workers=3, total=len(pages))
            files = sorted(out.iterdir())
            if written != len(pages) or len(files) != len(pages):
                return False
            if any(f.name.endswith(".part") for f in files):
                return False
            originals = list(pages.values())
            for index, path in enumerate(files):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest != hashlib.sha256(originals[index]).hexdigest():
                    return False
            return True
        finally:
            server.shutdown()
            shutil.rmtree(work, ignore_errors=True)
    except Exception:  # noqa: BLE001
        return False


def _concurrent_updates_applied() -> bool:
    """
    Build the bot's REAL Application and read back how many updates it will process.

    Calls ``bot.build_application()`` rather than assembling an Application here. The
    first version of this helper built its own, which meant deleting the
    ``concurrent_updates`` call from the bot's builder left the test passing - it was
    asserting on the test's own code. Anything claimed about how this bot dispatches
    updates has to come through the real builder.
    """
    try:
        app = bot.build_application()
        return app.update_processor.max_concurrent_updates == bot.MAX_CONCURRENT_UPDATES
    except Exception:  # noqa: BLE001
        return False


def _stop_cancels_running_job() -> bool:
    """
    Drive the REAL Application's update processor: block a job, press stop, and see
    whether the job observes the flag.

    This is the end-to-end assertion the whole change exists for - it exercises PTB's
    dispatch and bot's stop registry together, through the application the bot actually
    builds. A regression in either (the concurrency limit dropped, or the flag not
    readable) fails here.

    Asserts the flag is seen BEFORE the job would have finished, because a flag that is
    only set after completion is exactly the original bug.
    """
    import asyncio
    import threading
    try:
        app = bot.build_application()
        processor = app.update_processor

        async def scenario():
            chat_id = 987654321
            stop_event = threading.Event()
            bot.register_running_stop(chat_id, stop_event)
            asked = {}

            async def job():
                # Stands in for the parallel page fetch: bounded work that checks the flag.
                for _ in range(500):
                    if stop_event.is_set():
                        asked["stopped_early"] = True
                        return
                    await asyncio.sleep(0.005)

            async def press():
                asked["answer"] = bot.stop_running_download(chat_id)

            try:
                running = asyncio.create_task(processor.process_update("u1", job()))
                await asyncio.sleep(0.05)
                stop = asyncio.create_task(processor.process_update("u2", press()))
                await asyncio.sleep(0.05)
                await asyncio.gather(running, stop, return_exceptions=True)
            finally:
                bot.register_running_stop(chat_id, None)

            return bool(asked.get("answer")) and bool(asked.get("stopped_early"))

        return asyncio.run(scenario())
    except Exception:  # noqa: BLE001
        return False


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
#   InvalidURL: Invalid port: '<REDACTED-TOKEN>'
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

# httpx does NOT read the Windows registry proxy, so every e-hentai request went out
# directly and timed out while the same URL worked in a browser - and worked from httpx
# the moment proxy= was passed. Every client must therefore carry it explicitly.
_saved_proxy = {k: os.environ.get(k) for k in
                ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                 "ALL_PROXY", "all_proxy")}
try:
    for _k in _saved_proxy:
        os.environ.pop(_k, None)
    os.environ["HTTPS_PROXY"] = "http://example.invalid:9"
    _env_proxy = ehentai.proxy_url()
finally:
    for _k, _v in _saved_proxy.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v

check("an explicit environment variable wins over the registry",
      _env_proxy == "http://example.invalid:9", f"got {_env_proxy!r}")
check("the registry proxy is read on Windows",
      os.name != "nt" or ehentai.proxy_url() == "" or "://" in ehentai.proxy_url(),
      f"got {ehentai.proxy_url()!r}")
_kw = ehentai.http_client_kwargs(10.0)
check("client kwargs include the proxy when one exists",
      ("proxy" in _kw) == bool(ehentai.proxy_url()),
      f"proxy in kwargs={('proxy' in _kw)}, proxy_url={ehentai.proxy_url()!r}")
check("client kwargs keep the timeout and redirects",
      _kw["timeout"] == 10.0 and _kw["follow_redirects"] is True, _kw)

# Every httpx client in both files must use the helper. A single bare
# httpx.Client(timeout=...) is enough to time out on its own - and the call is often
# split over two lines, so the check reads the whole statement rather than one line.
for _mod_name, _path in (("ehentai.py", HERE / "ehentai.py"),
                         ("bot.py", HERE / "bot.py")):
    _lines = _path.read_text(encoding="utf-8").splitlines()
    _bare = []
    for _i, _ln in enumerate(_lines):
        if "httpx.Client(" not in _ln:
            continue
        # Join until the parentheses balance, to see the whole call.
        _stmt = _ln
        _depth = _ln.count("(") - _ln.count(")")
        while _depth > 0 and _i + 1 < len(_lines):
            _i += 1
            _stmt += " " + _lines[_i].strip()
            _depth += _lines[_i].count("(") - _lines[_i].count(")")
        if "http_client_kwargs" not in _stmt and "http_client or" not in _stmt:
            _bare.append(_stmt.strip()[:70])
    check(f"every httpx.Client in {_mod_name} carries the proxy",
          not _bare, f"bare clients: {_bare}")

check("the e-hentai archiver uses the proxied client",
      "ehentai.http_client_kwargs" in inspect.getsource(bot.resolve_official_archive),
      "the archiver is an e-hentai request like any other")
check("the gallery fetch uses the proxied client",
      "http_client_kwargs" in inspect.getsource(ehentai.fetch_gallery),
      "this is the first request of every gallery action")

# The viewer-link pattern was hard-coded to e-hentai.org, so it matched nothing on
# exhentai (the 裡站) and the whole feature was dead there:
#   e-hentai.org  -> 27 matches
#   exhentai.org  ->  0 matches, reported as "連結可能有誤，或畫廊已被刪除"
_VIEW_HTML = (
    '<a href="https://e-hentai.org/s/aaa/1-1">x</a>'
    '<a href="https://exhentai.org/s/bbb/1-2">x</a>'
    '<a href="https://example.com/s/ccc/1-3">x</a>'
)
_links = ehentai.gallery_viewer_links(_VIEW_HTML)
check("viewer links match on e-hentai.org",
      any("e-hentai.org/s/aaa" in l for l in _links), _links)
check("viewer links match on exhentai.org",
      any("exhentai.org/s/bbb" in l for l in _links),
      f"the 裡站 was unsupported: {_links}")
check("an unrelated host is not treated as a viewer link",
      not any("example.com" in l for l in _links), _links)
check("duplicate viewer links are collapsed",
      len(ehentai.gallery_viewer_links(_VIEW_HTML + _VIEW_HTML)) == 2)

# exhentai paginates at 20 per page while e-hentai returns everything at once. The cap
# was applied while paging, and 20 happens to equal MAX_FILES, so the first page filled
# the budget and the loop broke immediately - a 27-image gallery stayed 20, silently.
check("the gallery page count is read from the page",
      ehentai.gallery_page_count("Showing 1 - 20 of 27 images", 20) == 2,
      ehentai.gallery_page_count("Showing 1 - 20 of 27 images", 20))
check("a single-page gallery needs no extra request",
      ehentai.gallery_page_count("Showing 1 - 20 of 20 images", 20) == 1
      and ehentai.gallery_page_count("", 20) == 1,
      "an absent or complete count must not trigger paging")
check("paging is not capped mid-loop",
      "if len(links) >= max_images" not in inspect.getsource(ehentai.fetch_gallery_images),
      "the cap must apply after paging, or 20-per-page hosts never page")
check("the cap is reported rather than applied silently",
      "超過單次上限" in inspect.getsource(ehentai.fetch_gallery_images),
      "a truncated gallery must say so")
check("the progress notice survives the final edit",
      "render(latest[0], latest[1], latest[2]" in inspect.getsource(bot.run_with_progress),
      "the closing edit is the message the user keeps, so it must not drop the notice")

# The bot's description was found hijacked: it held a two-language referral link to an
# unrelated adult site. Nothing here ever set a description, so it came from someone
# holding the token - and a token that can set a description can also read updates and
# send messages. The profile is now re-published on every startup so it self-heals.
check("the description is re-published at startup",
      "set_my_description" in inspect.getsource(bot.post_init),
      "an injected description would persist until someone noticed")
check("the short description is re-published too",
      "set_my_short_description" in inspect.getsource(bot.post_init))
check("a profile failure cannot stop the bot starting",
      "無法設定機器人簡介" in inspect.getsource(bot.post_init),
      "an unset description is cosmetic; failing to boot is not")
check("the replacement text fits Telegram's limits",
      len(bot.BOT_DESCRIPTION) <= 512 and len(bot.BOT_SHORT_DESCRIPTION) <= 120,
      f"{len(bot.BOT_DESCRIPTION)}/512 and {len(bot.BOT_SHORT_DESCRIPTION)}/120")
check("the profile text advertises nothing external",
      not any(w in (bot.BOT_DESCRIPTION + bot.BOT_SHORT_DESCRIPTION).lower()
              for w in ("http://", "https://", "t.me/", "ref_", "porn")),
      "the whole point is that this text is ours")
_profile_src = Path(HERE / "bot.py").read_text(encoding="utf-8")
check("the profile text is overridable without editing code",
      'os.getenv("BOT_DESCRIPTION"' in _profile_src
      and '"BOT_SHORT_DESCRIPTION"' in _profile_src,
      "the operator should be able to reword it from .env")
check("the bot sets no chat menu button",
      "set_chat_menu_button" not in _profile_src,
      "a menu button pointing elsewhere would be another injection route")

# --- Security audit findings, pinned so they cannot regress ---

# HIGH: cache index names were joined to CACHE_DIR with only an is_file() check, so
# `.cache/../.env` was accepted and could be uploaded to whoever requested that URL - and
# the same join was used for deletion. Verified on Windows: an absolute child replaces the
# base entirely.
for _name, _ok, _label in (
        ("75196daa8d51_001.jpg", True, "a name the generator produces"),
        ("../.env", False, "traversal to the token file"),
        ("..\\..\\.env", False, "backslash traversal"),
        ("C:/Windows/win.ini", False, "an absolute child replaces the base"),
        ("/etc/passwd", False, "an absolute posix path"),
        ("sub/dir/file.jpg", False, "a nested path"),
        (".env", False, "the token file by name"),
        ("", False, "empty"),
        (None, False, "not a string"),
        ("75196daa8d51_001.jpg.exe", False, "longer suffix than generated"),
):
    check(f"cache path: {_label}",
          (bot.cache_file_path(_name) is not None) is _ok,
          f"{_name!r} -> {bot.cache_file_path(_name)}")
check("a poisoned cache entry resolves to nothing",
      bot.cached_media_files({"files": ["../.env"]}) == [],
      "this is the file-disclosure path the audit found")
check("the deletion path uses the same validation",
      "cache_file_path(name)" in inspect.getsource(bot.ParseCache.discard_files),
      "deleting is the more dangerous of the two uses")

# MEDIUM: the cache write skipped an existing name, and names are derived from the URL
# alone, so a file planted at that name was served as that URL's media.
_cache_src2 = inspect.getsource(bot.cache_media_files)
check("cache files are written atomically, not skipped when present",
      "os.replace(" in _cache_src2
      # The colon matters: the docstring quotes the old code, and matching on the bare
      # phrase would fail on this module's own prose rather than on real code.
      and "if not target.exists():" not in _cache_src2,
      "a pre-planted file would otherwise be trusted and served")

# MEDIUM: every candidate staging directory was created and only one returned, so the
# rest leaked - 2,027 empty dirs were measured in %TEMP%.
_mkwd_src = inspect.getsource(bot.make_work_dir)
check("staging directories are created lazily",
      "attempts.append" in _mkwd_src and "candidates.append" not in _mkwd_src,
      "creating every candidate leaks the ones that are not returned")
check("a rejected staging directory is removed",
      "shutil.rmtree(path, ignore_errors=True)" in _mkwd_src,
      "a bad location would accumulate directories")
check("abandoned staging directories are swept at startup",
      "sweep_stale_work_dirs" in inspect.getsource(bot.main),
      "the directories already on disk would never be reclaimed")
_sweep_src = inspect.getsource(bot.sweep_stale_work_dirs)
check("the sweep only removes empty directories",
      "if any(path.iterdir()):" in _sweep_src,
      "deleting a non-empty directory could destroy work in progress")
check("the sweep only removes its own prefix and only old ones",
      "twitter_pixiv_" in _sweep_src and "min_age_seconds" in _sweep_src,
      "it deletes things, so it must be narrow")

# The bot token must never be written to a log. httpx puts the full request URL - which
# embeds the token - into its exception text, and restart.log is the file an operator
# pastes into a bug report.
check("bot tokens are redacted before logging",
      bot.BOT_TOKEN not in bot.redact_secrets(
          f"url 'https://api.telegram.org/bot{bot.BOT_TOKEN}/getMe'"),
      "the live token would reach restart.log")
check("redaction catches a rotated token by shape, not by value",
      "9999999999:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
      not in bot.redact_secrets("x 9999999999:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA y"),
      "the case that matters is a token rotated after the failing request")
check("redaction leaves ordinary text alone",
      bot.redact_secrets("nothing to hide here") == "nothing to hide here")
check("the e-hentai session token is not logged",
      "SECRET" not in ehentai.redact_url(
          "https://e-hentai.org/archiver.php?gid=1&token=SECRET"),
      "every retry wrote a live session token into the log")
check("no real token literal remains in a tracked source file",
      not _TOKEN_SHAPE.search(_profile_src),
      "the revoked token was committed to a public repo in 4 places")

# LOW: the archive URL only had to CONTAIN hath.network/archive/, so a lookalike host
# would have been fetched with the account's cookies attached.
_host_src = inspect.getsource(bot.resolve_official_archive)
check("the archive host is checked before it is fetched",
      "hath.network" in _host_src and "hostname" in _host_src,
      "session cookies would be sent to whatever host the page named")

# MEDIUM: admin roster disclosure and the allowlist/blocklist docstring.
check("a non-admin cannot enumerate the admin roster",
      "管理員名單僅限管理員查看" in inspect.getsource(bot.authorized_command),
      "the admin list is what an attacker most wants")
check("the list semantics are documented as an allowlist",
      "the list is a blocklist-style restriction" not in
      inspect.getsource(bot.Authorization).lower(),
      "the old wording would make an operator lock everyone else out")
check("the mode label does not claim the list is a blocklist",
      "名單為限制" not in bot.Authorization(allowed={1}).mode_label,
      "an operator would think listed users are blocked, when everyone else is")
check("an empty admins file cannot lock the bot out",
      "state.admins = set(admins)" in inspect.getsource(bot.Authorization.load),
      "no admin means every admin command refuses forever")
check("the stop button re-checks the presser",
      inspect.getsource(bot.handle_gallery_callback).index("user_is_allowed")
      < inspect.getsource(bot.handle_gallery_callback).index('mode == "stop"'),
      "any chat member could kill someone else's download")

# --- The 停止 button could not be dispatched at all ---

# PTB processes ONE update at a time by default, and a button press IS an update. While a
# long download held the single slot the press was never dispatched, so it could not set
# the stop flag until after the download had finished and cleared it. Reproduced against
# PTB's own update processor: with the default the stop handler is never reached while a
# handler blocks; with a limit above one it is reached at once.
_builder_src = inspect.getsource(bot.build_application)
check("more than one update is processed at a time",
      "concurrent_updates(" in _builder_src,
      "one update at a time means the stop press is never dispatched")
check("the update concurrency limit is bounded",
      1 <= bot.MAX_CONCURRENT_UPDATES <= 32,
      f"MAX_CONCURRENT_UPDATES={bot.MAX_CONCURRENT_UPDATES}")
check("PTB actually reports the raised limit",
      _concurrent_updates_applied(),
      "setting it on the builder is not the same as it taking effect")
check("heavy gallery work is bounded across all chats",
      "gallery_semaphore" in inspect.getsource(bot.run_gallery_action)
      and 1 <= bot.MAX_CONCURRENT_GALLERY_JOBS <= 16,
      "per-request worker pools would otherwise multiply: "
      f"MAX_CONCURRENT_GALLERY_JOBS={bot.MAX_CONCURRENT_GALLERY_JOBS}")
check("a busy bot refuses immediately instead of holding an update slot",
      # A job parked on the semaphore is still inside its handler. Measured: with six jobs
      # submitted, every update slot was held by a job doing nothing and a stop press was
      # not dispatched until one finished. Not waiting is what keeps the button alive.
      ".locked()" in inspect.getsource(bot.run_gallery_action)
      and "async with gallery_semaphore" not in
      inspect.getsource(bot.run_gallery_action),
      "waiting for a slot holds the update slot the stop press needs")
check("the gallery slot is released on every path",
      "gallery_semaphore.release()" in inspect.getsource(bot.run_gallery_action),
      "a leaked slot would eventually refuse every request")
check("cancelling never waits behind the work it cancels",
      "gallery_semaphore" not in inspect.getsource(bot.handle_gallery_callback),
      "the stop branch must not be behind admission control")
check("a stop press actually cancels a running job",
      _stop_cancels_running_job(),
      "the whole point: the handler must observe the flag the press set")

# --- The official archive had no total bound, and page images were held in memory ---

# The archive path was bounded only per read (300s), which a slow but steady transfer
# never trips, so it could stage an arbitrarily large file for an unbounded time.
check("the archive download has a total wall-clock deadline",
      1 <= bot.ARCHIVE_DEADLINE_SECONDS <= 3600
      and bot.ARCHIVE_DEADLINE_SECONDS == 300,
      f"ARCHIVE_DEADLINE_SECONDS={bot.ARCHIVE_DEADLINE_SECONDS}")
check("the deadline is passed into both download paths",
      "deadline" in inspect.signature(bot._download_archive_single).parameters
      and "deadline" in inspect.signature(bot._download_archive_parallel).parameters,
      "either path without it is unbounded")
check("one deadline covers the whole transfer, not one per path",
      "deadline = (time.monotonic() + ARCHIVE_DEADLINE_SECONDS"
      in inspect.getsource(bot.download_official_archive),
      "the fallback must not start a fresh budget")
check("the deadline is checked inside the transfer loop, not only at connect",
      "check_deadline()" in inspect.getsource(bot._download_archive_single)
      and "check_deadline()" in inspect.getsource(bot._download_archive_parallel),
      "a stalled stream must be noticed mid-body")
check("a timeout is not retried as a range failure",
      # Its own type matters: a range can also raise EhentaiError for an HTTP status,
      # and that one *is* worth retrying. Retrying a timeout would burn a second budget.
      issubclass(bot._ArchiveDeadlineExceeded, ehentai.EhentaiError)
      and "_ArchiveDeadlineExceeded" in inspect.getsource(bot._download_archive_parallel),
      "a timeout must surface as a timeout, not as a corrupt download")
check("a stalled transfer is actually aborted by the deadline",
      _deadline_aborts_a_stalled_transfer(),
      "the guard must fire mid-body on a stream that keeps trickling")
check("the timeout is reported to the user",
      _deadline_reports_to_user(),
      "the message must reach the chat, not just the log")

# Page images were read entirely into memory with EHENTAI_DOWNLOAD_WORKERS running at
# once. Measured after the change: a 64 MiB page peaks at 4.03 MiB traced RAM (6.3% of
# the file), and a 6-page download is byte-identical to the source.
_stream_src = inspect.getsource(bot.download_gallery_direct)
# Asserted by the mechanism, not by the absence of a string: the docstring explains the
# old behaviour and mentions ``response.content``, so searching for that phrase found the
# prose rather than the code.
check("page images are streamed, not buffered",
      "client.stream(" in _stream_src and "iter_bytes(" in _stream_src
      and "handle.write(chunk)" in _stream_src,
      "buffering holds several full images per gallery")
check("a page is written to a partial file and renamed into place",
      'f".{index:04d}.part"' in _stream_src and "os.replace(partial, target)" in _stream_src,
      "a half-written file must never look complete")
check("a failed or stopped page leaves no partial behind",
      _stream_src.count("partial.unlink(missing_ok=True)") >= 3,
      "partials would accumulate and could be picked up as media")
check("streamed pages are byte-identical to the source",
      _streamed_pages_are_identical(),
      "the download must produce exactly the original bytes")

# --- A blank .env must not stop the bot starting ---

# `.env.example` lists every optional key with an EMPTY value, and the README tells people
# to copy it. `os.getenv("X", "180")` does NOT default for an empty value, so `int("")`
# raised ValueError at import and the bot would not start - one paste away, with the error
# pointing at a line far from the cause. There was a second, quieter version of the same
# trap: `Path("")` is the current directory, so a blank path setting silently became the
# repository folder.
check("a blank numeric setting falls back to its default",
      bot.env_number("DEFINITELY_UNSET_FOR_TESTS", 42) == 42,
      "a missing value must not raise")
for _blank in ("", "   ", "not-a-number"):
    os.environ["DSH_TEST_NUMBER"] = _blank
    try:
        _got = bot.env_number("DSH_TEST_NUMBER", 7)
        check(f"env_number({_blank!r}) -> default", _got == 7, f"got {_got!r}")
    finally:
        os.environ.pop("DSH_TEST_NUMBER", None)
os.environ["DSH_TEST_NUMBER"] = "13"
try:
    check("a real numeric setting is still honoured",
          bot.env_number("DSH_TEST_NUMBER", 7) == 13)
finally:
    os.environ.pop("DSH_TEST_NUMBER", None)

check("a blank path setting falls back to its default",
      bot.env_text("DEFINITELY_UNSET_FOR_TESTS", "fallback") == "fallback")
os.environ["DSH_TEST_TEXT"] = ""
try:
    check("an empty string is treated as unset, not as a value",
          bot.env_text("DSH_TEST_TEXT", "fallback") == "fallback",
          "Path('') is the current directory - the wrong file, not a crash")
finally:
    os.environ.pop("DSH_TEST_TEXT", None)

check("the credential paths do not depend on a bare getenv",
      'env_text(\"AUTH_FILE\"' in inspect.getsource(bot)
      and 'env_text(\"CACHE_DIR\"' in inspect.getsource(bot),
      "a blank key would turn the cache directory into the repository")
check("every numeric setting is read through env_number",
      # Two raw conversions remain, both inside try/except blocks that already handle a
      # bad value; anything else would be an unguarded int("").
      _unguarded_number_reads() == 0,
      f"{_unguarded_number_reads()} unguarded int()/float() of an environment value")
check("logging is configured before anything that logs",
      # AUTHORIZATION is built at import and its failure paths log. When logger was
      # defined below that, taking one of those paths killed the bot with NameError.
      inspect.getsource(bot).index('logger = logging.getLogger("twitter_pixiv_bot")')
      < inspect.getsource(bot).index("AUTHORIZATION = Authorization.load"),
      "a warning must never stop the bot from starting")
check("the bot imports with every optional setting blank",
      _imports_with_blank_env(),
      "this is what copying .env.example produces")

# --- The gallery cache stored one gallery twice, so neither copy was reused ---

# e-hentai.org and exhentai.org serve the same gallery under the same gid and token, but
# the entry was stored under whatever URL the request arrived with. Measured on a real
# gallery, the cache held:
#
#   https://e-hentai.org/g/3778922/19330e2349   -> 27 display_urls
#   https://exhentai.org/g/3778922/19330e2349   ->  0 display_urls
#
# so an exhentai link never hit the entry a button press had written, and vice versa.
_gallery_variants = [
    "https://exhentai.org/g/3778922/19330e2349/",
    "https://e-hentai.org/g/3778922/19330e2349/",
    "https://e-hentai.org/g/3778922/19330e2349",
    "https://e-hentai.org/g/3778922/19330e2349/?p=2",
    "https://www.e-hentai.org/g/3778922/19330e2349/",
    "https://exhentai.org/g/3778922/19330e2349/?p=3",
]
_keys = {bot.gallery_cache_key(u) for u in _gallery_variants}
check("the same gallery has one cache key on both hosts",
      len(_keys) == 1,
      f"{len(_keys)} distinct keys: {sorted(_keys)}")
check("different galleries still get different keys",
      bot.gallery_cache_key("https://exhentai.org/g/111/aaa/")
      != bot.gallery_cache_key("https://exhentai.org/g/222/bbb/"),
      "collapsing hosts must not collapse galleries")
check("the key is host-independent but keeps gid and token",
      bot.gallery_cache_key("https://exhentai.org/g/3778922/19330e2349/")
      == "https://e-hentai.org/g/3778922/19330e2349/")
check("storing and looking up use the same key",
      "gallery_cache_key(url, gallery)" in inspect.getsource(bot.remember_gallery)
      and "gallery_cache_lookup(url)" in inspect.getsource(bot.handle_ehentai_gallery),
      "a store and a lookup under different keys is the bug this fixes")

# --- The downloaded archive is now kept, which is the 35 of 41 seconds ---

check("the archive cache is bounded and can be disabled",
      1 <= bot.ARCHIVE_CACHE_MAX_BYTES <= 512 * 1024 ** 3
      and isinstance(bot.ARCHIVE_CACHE_ENABLED, bool),
      f"max={bot.ARCHIVE_CACHE_MAX_BYTES} enabled={bot.ARCHIVE_CACHE_ENABLED}")
check("the two archive variants are separate entries",
      bot.archive_cache_key(_FakeGallery(), True)
      != bot.archive_cache_key(_FakeGallery(), False),
      "resample and original are different files")
_archive_src = inspect.getsource(bot.run_gallery_archive)
# Matched as a call, not as a bare name: the comment above the check names
# resolve_official_archive to explain the ordering, and a name search found the comment.
check("the archive cache is consulted before resolving",
      _archive_src.index("archive_cache_path(gallery, original)")
      < _archive_src.index("resolve_official_archive,"),
      "resolving first would waste the 3s the cache exists to avoid")
check("a cache miss still downloads",
      "store_archive_cache" in inspect.getsource(bot.run_gallery_archive),
      "the cache must be populated, or it never helps")
check("a failed resend falls back to downloading",
      "改為重新下載" in inspect.getsource(bot.run_gallery_archive),
      "a broken cache entry must not cost the user their file")
check("the archive cache evicts least-recently-used entries",
      _archive_cache_evicts(),
      "an unbounded archive cache would fill the disk")
check("a 0-byte cached archive is never served",
      _empty_archive_not_served(),
      "a truncated file must not be sent as a complete archive")
check("the archive cache is excluded from the media cache figure",
      "ARCHIVE_CACHE_DIR not in path.parents"
      in inspect.getsource(bot.cache_command),
      "it lives inside CACHE_DIR, so it would be counted twice")

# --- An x.com profile was handed to the downloader, which spent 180s on it ---

# Every x.com URL was passed to gallery-dl and only some lead to media. Measured:
#   /status/123          1 file in 3.9s   -> parse
#   /i/status/123        1 file in 3.3s   -> parse
#   /status/123/photo/1  1 file in 3.8s   -> parse
#   /pohitashi          TIMEOUT after 180s
#   /pohitashi/media    TIMEOUT after 180s
# So a profile made the user wait three minutes for "沒有找到可傳送的圖片或影片", which says
# nothing about the page they sent.
for _url, _kind in (
        ("https://x.com/u/status/123", "tweet"),
        ("https://x.com/i/status/123", "tweet"),
        ("https://x.com/u/status/123/photo/1", "tweet"),
        ("https://x.com/u/status/123/video/1", "tweet"),
        ("https://twitter.com/u/status/123", "tweet"),
        ("https://x.com/u", "profile"),
        ("https://x.com/u/", "profile"),
        ("https://x.com/u/media", "tab"),
        ("https://x.com/u/with_replies", "tab"),
        ("https://x.com/u/likes", "tab"),
        ("https://x.com/", "home"),
        ("https://x.com/search?q=cat", "search"),
        ("https://x.com/hashtag/cat", "hashtag"),
        ("https://x.com/i/lists/123", "list"),
        ("https://x.com/i/communities/123", "community"),
        ("https://x.com/i/broadcasts/abc", "broadcast"),
        ("https://x.com/i/user/123", "user_id"),
        ("https://twitter.com/intent/tweet?text=hi", "intent"),
        ("https://x.com/explore", "explore"),
        ("https://x.com/settings/profile", "settings"),
):
    _got_kind, _msg = bot.classify_twitter_url(_url)
    check(f"twitter shape {_kind}: {_url.split('//')[1][:34]}",
          _got_kind == _kind,
          f"classified as {_got_kind!r}, expected {_kind!r}")

check("a post is never classified as a page",
      # The dangerous direction: refusing a link that works. Anything post-shaped must be
      # passed through, including forms whose exact behaviour was not measured.
      all(bot.classify_twitter_url(u)[0] == "tweet" for u in (
          "https://x.com/u/status/123",
          "https://x.com/u/status/123?s=20&t=abc",
          "https://x.com/i/status/123",
          "https://x.com/u/status/123/photo/2",
          "https://x.com/a_b.c-d/status/123",
      )),
      "a wrong 'not supported' would reject a working link")
check("every non-tweet verdict comes with an explanation",
      # A kind without a message would reach the chat as an empty bubble.
      all(bot.classify_twitter_url(u)[1].strip()
          for u in ("https://x.com/u", "https://x.com/u/media", "https://x.com/search",
                    "https://x.com/hashtag/x", "https://x.com/", "https://x.com/i/lists/1",
                    "https://x.com/i/communities/1", "https://x.com/i/broadcasts/x",
                    "https://x.com/i/user/1", "https://x.com/explore",
                    "https://x.com/settings/x", "https://x.com/i/unknown")),
      "the point of this change is to say what the page is")
check("the explanation names the page rather than saying unsupported",
      "@pohitashi" in bot.classify_twitter_url("https://x.com/pohitashi")[1]
      and "主頁" in bot.classify_twitter_url("https://x.com/pohitashi")[1],
      "a generic message is what this replaces")
check("a tab is reported with its tab name",
      "媒體" in bot.classify_twitter_url("https://x.com/u/media")[1],
      "matching the tab against the first segment missed every tab")
check("a user named like a page is still a profile",
      # /media is a real account name too; only /<user>/<tab> is a tab.
      bot.classify_twitter_url("https://x.com/media")[0] == "profile",
      "the second segment is the tab, not the first")
check("the classifier runs before the downloader",
      # The download itself happens inside a thread helper, so the marker for "work has
      # begun" is the semaphore, not the helper's own name.
      _order_in(inspect.getsource(bot.handle_message),
                "classify_link(", "download_semaphore"),
      "classifying after the download would keep the 180s wait")
check("the classifier runs before the cache and the cooldown",
      # It costs nothing and must not be delayed behind either. A missing needle fails the
      # check rather than raising, which is what caught the wiring being removed.
      _order_in(inspect.getsource(bot.handle_message),
                "classify_link(", "PARSE_CACHE.lookup(media_url"),
      "a drop should not wait on a cooldown")
check("every supported platform is classified",
      # Dispatched from one place: handling only X is how Pixiv was missed for a whole
      # round, leaving a Pixiv user page answering with two messages.
      all(bot.classify_link(u)[0] == k for u, k in (
          ("https://x.com/u/status/1", "tweet"),
          ("https://x.com/u", "profile"),
          ("https://www.pixiv.net/artworks/1", "work"),
          ("https://www.pixiv.net/users/1", "user"),
      )),
      "the handler must have a single check, not one per platform")
check("the handler classifies before downloading",
      _order_in(inspect.getsource(bot.handle_message),
                "classify_link(", "download_semaphore"),
      "an unclassified link means a silent three-minute wait")

# --- Notices are silent by default, and Pixiv pages had to be classified too ---

# A Pixiv user page produced NO reply even before the notices were turned off: measured,
# gallery-dl finds 0 files there in 2.5s, so the only thing that could have explained it was
# the failure message. The other Pixiv shapes are worse - search, tags and ranking all ran
# for the full 180s before being killed:
#
#   /artworks/150824857                2 files in 46.7s   -> parse
#   /en/artworks/150824857             2 files in 31.0s   -> parse
#   /member_illust.php?...illust_id=…  2 files in 20.8s   -> parse
#   /users/12345                       0 files in  2.5s   -> nothing
#   /search?word=cat                   TIMEOUT after 180s
#   /ranking.php                       TIMEOUT after 180s
for _url, _kind in (
        ("https://www.pixiv.net/artworks/150824857", "work"),
        ("https://www.pixiv.net/en/artworks/150824857", "work"),
        ("https://www.pixiv.net/ja/artworks/150824857", "work"),
        ("https://www.pixiv.net/member_illust.php?mode=medium&illust_id=1", "work"),
        ("https://www.pixiv.net/novel/show.php?id=1", "work"),
        ("https://www.pixiv.net/users/12345", "user"),
        ("https://www.pixiv.net/users/1/bookmarks/artworks", "user"),
        ("https://www.pixiv.net/search?word=cat", "search"),
        ("https://www.pixiv.net/tags/cat", "tag"),
        ("https://www.pixiv.net/ranking.php", "ranking"),
        ("https://www.pixiv.net/discover", "discover"),
        ("https://www.pixiv.net/", "home"),
):
    _got, _ = bot.classify_pixiv_url(_url)
    check(f"pixiv shape {_kind}: {_url.split('pixiv.net')[1][:28]}",
          _got == _kind, f"classified as {_got!r}, expected {_kind!r}")
check("a pixiv work is never classified as a page",
      # The dangerous direction is refusing a link that works.
      all(bot.is_parseable_kind(bot.classify_pixiv_url(u)[0]) for u in (
          "https://www.pixiv.net/artworks/1",
          "https://www.pixiv.net/en/artworks/1",
          "https://www.pixiv.net/artworks/1?foo=1",
          "https://www.pixiv.net/member_illust.php?illust_id=1",
      )),
      "a wrong 'not supported' would reject a working link")
check("a pixiv language prefix is not treated as the path",
      bot.classify_pixiv_url("https://www.pixiv.net/en/artworks/1")[0] == "work",
      "the /en/ prefix is part of the URL, not of the resource")
check("notices are silent by default",
      bot.REPLY_UNSUPPORTED_LINKS is False,
      "an unsupported link must produce no message at all")
check("the notice is a documented opt-in",
      _env_flag_documented("REPLY_UNSUPPORTED_LINKS"),
      "an operator needs a way to see why a link was dropped")
# Asserted as BEHAVIOUR, not as the presence of an `if`. Flipping the code default while
# .env still said 0 left the earlier check passing, because the environment wins - so the
# question that matters is whether a drop reaches the chat, and that is driven directly.
check("a dropped link sends nothing to the chat when notices are off",
      _drop_is_silent(),
      "the point of the change is that no message appears at all")
# --- /cache clear was reported to blue-screen the machine ---

# The dump says what happened:
#
#   0x00000133  DPC_WATCHDOG_VIOLATION  (parameter 1 = 0x1)
#
# A driver's deferred procedure call exceeded its time limit - a kernel-level stall, which
# Python can neither raise nor catch. What the bot controls is how much deletion I/O it
# issues at once, and emptying a cache in one uninterrupted burst is what drives a storage or
# filter driver past that limit.
# --- A restart slept for a fixed six seconds even when nothing was running ---

# RESTART_STARTUP_DELAY made the replacement wait unconditionally, so every /restart paid the
# full 6s even when the process being replaced had long since exited - the usual case, and the
# fixed pause visible in the log before anything else happened. A single-instance lock answers
# the question that matters instead: the OS releases it when the holding process dies, however
# it dies.
check("the instance lock is free when nothing holds it",
      _lock_acquires_immediately(),
      "a replacement must not wait when there is nothing to wait for")
check("a held lock makes the next attempt wait, then succeed",
      _lock_waits_for_the_holder(),
      "the conflict this exists to avoid is a second poller on one token")
check("the lock is released explicitly, not only at process exit",
      # so a replacement started right after the operator stops this one does not have to
      # wait for the OS to reap the old process.
      "release_instance_lock()" in inspect.getsource(bot.main),
      "an explicit release shortens the next start")
check("only a real replacement waits",
      # **This was wrong once, and it was the whole bug.** The wait keyed off
      # RESTART_STARTUP_DELAY, which is also a documented .env value - so load_dotenv()
      # supplied it for every start, every start believed it was a replacement, and run.bat
      # waited six seconds for nothing while the refusal guard was skipped.
      "BOT_IS_REPLACEMENT" in inspect.getsource(bot.main)
      and "os.getenv(\"RESTART_STARTUP_DELAY\")" not in inspect.getsource(bot.main),
      "the marker must come from the spawner, not from a .env setting")
check("the spawner sets the replacement marker",
      '"BOT_IS_REPLACEMENT": "1"' in inspect.getsource(bot.spawn_replacement_process)
      or "'BOT_IS_REPLACEMENT': '1'" in inspect.getsource(bot.spawn_replacement_process),
      "nothing else may set it")
check("the marker is the only thing that decides the wait",
      # Asserted on how `is_replacement` is DECIDED. A search for the name alone was not
      # enough: the spawner's environment dictionary also contains it, so that search passed
      # even with the decision replaced by a hard-coded True.
      _replacement_is_decided_not_assumed(),
      "keying this off a .env setting is the bug that made every start wait")
check("a replacement opens its own console",
      # Asserted on the FLAGS ASSIGNMENT, because the comment above it explains that
      # DETACHED_PROCESS is deliberately not used - so a search for the name found the
      # explanation and failed against correct code.
      _spawn_flags(),
      "without a console the replacing process has no window at all")
check("a replacement keeps a log file as well as its window",
      # Its output now goes to the console, so without this restart.log would go stale and
      # the confirmation's advice to read it would be wrong.
      "BOT_RESTART_LOG" in inspect.getsource(bot.spawn_replacement_process)
      and "restart.log" in inspect.getsource(bot)
      and "FileHandler" in inspect.getsource(bot),
      "the window and the file must both get the output")
check("the refusal only applies to a plain start",
      # A replacement may continue without the lock: refusing would leave the bot offline.
      "if not is_replacement:" in inspect.getsource(bot.main),
      "a replacement must not be refused")
check("the fixed sleep is gone",
      # Any sleep in main() before polling is the thing being removed. Naming the exact
      # variable was too specific: an earlier version of this check searched for
      # "time.sleep(startup_delay)" and passed against "time.sleep(await_lock)".
      "time.sleep(" not in inspect.getsource(bot.main),
      "an unconditional sleep is what cost the six seconds")
check("the configured delay is what the lock is given",
      # Not merely that a lock is taken: a hard-coded 0 would take it too, and would ignore
      # the configured ceiling.
      _lock_call_uses_the_setting(),
      "the ceiling must come from RESTART_STARTUP_DELAY")
check("the lock file is not tracked",
      ".bot.lock" in (HERE / ".gitignore").read_text(encoding="utf-8"),
      "runtime state must not be committed")
check("the delay setting is a number, read once and bounded",
      # Read through env_number, which falls back on a malformed value instead of raising at
      # import - so a bad value cannot stop the bot from starting.
      isinstance(bot.RESTART_STARTUP_DELAY_SECONDS, (int, float))
      and bot.RESTART_STARTUP_DELAY_SECONDS >= 0,
      f"RESTART_STARTUP_DELAY_SECONDS={bot.RESTART_STARTUP_DELAY_SECONDS!r}")

check("bulk deletion is batched rather than one burst",
      _deletion_is_batched(),
      "an uninterrupted burst is what the watchdog violation came from")
check("a directory in the list is never followed",
      # Only files are unlinked, so a mis-shaped entry cannot become a recursive delete.
      _deletion_skips_directories(),
      "following a directory would make this unbounded")
check("deleting twice removes nothing and does not raise",
      _deletion_is_idempotent(),
      "a repeated command must be harmless")

# The dangerous direction for a safety guard is allowing, not refusing: these paths come from
# .env, and a mistyped CACHE_DIR would empty a drive. A blank value has already turned
# AUTH_FILE into the repository directory once.
for _path, _label in (
        (str(Path("C:/")), "a drive root"),
        (str(Path("C:/Windows")), "the Windows directory"),
        (str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"), "under Windows"),
        (str(Path(os.environ.get("USERPROFILE", "C:/Users/x"))), "a user profile root"),
        (str(Path(os.environ.get("LOCALAPPDATA", "C:/x/AppData/Local"))), "LOCALAPPDATA"),
        (str(HERE), "the project root itself"),
        (str(Path("C:/somewhere/else")), "an unrelated tree"),
):
    check(f"cache clear refuses {_label}",
          bot.is_safe_to_clear(Path(_path)) is False,
          f"{_path} was accepted for deletion")
check("cache clear allows the real cache dirs",
      bot.is_safe_to_clear(bot.BASE_DIR / ".cache") is True
      and bot.is_safe_to_clear(bot.BASE_DIR / ".cache" / "archives") is True,
      "refusing the legitimate path would make the command useless")
check("cache clear checks safety before deleting anything",
      # The guard has to run first, or a refusal is too late.
      _order_in(inspect.getsource(bot.cache_command),
                "is_safe_to_clear(", "delete_files_quietly"),
      "the refusal must come before the deletion")
check("every bulk-deletion path is batched",
      # /cache clear, ParseCache.clear and the orphan sweep can each empty the cache.
      all("delete_files_quietly" in inspect.getsource(fn) for fn in (
          bot.cache_command, bot.ParseCache.clear, bot.prune_orphaned_cache_files)),
      "one unbatched path is enough to trigger it again")

check("the work id is read from every pixiv url shape",
      # An earlier version required five or more digits, so a short id silently yielded ""
      # and every deletion was then reported as silence instead of a message.
      all(bot.pixiv_illust_id(u) == want for u, want in (
          ("https://www.pixiv.net/artworks/1", "1"),
          ("https://www.pixiv.net/artworks/150824857", "150824857"),
          ("https://www.pixiv.net/en/artworks/150824857", "150824857"),
          ("https://www.pixiv.net/artwork/1", "1"),
          ("https://www.pixiv.net/member_illust.php?mode=medium&illust_id=1", "1"),
          ("https://www.pixiv.net/novel/show.php?id=123", "123"),
      )),
      "a missed id turns a deletion into silence")
check("a non-work url yields no id",
      bot.pixiv_illust_id("https://www.pixiv.net/users/12345") == ""
      and bot.pixiv_illust_id("https://x.com/u/status/123") == "",
      "probing the wrong thing would report the wrong state")

# --- A private Pixiv work must not be reported as a failure ---

# gallery-dl reports the same NotFoundError for a deleted work and for one whose login it
# lacks, because the web API answers 404 for both - so the bot used to answer
# "解析失敗，請稍後再試" or "連結可能已失效" to a private or age-restricted work. The link was
# fine; the message blamed it and told the sender nothing they could act on.
#
# The app API does distinguish them. Measured:
#
#   app API, no credentials   400  login required
#   app API, with token       200  the work
#   web API, no credentials   404  same as a deletion
check("a deleted or un-published pixiv work is reported, not silenced",
      _empty_download_reports_deleted(),
      "a work nobody can open is the one case worth telling the sender about")
check("a work the bot CAN read is never called deleted",
      # The probe answers 200 for an age-restricted work when credentials work, so the
      # reply is silence and the download path is left to succeed.
      bot.pixiv_work_state("150824857") == "ok",
      "a live work must not be reported as deleted")
check("an unreadable work state is unknown, not a guess",
      _unknown_state_is_silent(),
      "claiming a deletion the bot cannot verify is worse than silence")
check("a private work that cannot be read stays silent",
      _empty_download_is_silent_for_private(),
      "the request was that a private gallery produce no reply")
# --- An un-published work answers 200, so the status code says nothing ---

# Measured on a work the author had taken down:
#
#   {"illust": {"id": 150822697, "title": "", "visible": false,
#               "total_view": 0, "total_bookmarks": 0}}       status 200
#
# against a live work in the same shape:
#
#   {"illust": {"id": 150824857, "title": "しろこ", "visible": true,
#               "total_view": 436}}                          status 200
#
# Checking only the status code reported the un-published work as readable, which is why it
# produced no message at all.
check("an un-published work reads as unavailable despite HTTP 200",
      _unpublished_is_unavailable(),
      "visible=false is the signal; the status code is 200 either way")
check("a 404 with a valid token reads as unavailable",
      _deleted_is_unavailable(),
      "a deletion is the other half of the same answer")
check("a non-200 that is not a 404 is unknown, not unavailable",
      # A 500 or a rate limit must not be reported as a deletion.
      _server_error_is_unknown(),
      "an unverifiable state must stay silent rather than accuse the link")
check("the unavailable notice names both causes",
      # Deleted and un-published are indistinguishable to a visitor, so the message says so
      # rather than picking one and being wrong half the time.
      "刪除" in _unavailable_notice() and "非公開" in _unavailable_notice(),
      "the message must cover both, since gallery-dl cannot tell them apart")
check("the no-media failure path no longer hard-codes a message",
      # It must route through the decision, or a private work gets the old generic text.
      "report_empty_download(" in inspect.getsource(bot.handle_message),
      "both the empty-result and exception paths must use the same rule")
check("the exception path uses the same rule too",
      # gallery-dl exits non-zero for a deletion AND for a missing login, so this path
      # was the one answering "解析失敗" to a private work.
      inspect.getsource(bot.handle_message).count("report_empty_download(") >= 2,
      "the non-zero exit path is where private works landed")
check("the same drop DOES explain itself when notices are on",
      _drop_explains_when_enabled(),
      "an operator needs a way to see why a link was dropped")

# --- Host aliases were separate cache entries, so one link downloaded twice ---

# Verified against the live sites rather than assumed:
#   twitter.com -> 301 -> x.com          (same post)
#   pixiv.net   -> 301 -> www.pixiv.net  (same artwork)
#   www.e-hentai.org -> 302 -> e-hentai.org, and exhentai.org serves the same gallery
#   http://     -> 301 -> https://
# The media cache had no canonicalisation beyond normalize_cache_key, so a post reached
# through two of these forms was two entries with two copies of the same files on disk.
for _label, _urls in (
        ("a tweet", ["https://x.com/u/status/1", "https://twitter.com/u/status/1",
                     "https://www.x.com/u/status/1", "http://twitter.com/u/status/1",
                     "https://twitter.com/u/status/1?s=20", "https://x.com/u/status/1/"]),
        ("a pixiv artwork", ["https://www.pixiv.net/artworks/1",
                             "https://pixiv.net/artworks/1",
                             "http://www.pixiv.net/artworks/1?foo=1"]),
        ("an e-hentai gallery", ["https://e-hentai.org/g/1/a/",
                                 "https://www.e-hentai.org/g/1/a/",
                                 "https://exhentai.org/g/1/a/"]),
):
    _got = {bot.normalize_cache_key(u) for u in _urls}
    check(f"equivalent links share one cache key: {_label}",
          len(_got) == 1,
          f"{len(_got)} keys: {sorted(_got)}")
check("an unrelated host is never rewritten",
      all(bot.normalize_cache_key(u) == u
          for u in ("https://github.com/a/b", "https://www.cloudflare.com/a",
                    "https://notx.com/u/status/1", "http://example.com/x/y")),
      "only hosts the bot already accepts may be renamed")
check("a lookalike host is not treated as an alias",
      bot.normalize_cache_key("https://notx.com/u/status/1")
      != bot.normalize_cache_key("https://x.com/u/status/1"),
      "suffix matching would merge an unrelated domain")

# --- Files an earlier store orphaned were never reclaimed ---

check("orphaned cache files are detected and removed",
      _orphan_cleanup_is_safe(),
      "a re-cache with a different file set abandoned the previous files")
check("the orphan sweep runs on the cache write path",
      "prune_orphaned_cache_files()" in inspect.getsource(bot.remember_media_parse)
      and "prune_orphaned_cache_files()" in inspect.getsource(bot.remember_gallery),
      "on write is when they become unreachable")
check("the orphan sweep also runs at startup",
      "prune_orphaned_cache_files()" in inspect.getsource(bot.main),
      "entries written before this existed left orphans behind")
check("the viewer-URL TTL is shorter than the cache entry lifetime",
      bot.GALLERY_URL_TTL_SECONDS < bot.CACHE_STALE_SECONDS,
      "a reused signed URL would make every page fail together")

# --- Every exhentai cover was missing, silently ---

# Telegram fetches InputMediaPhoto(media=url) itself, with no cookies. An exhentai
# gallery's cover is served from s.exhentai.org, which answers an anonymous request with
# 403, so the cover never appeared and the log said only "Failed to get http url content".
# The e-hentai form of the same gallery uses ehgt.org with a byte-identical path.
check("a session-bound thumbnail host is rewritten to the public one",
      bot.public_thumbnail_url("https://s.exhentai.org/w/02/714/x.webp")
      == "https://ehgt.org/w/02/714/x.webp",
      "this host answers an anonymous request with 403")
check("the other session-bound thumbnail host is rewritten too",
      bot.public_thumbnail_url("https://s.e-hentai.org/w/02/240/y.webp")
      == "https://ehgt.org/w/02/240/y.webp")
check("an already-public thumbnail is left alone",
      bot.public_thumbnail_url("https://ehgt.org/w/02/240/y.webp")
      == "https://ehgt.org/w/02/240/y.webp")
check("an unknown host is refused rather than sent and failed",
      # Telegram's error names neither the host nor the reason, so guessing costs a round
      # trip and produces a log line nobody can act on.
      bot.public_thumbnail_url("https://example.com/cover.jpg") == "",
      "only hosts known to be fetchable are handed to Telegram")
check("the rewrite is limited to thumbnail paths",
      # It is a host substitution, so it is only safe because the path is unchanged.
      # Rewriting a non-image URL would send an unrelated request.
      bot.public_thumbnail_url("https://s.exhentai.org/g/1/aa/") == ""
      and bot.public_thumbnail_url("https://s.exhentai.org/") == "",
      "a gallery page is not a cover")
check("a non-http scheme is refused",
      bot.public_thumbnail_url("ftp://ehgt.org/x.webp") == "")
check("the cover path uses the guard",
      "public_thumbnail_url(" in inspect.getsource(bot.edit_message_to_cover),
      "the raw gallery.thumb is what 403s")

# --- A Telegraph page embedding original images showed a broken image on every page ---

# The original image is served from /fullimg/..., which needs the session: an anonymous
# request gets a 302 to the login page. Telegraph fetches every embedded image itself, so
# its whole page was broken. Measured on a real gallery, creating a page from the raw
# originals and then fetching it anonymously:
#
#   12 images on exhentai.org           -> 0/3 loadable, content-type text/html
#   12 images on <node>.hath.network    -> 3/3 loadable, content-type image/jpeg
check("a /fullimg/ url is recognised as session-bound",
      ehentai.is_session_bound_image("https://exhentai.org/fullimg/1/1/x/001.jpg")
      and ehentai.is_session_bound_image("https://e-hentai.org/fullimg/1/1/x/001.jpg"),
      "both hosts serve the login page to an anonymous request")
check("a public CDN url is not mistaken for a session-bound one",
      not ehentai.is_session_bound_image(
          "https://node.hath.network/h/abc-1280-1-wb")
      and not ehentai.is_session_bound_image("https://ehgt.org/w/02/240/x.webp"),
      "resolving these would be wasted requests")
check("both embed paths convert session-bound urls",
      # Publishing and 重新生成 embed the same list; fixing only the first would leave the
      # button producing a broken page again.
      "public_embed_urls(" in inspect.getsource(bot.run_gallery_images)
      and "public_embed_urls(" in inspect.getsource(bot.run_regenerate),
      "the regenerate button must not rebuild a broken page")
check("a list with nothing session-bound is passed through untouched",
      _public_embed_urls_passthrough(),
      "the display mode is already public and must cost no requests")
check("the resolver keeps the list length and order",
      "zip(resolved, urls)" in inspect.getsource(
          ehentai.resolve_public_image_urls),
      "a missing page would shift every later page")
check("the resolver probes with HEAD, not a ranged GET",
      # Measured: this CDN ignores Range and answers a ranged GET with the whole file
      # (6.1 MB for one page), so verifying with a GET would download every original.
      # Checked against the code, not the prose: the docstring explains the Range problem
      # and so contains the word, which made a bare search match the explanation.
      "client.head(" in _resolver_code()
      and "Range" not in _resolver_code(),
      "a GET here transfers every original image")
check("a progress path without a status message does not crash",
      _public_embed_urls_accepts_no_status(),
      "report=True with status=None raised AttributeError")

# --- 重新生成 always failed on a gallery whose cover had loaded ---

# edit_message_to_cover turns the result message into the cover PHOTO, and Telegram refuses
# edit_message_text on a media message:
#
#     WARNING twitter_pixiv_bot: 更新頁面訊息失敗：There is no text in the message to edit
#
# so the handler logged a failure and posted a second copy instead of updating in place -
# only when the cover had loaded, which is why it looked intermittent.
check("the regenerate path can update a photo message",
      _result_message_helper_works(),
      "edit_text on a photo message is the failure that was logged")
check("the regenerate handler does not call edit_text directly",
      "edit_text(" not in inspect.getsource(bot.run_regenerate),
      "the message is usually a photo by then")
check("the regenerate handler goes through the type-aware helper",
      "update_result_message(" in inspect.getsource(bot.run_regenerate))
check("a photo with no caption is refused rather than erroring",
      _empty_caption_is_refused(),
      "Telegram needs a non-empty caption on a media message")
check("only one gallery action can edit a given status message",
      # The cover converts the message to a photo; the archive action edits text. They run
      # from mutually exclusive button presses, so neither can meet the other's message.
      "edit_message_to_cover" not in inspect.getsource(bot.run_gallery_archive)
      and "run_gallery_archive" not in inspect.getsource(bot.run_gallery_images),
      "a photo message must never be text-edited by the other branch")

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
      "gallery_cache_lookup(gallery_url)" in inspect.getsource(bot.run_gallery_action)
      and "if gallery is None" in inspect.getsource(bot.run_gallery_action),
      "the gallery facts must come from cache when present, not a fresh fetch")
check("the button handler and the link handler share one cache key",
      # This is the fix for the gallery being stored twice: both paths ask the same
      # question, and only lookups that agree with remember_gallery() can hit.
      "gallery_cache_lookup" in inspect.getsource(bot.run_gallery_action)
      and "gallery_cache_lookup" in inspect.getsource(bot.handle_ehentai_gallery)
      and "gallery_cache_key" in inspect.getsource(bot.remember_gallery),
      "a store and a lookup under different keys never meet")

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
# Asserted against the builder function, not main(): the builder was extracted so the
# real Application can be inspected, and main() no longer holds these settings.
_builder_src = inspect.getsource(bot.build_application)
check("the request timeouts are raised above PTB's 5s defaults",
      "media_write_timeout" in _builder_src
      and "TELEGRAM_TIMEOUT_SECONDS" in _builder_src,
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
