"""
Upload gallery images to Cloudflare R2, so the URLs never expire.

**Why this exists.** e-hentai's embedded images are signed links with a keystamp that expires, and
when it does every image on a Telegraph page stops loading at once. That is what the whole
重新生成 feature was built to work around - re-sweep, re-sign, edit the page in place.

An image copied into R2 has no such link. Its URL is stable for as long as the object exists, so a
page built once keeps working, and a gallery parsed a month ago can be served without touching
e-hentai at all.

**Why the key is a content hash.** Two consequences, both wanted:

  - the same image uploaded twice is the same object, so re-parsing a gallery costs nothing for
    the images that have not changed, and a retry after a partial failure uploads only the gaps
  - the URL cannot be guessed from the gallery id, so the fact that a page embeds `img.example`
    paths does not let anyone enumerate what else is in the bucket

That second point is the privacy mitigation for hosting something that has to be anonymously
readable: Telegraph fetches these without credentials, so the URLs are public by necessity, and a
hash is what keeps them from being *findable*.

**Why two levels of prefix.** `galleries/ab/cd/<hash>.jpg` spreads objects over 65536 prefixes.
R2 does not care, but listing a single flat prefix of hundreds of thousands of keys is unpleasant
to work with and every tool that touches it has to page.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import httpx

logger = __import__("logging").getLogger("twitter_pixiv_bot")

# The root prefix for everything this bot uploads. Keeping it means a bucket can hold other things
# without the bot's objects being mixed in with them.
GALLERY_PREFIX = "galleries"

# Long enough that a re-read never goes back to e-hentai. The objects are immutable - the key is
# their hash - so there is no reason for a shorter one.
CACHE_CONTROL = "public, max-age=31536000, immutable"

# R2 is behind Cloudflare and handles concurrency well, but the phone's uplink is the bottleneck and
# a hundred simultaneous uploads would just make every one of them slower.
DEFAULT_WORKERS = 8
DEFAULT_TIMEOUT = 60.0

# Images are small, and refusing a large one outright would lose a page rather than one image.
MAX_IMAGE_BYTES = 32 * 1024 * 1024


class R2Error(Exception):
    """Anything that stopped an upload. The message is meant to be readable in a log."""


@dataclass
class R2Settings:
    """The five values from .env that make R2 usable."""

    account_id: str
    access_key: str
    secret_key: str
    bucket: str
    public_base: str

    @property
    def endpoint(self) -> str:
        """R2's S3 endpoint for this account. The account id is part of the hostname."""
        return f"https://{self.account_id}.r2.cloudflarestorage.com"

    def public_url(self, key: str) -> str:
        """The anonymous URL for an object, which is what gets embedded in a Telegraph page."""
        return f"{self.public_base.rstrip('/')}/{key.lstrip('/')}"

    def describe(self) -> str:
        """A line safe to log: no key material, just enough to know which bucket is in use."""
        return f"R2 bucket '{self.bucket}' via {self.public_base}"


def r2_settings() -> R2Settings | None:
    """
    Read the R2 configuration from the environment, or None when it is not set up.

    Returning None rather than raising is deliberate: the bot has to keep working for someone who
    has not configured R2, falling back to e-hentai's own links. A hard requirement here would turn
    a missing feature into a bot that does not run.

    Partial configuration is treated as none, and logged - a half-filled block would otherwise fail
    on the first upload, after a gallery had been downloaded.
    """
    values = {
        "account_id": os.environ.get("R2_ACCOUNT_ID", "").strip(),
        "access_key": os.environ.get("R2_ACCESS_KEY", "").strip(),
        "secret_key": os.environ.get("R2_SECRET_KEY", "").strip(),
        "bucket": os.environ.get("R2_BUCKET", "").strip(),
        "public_base": os.environ.get("R2_PUBLIC_BASE", "").strip(),
    }
    present = [name for name, value in values.items() if value]
    if not present:
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        logger.warning("R2 設定不完整，缺少 %s；本次不啟用 R2", "、".join(sorted(missing)))
        return None
    if not values["public_base"].startswith(("http://", "https://")):
        logger.warning("R2_PUBLIC_BASE 不是網址：%r；本次不啟用 R2", values["public_base"])
        return None
    return R2Settings(**values)


