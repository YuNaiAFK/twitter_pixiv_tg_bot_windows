"""
Install Pillow into `vendor/` without pip.

Use this only when ``python -m pip install -r requirements.txt`` cannot write to
site-packages on this machine (a locked-down or sandboxed host). pip fails there
with a PermissionError while writing wheel metadata; a wheel is just a zip whose
contents map onto a site-packages layout, so unpacking it into ``vendor/``
achieves the same result. bot.py appends ``vendor/`` to sys.path only when PIL is
not importable from the normal locations, so a real pip install always wins.

Run with the project's own interpreter:

    .venv\\Scripts\\python.exe install_pillow_offline.py
"""

import json
import sys
import urllib.request
import zipfile
from pathlib import Path

PYPI_JSON = "https://pypi.org/pypi/Pillow/json"
VENDOR = Path(__file__).resolve().parent / "vendor"


def find_wheel(data: dict) -> dict | None:
    """
    Pick the wheel matching this interpreter exactly.

    The tag comparison is on whole filename segments: "cp314" is a substring of
    the free-threaded "cp314t" build, which installs but fails to import.
    """
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    version = data["info"]["version"]

    candidates = [
        item for item in data["releases"].get(version, [])
        if item["packagetype"] == "bdist_wheel"
        and tag in item["filename"].split("-")
        and "win_amd64" in item["filename"]
    ]
    if not candidates:
        print(f"没有找到 Pillow {version} 的 {tag} win_amd64 wheel")
        return None
    return candidates[0]


def main() -> int:
    print(f"Python {sys.version.split()[0]}")

    with urllib.request.urlopen(PYPI_JSON, timeout=60) as response:
        data = json.load(response)

    chosen = find_wheel(data)
    if chosen is None:
        return 1

    print(f"下载 {chosen['filename']}")
    wheel_path = VENDOR.parent / "_pillow.whl"
    with urllib.request.urlopen(chosen["url"], timeout=600) as response:
        wheel_path.write_bytes(response.read())
    print(f"已下载 {wheel_path.stat().st_size} 字节")

    VENDOR.mkdir(exist_ok=True)
    with zipfile.ZipFile(wheel_path) as archive:
        archive.extractall(VENDOR)
    wheel_path.unlink()

    print(f"已解压到 {VENDOR}")

    # Prove the unpacked copy really imports, including its bundled DLLs.
    sys.path.insert(0, str(VENDOR))
    try:
        import PIL
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        print(f"导入失败：{exc}")
        return 1

    print(f"Pillow {PIL.__version__} 可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
