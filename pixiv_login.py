"""
Pixiv OAuth in one local process, so the code and its PKCE verifier match.

Why this exists instead of just using `gallery-dl oauth:pixiv`: the authorization
code is bound to the PKCE ``code_verifier`` of the process that requested it, and
that verifier never leaves the process. A code copied to another process (or to
another person) can therefore never be exchanged, and Pixiv reports that with the
same ``invalid_request`` it uses for an expired code, so the failure is opaque.
This script keeps both halves in one run.

Two modes:
    python pixiv_login.py           # manual: paste the code (or callback URL)
    python pixiv_login.py --auto    # captures the code automatically via Chrome

The manual path is the default because it depends on nothing but a browser.
"""

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from base64 import b64encode
from pathlib import Path

# --auto needs the `websockets` package for Chrome's DevTools Protocol. It may
# only be present in the local vendor folder when pip cannot write to
# site-packages, so that folder is added the same way bot.py does it.
_VENDOR = Path(__file__).resolve().parent / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.append(str(_VENDOR))

CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"

ENV_PATH = Path(__file__).resolve().parent / ".env"

# The callback request carrying the code looks like .../callback?state=..&code=..
CODE_IN_URL = re.compile(r"[?&]code=([^&\s]+)")

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def make_verifier() -> str:
    """gallery-dl's util.generate_token(32): 64 hex characters."""
    return os.urandom(32).hex()


def challenge_for(verifier: str) -> str:
    """
    Build the S256 challenge the way gallery-dl does.

    binascii.b2a_base64 appends a newline, and gallery-dl drops the last two
    bytes of its output - the '=' padding and that newline - before swapping
    + and / for the URL-safe - and _. Standard base64 plus rstrip("=") is
    equivalent and yields the 43 characters PKCE expects. Using
    urlsafe_b64encode instead would already have replaced those characters and
    would therefore produce a different string.
    """
    digest = hashlib.sha256(verifier.encode()).digest()
    return b64encode(digest).decode().rstrip("=").replace("+", "-").replace("/", "_")


