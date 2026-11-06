"""R2 重複檔案與並行上傳的離線測試；不讀取憑證、不連線外部服務。"""
import importlib.util
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import r2


class S3Failure(Exception):
    def __init__(self, code, status):
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class MemoryBucket:
    def __init__(self):
        self.objects = {}
        self.puts = []
        self.lookup_error = None
        self.put_error = None
        self.race_key = None
        self.lock = threading.Lock()

    def list_objects_v2(self, **kwargs):
        if self.lookup_error:
            raise self.lookup_error
        with self.lock:
            return {"Contents": [
                {"Key": key} for key in sorted(self.objects)
                if key.startswith(kwargs["Prefix"])
            ]}

    def put_object(self, **kwargs):
        with self.lock:
            self.puts.append(kwargs)
            key = kwargs["Key"]
            if self.put_error:
                raise self.put_error
            if key == self.race_key:
                self.objects[key] = kwargs["Body"]
                raise S3Failure("PreconditionFailed", 412)
            if key in self.objects and kwargs.get("IfNoneMatch") == "*":
                raise S3Failure("PreconditionFailed", 412)
            self.objects[key] = kwargs["Body"]
            return {}


class R2DedupTests(unittest.TestCase):
    def setUp(self):
        self.bucket = MemoryBucket()
        self.settings = r2.R2Settings("account", "key", "secret", "bucket", "https://img.example")
        self.images = {}
        self.client_patch = patch.object(r2, "_make_client", return_value=self.bucket)
        self.fetch_patch = patch.object(
            r2, "fetch_image", side_effect=lambda url, **kwargs: self.images[url])
        self.client_patch.start()
        self.fetch_patch.start()
        self.addCleanup(self.fetch_patch.stop)
        self.addCleanup(self.client_patch.stop)

    def upload(self, urls, **kwargs):
        return r2.upload_many(urls, settings=self.settings, **kwargs)

    def test_existing_content_is_reused(self):
        data = b"existing image"
        key = r2.content_key(data)
        self.bucket.objects[key] = data
        self.images["source.jpg"] = (data, "image/jpeg")
        report = self.upload(["source.jpg"])
        self.assertEqual((report.uploaded, report.reused, report.failed), (0, 1, 0))
        self.assertEqual(report.urls(), [self.settings.public_url(key)])
        self.assertEqual(self.bucket.puts, [])

    def test_legacy_extension_is_reused_for_different_filename(self):
        data = b"same bytes"
        old_key = r2.content_key(data, extension="jpg")
        self.bucket.objects[old_key] = data
        self.images["renamed.png"] = (data, "image/png")
        report = self.upload(["renamed.png"])
        self.assertEqual(report.reused, 1)
        self.assertEqual(report.urls(), [self.settings.public_url(old_key)])
        self.assertEqual(self.bucket.puts, [])

    def test_parallel_duplicates_upload_once_and_consume_one_budget_reservation(self):
        data = b"identical image"
        urls = [f"source/{i}.{'jpg' if i % 2 else 'png'}" for i in range(20)]
        self.images.update({url: (data, "image/jpeg") for url in urls})
        report = self.upload(urls, workers=8, budget=len(data))
        self.assertEqual((report.uploaded, report.reused, report.failed), (1, 19, 0))
        self.assertEqual(report.bytes_sent, len(data))
        self.assertEqual(len(self.bucket.puts), 1)
        self.assertEqual(self.bucket.puts[0]["IfNoneMatch"], "*")
        self.assertEqual(len(set(report.urls())), 1)
        self.assertEqual(len(report.urls()), len(urls))

    def test_two_gallery_jobs_share_duplicate_protection(self):
        barrier = threading.Barrier(2)
        data = b"shared between galleries"

        def fetch(url, **kwargs):
            barrier.wait(timeout=5)
            return data, "image/jpeg"

        with patch.object(r2, "fetch_image", side_effect=fetch):
            with ThreadPoolExecutor(max_workers=2) as pool:
                reports = list(pool.map(lambda url: self.upload([url], workers=1), ["a", "b"]))
        self.assertEqual(sum(report.uploaded for report in reports), 1)
        self.assertEqual(sum(report.reused for report in reports), 1)
        self.assertEqual(len(self.bucket.puts), 1)

    def test_distinct_content_with_same_filename_is_not_skipped(self):
        self.images = {"a/image.jpg": (b"first", "image/jpeg"),
                       "b/image.jpg": (b"second", "image/jpeg")}
        report = self.upload(list(self.images))
        self.assertEqual(report.uploaded, 2)
        self.assertEqual(len(set(report.urls())), 2)

    def test_failed_duplicate_lookup_does_not_upload(self):
        self.images["source"] = (b"image", "image/jpeg")
        self.bucket.lookup_error = S3Failure("AccessDenied", 403)
        report = self.upload(["source"])
        self.assertEqual(report.failed, 1)
        self.assertIn("無法檢查重複檔案", report.results[0].error)
        self.assertEqual(report.urls(), ["source"])
        self.assertEqual(self.bucket.puts, [])

    def test_conditional_race_reuses_object_and_refunds_budget(self):
        first, second = b"first", b"second image"
        self.images = {"a": (first, "image/jpeg"), "b": (second, "image/jpeg")}
        self.bucket.race_key = r2.content_key(first)
        report = self.upload(["a", "b"], workers=1, budget=len(second))
        self.assertEqual((report.uploaded, report.reused, report.failed), (1, 1, 0))
        self.assertEqual(report.bytes_sent, len(second))
        self.assertTrue(all(result.public_url for result in report.results))

    def test_failed_upload_does_not_claim_public_url_and_can_retry(self):
        self.images["source"] = (b"image", "image/jpeg")
        self.bucket.put_error = S3Failure("ServiceUnavailable", 503)
        failed = self.upload(["source"])
        self.assertEqual(failed.failed, 1)
        self.assertEqual(failed.urls(), ["source"])
        self.bucket.put_error = None
        retried = self.upload(["source"])
        self.assertEqual((retried.uploaded, retried.failed), (1, 0))

    def test_duplicate_can_be_reused_after_another_image_hits_budget(self):
        old = b"old"
        old_key = r2.content_key(old, extension="webp")
        self.bucket.objects[old_key] = old
        self.images = {"new": (b"new image too large", "image/jpeg"),
                       "old": (old, "image/webp")}
        report = self.upload(["new", "old"], workers=1, already_used=10, budget=10)
        self.assertTrue(report.budget_stopped)
        self.assertEqual((report.failed, report.reused), (1, 1))
        self.assertEqual(report.urls()[1], self.settings.public_url(old_key))
        self.assertEqual(self.bucket.puts, [])

    def test_similar_prefixes_are_not_duplicates(self):
        data = b"image"
        key = r2.content_key(data)
        self.bucket.objects[key + "0.jpg"] = b"different"
        self.bucket.objects[key + "/thumbnail"] = b"different"
        self.images["source"] = (data, "image/jpeg")
        self.assertEqual(self.upload(["source"]).uploaded, 1)

    def test_legacy_lookup_checks_following_pages(self):
        key = r2.content_key(b"image")
        with patch.object(self.bucket, "list_objects_v2", side_effect=[
            {"Contents": [{"Key": key + "/thumbnail"}], "IsTruncated": True,
             "NextContinuationToken": "next"},
            {"Contents": [{"Key": key + ".jpg"}]},
        ]) as listing:
            self.assertEqual(r2._existing_content_key(self.bucket, "bucket", key), key + ".jpg")
            self.assertEqual(listing.call_args.kwargs["ContinuationToken"], "next")


