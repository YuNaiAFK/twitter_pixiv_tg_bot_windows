# X/Twitter + Pixiv Telegram Bot (Windows)

## Who can do what
The bot is meant to be public: anyone who can see it in a group can send a link
and get the result. Managing the bot is separate from that.

| Command | Who | Notes |
| --- | --- | --- |
| `/start`, `/help` | Anyone | Usage text only, no deployment detail |
| `/id` | Anyone | Shows the caller their own ID |
| `/authorized` | Admin or authorized | The current lists, the mode, and the file in use |
| `/grant <ID>` | Admin or authorized | Authorize a user. In a group with no ID: authorize **that group** |
| `/revoke <ID>` | Admin or authorized | Remove a user. In a group with no ID: remove **that group** |
| `/promote <ID>` | **Admin only** | Make a user an administrator |
| `/demote <ID>` | **Admin only** | Remove administrator rights |
| `/lock on\|off` | **Admin only** | Private-chat / user-list mode |
| `/lockgroup on\|off` | **Admin only** | Group-chat mode |
| `/cookies` | **Admin only** | Credential files, expiry dates, error strings |
| `/status` | **Admin only** | Limits, authorization state, runtime settings |
| `/cache` | **Admin only** | Parse-cache contents; `/cache clear` empties it |
| `/restart` | **Admin only** | Replaces the running process with a fresh copy |

**Parsing is open to everyone.** The authorized list is a *restriction*: an empty
list means anyone may parse, and adding someone to it does not grant them anything
they did not already have. `/revoke` is how a specific user is cut off without
locking the bot down for everybody.

### Authorizing a whole group
Run `/grant` **in the group**, with no argument, and that group is authorized — so
every member of it may parse, with no per-user grant. This is the practical way to
use it, since a chat ID is awkward to obtain by hand.

| Groups authorized | `/lockgroup off` (default) | `/lockgroup on` |
| --- | --- | --- |
| none | every group may parse | **no** group may parse |
| one or more | only those groups | only those groups |

**`/lockgroup` is separate from `/lock` on purpose.** `/lock` governs private chats
and the user list; it says nothing about groups. With only `/lock on`, every group
stayed wide open — the bot looked locked down in private chats while any group could
still use it. That is why there are two switches answering two different questions:

| Question | Switch |
| --- | --- |
| May people use it in private chats? | `/lock on\|off` |
| May groups use it? | `/lockgroup on\|off` |

`/lockgroup on` is **allowed** with an empty group list — that blocks every group
until one is authorized, and the reply says so. Gating first and then running
`/grant` in the group is the natural order, and blocking it would block the very
sequence that makes the feature usable:

```
1. /lockgroup on          → 守門模式; no group may parse yet
2. /grant   (in a group)  → that group is now the only one that may parse
3. any other group        → still refused
```

An administrator can always undo it with `/lockgroup off` in a private chat, so
there is no state this can reach that cannot be undone.

Three things worth knowing:

- **Managing lists inside a group is administrators only.** Group authorization is
  decided in a group, so if any authorized user could run it, any member of an
  authorized group could change the lists — self-service for everyone the bot
  already admits. In a private chat, authorized users keep the ability.
- **Administrators pass everywhere**, including in a group that is not listed, so
  an operator is never locked out of their own bot.
- **Private chats are unaffected by the group list and the group mode**, and groups
  are unaffected by the user list and `/lock`. A private chat has no group to speak
  for it, so the user rules apply there.

`/revoke` in a group removes that group; the reply notes when the list becomes empty
and every group is welcome again.

### Two modes, and the one thing to get right
These govern **private chats and the user list**:

| Mode | Empty list means | Populated list means |
| --- | --- | --- |
| **Open** (default, `/lock off`) | anyone may parse | only listed users may parse |
| **Locked** (`/lock on`) | **nobody** may parse | only listed users may parse |

An empty list meaning "nobody" is the dangerous reading, so it is never chosen for
you: the default is open, and `/lock on` is refused while no administrator or
authorized user exists. Administrators always pass in both modes, so a locked bot
can still be unlocked from inside it.

### Bootstrap, then manage in chat
The first administrator has to come from `.env`, because there is nobody to grant
it:

```ini
ADMIN_USER_IDS=123456789
```

After that, one restart and the rest is done from Telegram with `/promote`. That is
the point of the interface: applying an authorization change should not require
reaching the host.

Send `/id` to the bot to learn your own numeric ID. **Only numeric IDs are
accepted**, never `@username`: a username can be changed and re-registered by
someone else, so an authorization recorded against one would eventually point at a
different person.

**An empty `ADMIN_USER_IDS` does not mean "everyone is an admin".** It means the
admin commands are closed to all, and the owner is told how to enable them. On a
public bot the opposite default would hand `/cookies` to any stranger, which would
expose local file paths and credential state.

### Where the lists live
Changes are written to `authorized.json` next to `bot.py` and survive restarts,
including group chat IDs — which are negative, and of the `-100…` form for
supergroups. `.env` supplies the starting values; the file holds what the commands
changed.

```ini
AUTH_FILE=authorized.json
AUTH_LOCKED=0            # 1 to start with private chats gated
AUTH_GROUP_LOCKED=0      # 1 to start with groups gated
```

A corrupt or unreadable file falls back to the `.env` values and says so in the
log, rather than crashing or silently widening access. Every command that changes a
list reports a failed write in chat, so a change that will not survive a restart
does not look like one that will.

The file is gitignored: it holds real user IDs.

Administrator commands are left out of the published Telegram command menu, so the
menu does not advertise what a visitor cannot use. Administrators and authorized
users get a menu scoped to their own chat, which is how they discover the
management commands.

### Failure replies are one short line
A failure says what happened in a single sentence and nothing more:

```
取不到連結內容，可能是平台限制，請稍後再試。

原始連結：https://x.com/...
```

The technical diagnosis — credential state, the `bot.py --check --live` command
line, the exception type — is **written to the log instead of the chat**. It used
to be appended to the reply when an administrator asked, which meant an operator
saw a paragraph of shell commands in the middle of a conversation. The information
is not lost: it is in the console log and in `restart.log`, and `/cookies` reports
the credential state on demand.

A successful parse posts only its result, with no accompanying explanation.

The `@BotFather` privacy-mode step is deliberately **not** in any chat reply —
it is deployment detail. See *Using it in Telegram groups* below.

### /restart
Restarts the bot without host access, which is the point: applying a change to
`.env` or to a credential file otherwise means reaching the machine. It is
administrator only, because it briefly takes the bot offline.

What it does:

1. Starts a second copy of `bot.py`, **detached** so it does not belong to the
   console that started the current one. The copy waits a few seconds before it
   starts polling, so it does not race the process it is replacing for Telegram's
   `getUpdates` lock.
2. Replies with the new process's PID and the log path.
3. Stops polling after a short delay, letting that reply reach Telegram first and
   allowing the in-flight update to finish.

The new copy writes to `restart.log` next to `bot.py`, so read that if the bot does
not come back. A failed spawn is reported in the chat and **the current process
keeps running** — a restart that cannot happen must not look like one that did.

```ini
RESTART_STARTUP_DELAY=6      # seconds a replacement waits before polling
RESTART_LOG_MAX_MB=2         # restart.log is rotated past this size
```