def exchange(code: str, verifier: str):
    """POST the token request exactly as gallery-dl's OAuthPixiv does."""
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "include_policy": "true",
        "redirect_uri": REDIRECT_URI,
    }
    request = urllib.request.Request(
        "https://oauth.secure.pixiv.net/auth/token",
        data=urllib.parse.urlencode(data).encode(),
        headers={
            "User-Agent": "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


# -------------------- manual capture --------------------

def read_code_manual() -> str:
    raw = input("把 code（或整条 callback 网址）粘贴到这里，然后回车：").strip()
    match = CODE_IN_URL.search(raw)
    if match:
        raw = match.group(1)
        print("已从网址中取出 code")
    return urllib.parse.unquote(raw)


# -------------------- automatic capture --------------------


def find_browser() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def cdp_capture(browser: str, login_url: str, timeout: float = 240.0) -> str | None:
    """
    Drive a throwaway Chrome and read the authorization code off the wire.

    DevTools' Network tab is the documented way to find the code, but it is easy
    to miss and the code dies 30 seconds after login. Chrome's DevTools Protocol
    exposes the same request stream programmatically, so the code can be read the
    moment the browser issues it.

    Ordering matters: the browser is started on a blank page, the network
    listener is attached, and only then is the login URL navigated to. Handing
    the URL to the browser at launch time loses the callback request when the
    page redirects before the listener is ready.

    A dedicated --user-data-dir is used so this never touches the login state or
    tabs of the user's normal browser profile.
    """
    port = free_port()
    profile = Path(tempfile.mkdtemp(prefix="pixiv_oauth_"))
    process = subprocess.Popen(
        [
            browser,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        # Wait for the debugging endpoint to answer.
        version_url = f"http://127.0.0.1:{port}/json/version"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(version_url, timeout=2) as response:
                    json.load(response)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.4)
        else:
            print("无法连接到浏览器的调试端口。")
            return None

        try:
            from websockets.sync.client import connect
        except ImportError:
            print("缺少 websockets 库，无法自动抓取。请改用手动模式，")
            print("或运行：.venv\\Scripts\\python.exe -m pip install websockets")
            return None

        # The blank page target. Subscribe before navigating, so the callback
        # request cannot slip past.
        deadline = time.monotonic() + 15
        page = None
        while page is None and time.monotonic() < deadline:
            try:
                targets = json.loads(
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json", timeout=5
                    ).read()
                )
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
                continue
            page = next(
                (t for t in targets
                 if t.get("type") == "page" and t.get("webSocketDebuggerUrl")),
                None,
            )
            if page is None:
                time.sleep(0.3)

        if page is None:
            print("没有找到可用的浏览器页面。")
            return None

        print("已打开浏览器，请在其中登录 Pixiv（无需打开开发者工具）。")
        print("登录完成后会自动抓取 code，请稍候……")

        deadline = time.monotonic() + timeout
        with connect(page["webSocketDebuggerUrl"], max_size=None) as socket_conn:
            socket_conn.send(json.dumps({"id": 1, "method": "Network.enable"}))
            socket_conn.send(json.dumps({
                "id": 2,
                "method": "Page.navigate",
                "params": {"url": login_url},
            }))

            while time.monotonic() < deadline:
                try:
                    raw = socket_conn.recv(timeout=5)
                except TimeoutError:
                    continue
                except Exception:  # noqa: BLE001
                    break

                try:
                    message = json.loads(raw)
                except ValueError:
                    continue

                method = message.get("method", "")
                params = message.get("params", {})

                # The redirect target is checked too: some callbacks are reached
                # via a document navigation rather than an XHR.
                urls = []
                if method == "Network.requestWillBeSent":
                    urls.append(params.get("request", {}).get("url", ""))
                elif method == "Page.frameNavigated":
                    urls.append(params.get("frame", {}).get("url", ""))

                for url in urls:
                    if "/auth/pixiv/callback" not in url:
                        continue
                    match = CODE_IN_URL.search(url)
                    if match:
                        print("已自动抓到 code。")
                        return urllib.parse.unquote(match.group(1))

        print("等待超时，没有抓到 code。")
        return None
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:  # noqa: BLE001
            process.kill()


# -------------------- result handling --------------------


def save_token(token: str) -> None:
    if not ENV_PATH.is_file():
        print(f"未找到 {ENV_PATH}，请手动把上面的值填入 PIXIV_REFRESH_TOKEN。")
        return

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    updated, replaced = [], False
    for line in lines:
        if line.startswith("PIXIV_REFRESH_TOKEN="):
            updated.append(f"PIXIV_REFRESH_TOKEN={token}")
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(f"PIXIV_REFRESH_TOKEN={token}")
    ENV_PATH.write_text("\n".join(updated) + "\n", encoding="utf-8")
    print(f"已写入 {ENV_PATH}")
    print("下一步：运行 .venv\\Scripts\\python.exe bot.py --check 验证")


def main() -> int:
    auto = "--auto" in sys.argv
    verifier = make_verifier()
    query = urllib.parse.urlencode({
        "client": "pixiv-android",
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "S256",
    })
    login_url = f"{LOGIN_URL}?{query}"

    print("=" * 72)
    print("Pixiv 登录" + ("（自动抓取）" if auto else "（手动粘贴）"))
    print("=" * 72)
    print()

    if auto:
        browser = find_browser()
        if browser is None:
            print("没有找到 Chrome/Edge，请改用手动模式：python pixiv_login.py")
            return 1
        print("使用浏览器：", browser)
        code = cdp_capture(browser, login_url)
        if not code:
            print()
            print("自动抓取失败，请改用手动模式：")
            print("  .venv\\Scripts\\python.exe pixiv_login.py")
            return 1
    else:
        print("即将打开浏览器。请：")
        print("  1. 按 F12，切到 Network（网络）标签")
        print("  2. 在页面里登录 Pixiv")
        print("  3. 在 Network 里找最后一条 callback?state=... 请求")
        print("  4. 复制它的 code 参数（登录后 30 秒内有效）")
        print("  5. 回到这里粘贴并回车")
        print()
        print("提示：加 --auto 参数可以自动抓取，省去这些步骤。")
        print()
        print("如果浏览器没自动打开，手动访问：")
        print(login_url)
        print()
        try:
            webbrowser.open(login_url)
        except Exception as exc:  # noqa: BLE001
            print(f"（自动打开浏览器失败：{exc}）")
        try:
            code = read_code_manual()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 1

    if not code:
        print("没有读到 code。")
        return 1

    print()
    print("正在用本次生成的 code_verifier 交换 refresh-token ...")
    status, body = exchange(code, verifier)

    try:
        payload = json.loads(body)
    except ValueError:
        payload = None

    if payload and payload.get("refresh_token"):
        token = payload["refresh_token"]
        print()
        print("=" * 72)
        print("成功取得 refresh-token")
        print("=" * 72)
        print()
        print(token)
        print()
        save_token(token)
        return 0

    print()
    print("失败。Pixiv 返回：")
    print(json.dumps(payload, indent=2, ensure_ascii=False) if payload else body[:600])
    print()
    print("如果 error 是 invalid_request / invalid_grant，说明 code 已过期")
    print("（登录后 30 秒内有效），重新运行本脚本再试一次即可。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
