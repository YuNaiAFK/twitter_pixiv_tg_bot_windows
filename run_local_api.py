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
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
COMPOSE_FILE = HERE / "docker-compose.yml"

# The server accepts one bot, so the bot token has to be the one this project uses.
DEFAULT_PORT = 8081
DEFAULT_BINARY_NAMES = ("telegram-bot-api", "telegram-bot-api.exe")


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


def verify_credentials(api_id: str, api_hash: str) -> int:
    """
    Check the **format** of api_id/api_hash and that Telegram is reachable.

    This deliberately makes no claim that the pair is *valid*, because there is no
    unauthenticated endpoint that would tell us. The obvious attempt - presenting the
    pair where a bot token goes - does not work: Telegram answers "Unauthorized" for
    any malformed token, so a correct pair and an invented one produce the **same**
    reply. Using that as a pass would be false reassurance, and was measured to be
    exactly that before this was rewritten.

    The only real check is to start the server: it authenticates on startup and fails
    loudly if the credentials are wrong. ``docker compose logs`` shows that.

    Returns a process exit code: 0 when the format is plausible, 1 when it is not.
    """
    import httpx

    print("=== 檢查 api_id / api_hash 格式 ===")
    print()
    ok = True
    if not api_id or not api_hash:
        print("  ✗ 尚未填寫，請先在 .env 設定 TELEGRAM_API_ID 與 TELEGRAM_API_HASH。")
        return 1
    if not api_id.strip().isdigit():
        print(f"  ✗ api_id 應該是一串數字，目前是 {api_id!r}。")
        ok = False
    else:
        print(f"  ✓ api_id 格式正確（數字，{len(api_id.strip())} 位）")
    if api_hash.strip().lower() != api_hash.strip():
        print("  ⚠ api_hash 含有大寫；官方值是小寫十六進位，請確認沒有複製錯誤。")
    try:
        int(api_hash.strip(), 16)
        if len(api_hash.strip()) == 32:
            print("  ✓ api_hash 格式正確（32 位十六進位）")
        else:
            print(f"  ⚠ api_hash 長度是 {len(api_hash.strip())}，通常是 32")
    except ValueError:
        print(f"  ✗ api_hash 不是十六進位：{api_hash!r}")
        ok = False

    try:
        httpx.get("https://api.telegram.org", timeout=20.0)
        print("  ✓ 可以連線 api.telegram.org")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 無法連線 api.telegram.org：{type(exc).__name__}")
        ok = False

    print()
    print("  注意：這只檢查格式，**無法**確認憑證是否真的有效 ——")
    print("  Telegram 沒有提供未經驗證就能檢查 api_id/api_hash 的端點。")
    print("  真正的驗證會在伺服器啟動時發生：")
    print("    docker compose up -d && docker compose logs -f")
    return 0 if ok else 1


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