Two failure modes were hit while building this, both fixed and both worth
remembering:

- **`stop_running()` was called from the wrong thread, so it did nothing.** The
  first version ran the shutdown on a worker thread, but
  `Application.stop_running()` is implemented as
  `asyncio.get_running_loop().stop()` — and a worker thread has no running loop, so
  it raised `RuntimeError` and the old process kept polling. The replacement then
  died with *Conflict: terminated by other getUpdates request*, and the bot was left
  **offline**. The request is now handed to the loop with `call_soon_threadsafe`.
  The exception is also no longer swallowed silently, since a bare `except` was the
  only reason this was hard to see.
- **Two pollers on one token are fatal to the new one.** Telegram answers the second
  `getUpdates` with *Conflict: terminated by other getUpdates request*. Even with the
  shutdown fixed, the replacement can reach polling in the moment before the old
  process releases the lock, so the replacement now **waits before it starts
  polling** (`RESTART_STARTUP_DELAY`). That removes the race rather than relying on
  the new process surviving it.
- **The replacement inherited the parent's stdout pipe.** On Windows the default is
  `close_fds=False`, so the new process held every inheritable handle — including
  the stdout pipe of whatever started the bot. Any launcher capturing that pipe
  (`subprocess.run(..., capture_output=True)`, a service wrapper, a CI step) then
  blocked forever waiting for an EOF that could not arrive while the bot lived.
  The spawn now passes `close_fds=True`.
- **`restart.log` was written in the machine's code page**, so the file the reply
  tells the operator to read was mojibake. The spawn now forces
  `PYTHONIOENCODING=utf-8`, and the file is rotated past `RESTART_LOG_MAX_MB` so a
  bot that restarts occasionally does not grow it without limit.

There is a real process test for this in `test_bot_features.py` (section 1ab): it
starts a child process using the same `schedule_restart`, and requires that the
child **exits on its own**, that a replacement is spawned, and that the replacement
reaches polling without a `Conflict`. A restart that leaves no bot running is the
failure that matters, and only a real process can catch it.

`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` is used rather
than `DETACHED_PROCESS` alone: with no console at all, any accidental `print` in the
new process would fail on a binary file handle, whereas `CREATE_NO_WINDOW` gives it
a console that simply is not shown.

## Cleaning up the chat
Once a request succeeds the bot removes the clutter it created:

- the **"正在解析…" notice**, and
- the **original message that carried the link**.

Both are controlled in `.env`:

```ini
DELETE_STATUS_MESSAGE=1
DELETE_SOURCE_MESSAGE=1
```

Set either to `0` to keep that message.

**In a group, deleting someone else's message requires the bot to be an
administrator with the _Delete messages_ right.** Without it Telegram refuses the
delete, the bot logs a single warning, and only its own notice is removed — the
request still succeeds, and the parsed result is unaffected. In a private chat no
special right is needed.

Nothing is deleted when a request **fails**: the status message is then the only
explanation the user has, and the source link is their only reference.

## Features
- **X/Twitter, Pixiv and e-hentai links only.** Links on any other site are never
  downloaded or fetched, so the bot cannot be used as a general-purpose link
  fetcher.
- **Pixiv artwork is sent as the original image files** — no ZIP, no recompression,
  no downscaling. Multi-page works arrive as albums.
- **e-hentai galleries become Telegraph articles** with the gallery's facts and
  every tag grouped by namespace, delivered as a single chat message with the cover
  as the message body.
- X/Twitter images and videos sent back to Telegram.
- Telegram invite links (`t.me/+...`, `t.me/joinchat/...`) identified separately.
- Public Telegram links are recognized but not falsely labeled as groups because they can point to a group, channel, user or bot.
- Cooldown on links, download timeout, concurrent download limit, per-file size limit.
- One request per user at a time, refused only while one is actually running.
- Optional user allowlist and per-site login cookies.
- Administrator-only `/cookies`, `/status` and `/restart`; the bot is safe to run in
  a public group.

## How a message is routed
| Message contains | What the bot does |
| --- | --- |
| An `x.com` / `twitter.com` artwork link | Downloads the media; images go back as an album of photos |
| A `pixiv.net` artwork link | Downloads the artwork and sends the **original files** as albums |
| An `e-hentai.org` / `exhentai.org` gallery link | Shows the gallery's facts and tags, plus four action buttons |
| A link on any other site | Replies with `SUPPORTED_SITES_NOTICE`; nothing is fetched |
| A `t.me` invite/public link | Explains that it does not join or classify it |
| Several links in one message | An X/Twitter or Pixiv artwork link wins; then a gallery link |

The source link (`原始連結：<url>`) is attached to every result. Disable it with
`ATTACH_SOURCE_LINK=0`.

To answer unsupported links with a different message, or to stay silent instead
(which suits a busy public group):

```ini
SUPPORTED_SITES_NOTICE=目前只支援 X/Twitter、Pixiv 與 e-hentai 連結。
REPLY_UNSUPPORTED_LINKS=1
```

## e-hentai galleries → Telegraph
Send a gallery link such as `https://e-hentai.org/g/3672745/82d9d6d00e/` and the
bot replies with the gallery's facts and every tag grouped by its namespace —
Language, Artist, Group, Parody, Character, Female, Male and so on — plus four
buttons:

| Button | What it does |
| --- | --- |
| Telegraph · 顯示圖 | Publishes a Telegraph page embedding the site's 1280px images. Nothing is sent to the chat |
| Telegraph · 原圖 | Publishes a Telegraph page embedding the full-resolution images |
| 歸檔 · 1280 | Downloads the 1280px set and sends it as one ZIP |
| 歸檔 · 原圖 | Downloads the full-resolution set and sends it as one ZIP |

Nothing is downloaded until a button is pressed, so a link you are only curious
about costs one metadata request.

### Layout: the source link is the first item's caption
**Only X/Twitter carries the source link. Pixiv sends the images alone.** Requested
explicitly, and it is a per-call option (`attach_link`) rather than a global, because
the two platforms are configured differently.

X/Twitter — the link is the **first item's caption**, so it reads above the images:

```
[ 原始連結：https://x.com/pohitashi/status/... ]   ← caption on item 0
[ 相簿：page0 · page1 · page2 ]
```

Pixiv — no caption at all:

```
[ 相簿：page0 · page1 · page2 ]
```

The layouts that were tried, and why this one:

| Approach | Where the link lands | Cost |
| --- | --- | --- |
| Caption on the first item | **above** the images | one message |
| A separate message | above the album | **two** messages |
| A url button alone | below the images | one message |
| Caption **and** button | above, then again below | one message, **link shown twice** |

**The url button is no longer attached.** It was introduced when the link could only be
a caption drawn against the media, so it acted as the guaranteed-reachable copy. With
the link in the caption on a message that itself survives cleanup, a button pointing at
the same address printed the link a second time — reported as redundant.
`build_source_keyboard` is kept and documented as intentionally unused, because it is
the only place that keyboard exists and reattaching it should be a one-line change if a
client is ever found to render the caption somewhere unreachable. Reattaching it
**without** removing the caption link would restore the duplication.

**Only the first item is captioned.** Telegram rejects a media group in which every
item has one, and one link is what is wanted anyway.

