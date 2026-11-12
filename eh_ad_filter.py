"""本機辨識已確認廣告樣本與明確導流／商業推廣；不輸出圖片或 OCR 文字。"""
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from PIL import Image, ImageOps, UnidentifiedImageError
import ehentai
from eh_ad_rules import has_external_ads


class AdFilterError(ehentai.EhentaiError):
    pass


@dataclass
class AdFilterReport:
    kept: list
    removed: list


def ocr_binary():
    configured = os.getenv("AD_OCR_BINARY", "").strip()
    candidate = configured or shutil.which("tesseract")
    if not candidate and os.name == "nt":
        candidate = str(Path(os.getenv("ProgramFiles", "C:/Program Files")) / "Tesseract-OCR/tesseract.exe")
    if not candidate or not Path(candidate).is_file():
        raise AdFilterError("缺少本機 OCR（Tesseract）；本次未上傳，請完成去廣告依賴安裝。")
    try:
        subprocess.run([candidate, "--version"], check=True, capture_output=True, timeout=8)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdFilterError("本機 OCR 執行失敗；本次未上傳，請檢查 Tesseract 與執行庫。") from exc
    return candidate


def signature(image):
    gray = ImageOps.exif_transpose(image).convert("L")
    a = list(gray.resize((17, 16), Image.Resampling.BICUBIC).get_flattened_data())
    b = list(gray.resize((16, 16), Image.Resampling.BICUBIC).get_flattened_data())
    avg = sum(b) / len(b)
    return (sum(int(a[y*17+x] > a[y*17+x+1]) << (y*16+x) for y in range(16) for x in range(16)),
            sum(int(value > avg) << index for index, value in enumerate(b)))


def matches_sample(value, samples):
    return any((value[0] ^ sample[0]).bit_count() <= 18 and
               (value[1] ^ sample[1]).bit_count() <= 18 for sample in samples)


def text_ad_reason(text):
    value = re.sub(r"\s+", "", text).casefold().replace("：", ":")
    # 已確認廣告的導流網域；一般作者／販售平台網址不在此名單。
    known = ("mdaa11.com", "jm-3x.cc", "17mimei.live", "astonekm.net", "anime.bllaw-0.com")
    if any(domain in value for domain in known):
        return "confirmed-ad-domain"
    has_url = bool(re.search(r"(?:https?://|www\.)[a-z0-9]|[a-z0-9-]+\.(?:app|com|net|cc|xyz|live)(?:[/\s]|$)", value))
    # 作者資訊頁可有 QR／網址；必須同時有商業或全站導流語句才排除。
    if any(site in value for site in ("pixiv.net", "fanbox.cc", "patreon.com", "booth.pm", "dlsite.com", "melonbooks")):
        return ""
    scan = any(word in value for word in ("扫码", "掃碼", "扫描二维码", "掃描二維碼"))
    commerce = any(word in value for word in ("优惠券", "優惠券", "全网最低", "全網最低", "淘宝搜索", "淘寶搜索"))
    platform = any(word in value for word in ("全站免费", "全站免費", "永久免费", "永久免費", "成人app", "海量漫画", "海量漫畫", "免费下载", "免費下載", "在线观看", "在線觀看"))
    action = any(word in value for word in ("下载", "下載", "安装", "安裝", "进入", "進入", "领取", "領取", "注册", "註冊"))
    if scan and commerce:
        return "commerce-and-scan"
    if platform and (has_url or scan or action):
        return "external-platform-promotion"
    return ""


def open_for_analysis(data, directory):
    source = Path(directory) / "source.bin"
    source.write_bytes(data)
    try:
        return Image.open(io.BytesIO(data))
    except UnidentifiedImageError:
        # 部分 Termux Pillow 沒有 WebP 模組，沿用系統 libwebp 解碼器。
        decoder = shutil.which("dwebp")
        if data[:4] != b"RIFF" or not decoder:
            raise
        target = Path(directory) / "decoded.png"
        subprocess.run([decoder, str(source), "-o", str(target)], check=True, capture_output=True, timeout=20)
        return Image.open(target)


