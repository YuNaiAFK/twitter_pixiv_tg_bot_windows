import ast
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock,patch
from eh_ad_rules import has_external_ads,rule_version
from eh_ad_filter import scan_images,AdFilterError,text_ad_reason,matches_sample

class AdRulesTests(unittest.TestCase):
    def setUp(self):
        self.tagged=NS(tags={"other":["extraneous ads"]},page_url="https://e-hentai.org/g/999/abc/")
    def test_tag_gate_never_reads_images(self):
        fetch=Mock()
        report=scan_images(["one","two"],NS(tags={}),fetcher=fetch)
        self.assertEqual(report.kept,["one","two"]);fetch.assert_not_called()
        self.assertFalse(has_external_ads(NS(tags={"other":["watermarked","scanmark"]})))
    def test_all_tagged_galleries_use_same_content_version(self):
        self.assertTrue(rule_version("https://e-hentai.org/g/999/abc/",self.tagged))
        self.assertEqual(rule_version("a",self.tagged),rule_version("b",self.tagged))
        self.assertFalse(rule_version("a",NS(tags={})))
    def test_content_not_page_number_selects_ads_and_preserves_order(self):
        images=["normal1","normal2","normal3","ad"]
        with patch("eh_ad_filter.ocr_binary",return_value="ocr"):
            report=scan_images(images,self.tagged,fetcher=lambda url:url,inspector=lambda data,*args:"sample" if data=="ad" else "")
        self.assertEqual(report.kept,["normal1","normal2","normal3"])
        self.assertEqual(report.removed,[(4,"sample")])
    def test_no_reliable_match_or_scan_failure_prevents_upload(self):
        with patch("eh_ad_filter.ocr_binary",return_value="ocr"):
            with self.assertRaises(AdFilterError):scan_images(["one","two","three","four"],self.tagged,fetcher=lambda u:b"x",inspector=lambda *args:"")
            with self.assertRaises(AdFilterError):scan_images(["one","two","three","four"],self.tagged,fetcher=Mock(side_effect=TimeoutError()))
    def test_creator_qr_and_url_alone_are_not_ads(self):
        for text in ("掃碼關注作者", "https://example.com", "作者後記 https://pixiv.net/123 掃碼下載", "QR CODE"):
            self.assertEqual(text_ad_reason(text),"")
        self.assertTrue(text_ad_reason("全站免費 掃碼下載 http://example.com"))
        self.assertTrue(text_ad_reason("掃碼領取優惠券"))
    def test_real_image_signatures_keep_26_and_remove_only_confirmed_7(self):
        base=Path(__file__).parent
        samples=[(int(s["dhash"],16),int(s["ahash"],16)) for s in json.loads((base/"ad_samples.json").read_text())["samples"]]
        data=json.loads((base/"ad_regression_signatures.json").read_text())
        removed=[row["page"] for row in data if matches_sample((int(row["dhash"],16),int(row["ahash"],16)),samples)]
        self.assertEqual(removed,list(range(27,34)))
    def test_publish_and_regenerate_filter_before_r2(self):
        tree=ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        for name in ("run_gallery_images","run_regenerate"):
            node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
            calls=[n for n in ast.walk(node) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)]
            self.assertLess(next(n.lineno for n in calls if n.func.id=="remove_gallery_ads"),next(n.lineno for n in calls if n.func.id=="host_on_r2"))


class FilterPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_filter_cannot_reach_r2_or_create_page(self):
        import asyncio,threading,logging
        from unittest.mock import AsyncMock
        tree=ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="run_gallery_images")
        host=AsyncMock();create=Mock()
        ns=dict(Path=Path,asyncio=asyncio,threading=threading,cached_telegraph_entry=lambda *args:None,
                gallery_page_urls=AsyncMock(return_value=(["image"],[])),source_link_options=lambda:None,
                public_embed_urls=AsyncMock(return_value=["image"]),remove_gallery_ads=AsyncMock(side_effect=AdFilterError("unconfirmed")),
                host_on_r2=host,ehentai=NS(create_telegraph_page=create),logger=logging.getLogger("ad-filter-test"))
        exec(compile(ast.Module(body=[node],type_ignores=[]),"<ad-filter-pipeline>","exec"),ns)
        with self.assertRaises(AdFilterError):
            await ns["run_gallery_images"](NS(),NS(edit_text=AsyncMock()),NS(tags={"other":["extraneous ads"]},page_url="url"),"view",Path("."),"",NS())
        host.assert_not_awaited();create.assert_not_called()


class TailWindowTests(unittest.TestCase):
    def test_first_three_never_fetched_even_if_they_match_ads(self):
        tagged=NS(tags={"other":["extraneous ads"]})
        fetch=Mock(side_effect=lambda url:url)
        with patch("eh_ad_filter.ocr_binary",return_value="ocr"):
            report=scan_images(["ad1","ad2","ad3","keep","ad4"],tagged,fetcher=fetch,
                               inspector=lambda data,*args:"sample" if data.startswith("ad") else "")
        self.assertEqual(report.kept,["ad1","ad2","ad3","keep"])
        self.assertEqual({call.args[0] for call in fetch.call_args_list},{"keep","ad4"})
    def test_only_last_ten_of_long_gallery_are_fetched(self):
        tagged=NS(tags={"other":["extraneous ads"]})
        images=[str(i) for i in range(1,34)];fetch=Mock(side_effect=lambda url:url)
        with patch("eh_ad_filter.ocr_binary",return_value="ocr"):
            report=scan_images(images,tagged,fetcher=fetch,inspector=lambda data,*args:"sample" if int(data)>=27 else "")
        self.assertEqual(report.kept,images[:26])
        self.assertEqual([p for p,r in report.removed],list(range(27,34)))
        self.assertEqual({c.args[0] for c in fetch.call_args_list},set(images[-10:]))
    def test_three_or_fewer_pages_skip_engine_and_images(self):
        with patch("eh_ad_filter.ocr_binary") as engine:
            fetch=Mock()
            report=scan_images(["1","2","3"],NS(tags={"other":["extraneous ads"]}),fetcher=fetch)
            self.assertEqual(report.kept,["1","2","3"])
            engine.assert_not_called();fetch.assert_not_called()