**The position is Telegram's to decide, not ours.** An album caption is drawn by the
client *against the media*, which is why an earlier attempt landed the link in the
middle of the set when it was attached to a later item. Anchoring it to the first item
is the best that can be done from here.

The caption goes on the **first album only**, so a 30-page work does not carry the same
link on all three of its messages. A separate text message is now only a fallback for
the case where nothing was sent at all.

**The cached replay follows the same split.** `send_cached_media` passes
`attach_link=(platform != "pixiv")`, so a repeat of a cached Pixiv link does not grow a
link that the original send did not have.

### One message per request, with the cover in it
The gallery's cover becomes the body of the reply, so the result is **one message**:
the cover photo with the gallery's facts, tags and page link as its caption.

It is done by *editing the message the bot already sent*, not by posting a second
one. Posting the cover separately made every reply arrive as two messages, which
read as the bot repeating itself for no reason.

The image is referenced **by URL**, so Telegram fetches it and the bot downloads no
bytes for it. It uses e-hentai's static thumbnail host (`ehgt.org`) rather than the
time-limited `hath.network` links the page images use, so a cover posted in the chat
keeps working.

A missing thumbnail is not an error: the same message falls back to text and the
request carries on, because refusing to deliver a gallery over a missing cover would
be the wrong trade. The 重新生成 button is attached in both cases — `edit_media`
replacing the message body would otherwise drop it.

### The Telegraph page does not repeat the cover
The page is the title, the facts, the tags grouped by namespace, then the page
images. The cover is left out: it is already in the chat message, and putting it at
the top of the page spent content budget the actual pages need.

### Tag values are stripped of wiki notes
e-hentai attaches a wiki annotation to some tags, which used to render as
`japanese | definition revised please read the wiki`. That is site boilerplate
rather than part of the tag, so it is removed before display and translation.

### Nothing is parsed twice
A link that has already been parsed is served from a cache, so a repeat costs no
download and no requests. Measured on a 5-page gallery: **1.93s → 0.02s**, because
the per-page viewer sweep is skipped entirely.

Two things are cached, because the expensive work differs:

| Kind | Cached | The saving |
| --- | --- | --- |
| e-hentai | the gallery's facts and the image URLs from the viewer sweep | the sweep is one request per page — about a minute on a 53-page gallery |
| media | the downloaded files themselves, copied aside | no download and no gallery-dl run at all |

Verified on a repeat request through the real handler:

```
first request    viewer sweep 1.93s
second request   cache hit    0.02s
```

For Pixiv and X the same applies to the files themselves:

```
first request    2.09s   Pixiv 解析完成，已傳送原圖 2/2 個檔案。
second request   0.01s   已從快取傳送 2/2 個檔案。
```

**A cached result is not subject to the link cooldown.** That cooldown exists to stop
repeated *download* work, which a cache hit does none of; applying it would make a
repeat wait ten seconds for an answer already on disk — the one case where waiting
is pure loss. The cooldown still applies when nothing is cached.

**A cached result cleans up the chat exactly as a download does.** Both the progress
notice and the link message are removed, and the delivered files stay. Serving from
the cache without that cleanup left two extra messages behind, so a cache hit
produced *more* clutter than a fresh download — the opposite of the point.

The send uses a **scratch** directory, never the cache: the sender writes downscaled
copies for oversized images, and pointing that at the cache directory would let it
overwrite the files being cached.

What is deliberately **not** cached is a failure. A link that failed is retried,
because the usual cause is transient or a credential the operator has since fixed;
caching it would make one bad moment permanent.

**There is deliberately no `gallery-dl --download-archive`.** It looks like a
natural backstop for the cache, but it conflicts with the per-request output
directory: the archive records that a file has been downloaded and gallery-dl then
skips it, while each request asks for a *fresh* temp directory. On a repeat it
therefore wrote nothing at all, and the bot reported "沒有找到可傳送的圖片或影片"
for a link it had just delivered. Reusing a previous parse is the result cache's
job, and when the cache misses a re-download is the correct behaviour.

**Cached image URLs still expire.** e-hentai's image links are time-limited, so the
cached sweep is only good for a while; the entry expires on its own, and
重新生成 Telegraph 頁面 is the supported way to refresh a page whose images have
stopped loading. A regenerate deliberately ignores the cache and writes the fresh
URLs back.

```ini
CACHE_FILE=parse_cache.json
CACHE_DIR=.cache
CACHE_STALE_SECONDS=1209600   # 14 days
CACHE_MAX_ENTRIES=300
```

The cache is bounded on both axes and prunes the oldest entries first, removing
their files as well — an entry dropped without its files would leak disk space that
nothing references. A corrupt cache file is discarded rather than fatal, since a
cache is an optimisation and losing it costs time only. A **partially** present file
set is treated as no cache at all, because sending half an artwork as though it were
the whole thing is worse than downloading it again.

`/cache` shows the contents, the hit/miss counters and the disk usage; `/cache clear`
empties it. Both are administrator only.

### The Telegraph actions post a page, not images
The gallery pages are already visible on the Telegraph page, so that set is **not**
also posted into the chat — that only duplicated the gallery and made the result
look redundant. The reply is one message: the cover, the gallery's facts and tags,
the page link, and a **重新生成 Telegraph 頁面** button:

```
📚 [umeckiti] Hanazono Yurine
分類 Artist CG　語言 Japanese　評分 3.00　5 頁　7.2 MiB
• 語言 / Language：日文（japanese）
• 原作 / Parody：jashin-chan dropkick
• 女性 / Female：眼罩（eyepatch）, 哥德蘿莉（gothic lolita）, 雙馬尾（twintails）

📄 Telegraph（內嵌 5 頁圖片）：https://telegra.ph/...
（成人內容，Telegraph 頁面可能被移除）
```

There is no extra line pointing at the archive mode; the buttons are already on
screen, so saying so again was noise.

To obtain the actual files, use one of the **歸檔** actions. They are the only
paths that download and deliver image bytes.

This also removed a whole phase: the Telegraph actions no longer download the
gallery at all, because Telegraph fetches every embedded image itself. They only
read image URLs, which is one request per page.

The two Telegraph actions still differ meaningfully — Telegraph loads whatever is
embedded, so 顯示圖 is a light page (about 0.14 MB a page) and 原圖 a heavy one
(about 10 MB a page).

### Tags are shown in Traditional Chinese
Tag names are rendered as Traditional Chinese (Taiwan) with the original English
beside them, so nothing is lost if a translation is unfamiliar:

```
• 女性 / Female：眼罩（eyepatch）, 哥德蘿莉（gothic lolita）, 雙馬尾（twintails）
• 其他 / Other：差分集（variant set）
```

Proper nouns — artist, group, character and parody names — have no Chinese form
and are shown alone instead of being repeated: `momozu komamochi`, not
`momozu komamochi（momozu komamochi）`.

The table lives in `tag_translations.py` as committed data (about 380 entries),
not fetched at runtime, so rendering a gallery never depends on a second network
service. Add an entry there to extend it; unknown tags pass through unchanged
rather than disappearing.

### The 停止 button stops both kinds of download
There are two mechanisms, because the download has two shapes:

| Download | How stopping works |
| --- | --- |
| gallery-dl (the fallback) | a child process, killed |
| The parallel fetch | threads **inside this process** — no process to kill, so a flag |