def inspect_image(data, samples, binary, tessdata):
    with tempfile.TemporaryDirectory(prefix="eh-ad-") as directory:
        with open_for_analysis(data, directory) as image:
            if matches_sample(signature(image), samples):
                return "confirmed-sample"
            gray = ImageOps.autocontrast(ImageOps.exif_transpose(image).convert("L"))
            gray.thumbnail((1600, 2400))
            env = dict(os.environ, OMP_THREAD_LIMIT="1")
            text = ""
            for region, psm in ((gray, "11"), (gray.crop((0, gray.height//2, gray.width, gray.height)), "6")):
                target = Path(directory) / "ocr.png"
                region.save(target)
                result = subprocess.run([binary, str(target), "stdout", "--tessdata-dir", str(tessdata),
                                         "-l", "chi_sim+eng", "--psm", psm],
                                        capture_output=True, text=True, encoding="utf-8", timeout=25, check=True, env=env)
                text += "\n" + result.stdout
                reason = text_ad_reason(text)
                if reason:
                    return reason
    return ""


def ad_candidate_pages(count):
    # 圖片頁碼從 1 起算；前三張永遠不取圖、不辨識、不排除。
    return range(max(4, count - 9), count + 1)


def scan_images(urls, gallery, *, cookies_path=None, stop_event=None, fetcher=None, inspector=None, progress=None):
    items = list(urls)
    candidates = list(ad_candidate_pages(len(items)))
    if not has_external_ads(gallery) or not candidates:
        return AdFilterReport(items, [])
    binary = ocr_binary()
    base = Path(__file__).resolve().parent
    tessdata = base / "tessdata"
    if not all((tessdata / (lang + ".traineddata")).is_file() for lang in ("chi_sim", "eng")):
        raise AdFilterError("去廣告 OCR 語言模型缺失；本次未上傳。")
    samples = [(int(s["dhash"], 16), int(s["ahash"], 16)) for s in json.loads((base / "ad_samples.json").read_text())["samples"]]
    def one(item):
        page, url = item
        if stop_event is not None and stop_event.is_set():
            raise AdFilterError("已停止廣告檢查；本次未上傳。")
        try:
            if fetcher is not None:
                data = fetcher(url)
            else:
                import httpx, r2
                with httpx.Client(**ehentai.http_client_kwargs(25)) as client:
                    data, _ = r2.fetch_image(url, referer=gallery.page_url, cookies=None, http_client=client)
                    # public_embed_urls 已將需要登入的網址轉成可公開讀取的圖片網址。
            reason = (inspector or inspect_image)(data, samples, binary, tessdata)
            return page, url, reason
        except AdFilterError:
            raise
        except Exception as exc:
            raise AdFilterError(f"第 {page} 頁無法完成廣告檢查（{type(exc).__name__}）；本次未上傳。") from exc
    removed = []
    pool = ThreadPoolExecutor(max_workers=2)
    futures = [pool.submit(one, (page, items[page - 1])) for page in candidates]
    try:
        for done, future in enumerate(futures, 1):
            page, url, reason = future.result()
            if reason:
                removed.append((page, reason))
            if progress:
                progress(done, len(candidates))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    if not removed:
        raise AdFilterError("畫廊標示外部廣告，但未能可靠辨識廣告頁；本次未上傳，請提供廣告頁碼供確認。")
    removed_pages = {page for page, reason in removed}
    kept = [url for page, url in enumerate(items, 1) if page not in removed_pages]
    if not kept or len(removed) > len(items) / 2:
        raise AdFilterError("疑似廣告頁比例過高，為避免誤刪已停止製作；本次未上傳。")
    return AdFilterReport(kept, removed)