class ExpiredImageTests(unittest.TestCase):
    def test_expired_signature_has_refreshable_error(self):
        import httpx
        from eh_ad_filter import AdImageFetchError
        for status in (401,403,404,410):
            response=httpx.Response(status,request=httpx.Request("GET","https://img.test/expired"))
            with patch("eh_ad_filter.ocr_binary",return_value="ocr"):
                with self.assertRaises(AdImageFetchError):
                    scan_images(["1","2","3","4"],NS(tags={"other":["extraneous ads"]}),
                                fetcher=Mock(side_effect=httpx.HTTPStatusError("expired",request=response.request,response=response)))


class RefreshPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_once_before_any_r2_upload(self):
        import asyncio,threading,logging
        from unittest.mock import AsyncMock
        from eh_ad_filter import AdImageFetchError
        import ehentai
        tree=ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="run_gallery_images")
        for second in (["clean"],AdImageFetchError("still expired")):
            scan=AsyncMock(side_effect=[AdImageFetchError("403"),second])
            sweep=AsyncMock(return_value=(["image"],[]));host=AsyncMock(return_value=["hosted"])
            create=Mock(return_value=NS(url="https://telegra.ph/page"))
            ns=dict(Path=Path,asyncio=asyncio,threading=threading,cached_telegraph_entry=lambda *a:None,
                    gallery_page_urls=sweep,source_link_options=lambda:None,public_embed_urls=AsyncMock(return_value=["image"]),
                    remove_gallery_ads=scan,host_on_r2=host,ehentai=NS(create_telegraph_page=create,TelegraphError=ehentai.TelegraphError),
                    TELEGRAPH_AUTHOR="test",remember_telegraph_page=Mock(),build_gallery_info=lambda *a,**k:"summary",
                    escape_html=lambda s:s,remember_regenerate=Mock(),regenerate_keyboard=lambda *a:None,
                    edit_message_to_cover=AsyncMock(return_value=True),logger=logging.getLogger("refresh-test"),EH_CHANNEL_PUBLISHER=None)
            exec(compile(ast.Module(body=[node],type_ignores=[]),"<refresh-pipeline>","exec"),ns)
            with patch("channel_publish.offer_eh_publish",new_callable=AsyncMock):
                call=ns["run_gallery_images"](NS(chat_id=1),NS(edit_text=AsyncMock()),NS(page_url="url",gid="1",token="a"),"view",Path("."),"",NS(id=1))
                if isinstance(second,Exception):
                    with self.assertRaises(AdImageFetchError):await call
                    host.assert_not_awaited();create.assert_not_called()
                else:
                    await call
                    host.assert_awaited_once()
                    self.assertEqual(host.await_args.args[0],["clean"])
            self.assertEqual(sweep.await_count,2)
            self.assertTrue(sweep.await_args.kwargs["force"])
            self.assertEqual(scan.await_count,2)


class NewAdSampleTests(unittest.TestCase):
    def test_3391383_only_last_two_match_and_first_three_not_fetched(self):
        base=Path(__file__).parent
        rows=json.loads((base/"ad_regression_tail_3391383.json").read_text())
        by_page={str(row["page"]):row for row in rows}
        fetched=[]
        def fetch(url):
            fetched.append(int(url))
            return by_page[url]
        def inspect(row,samples,*args):
            value=(int(row["dhash"],16),int(row["ahash"],16))
            return "sample" if matches_sample(value,samples) else ""
        with patch("eh_ad_filter.ocr_binary",return_value="ocr"):
            report=scan_images([str(i) for i in range(1,9)],NS(tags={"other":["extraneous ads"]}),fetcher=fetch,inspector=inspect)
        self.assertEqual(report.kept,[str(i) for i in range(1,7)])
        self.assertEqual(report.removed,[(7,"sample"),(8,"sample")])
        self.assertEqual(sorted(fetched),[4,5,6,7,8])


class SoftImageErrorTests(unittest.TestCase):
    def test_http_200_error_body_requests_refresh_before_decoder(self):
        from eh_ad_filter import AdImageFetchError
        for data in (b"Invalid request",b"<html>expired</html>",b""):
            with patch("eh_ad_filter.ocr_binary",return_value="ocr"),patch("eh_ad_filter.inspect_image") as inspect:
                with self.assertRaises(AdImageFetchError):
                    scan_images(["1","2","3","4"],NS(tags={"other":["extraneous ads"]}),fetcher=lambda url:data)
                inspect.assert_not_called()
    def test_real_image_signature_is_not_mistaken_for_error_body(self):
        import io
        from PIL import Image
        from eh_ad_filter import validate_image_response
        buffer=io.BytesIO();Image.new("RGB",(4,4),"white").save(buffer,format="PNG")
        validate_image_response(buffer.getvalue(),7)