The parallel fetch is the normal path for every mode, including 歸檔. It cannot be
killed, so stopping is a shared flag the workers read: they check it before each
request and again after the response comes back, and a result that arrives after the
stop is discarded rather than written.

This was **broken** when the parallel fetch was introduced. Only the flag was passed
and nothing registered it, so `stop_running_download` found no entry and answered
「目前沒有進行中的下載」 while the download ran to completion — the button worked only
for gallery-dl, which registered a real subprocess. A process-less stop is now its own
case (`process is None`): the flag is set and no kill is attempted.

The per-request timeout is 30s rather than 120s for the same reason — a request already
in flight cannot be interrupted, so this bounds how long the button takes to take
effect on the workers that are mid-download.

**The 補齊缺少的頁面 fallback needed its own fixes.** It runs a child process, so the
button kills it — but two faults made that pointless:

- There was no stop check **after** the fallback, so the download was reported as
  completed and the archive was packaged and sent anyway.
- `download_gallery_media` called `stop_event.clear()` on entry. That dates from when
  each request spawned its own gallery-dl run and a leftover flag had to be discarded;
  now the flag is created per request and shared across its phases, so clearing it
  erased a stop that arrived **between** phases and the fallback ran to completion.

The flag is no longer cleared anywhere, the result is re-checked after the fallback,
and packaging honours a stop too — a large gallery takes real time to zip while the
button is still on screen.

A regression test drives the fallback with a fake child process that dies when killed,
and asserts the process is killed, the result is reported not-completed, a stop pressed
between phases skips the fallback, and entering the fallback does not clear the flag.

A regression test starts a local HTTP server, runs a 60-page download, presses stop
from the progress callback, and asserts that most pages were never fetched and that
the files already written are kept.

### Why 補齊缺少的頁面 used to appear, and what replaced it
That step is gallery-dl filling gaps after a parallel download came up short. It
exists because gallery-dl re-signs pages itself, but it is **serial** (3-6 s per
request), so reaching it is a big slowdown — and the usual cause was not a flaky
fetch at all.

e-hentai serves page images from **signed, time-limited URLs**, while the cache entry
carrying them lives **14 days**. A cached sweep that outlives its signature fails for
**every page at once**:

```
CACHE_STALE_SECONDS      1209600  (14 days)
signed URL lifetime      hours
```

So there are now three escalating steps, and the cheap one comes first:

| Step | Mechanism | Cost |
| --- | --- | --- |
| 1 | Cached sweep older than `GALLERY_URL_TTL_SECONDS` is **re-swept** | one parallel sweep |
| 2 | A short download retries **once with fresh URLs** | one parallel pass |
| 3 | gallery-dl, only if pages are still missing | serial, minutes |

```ini
GALLERY_URL_TTL_SECONDS=7200   # 2h: how long a cached URL list is trusted
```

A stop is not treated as a short download: a cancelled fetch returns without
refreshing and without starting gallery-dl.

### Page downloads are parallel where pages are still downloaded
The **歸檔** modes no longer download pages at all — they fetch e-hentai's official
archive (see below). The Telegraph modes collect URLs only, so pages are fetched in
parallel by the page-download path that remains available for a gallery whose official
archive cannot be produced.

That path used to run gallery-dl while the Telegraph modes used a parallel fetch, so
the same pages were slow in one place and fast in the other. Measured on a 5-page
gallery:

| Path | Time |
| --- | --- |
| gallery-dl, the old route | **27.7s** |
| parallel fetch | **2.9s** |

The download phase alone is therefore about **9x faster**, and the page-URL sweep is
cached, so a gallery already looked at skips it entirely. Nothing is downloaded twice
either: the sweep is stored with the gallery, so whichever action runs first pays for
it and the other reuses it.

```ini
EHENTAI_DOWNLOAD_WORKERS=6     # parallel image downloads
EHENTAI_VIEWER_WORKERS=6       # parallel viewer-page requests
```

### Download progress is shown as a percentage
Both slow phases update the status message while they run, with a bar, a
percentage and a page count:

```
正在下載 5 頁圖片……
██░░░░░░░░ 20% (1/5)
（可按「停止」中止）
```

A real 5-page download, one update per completed page:

```
  5.5s  ██░░░░░░░░ 20% (1/5)
 11.7s  ████░░░░░░ 40% (2/5)
 15.6s  ██████░░░░ 60% (3/5)
 21.1s  ████████░░ 80% (4/5)
 27.2s  ██████████ 100% (5/5)
```

The collection phase — reading one viewer page per gallery page — has its own bar,
because on a 53-page gallery that step alone takes about a minute.

How it works: `--Print 'file:<marker>'` makes gallery-dl emit one line per
completed file *while still downloading*, so counting those lines gives a real
page count instead of guessing from log wording. Updates are throttled to one edit
every 1.5 seconds, because Telegram rate-limits message edits; the final state is
always written, so the message never stops short of 100%.

Two traps worth recording, since both were hit while building this:

- `subprocess.run` cannot be interrupted, so the downloader runs under `Popen`
  with a line-reader thread to stream progress. `--Print` (capital P) is required
  rather than `--print`, and its format string must contain a space, because
  gallery-dl wraps a bare token in braces and then resolves it as a field name.
- `asyncio.wait_for(queue.get(), timeout=...)` cancels the getter on every
  timeout, and a queue whose getter keeps being cancelled stops waking reliably —
  the message then sat unchanged until the download finished. Polling and draining
  the queue has no cancellation to corrupt.

### A "停止" button interrupts a long download
While a download runs, the progress message carries a **停止** button. Downloading
every original page of a large gallery takes minutes and moves hundreds of
megabytes, so cancelling must be possible.

- Stopping kills the gallery-dl child process and reports **"已停止下載"**, not a
  failure. (A killed process exits non-zero, so the cancellation flag is checked
  before the exit status — otherwise a deliberate stop would surface as a
  download error with an empty message.)
- Cancelling before the archives are built means no archive is sent. A download
  stopped *after* packing leaves an incomplete archive, so stop during the
  download phase rather than mid-send.
- The registry is keyed by chat, so pressing stop in one chat cannot kill another
  chat's download.
- Partial files stay in the request's temporary directory and are removed with it.


Measured sizes on a real gallery: the 1280px image is about **0.14 MB** a page
(1280x1791) and the original about **10 MB** a page (2518x3524), so the two
"原圖" actions are far heavier than they look.

### 歸檔 uses e-hentai's OFFICIAL archive
The **歸檔** modes no longer build an archive. They ask e-hentai's archiver for the ZIP
e-hentai itself publishes, which is both more accurate and far less work: **no page is
downloaded at all** — one POST returns the archive URL, one streamed GET delivers the
file, and the filename is e-hentai's own (`(C106) [circle] title [中国翻訳]-1280x.zip`).

The flow has three steps, and every one is required:

| Step | Request | Result |
| --- | --- | --- |
| 1 | `POST archiver.php` with `dltype=org` or `res` | a page saying the file is being prepared |
| 2 | GET the `hath.network/archive/...` link from it | **HTML**, not the file — a waiting page |
| 3 | the same URL with **`?start=1`** | the ZIP, with `Content-Disposition` naming it |

Step 2 is the trap: that URL returns HTML, so treating it as the download yields a
16-byte "file". A ranged `GET` (`Range: bytes=0-3`) reads the real filename and total
size without pulling the whole archive.

