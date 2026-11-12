import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock,MagicMock,patch
import r2
from test_r2_dedup import MemoryBucket
from channel_links import ChannelLinkChecker

class DownloadReuseTests(unittest.TestCase):
    def test_cached_ocr_bytes_skip_download_and_other_images_share_one_client(self):
        with tempfile.TemporaryDirectory() as directory:
            image=Path(directory)/"page.bin";image.write_bytes(b"cached-image")
            bucket=MemoryBucket();settings=r2.R2Settings("account","key","secret","bucket","https://img.test")
            transport=MagicMock();shared=transport.__enter__.return_value
            fetch=Mock(side_effect=lambda url,**kwargs:(url.encode(),"image/png"))
            with patch.object(r2,"_make_client",return_value=bucket),patch.object(r2.httpx,"Client",return_value=transport) as create,patch.object(r2,"fetch_image",fetch):
                report=r2.upload_many(["cached","new-a","new-b"],settings=settings,download_cache={"cached":(image,"image/png")})
            self.assertEqual(report.uploaded,3)
            self.assertEqual({call.args[0] for call in fetch.call_args_list},{"new-a","new-b"})
            self.assertTrue(all(call.kwargs["http_client"] is shared for call in fetch.call_args_list))
            create.assert_called_once();transport.__exit__.assert_called_once()
            self.assertEqual(bucket.objects[r2.content_key(b"cached-image")],b"cached-image")
    def test_custom_fetcher_retains_original_contract_without_http_client(self):
        with patch.object(r2,"_make_client",return_value=MemoryBucket()),patch.object(r2.httpx,"Client") as client:
            report=r2.upload_many(["local"],settings=r2.R2Settings("a","b","c","d","https://img.test"),image_fetcher=lambda url:(b"local-data","image/png"))
        self.assertEqual(report.uploaded,1);client.assert_not_called()
    def test_ocr_retained_files_are_written_to_task_cache(self):
        from eh_ad_filter import scan_images
        with tempfile.TemporaryDirectory() as directory,patch("eh_ad_filter.ocr_binary",return_value="ocr"):
            cache={}
            report=scan_images(["1","2","3","4","5"],NS(tags={"other":["extraneous ads"]}),
                fetcher=lambda url:url.encode(),inspector=lambda data,*args:"sample" if data==b"5" else "",
                cache_dir=Path(directory),download_cache=cache)
            self.assertEqual(report.kept,["1","2","3","4"])
            self.assertEqual(set(cache),{"4"})
            self.assertEqual(cache["4"][0].read_bytes(),b"4")

class BatchLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_channels_use_one_connection_and_keep_first_match(self):
        with tempfile.TemporaryDirectory() as directory:
            client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),
                      get_me=AsyncMock(return_value=NS(bot=True,id=42)),get_input_entity=AsyncMock(side_effect=lambda cid:cid),get_messages=AsyncMock())
            with patch.dict("os.environ",{"TELEGRAM_API_ID":"123","TELEGRAM_API_HASH":"test","BOT_TOKEN":"42:test"}),patch("telethon.TelegramClient",return_value=client),patch("channel_links.indexed_links",AsyncMock(side_effect=[None,99])) as index:
                result=await ChannelLinkChecker(Path(directory)).find_first([-1001,-1002,-1003],"https://e-hentai.org/g/1/abc/")
            self.assertEqual(result,(-1002,99))
            client.connect.assert_awaited_once();client.disconnect.assert_awaited_once()
            self.assertEqual(index.await_count,2);client.get_messages.assert_not_awaited()
    async def test_single_channel_publish_api_still_returns_message_id(self):
        with tempfile.TemporaryDirectory() as directory:
            client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),
                      get_me=AsyncMock(return_value=NS(bot=True,id=42)),get_input_entity=AsyncMock(return_value="peer"),
                      get_messages=AsyncMock(return_value=[NS(text="",entities=[])]))
            with patch.dict("os.environ",{"TELEGRAM_API_ID":"123","TELEGRAM_API_HASH":"test","BOT_TOKEN":"42:test"}),patch("telethon.TelegramClient",return_value=client),patch("channel_links.indexed_links",AsyncMock(return_value=99)):
                self.assertEqual(await ChannelLinkChecker(Path(directory))(-1001,7,[8],"https://e-hentai.org/g/1/abc/"),99)
