"""
Run a self-hosted Telegram Bot API server (tdlib/telegram-bot-api).

Why this exists
---------------
The public API caps a bot's upload at 50 MB and only accepts file bytes. A self-hosted
server started with ``--local`` raises that to 2000 MB and accepts a **local path**
instead of the bytes, which is what lets this bot deliver an e-hentai original archive
(~148 MB and up) and a full Pixiv set without splitting or recompressing anything.

Two ways to run the server
--------------------------
**Docker (recommended, no compiler needed).** ``docker-compose.yml`` in this directory
runs the ``aiogram/telegram-bot-api`` image with ``--local``:

    docker compose up -d
    python run_local_api.py --check      # confirms the container answers

**A native binary.** Building one needs a C++17 compiler, CMake, OpenSSL, zlib and
gperf. Point ``--binary`` (or ``TELEGRAM_BOT_API_BIN``) at a built ``telegram-bot-api``
and this script will start it.

Either way the server needs **your own** ``api_id`` and ``api_hash`` from
https://my.telegram.org -> API development tools. These are an application's
credentials, not the bot token, and cannot be created for you.

Usage
-----
    python run_local_api.py --check          # report what is and is not ready
    python run_local_api.py --binary "C:\\path\\telegram-bot-api.exe"
    python run_local_api.py --docker         # docker compose up -d for you

Once it is running, put these in .env and restart the bot:

    TELEGRAM_API_BASE=http://127.0.0.1:8081
    TELEGRAM_LOCAL=1
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
COMPOSE_FILE = HERE / "docker-compose.yml"

# The server accepts one bot, so the bot token has to be the one this project uses.
DEFAULT_PORT = 8081
DEFAULT_BINARY_NAMES = ("telegram-bot-api", "telegram-bot-api.exe")

# The host-side relay the container needs in order to reach Telegram at all. See
# host_proxy_relay.py: the proxy listens on loopback only, which a container cannot reach.
RELAY_SCRIPT = HERE / "host_proxy_relay.py"
RELAY_PORT = 17900
DEFAULT_PROXY_PORT = 7897


def load_env(path: Path) -> dict[str, str]:
    """Read .env into a dict. Missing file is not an error here."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def find_binary(explicit: str, env: dict[str, str]) -> Path | None:
    """
    Locate the server executable.

    Order: the --binary argument, the environment, this directory, then PATH. An
    explicit path that does not exist is reported rather than silently ignored,
    because "it ran the wrong copy" is a confusing failure.
    """
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if env.get("TELEGRAM_BOT_API_BIN"):
        candidates.append(env["TELEGRAM_BOT_API_BIN"])
    if os.environ.get("TELEGRAM_BOT_API_BIN"):
        candidates.append(os.environ["TELEGRAM_BOT_API_BIN"])

    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
        print(f"  ! 指定的執行檔不存在：{path}")

    for name in DEFAULT_BINARY_NAMES:
        beside = HERE / name
        if beside.is_file():
            return beside
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def missing_build_tools() -> list[str]:
    """Which build tools are absent, for the guidance when no binary is found."""
    return [tool for tool in ("cmake", "git") if shutil.which(tool) is None]