**The H@H downloader on the same page is deliberately unused** — it requires a running
H@H client and otherwise answers *"Your H@H client appears to be offline."*

| Option | `dltype` | Measured size | Telegram |
| --- | --- | --- | --- |
| 歸檔 · 原圖 | `org` | **148.2 MiB** | ❌ over the 50 MB bot limit |
| 歸檔 · 1280 | `res` | **3.2 MiB** | ✅ |

The size is reported before the attempt and the upload is still tried, so the answer
comes from Telegram rather than from a local refusal:

```
官方歸檔 148.2 MB，超過 Telegram 的 45MB 建議上限，仍嘗試傳送……
```

If the upload is refused, the reply says so and points at **歸檔 · 1280**, which is the
resample and normally fits.

The archive is **streamed to disk**, never buffered: the original is routinely over
100 MB, so reading it into memory before handing it to Telegram would be wasteful and
could exhaust it. The same 停止 button ends the transfer.

The official filename carries the gallery title, which contains characters Windows
forbids in a path (`:`, `?`, `|`), so it is sanitised before use — path separators are
neutralised, reserved device names (`CON`, `NUL`, `COM1`) avoided, and overlong names
shortened.

### The gallery page is the data source
One HTTP GET returns the title (English and Japanese), category, language,
rating, page count, size, date, favourites, cover and the full tag list in about
1.4 seconds. That was chosen after measuring the alternatives on a 53-page
gallery:

| Approach | Time | Category | Language | Tag namespaces |
| --- | --- | --- | --- | --- |
| `gallery-dl -j` (plain) | **275 s** | yes | yes | yes |
| `gallery-dl -j` + `source: metadata` | 1.4 s | **no** | **no** | **no** |
| `gallery-dl -j` + `metadata: true` | **265 s** | yes | yes | yes |
| one GET of the gallery page | **1.4 s** | yes | yes | yes |

`gallery-dl -j` walks every image page even when only metadata is requested, which
is what makes it unusable in a chat. Its `source: metadata` mode calls e-hentai's
gdata API once and is fast, but that response has no category name, no language
and no uploader, and returns tags as flat `namespace:tag` strings.

The parsing regexes were each checked against real markup, because the obvious
patterns silently match the wrong thing: the cover lives in an inline background
style (the first `<img>` on the page is a rating graphic), and
`<div id="taglist">` does not contain the `<tr>` rows directly.

### ⚠️ Telegraph image links expire
Telegraph embeds an image by fetching its URL, and the URLs e-hentai serves
(`*.hath.network`) are **time-limited and tied to the login session**. A Telegraph
page built today can therefore show broken images in a few days. The tag and
facts sections are static HTML and remain correct.

If a permanent copy matters, use an archive action instead — those deliver the
actual files.

### ⚠️ Telegraph image links expire — use 重新生成
Telegraph embeds an image by fetching its URL, and the URLs e-hentai serves
(`*.hath.network`) are **time-limited and tied to the login session**. A page built
today can show broken images in a few days. The tag and fact sections are static
HTML and stay correct forever.

A **重新生成 Telegraph 頁面** button is attached to each finished result. Pressing it
fetches freshly signed URLs for the same gallery and publishes a new page,
replacing the old message in place. The button is remembered per chat and user, so
one person cannot rebuild another's page, and a stale button from an earlier
session is refused rather than guessed at.

**Why not host the images permanently instead.** Measured, not assumed:

| Host | Result |
| --- | --- |
| catbox.moe | `412 Invalid uploader` |
| 0x0.st | uploads disabled ("AI botnet spam") |
| uguu.se | works, but deletes files after 3 hours |
| tmpfiles.org | 1 hour |

Free permanent image hosting is not available here. If you have an API key for a
paid or self-hosted host (imgbb, Cloudflare R2, your own server), the images can be
uploaded there and embedded permanently — otherwise 重新生成 is the mechanism.

### Public vs adult galleries
e-hentai marks each gallery with a category such as `Non-H`, `Manga` or
`Doujinshi`. Public galleries (`Non-H`, `Artbook`) need no login. Adult ones
require an e-hentai session, and without one the site answers exactly like a
deleted gallery — which is why the bot says "may need a login" rather than
"not found".

```ini
EHENTAI_COOKIES=cookies/ehentai.txt
```

The file needs the e-hentai domain's cookies: `ipb_member_id` and
`ipb_pass_hash` (and `sk` plus `igneous` for exhentai). Both the `/cookies`
command and `bot.py --check` report what is missing.

```ini
EHENTAI_TIMEOUT=60           # metadata request
EHENTAI_DOWNLOAD_TIMEOUT=900 # downloading every page
```

### Telegraph and adult content
Telegraph's terms forbid adult material, so a page built for an H gallery **can be
removed**. Building it anyway is the default because that is the requested
behaviour, but the choice is explicit:

```ini
TELEGRAPH_ALLOW_ADULT=1   # 1 = always build the page
                          # 0 = answer adult galleries with the text info only
```

An anonymous Telegraph account is created per page, so pages are independent: if
one is removed, the others are unaffected.

## Why Pixiv files are sent as documents
Telegram has no way to deliver an image untouched through the photo path:
`sendPhoto` re-encodes the picture server-side and renames it to `.jpg`, so a
"photo" is never the original. Pixiv artwork is therefore sent as **documents**,
which Telegram passes through byte-for-byte, and the original filenames
(`150653396_p0.jpg`) are preserved.

Consequences worth knowing:
- Images arrive as files rather than inline photos. Tap to open, use *Save to
  gallery* on mobile, or download on desktop.
- Multi-page works are grouped into albums of up to 10, so a 30-page set arrives
  as three albums instead of thirty messages.
- A file over `MAX_UPLOAD_MB` is reported and skipped rather than silently
  shrunk — the point is to deliver the original, not a substitute.
- X/Twitter keeps the normal photo path, because a compressed preview is what is
  expected from a post.


## Setup
1. Install Python 3.11+ from https://www.python.org/downloads/ (enable PATH).
2. Copy `.env.example` to `.env` and set `BOT_TOKEN` to a newly generated token.
3. Open CMD in this folder and run:
   ```bat
   py -m venv .venv
   .venv\Scripts\activate
   py -m pip install --upgrade pip
   py -m pip install -r requirements.txt
   py -m pip install -U gallery-dl
   ```
4. Configure login cookies (see below) if you want age-restricted or
   login-only Pixiv/X content.
5. Run `run.bat`.
6. Test by sending `/start` to the bot.

### If pip cannot write to the venv
On a host that denies writes under `.venv\Lib\site-packages`, pip stops with
`PermissionError` while writing wheel metadata. The bot still runs; only Pillow
is affected. Install it unpacked instead:

```bat
.venv\Scripts\python.exe install_pillow_offline.py
```

