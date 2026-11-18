"""EH 封面下載與相容 JPEG，僅影響 Telegram 封面。"""
import io
import tempfile
from PIL import Image, ImageOps
import httpx
import ehentai
import r2
from eh_ad_filter import open_for_analysis


def download_cover(url):
    with httpx.Client(**ehentai.http_client_kwargs(25)) as client:
        data, _ = r2.fetch_image(url, http_client=client)
    with tempfile.TemporaryDirectory(prefix="eh-cover-") as directory:
        with open_for_analysis(data, directory) as image:
            image.seek(0)
            with ImageOps.exif_transpose(image) as oriented:
                oriented.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
                with oriented.convert("RGBA") as rgba, Image.new("RGB", oriented.size, "white") as output:
                    output.paste(rgba, mask=rgba.getchannel("A"))
                    buffer = io.BytesIO()
                    output.save(buffer, format="JPEG", quality=95, optimize=True)
                    return buffer.getvalue()