def find_docker() -> Path | None:
    """
    Locate the Docker CLI, including where Docker Desktop installs by default.

    ``shutil.which("docker")`` alone is not enough on Windows: Docker Desktop can be
    installed **per user** under ``%LOCALAPPDATA%\\Programs\\DockerDesktop``, which does
    not put ``docker`` on ``PATH``. On this machine that is exactly the case, so the
    earlier version reported "docker compose 不可用" while Docker was installed, running,
    and had the image pulled - a misleading answer that would send someone off to
    reinstall something they already had.
    """
    found = shutil.which("docker")
    if found:
        return Path(found)

    candidates = []
    for root in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles"),
                 os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramData")):
        if not root:
            continue
        candidates += [
            Path(root) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe",
            Path(root) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe",
        ]
    # The CLI also ships as docker-compose only in older installs; the plugin form
    # below needs the docker binary, so that is what is searched for.
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def docker_command() -> list[str] | None:
    """
    The Docker CLI and its compose subcommand, or None.

    ``docker compose`` (v2, a plugin) is preferred over the legacy ``docker-compose``
    standalone, because the v2 form is what current Docker Desktop ships.
    """
    cli = find_docker()
    if cli is None:
        return None
    try:
        probe = subprocess.run([str(cli), "compose", "version"], capture_output=True,
                               text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return [str(cli), "compose"] if probe.returncode == 0 else None


def docker_running(compose: list[str]) -> bool:
    """Whether the compose service is up and its container is running."""
    try:
        probe = subprocess.run(
            compose + ["ps", "--status", "running", "--services"],
            cwd=str(HERE), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(probe.stdout.strip())


def server_answers(port: int) -> bool:
    """
    Whether something is listening on the server's port.

    A bare TCP connect: the Bot API answers ``getMe`` only with a valid token, so this
    checks reachability without needing the token or making an authenticated call.
    """
    import socket

    for host in ("127.0.0.1", "::1"):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(2.0)
                if sock.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def report(binary: Path | None, env: dict[str, str], port: int = DEFAULT_PORT) -> list[str]:
    """Print readiness and return the list of blocking problems."""
    problems: list[str] = []

    print("=== 自架 Bot API 伺服器檢查 ===")
    print()

    api_id = env.get("TELEGRAM_API_ID") or os.environ.get("TELEGRAM_API_ID", "")
    api_hash = env.get("TELEGRAM_API_HASH") or os.environ.get("TELEGRAM_API_HASH", "")
    token = env.get("BOT_TOKEN") or os.environ.get("BOT_TOKEN", "")
    compose = docker_command()

    print(f"  api_id          : {'已設定' if api_id else '缺少'}")
    print(f"  api_hash        : {'已設定' if api_hash else '缺少'}")
    print(f"  BOT_TOKEN       : {'已設定' if token else '缺少'}")
    print(f"  docker compose  : {'可用' if compose else '不可用'}")
    print(f"  伺服器執行檔     : {binary if binary else '找不到'}")
    print(f"  連接埠 {port} 有服務  : {'是' if server_answers(port) else '否'}")
    print()

    if not api_id or not api_hash:
        problems.append(
            "缺少 TELEGRAM_API_ID / TELEGRAM_API_HASH：請到 "
            "https://my.telegram.org -> API development tools 建立應用程式後填入 .env。"
        )
    if not token:
        problems.append("缺少 BOT_TOKEN，伺服器無法綁定你的 bot。")
    # Either a container or a native binary is enough, so only complain when neither
    # route is available.
    if compose is None and binary is None:
        tools = missing_build_tools()
        problems.append(
            "既沒有可用的 docker compose，也找不到 telegram-bot-api 執行檔。"
            "建議安裝 Docker Desktop 後使用 docker compose（不需要編譯器）；"
            "或自行編譯伺服器（需要 C++17 編譯器、CMake、OpenSSL、zlib、gperf，"
            "本機缺少：" + ("、".join(tools) if tools else "未知") + "）"
            "並以 --binary 指定。"
        )

    if problems:
        print("=== 尚未就緒 ===")
        for problem in problems:
            print(f"  • {problem}")
        print()
        print("  官方編譯說明產生器：https://tdlib.github.io/telegram-bot-api/build.html")
        print("  Docker 映像：https://hub.docker.com/r/aiogram/telegram-bot-api")
    else:
        print("=== 就緒 ===")
        if compose is not None:
            state = "執行中" if docker_running(compose) else "尚未啟動"
            print(f"  docker compose：{state}")
            if state == "尚未啟動":
                print("  啟動方式： docker compose up -d")
        if binary is not None:
            print(f"  原生執行檔： python run_local_api.py --binary \"{binary}\"")
        if server_answers(port):
            print(f"  伺服器已在 127.0.0.1:{port} 回應")
            print("  接著在 .env 設定：")
            print(f"    TELEGRAM_API_BASE=http://127.0.0.1:{port}")
            print("    TELEGRAM_LOCAL=1")
            print("  然後重新啟動 bot（run.bat）。")
    return problems


def build_command(binary: Path, env: dict[str, str], port: int,
                  workdir: Path) -> list[str]:
    """
    Assemble the server command line.

    ``--local`` is the important flag: it is what raises the upload ceiling to 2000 MB
    and allows uploading by local path. Without it the server still enforces the public
    50 MB limit, so the bot would gain nothing.
    """
    return [
        str(binary),
        "--api-id", str(env.get("TELEGRAM_API_ID")
                        or os.environ.get("TELEGRAM_API_ID", "")),
        "--api-hash", str(env.get("TELEGRAM_API_HASH")
                          or os.environ.get("TELEGRAM_API_HASH", "")),
        "--local",
        "--http-port", str(port),
        # Keeps the server's working state beside the project.
        "--dir", str(workdir),
        "--temp-dir", str(workdir / "temp"),
        "--log", str(workdir / "telegram-bot-api.log"),
        "--verbosity", "1",
    ]


def credential_report(api_id: str, api_hash: str) -> tuple[bool, bool]:
    """
    Report the format of api_id/api_hash. Returns (format_ok, present).

    Split out from the live check so ``--check`` can report the format without starting
    anything, while ``--verify-credentials`` goes on to ask the server for a verdict.
    """
    print("=== 1. 是否已取得（有沒有值）===")
    present = bool(api_id and api_hash)
    if not present:
        print("  ✗ 尚未取得：.env 裡的 TELEGRAM_API_ID / TELEGRAM_API_HASH 還是空的。")
        print("    沒有看到那兩個值，就代表申請沒有成功。")
        return False, False
    print("  ✓ 已填入兩個值")

    print()
    print("=== 2. 格式是否正確 ===")
    ok = True
    if not api_id.strip().isdigit():
        print(f"  ✗ api_id 必須是純數字，目前是 {api_id!r}")
        ok = False
    else:
        print(f"  ✓ api_id 是數字（{len(api_id.strip())} 位）")
    if api_hash.strip().lower() != api_hash.strip():
        print("  ⚠ api_hash 含有大寫字元；官方值是小寫十六進位，請確認沒有複製錯。")
    try:
        int(api_hash.strip(), 16)
        if len(api_hash.strip()) == 32:
            print("  ✓ api_hash 是 32 位十六進位")
        else:
            print(f"  ⚠ api_hash 長度 {len(api_hash.strip())}，通常是 32 位")
    except ValueError:
        print(f"  ✗ api_hash 不是十六進位：{api_hash!r}")
        ok = False
    return ok, True


def verify_credentials_live(port: int = DEFAULT_PORT) -> int:
    """
    Ask a running server for the verdict on api_id/api_hash.

    This is the **only** honest live check. There is no unauthenticated Telegram
    endpoint that reports whether a pair is valid: presenting it where a bot token goes
    answers "Unauthorized" for a valid pair and an invented one alike, so that was
    measured to be false reassurance and removed.

    A running Bot API server, by contrast, does authenticate upstream, and its answer is
    unambiguous - a bad api_id produces an upstream failure while a good one lets
    getMe through. So the check is: is the local server up, and does it relay a request
    to Telegram successfully.

    Returns 0 when the server confirmed the credentials, 1 otherwise.
    """
    import httpx

    print()
    print("=== 3. 是否真的有效（由自架伺服器向 Telegram 確認）===")
    if not server_answers(port):
        print(f"  ✗ 127.0.0.1:{port} 沒有服務在跑，無法確認。")
        print("    先啟動伺服器，再回來檢查：")
        print("      python run_local_api.py")
        print("      python run_local_api.py --verify-credentials")
        return 1

    token = (load_env(ENV_FILE).get("BOT_TOKEN")
             or os.environ.get("BOT_TOKEN", "")).strip()
    if not token:
        print("  ✗ 缺少 BOT_TOKEN，無法透過伺服器發出請求。")
        return 1

    url = f"http://127.0.0.1:{port}/bot{token}/getMe"
    try:
        response = httpx.get(url, timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 無法連線本機伺服器：{type(exc).__name__}: {exc}")
        return 1

    print(f"  伺服器回應 HTTP {response.status_code}")
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        # The local server answers a non-2xx with an empty body, so the *status* is the
        # evidence. 502 is the Bot API server's own "I could not complete this upstream"
        # and, with credentials as the only upstream dependency at this point, a wrong
        # api_id/api_hash is by far the likeliest cause - so say that rather than
        # blaming the server for an unparseable body, which sent the reader looking in
        # the wrong place.
        body = response.text.strip()[:160]
        if body:
            print(f"  回應內容：{body!r}")
        if response.status_code == 502:
            print("  ✗ 伺服器無法向 Telegram 認證 —— api_id/api_hash 很可能是錯的。")
            print("    請再看啟動日誌確認： docker logs telegram-bot-api --tail 30")
        elif response.status_code in (401, 403):
            print("  ✗ 憑證被拒絕。")
        else:
            print("  ✗ 未能確認，請看伺服器日誌： docker logs telegram-bot-api --tail 30")
        return 1

    if payload.get("ok"):
        who = (payload.get("result") or {}).get("username", "?")
        print(f"  ✓ 憑證有效：伺服器成功向 Telegram 認證（@{who}）")
        print()
        print("  下一步：在 .env 設定")
        print(f"    TELEGRAM_API_BASE=http://127.0.0.1:{port}")
        print("    TELEGRAM_LOCAL=1")
        print("  然後用 run.bat 重啟 bot。")
        return 0

    description = str(payload.get("description") or "")
    print(f"  錯誤訊息：{description!r}")
    if "Unauthorized" in description or response.status_code in (401, 403):
        print("  ✗ 憑證無效：伺服器有回應，但 Telegram 拒絕了它。")
    elif response.status_code == 502:
        print("  ✗ 伺服器無法向 Telegram 認證 —— api_id/api_hash 很可能是錯的。")
        print("    也可能是啟動時就失敗了，請看： docker compose logs --tail 30")
    else:
        print("  ✗ 未能確認，請看伺服器日誌： docker compose logs --tail 30")
    return 1


def verify_credentials(api_id: str, api_hash: str) -> int:
    """
    Report the presence and format of api_id/api_hash, then say how to confirm validity.

    Format checking is all that can be done offline, and the reason is recorded rather
    than glossed over: the obvious online attempt does not work, because Telegram
    answers "Unauthorized" identically for a valid pair and an invented one. Only a
    running server can answer, which is what ``--verify-credentials`` does when one is
    already up.

    Returns a process exit code: 0 when the format is plausible, 1 when it is not.
    """
    print("=== 檢查 api_id / api_hash ===")
    print()
    ok, present = credential_report(api_id, api_hash)
    if not present:
        return 1

    # If a server is already running, settle the question now instead of asking the
    # operator to interpret logs.
    if server_answers(DEFAULT_PORT):
        return verify_credentials_live(DEFAULT_PORT)

    print()
    print("  格式沒問題，但**這還不能證明它有效** ——")
    print("  Telegram 沒有提供未經驗證就能檢查 api_id/api_hash 的端點。")
    print("  唯一可靠的確認方式是啟動伺服器，讓它向 Telegram 認證：")
    print()
    print("    python run_local_api.py                    # 啟動")
    print("    python run_local_api.py --verify-credentials   # 取得判定")
    return 0 if ok else 1


def port_listening(port: int, host: str = "127.0.0.1") -> bool:
    """Whether something is listening on a loopback port. A bare TCP connect."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.5)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


def docker_daemon_up(cli: Path) -> bool:
    """
    Whether the Docker *daemon* is answering, not merely whether the CLI exists.

    This is the distinction that matters at startup: Docker Desktop can be installed and
    its CLI on disk while the engine is stopped, and every compose command then fails with
    a named-pipe error rather than anything a user can act on.
    """
    try:
        probe = subprocess.run([str(cli), "info"], capture_output=True, text=True,
                               timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def find_docker_desktop() -> Path | None:
    """The Docker Desktop executable, which starts the engine when launched."""
    if os.name != "nt":
        return None
    candidates = []
    for root in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA"),
                 os.environ.get("ProgramFiles(x86)")):
        if not root:
            continue
        candidates.extend([
            Path(root) / "Docker" / "Docker" / "Docker Desktop.exe",
            Path(root) / "Programs" / "DockerDesktop" / "Docker Desktop.exe",
        ])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def upstream_proxy_port(env: dict[str, str]) -> int:
    """
    The host proxy's port, which the relay forwards to.

    **Not** the port in ``TELEGRAM_MT_PROXY``: that is the relay's own address as the
    container sees it (``socks5://192.168.65.254:17900``), so reading it here produced
    17900 as the upstream - a relay pointing at itself. The proxy port comes from the
    SOCKS5 setting the bot itself uses.
    """
    for name in ("TELEGRAM_PROXY", "ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        value = (env.get(name) or os.environ.get(name) or "").strip()
        if not value:
            continue
        tail = value.rstrip("/").rsplit(":", 1)[-1]
        try:
            port = int(tail)
        except ValueError:
            continue
        if 0 < port < 65536:
            return port
    return DEFAULT_PROXY_PORT


def start_relay(proxy_port: int, relay_port: int) -> bool:
    """
    Start the host relay if it is not already listening. Returns whether it is up.

    Detached on purpose: the relay has to outlive this script, because it is what the
    container uses for every request to Telegram. The bot's own shell is not the right
    parent for it.
    """
    if port_listening(relay_port):
        print(f"  轉接埠 {relay_port}：已在執行")
        return True

    python = sys.executable
    if not RELAY_SCRIPT.is_file():
        print(f"  找不到 {RELAY_SCRIPT.name}，容器將無法連上 Telegram。")
        return False

    print(f"  啟動轉接（{relay_port} -> 127.0.0.1:{proxy_port}）……")
    creation = 0
    if os.name == "nt":
        # DETACHED_PROCESS so it is not killed with this console, and no window.
        # The name has no CREATE_ prefix; CREATE_DETACHED_PROCESS does not exist.
        creation = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [python, str(RELAY_SCRIPT),
             "--list-port", str(relay_port), "--proxy-port", str(proxy_port)],
            cwd=str(HERE),
            creationflags=creation,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        print(f"  啟動轉接失敗：{exc}")
        return False

    for _ in range(20):
        if port_listening(relay_port):
            print(f"  轉接埠 {relay_port}：已啟動")
            return True
        time.sleep(0.5)
    print(f"  轉接埠 {relay_port}：啟動後仍未監聽")
    return False


def wait_for_daemon(cli: Path, timeout: float = 240.0) -> bool:
    """Wait for the Docker engine, launching Docker Desktop first if it is not running."""
    if docker_daemon_up(cli):
        return True

    desktop = find_docker_desktop()
    if desktop is not None:
        print(f"  Docker 引擎尚未啟動，正在開啟 {desktop.name}……")
        try:
            subprocess.Popen([str(desktop)], close_fds=True)
        except OSError as exc:
            print(f"  無法開啟 Docker Desktop：{exc}")
    else:
        print("  找不到 Docker Desktop，請手動啟動 Docker。")

    print("  等待 Docker 引擎就緒（首次啟動可能需要一兩分鐘）……")
    deadline = time.monotonic() + timeout
    shown = 0
    while time.monotonic() < deadline:
        if docker_daemon_up(cli):
            print("  Docker 引擎：就緒")
            return True
        time.sleep(3.0)
        shown += 3
        if shown % 30 == 0:
            print(f"    已等待 {shown} 秒……")
    print(f"  Docker 引擎在 {int(timeout)} 秒內沒有就緒。")
    return False


def ensure(env: dict[str, str], port: int = DEFAULT_PORT) -> int:
    """
    Bring up everything the bot needs to reach Telegram, and wait until it is ready.

    Called by run.bat before the bot starts, so a first run on a machine where nothing has
    been started yet works without any manual steps. Idempotent: everything already running
    is detected and left alone, so a second run does nothing visible.

    Three things have to be up, and they fail in ways that do not name the real problem:

      1. the Docker engine  - without it, compose fails on a named pipe
      2. the host relay     - without it, the container cannot reach Telegram at all, and
                              TDLib reports it as a generic connection failure
      3. the server itself  - without it, the bot falls back to the public API and the
                              upload limit silently drops from 2000 MB to 50 MB
    """
    print("檢查自架 Bot API 伺服器……")
    env_port = env.get("TELEGRAM_API_BASE", "")
    configured = port
    if env_port.startswith("http"):
        try:
            configured = int(env_port.rstrip("/").rsplit(":", 1)[-1])
        except ValueError:
            configured = port
    proxy_port = upstream_proxy_port(env)

    if server_answers(configured):
        print(f"  伺服器（port {configured}）：已在執行，不需要啟動")
        # The relay is still checked: the server can be up while the relay it needs has
        # died, and that shows up later as a Telegram connection failure.
        start_relay(proxy_port, RELAY_PORT)
        return 0

    cli = find_docker()
    compose = docker_command()
    if cli is None or compose is None:
        print("  找不到可用的 Docker／docker compose。")
        print("  請安裝 Docker Desktop，或改用原生 telegram-bot-api 執行檔：")
        print("    python run_local_api.py --binary <路徑>")
        return 1

    # The relay first: the container needs it as soon as it starts, and starting it later
    # would leave the first requests failing.
    relay_ok = start_relay(proxy_port, RELAY_PORT)

    if not wait_for_daemon(cli):
        return 1

    if docker_running(compose):
        print("  容器：已在執行")
    else:
        print("  啟動容器：", " ".join(compose + ["up", "-d"]))
        code = subprocess.call(compose + ["up", "-d"], cwd=str(HERE))
        if code != 0:
            print(f"  docker compose 失敗（離開碼 {code}）。")
            return code

    print("  等待伺服器就緒……")
    for _ in range(60):
        if server_answers(configured):
            print(f"  伺服器（port {configured}）：就緒")
            if not relay_ok:
                print("  ⚠ 轉接未啟動，容器可能無法連上 Telegram。")
            return 0
        time.sleep(2.0)
    print("  伺服器在 120 秒內沒有就緒。查看日誌： docker compose logs -f")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a self-hosted Telegram Bot API server for this bot.")
    parser.add_argument("--binary", default="",
                        help="Path to a native telegram-bot-api executable.")
    parser.add_argument("--docker", action="store_true",
                        help="Start the server with docker compose instead of a binary.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"HTTP port (default {DEFAULT_PORT}).")
    parser.add_argument("--check", action="store_true",
                        help="Only report readiness; do not start anything.")
    parser.add_argument("--ensure", action="store_true",
                        help="Start whatever is missing and wait until the server is "
                             "ready. Used by run.bat so a first run works with no manual "
                             "steps, and does nothing when everything is already up.")
    parser.add_argument("--verify-credentials", action="store_true",
                        help="Check api_id/api_hash against Telegram, then exit.")
    parser.add_argument("--workdir", default=str(HERE / "bot-api-data"),
                        help="Where a native server keeps its state.")
    args = parser.parse_args()

    env = load_env(ENV_FILE)
    if args.verify_credentials:
        return verify_credentials(
            env.get("TELEGRAM_API_ID") or os.environ.get("TELEGRAM_API_ID", ""),
            env.get("TELEGRAM_API_HASH") or os.environ.get("TELEGRAM_API_HASH", ""))

    # Before the readiness report, because --ensure is what brings things up.
    if args.ensure:
        return ensure(env, args.port)

    binary = find_binary(args.binary, env)
    problems = report(binary, env, args.port)

    compose = docker_command()
    # Docker is the default when it is available and no binary was named: it needs no
    # compiler, which is the whole reason it is recommended.
    use_docker = args.docker or (binary is None and compose is not None)

    if args.check:
        return 1 if problems else 0
    if problems:
        return 1

    if use_docker:
        if compose is None:
            print("  docker compose 不可用，無法用 Docker 啟動。")
            return 1
        if not COMPOSE_FILE.is_file():
            print(f"  找不到 {COMPOSE_FILE.name}，無法用 Docker 啟動。")
            return 1
        print()
        print("  啟動容器：", " ".join(compose + ["up", "-d"]))
        code = subprocess.call(compose + ["up", "-d"], cwd=str(HERE))
        if code != 0:
            print(f"  docker compose 失敗（離開碼 {code}）。")
            return code
        print()
        print("  已啟動。查看日誌： docker compose logs -f")
        print("  接著在 .env 設定：")
        print(f"    TELEGRAM_API_BASE=http://127.0.0.1:{args.port}")
        print("    TELEGRAM_LOCAL=1")
        print("  然後重新啟動 bot（run.bat）。")
        return 0

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "temp").mkdir(parents=True, exist_ok=True)

    command = build_command(binary, env, args.port, workdir)
    shown = list(command)
    # The api_hash is a credential; do not print it.
    for flag in ("--api-hash",):
        if flag in shown:
            shown[shown.index(flag) + 1] = "***"
    print()
    print("  啟動：", " ".join(shown))
    print()
    print(f"  伺服器位址： http://127.0.0.1:{args.port}")
    print("  接著在 .env 設定：")
    print(f"    TELEGRAM_API_BASE=http://127.0.0.1:{args.port}")
    print("    TELEGRAM_LOCAL=1")
    print("  然後重新啟動 bot。按 Ctrl+C 結束伺服器。")
    print()

    try:
        return subprocess.call(command)
    except KeyboardInterrupt:
        print("\n  已停止。")
        return 0
    except OSError as exc:
        print(f"  啟動失敗：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