That unpacks the matching wheel into `vendor\`. `bot.py` appends `vendor\` to
`sys.path` only when Pillow is not importable normally, and `pixiv_login.py` does
the same for `websockets`, so a real pip install always wins.

## Login credentials (Twitter / Pixiv)
The two sites need **different** kinds of credential, and this is the single most
common source of confusion:

| Site | What it needs | Why |
| --- | --- | --- |
| X/Twitter | cookies: `auth_token` + `ct0` | No usable password login for this kind of client |
| Pixiv | an OAuth **refresh token** | Artwork pages go through Pixiv's App API, which a cookie cannot authorise |

### X/Twitter cookies
1. Log in to X in your normal browser.
2. Export cookies in **Netscape format** with a browser extension such as
   *Get cookies.txt LOCALLY* or *cookies.txt*. Use `.txt`, not JSON.
   - Export while logged in to `x.com` **and** `twitter.com`.
3. Save it as `cookies/twitter.txt` and point `.env` at it:
   ```ini
   TWITTER_COOKIES=cookies/twitter.txt
   ```

### Pixiv refresh token
A `PHPSESSID` cookie is valid for pixiv.net's own web endpoints but *not* for the
App API that gallery-dl uses, so a refresh token is required.

The authorization code is bound to the PKCE `code_verifier` of the process that
requested it, and that verifier never leaves the process. A code copied to another
process can therefore **never** be exchanged, and Pixiv reports that with the same
`invalid_request` it uses for an expired code. Use the bundled helper, which keeps
both halves in one run:

```bat
.venv\Scripts\python.exe pixiv_login.py --auto
```

`--auto` drives a throwaway Chrome (separate profile, so your own browser is
untouched) and reads the code off the wire automatically. The token is written
straight into `.env`.

If you prefer gallery-dl's own flow, or `--auto` cannot run:

```bat
.venv\Scripts\python.exe pixiv_login.py          # manual paste
.venv\Scripts\gallery-dl.exe oauth:pixiv         # gallery-dl's equivalent
```

For both manual paths, press F12 → Network *before* logging in, then copy the
`code` parameter of the last `callback?state=...` request. The code expires **30
seconds** after login. A `accounts.pixiv.net/post-redirect?...code_challenge=...`
URL is the *start* of the flow and contains no code; the helpers reject it with an
explicit reason.

`cookies/pixiv.txt` is still useful for pixiv.net web requests and is checked by
`/cookies`.

### Verify
```bat
.venv\Scripts\python.exe bot.py --check
```
This reports each site's real state — including "Pixiv: 缺少 refresh-token" when
only a cookie is present. Administrators can also send `/cookies` in Telegram
(see *Who can do what* above).

Add `--live` to make one real request per site and prove the credential actually
works, rather than merely carrying a future date:

```bat
.venv\Scripts\python.exe bot.py --check --live
```

```
在线验证（会向站点发出一次真实请求）：
  [OK] X/Twitter：登录有效（注意：X 可能仍不返回推文内容）
  [OK] Pixiv：PHPSESSID 有效（关注 567，粉丝 2）
  [OK] Pixiv refresh-token：已配置（不过期）
```

## Do the credentials expire?
Yes, but the three credentials behave very differently. Measured on this setup:

| Credential | Lifetime | What happens when it lapses |
| --- | --- | --- |
| X/Twitter `auth_token` + `ct0` | ~1 year | **All X access stops.** Re-export the cookies |
| X/Twitter cookie, if revoked | Immediately | X invalidates sessions server-side on password change or logout |
| Pixiv `PIXIV_REFRESH_TOKEN` | **Does not expire** | Nothing, unless revoked or the password changes |
| Pixiv `PHPSESSID` | ~1 month | Only pixiv.net's AJAX endpoints (the R-18 check); artwork downloads keep working |

The bot warns on startup once a credential is within `EXPIRY_WARN_DAYS`
(default 14) of expiry, and the admin `/cookies` command always shows the
remaining days, so a failure never arrives unannounced.

### Why there is no automatic cookie grabber
Reading cookies straight out of the browser is the obvious automation, and it
does not work reliably here:

- Chrome holds an exclusive lock on its `Cookies` database, so `gallery-dl
  --cookies-from-browser chrome` fails with `PermissionError` **while Chrome is
  running** — and copying the file first also fails, because the lock is shared.
- Closing Chrome does not solve it either: this profile uses Chrome's App-Bound
  Encryption (`app_bound_encrypted_key` is present in `Local State`), which binds
  the cookie key to the browser process rather than the Windows user, so
  gallery-dl cannot decrypt the values even with the file unlocked.

Because of that, the supported workflow is a re-export when warned, plus
`--check --live` to confirm. Two things make it cheap:

1. The X cookie lasts about a year, and the Pixiv refresh token never expires —
   so this is a rare task, not a routine one.
2. Only the cookies for these two sites are needed. Re-running the exporter and
   overwriting `cookies/twitter.txt` is enough; nothing else has to be touched.

If you would rather automate it, the workable approach is a **separate browser
profile** used only for these two sites, kept closed while the bot runs, with
`GALLERY_DL_COOKIES` pointed at an export from it. Ask and it can be set up.

Cookies and tokens expire; when downloads start failing, re-export the cookies
and re-run the OAuth flow. `cookies/` and `gallery-dl.conf` are gitignored, so
credentials are never committed.

## Known limitation: X/Twitter returns no results
Verified repeatedly, including against a long-lived public post (a NASA tweet) and
X's own unauthenticated syndication endpoint, which returns HTTP 200 with an empty
body. The cookies are fine — X withholds the tweet body from this kind of client.
This is an X-side restriction tracked upstream in
[mikf/gallery-dl#9267](https://github.com/mikf/gallery-dl/issues/9267), not a
misconfiguration here.

What to do:
- Confirm the credentials with `bot.py --check`; re-exporting cookies will not
  help if that already says "已登录".
- Try `python -m pip install -U gallery-dl` — upstream updates periodically
  work around X changes.
- Pixiv is unaffected by this.

The bot reports this case explicitly instead of claiming that login failed.

## A trap worth remembering: an album item needs a FILE, not a path
`send_media_album` builds the media group, and for each item it must pass an **open
file object**. A `str` is read by Telegram as a URL or file_id, so a Windows path
becomes `file:///C:/...` and the whole album is rejected:

```
BadRequest: Invalid file http url specified: unsupported url protocol
```

The caller then fell back to one message per page, which looked exactly like the
album feature not existing. This was invisible to a test that only checked the
element's type name, so the test now asserts that each element is an *uploaded file*
(`InputFile` with content), carries the real filename, and is not a path or a
`file://` url.

The streams are opened lazily and closed in a `finally`, so a failure part-way
through does not leak handles on files the caller is about to delete.

## A trap worth remembering: gallery-dl's `--` terminator
`build_gallery_dl_command` writes its options first and then `-- <url>`. That
terminator stops a URL beginning with a dash being read as an option — but it also
means **nothing may be appended after it**. An option placed there is parsed as a
second URL:

```
[2/3] --download-archive
[gallery-dl][error] Unsupported URL '--download-archive'
```

That one mistake broke **every Pixiv and X download at once**, while the credential
check kept reporting everything healthy, because the credentials were healthy — the
command was malformed. A regression test now asserts that the terminator appears
exactly once and that only the URL follows it, for every platform.

The same feature had a second, worse failure: once the flag was in the right place,
`--download-archive` made a *repeat* download write nothing into the fresh temp
directory, so a link that had worked returned "沒有找到可傳送的圖片或影片". It has
been removed entirely — see the caching section.