def content_key(data: bytes, *, extension: str = "", prefix: str = GALLERY_PREFIX) -> str:
    """
    The object key for a blob: its sha256, sharded two levels deep, with an extension.

    The extension is cosmetic - R2 serves whatever ContentType is set at upload time and does not
    care about the name - but it makes a bucket browsable by a human, which matters the one time
    something has to be checked by hand.

    An unknown extension produces a key with none, which is valid and still served correctly.
    """
    digest = hashlib.sha256(data).hexdigest()
    name = digest
    if extension:
        name = f"{digest}.{extension.lstrip('.').lower()}"
    return f"{prefix}/{digest[:2]}/{digest[2:4]}/{name}"


def extension_for(url: str, content_type: str = "") -> str:
    """
    Guess a file extension from the URL, falling back to the content type.

    e-hentai's URLs end in the real filename, so the URL is usually enough. The content type is the
    fallback for the ones that do not.
    """
    path = url.split("?", 1)[0].split("#", 1)[0]
    _, _, last = path.rpartition("/")
    if "." in last:
        candidate = last.rsplit(".", 1)[1].lower()
        # Guard against a query-shaped tail that survived the split, and absurd lengths.
        if 1 <= len(candidate) <= 5 and candidate.isalnum():
            return candidate
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip().lower())
        if guessed:
            return guessed.lstrip(".")
    return ""


