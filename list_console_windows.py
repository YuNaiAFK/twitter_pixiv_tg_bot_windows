"""
List every visible console window and the process that owns it.

Written to settle one question: which process puts a window on screen when the bot and its
proxy relay start. EnumWindows needs an instantiated callback - passing the function object
raises "argument 2 must be str, not int" from inside the callback, which silently produces no
results - so the wrapper is built once, here, instead of being retyped each time.

Usage:

    python list_console_windows.py
"""

import ctypes
import sys
from ctypes import wintypes

user32 = ctypes.windll.user32

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def console_windows() -> list[tuple[int, str]]:
    """Every visible window of class ConsoleWindowClass, as (pid, title)."""
    found: list[tuple[int, str]] = []

    def callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if user32.IsWindowVisible(hwnd):
            klass = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, klass, 256)
            if klass.value == "ConsoleWindowClass":
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, title, 512)
                found.append((int(pid.value), title.value))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def process_name(pid: int) -> str:
    """
    An executable name for a pid, for the report. Best effort.

    ``os.listdir("/proc")`` is not it - that is Linux, and on Windows the branch never runs,
    so every line read "?" while looking like it worked.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return "?"
    first = (result.stdout or "").strip().splitlines()
    if not first or "No tasks" in first[0] or "沒有" in first[0]:
        return "?"
    # "name","pid","session","session#","mem"
    return first[0].split('","')[0].strip('"')


def main() -> int:
    rows = console_windows()
    if not rows:
        print("  no visible console windows")
        return 0
    for pid, title in rows:
        print(f"  pid={pid:<8} {process_name(pid):<16} title={title!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