## Version control
This project is a git repository. **Commit before making sweeping changes** — that is
not ceremony. A stray cleanup command once removed `test_bot_features.py` (733 tests)
and there was no history, no `.pyc`, no backup and no shadow copy to recover it from,
so it was gone.

```sh
git status           # what is uncommitted
git add -A
git commit -m "..."  # commit early, commit often
git log --oneline    # what changed
```

`.gitignore` deliberately excludes everything secret or generated, and this was
verified rather than assumed:

| Ignored | Why |
| --- | --- |
| `.env` | bot token, api id/hash, refresh token |
| `cookies/` | Twitter, Pixiv and e-hentai session cookies |
| `authorized.json` | the users, groups and admins lists |
| `parse_cache.json`, `.cache/` | the parse cache and its files |
| `restart.log*`, `bot_console.log` | runtime logs |
| `gallery-dl.conf`, `.gallery-dl-cache/` | generated from `.env` |
| `.venv/`, `vendor/`, `__pycache__/` | environments and dev-only tools |
| `bot-api-data/`, `telegram-bot-api*` | self-hosted server state and binary |

`git check-ignore -v <path>` confirms any individual path. Twelve files are tracked:
the four Python modules, the two runner scripts, `docker-compose.yml`, `run.bat`,
`requirements.txt`, `README.md`, `.gitignore` and `.env.example`.

### Pushing an update to GitHub
The remote is <https://github.com/YuNaiAFK/twitter_pixiv_tg_bot_windows> (public), and
`main` tracks `origin/main`. Whenever the bot changes, push it — that is the off-site
copy, and its absence is what made the loss of the test suite unrecoverable.

```sh
git status                              # see what changed
git add -A
git commit -m "what changed and why"
git push
```

**Verify the exclusions before every push that touches configuration.** The command
below lists what would actually be published, and must print **`SAFE`**:

```sh
git ls-files | findstr /I /R "^\.env$ cookies authorized parse_cache restart\.log"
```

`/R` with anchored patterns is deliberate: a plain `findstr /I "env"` also matches
`.env.example`, which is a placeholder template and *should* be published. A check that
cries wolf on a safe file is one people learn to ignore, which defeats it.

#### The dead proxy, and why pushes fail with a connection error
The global git config points at `http://127.0.0.1:10808`, a proxy that is **not
running**, so git could not reach GitHub at all:

```
fatal: unable to access 'https://github.com/...': Failed to connect to github.com
port 443 via 127.0.0.1 after 2058 ms: Could not connect to server
```

Measured on this machine: direct connections work, `127.0.0.1:7897` works, and
`:10808` and `:8080` do not. This repository therefore carries a **local** override
that clears the proxy, so `git push` works without extra flags:

```sh
git config --local http.proxy ""
git config --local https.proxy ""
```

The global setting is deliberately left alone — other tools may depend on it. If git
fails in a **different** repository with the same connection error, this is the cause:

```sh
git config --global --unset http.proxy
git config --global --unset https.proxy
```

## Command-line helpers
| Command | Purpose |
| --- | --- |
| `bot.py` | Run the bot |
| `bot.py --check` | Print configuration, credentials, gallery-dl and Pillow status |
| `bot.py --check --live` | The above, plus a real authenticated request per site |
| `bot.py --dry-run` | Build all handlers and exit without connecting to Telegram |
| `bot.py --pixiv-token <url\|code>` | Extract the OAuth `code` from a pasted callback URL |
| `pixiv_login.py [--auto]` | Complete the Pixiv OAuth flow and write the refresh token to `.env` |
| `install_pillow_offline.py` | Install Pillow into `vendor/` when pip cannot |
| `test_bot_features.py` | Self-tests for routing, credentials, sending and cleanup |