@dataclass
class UploadResult:
    """One uploaded image: its original URL, where it now lives, and whether it worked."""

    source_url: str
    key: str = ""
    public_url: str = ""
    size: int = 0
    content_type: str = ""
    error: str = ""
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class UploadReport:
    """What an upload run did, in the shape the caller needs to report it."""

    total: int = 0
    uploaded: int = 0
    reused: int = 0
    failed: int = 0
    bytes_sent: int = 0
    results: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every image ended up with a usable public URL."""
        return self.failed == 0 and self.total > 0

    def urls(self) -> list:
        """The public URLs in the original order, with failures left as their source URL.

        Keeping the length and order means the caller can build a page directly, and a page with one
        image still pointing at e-hentai is better than a page that lost a position.
        """
        return [r.public_url or r.source_url for r in self.results]

    def summary(self) -> str:
        if not self.total:
            return "沒有圖片需要上傳"
        parts = [f"上傳 {self.uploaded} 張"]
        if self.reused:
            parts.append(f"沿用 {self.reused} 張")
        if self.failed:
            parts.append(f"失敗 {self.failed} 張")
        if self.bytes_sent:
            parts.append(f"{self.bytes_sent / 1024 / 1024:.1f} MB")
        return "，".join(parts)


def _make_client(settings: R2Settings, timeout: float):
    """
    A boto3 S3 client pointed at R2.

    Imported here rather than at module scope so the rest of the bot imports without boto3 present -
    a missing optional dependency should not stop the bot starting.
    """
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise R2Error(
            "boto3 未安裝，無法上傳到 R2（請執行 scripts/install-boto3.sh）"
        ) from exc

    return boto3.session.Session().client(
        "s3",
        endpoint_url=settings.endpoint,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        # R2 ignores the region but botocore requires one; "auto" is what Cloudflare documents.
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=min(15.0, timeout),
            read_timeout=timeout,
            max_pool_connections=DEFAULT_WORKERS * 2,
        ),
    )


def fetch_image(url: str, *, timeout: float = DEFAULT_TIMEOUT, referer: str = "",
                cookies: dict | None = None,
                http_client: httpx.Client | None = None) -> tuple[bytes, str]:
    """
    Download one image, with the headers e-hentai's CDN expects.

    Streamed and size-capped rather than read whole: an unexpected response - an error page, or a
    redirect into something enormous - should be refused rather than held in memory on a phone.
    """
    headers = {}
    if referer:
        headers["Referer"] = referer
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    client = http_client or httpx.Client(timeout=timeout, follow_redirects=True)
    owns = http_client is None
    try:
        with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            chunks = []
            total = 0
            for chunk in response.iter_bytes(256 * 1024):
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise R2Error(f"圖片過大（超過 {MAX_IMAGE_BYTES // 1024 // 1024} MB）")
                chunks.append(chunk)
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
            return b"".join(chunks), content_type
    finally:
        if owns:
            client.close()


def upload_many(
    urls,
    *,
    settings: R2Settings,
    workers: int = DEFAULT_WORKERS,
    timeout: float = DEFAULT_TIMEOUT,
    referer: str = "",
    cookies: dict | None = None,
    key_prefix: str = GALLERY_PREFIX,
    progress=None,
    stop_event=None,
) -> UploadReport:
    """
    Download each URL and put it in R2, in parallel, keeping the original order.

    Every failure is recorded per image rather than raised: one image that cannot be fetched must not
    cost the other hundred and ninety-nine. The caller gets a report whose `urls()` has the same
    length and order as the input, so building the page is a straight substitution.

    ``progress`` is called with (done, total) after each image. ``stop_event`` is checked before each
    upload so a cancelled request stops promptly rather than after the whole gallery.
    """
    if not settings:
        raise R2Error("未設定 R2")

    items = list(urls)
    report = UploadReport(total=len(items))
    if not items:
        return report

    client = _make_client(settings, timeout)
    lock = threading.Lock()
    done = 0

    def one(item):
        index, url = item
        if stop_event is not None and stop_event.is_set():
            return index, UploadResult(url, error="已取消")
        try:
            data, content_type = fetch_image(url, timeout=timeout, referer=referer,
                                             cookies=cookies)
        except Exception as exc:  # noqa: BLE001
            return index, UploadResult(url, error=f"{type(exc).__name__}: {exc}")

        extension = extension_for(url, content_type)
        key = content_key(data, extension=extension, prefix=key_prefix)
        result = UploadResult(source_url=url, key=key, size=len(data),
                              content_type=content_type or "application/octet-stream",
                              public_url=settings.public_url(key))

        # Cheap idempotency: the key is the content hash, so an existing object is by definition the
        # same bytes. This is what makes a retry after a partial failure cost only the gaps.
        try:
            client.head_object(Bucket=settings.bucket, Key=key)
            result.skipped = True
            return index, result
        except Exception:  # noqa: BLE001
            pass

        try:
            client.put_object(
                Bucket=settings.bucket,
                Key=key,
                Body=data,
                ContentType=result.content_type,
                CacheControl=CACHE_CONTROL,
            )
            return index, result
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
            return index, result

    results: list = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(one, (i, url)): i for i, url in enumerate(items)}
        for future in as_completed(futures):
            try:
                index, result = future.result()
            except Exception as exc:  # noqa: BLE001
                index = futures[future]
                result = UploadResult(items[index], error=f"{type(exc).__name__}: {exc}")
            results[index] = result

            with lock:
                done += 1
                if result.error:
                    report.failed += 1
                elif result.skipped:
                    report.reused += 1
                else:
                    report.uploaded += 1
                    report.bytes_sent += result.size
                if progress is not None:
                    try:
                        progress(done, len(items))
                    except Exception:  # noqa: BLE001
                        pass

    # A None here would mean a future never completed, which as_completed does not allow; the
    # substitution is a guard so the report is always the same shape as the input.
    report.results = [r if r is not None else UploadResult(items[i], error="未完成")
                      for i, r in enumerate(results)]
    return report


def check_connection(settings: R2Settings, *, timeout: float = 20.0) -> tuple:
    """
    Verify the credential with one round trip. Returns (ok, message).

    Used by the panel and by the first parse after setup, so a wrong key is reported as itself
    rather than as every image failing individually.
    """
    if not settings:
        return False, "未設定 R2"
    try:
        client = _make_client(settings, timeout)
        client.list_objects_v2(Bucket=settings.bucket, MaxKeys=1)
        return True, f"連線正常（{settings.describe()}）"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