@unittest.skipUnless(importlib.util.find_spec("boto3"), "未安裝選用的 boto3")
class R2SdkContractTests(unittest.TestCase):
    def check_conditional_upload(self, already_created):
        from botocore.stub import Stubber

        settings = r2.R2Settings("account", "key", "secret", "bucket", "https://img.example")
        client = r2._make_client(settings, timeout=1)
        self.addCleanup(client.close)
        data = b"SDK contract image"
        key = r2.content_key(data)
        with Stubber(client) as stubber:
            stubber.add_response("list_objects_v2", {"Contents": []}, {
                "Bucket": settings.bucket, "Prefix": key, "MaxKeys": 100,
            })
            expected = {
                "Bucket": settings.bucket, "Key": key, "Body": data,
                "ContentType": "image/jpeg", "CacheControl": r2.CACHE_CONTROL,
                "IfNoneMatch": "*",
            }
            if already_created:
                stubber.add_client_error("put_object", service_error_code="PreconditionFailed",
                                         http_status_code=412, expected_params=expected)
            else:
                stubber.add_response("put_object", {"ETag": "etag"}, expected)
            with patch.object(r2, "_make_client", return_value=client), patch.object(
                    r2, "fetch_image", return_value=(data, "image/jpeg")):
                report = r2.upload_many(["source"], settings=settings)
            stubber.assert_no_pending_responses()
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.reused, int(already_created))
        self.assertEqual(report.uploaded, int(not already_created))

    def test_sdk_accepts_conditional_put(self):
        self.check_conditional_upload(already_created=False)

    def test_sdk_precondition_failure_is_reused(self):
        self.check_conditional_upload(already_created=True)


if __name__ == "__main__":
    unittest.main()
