"""
Verify the real main() boot path without polling Telegram.

Why this exists
---------------
`--dry-run` returns **before** the API probe, so it can never show the raised upload
limit - that is by design, but it means the only was to check the limit used to be
starting the bot for real. This runs `main()` with polling replaced by a recorder, so
the whole startup path executes, the probe runs, and nothing touches Telegram's
getUpdates queue. It is a check, not part of the bot.

Exit code 0 means the boot path completed and the limits were raised.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent / "vendor"))

import bot  # noqa: E402
from telegram.ext import Application  # noqa: E402

recorded = {}

_original_run_polling = Application.run_polling


def fake_run_polling(self, *args, **kwargs):
    """Stand in for polling: keep the built application, do nothing with it."""
    recorded["app"] = self
    recorded["polling_args"] = kwargs


Application.run_polling = fake_run_polling
bot.Application.run_polling = fake_run_polling

# This never reaches polling, so it cannot be a second poller and must not be refused by the
# single-instance guard. Without this the check fails whenever the bot is running, which looks
# like a boot-path regression and is really the guard working correctly.
os.environ["BOT_SKIP_INSTANCE_LOCK"] = "1"

try:
    bot.main()
except SystemExit as exc:
    # main() may exit cleanly after building; that is still a successful boot.
    recorded["system_exit"] = exc.code

app = recorded.get("app")
print()
print("=== boot path result ===")
print(f"  main() reached polling : {'yes' if app is not None else 'NO'}")
print(f"  upload limit now       : {bot.upload_limit_bytes() // 1024 // 1024} MB")
print(f"  uploads by local path  : {bot.can_upload_by_path()}")
print(f"  API root               : {bot.telegram_server_root() or '(public api.telegram.org)'}")

ok = app is not None and bot.upload_limit_bytes() // 1024 // 1024 == 2000
print()
print("  RESULT:", "PASS - the boot path runs and the limit is raised" if ok
      else "FAIL - see the values above")
sys.exit(0 if ok else 1)