## Staging directory and caches
Downloads are staged in a temporary directory per request and deleted
afterwards, in this order: `WORK_DIR` if set, then `.downloads\` beside
`bot.py`, then the system temp folder. The first one that accepts a write wins,
so a read-only or space-limited location degrades instead of failing every
request. Point `WORK_DIR` at a roomy drive for large Pixiv sets.

`gallery-dl.conf` is generated next to `bot.py` on startup. It exists so
gallery-dl's SQLite cache lands in `.gallery-dl-cache\` instead of `%APPDATA%`:
where that profile is read-only, gallery-dl fails with *attempt to write a
readonly database* and both sites stop working. The file also carries
`PIXIV_REFRESH_TOKEN` when one is configured. Override either location with
`GALLERY_DL_CACHE`, or supply a whole config with `GALLERY_DL_CONFIG`.

## Using it in Telegram groups
- Add the bot to the group.
- If the bot needs to see ordinary group messages, use `@BotFather` -> `/setprivacy` -> select bot -> `Disable`, then remove and re-add the bot to the group.
- Alternatively, users can mention/reply to the bot where privacy mode permits it.
- Do not add the bot as an administrator unless it truly needs admin privileges.

### With privacy mode off, only messages with a link are answered
Privacy mode is off on this deployment (`can_read_all_group_messages: True`), so
Telegram delivers **every** group message — including the ones with nothing to
parse. Two rules keep that from turning into chatter:

- **A message with no link is answered only when the bot was addressed directly** —
  an `@mention` of **the bot's own username**, or a reply to one of its own messages.
  Everything else is left alone, so the bot never looks like it is reading the
  conversation. A mention of *anyone else* does not count: an entity of type
  `mention` only tells you a username was written, not whose, and treating every one
  as "addressed to me" put the help text into unrelated conversations (a reply to
  `@shiroko9008` picked up the bot, whose username is `Shiroko9008_Parse_Link_bot`).
- **Authorization is checked *after* a link is found.** It used to run first, which
  meant any message from a non-authorized user was answered with
  "此機器人目前不允許你的帳號使用" — even a plain remark like "i send no link". That
  is the false positive this ordering fixes.

A refused user never reaches the cache, the downloader or Telegraph: the gate runs
before all of them. A regression test primes a cache hit and asserts the lookup is
never made for a refused user.

### One request at a time, not a waiting period
A user may have **one** request in flight. A second one is refused with an
explanation of what is happening, and is accepted the moment the first finishes.

This replaced a flat 10-second cooldown that applied to everything, including
button presses. That version produced an "操作太快了" popup whenever someone picked
a second action right after the first completed — the remaining cooldown was still
counting down even though nothing was running — so it slowed real work down while
adding no protection. A timer answers "how long since the last request", which is
not the question that matters; "is work running right now" is.

Links keep their own cooldown, because sending a link starts real download work and
repeated sending is the abuse case. Buttons are not subject to it.

```ini
USER_COOLDOWN_SECONDS=10   # links only
REQUEST_STALE_SECONDS=1800 # reclaim a slot if a request died without releasing it
```

A slot is always released, including when the work raises, and the stale limit
means no crash can lock a user out permanently.

## Security
- A token exposed in chat should be revoked via `@BotFather` `/revoke`; put the new token only in `.env`.
- Never share `.env` or cookies. `.env` and `cookies/` are gitignored; keep it that way.
- Leave `ALLOWED_USER_IDS` empty for public access; set IDs to restrict usage. `/id` displays the numeric Telegram user and chat IDs.
- Configure `ADMIN_USER_IDS` before exposing the bot publicly; otherwise
  `/cookies` and `/status` are simply unavailable, which is the safe default.
- Only X/Twitter and Pixiv URLs are ever fetched, so a visitor cannot use the bot
  to reach an arbitrary address.
- Public deployment needs stronger abuse protection and monitoring before large-scale use.
- Only download or redistribute content you are authorized to access.

## Limitations
- Public `t.me/<name>` URLs cannot be reliably classified as groups versus channels/users/bots based on URL alone.
- The bot does not join or validate Telegram invite links.
- Only X/Twitter and Pixiv are supported; links on other sites are not fetched.
- X/Twitter frequently returns no content to this kind of client (see above).
- Site changes/authentication can affect gallery-dl.
- Current sample uses polling and a temporary directory; it is a single-process prototype.

## Self-hosted Bot API server (optional, removes the 50 MB ceiling)
The public Bot API caps a bot's upload at **50 MB** and only accepts file bytes. A
self-hosted [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) server raises
that to **2000 MB** and, when started with `--local`, accepts a **local path** instead
of the bytes:

| | Public API | Self-hosted |
| --- | --- | --- |
| Upload | 50 MB | 2000 MB |
| Upload by local path | ❌ | ✅ with `--local` |
| Download | 20 MB | no limit |

This is what would let **歸檔 · 原圖** (a 148 MB archive and up) actually arrive.

### How to get api_id and api_hash
These are **not** the bot token and cannot be created by anyone but you — they identify
an *application*, and Telegram ties them to your account.

1. Open <https://my.telegram.org> and sign in with your **phone number** in
   international format (e.g. `+886912345678`). Telegram sends a login code — look for
   it in the **Telegram app**, in the chat from *Telegram* (not by SMS).
2. Go to **API development tools** (<https://my.telegram.org/apps>).
3. If no app exists, fill in the short form. Nothing is reviewed, but the fields **are**
   validated, and a rejected field shows only a generic `ERROR` dialog — so get these
   right the first time rather than burning attempts:

   | Field | Rule | Example |
   | --- | --- | --- |
   | **App title** | up to 64 chars; no `@`, `#` or other symbols; avoid the reserved word *telegram* | `shiroko9008parselinkbot` |
   | **Short name** | **lowercase letters and digits only**, 5–32 chars, no spaces or hyphens | `skr9008plbot` |
   | **URL** | may be left empty | |
   | **Platform** | pick one, e.g. *Desktop* | |

   **The short name is lowercase-only**, which is the rule most easily missed: the page's
   own hint says only *"alphanumeric, 5–32 characters"* and does **not** mention case, so
   `skr9008PLBot` looks valid and is refused. The submission then sits on *Saving…* and
   raises `my.telegram.org 顯示 ERROR`.

   **That `ERROR` dialog never says why.** The page's own `createApp()` hands the raw
   jQuery XHR object to `alert()` on failure, so a real message from the server is
   replaced by a useless `ERROR` — only a *successful* HTTP response has its body
   alerted. To see the actual reason, run this in the browser console on `/apps`
   (F12 → Console):

   ```js
   $.post('/apps/create', {hash:$('[name=hash]').val(),
     app_title:'shiroko9008parselinkbot', app_shortname:'skr9008plbot',
     app_url:'https://example.com', app_platform:'desktop', app_desc:''})
    .done(t => alert('OK: '+JSON.stringify(t)))
    .fail((x,s,e) => alert('HTTP '+x.status+' :: '+x.responseText));
   ```

   It prints the status and body instead of swallowing them. Interpreting the answer:

   | Result | Meaning |
   | --- | --- |
   | `OK: ...` | created — reload `/apps` to read the `api_id` / `api_hash` |
   | `HTTP 401 :: Unauthorized` | the session or the page's `hash` expired — reload `/apps` and retry |
   | `HTTP 429` or a *too many tries* body | the login rate limit — **stop retrying**, wait hours or change network |
   | any other body text | a field was rejected; the text names which |

4. Submit. The page then shows:
   - **App api_id** — a number, e.g. `1234567`
   - **App api_hash** — 32 hex characters, e.g. `0123456789abcdef0123456789abcdef`
5. Put them in `.env`:

```ini
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
```

6. Sanity-check the format: `python run_local_api.py --verify-credentials`.

**That check only validates the format.** Telegram offers no endpoint that confirms an
`api_id`/`api_hash` pair without authenticated access, so the only real test is to start
the server — it authenticates on startup and fails loudly if they are wrong:

```sh
docker compose up -d && docker compose logs -f
```

Treat both as secrets. Anyone holding them can act as your application, and Telegram can
restrict an account whose credentials are abused. Do **not** paste them into chats,
issues, or commits — `.env` is already in `.gitignore`.

### Setup with Docker (recommended — no compiler needed)
There are no official prebuilt Windows binaries and building one needs a C++17
compiler, CMake, OpenSSL, zlib and gperf. The `aiogram/telegram-bot-api` image avoids
all of that.

1. **Get your own `api_id` and `api_hash`** from
   <https://my.telegram.org> → *API development tools*. These belong to an
   **application**, not to the bot, and cannot be generated for you.
2. **Install Docker Desktop** and start it. On Windows 11 this needs the WSL2 or
   Hyper-V backend, which requires administrator rights.
3. **Put the credentials in `.env`**:

```ini
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
```

4. **Start the server**:

```sh
docker compose up -d
python run_local_api.py --check      # confirms the container answers on 8081
```

5. **Point the bot at it** and restart with `run.bat`:

```ini
TELEGRAM_API_BASE=http://127.0.0.1:8081
TELEGRAM_LOCAL=1
```

`docker-compose.yml` is committed and commented. The environment variables it sets are
the ones the image's own `docker-entrypoint.sh` translates into arguments — notably:

```sh
append_flag_from_env "TELEGRAM_LOCAL" "--local"
```

So `TELEGRAM_LOCAL: "1"` is what produces `--local`. Without it the server keeps the
public 50 MB limit and refuses local paths, and the whole switch gains nothing.
`TELEGRAM_API_ID` and `TELEGRAM_API_HASH` use `:?` in the compose file, so a missing
credential stops compose instead of starting a server that cannot work.

### Setup with a native binary
No Docker, but no compiler either? Point the runner at a binary you built or trust:

```sh
python run_local_api.py --binary "C:\path\to\telegram-bot-api.exe"
```

Build instructions: <https://tdlib.github.io/telegram-bot-api/build.html>.

### `--local` is the flag that matters
Without it the server still enforces the public 50 MB limit, so switching gains
nothing. `TELEGRAM_LOCAL=1` is the bot's own statement that the server runs that way —
the server does not report its mode, and assuming `--local` were on would make **every**
file send fail rather than degrade gracefully.

The bot **probes** the server at startup rather than assuming: it calls `getMe` and only
raises its limits if a server actually answers. An unreachable or misconfigured server
therefore leaves the public limits in force, and `/status` reports which limits are
active and why.

```ini
TELEGRAM_API_BASE=              # empty = use api.telegram.org
TELEGRAM_LOCAL=                 # 1 only if the server runs with --local
```

`run_local_api.py` is a thin, readable wrapper. With Docker available it prefers the
container (no compiler needed); it reads `.env`, checks for the credentials, probes
whether something already answers on port 8081, redacts the `api_hash` when printing a
command line, and reports what is missing. It does **not** build the server.
