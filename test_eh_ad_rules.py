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
        images=["ad","normal1","normal2","normal3"]
        with patch("eh_ad_filter.ocr_binary",return_value="ocr"):
            report=scan_images(images,self.tagged,fetcher=lambda url:url,inspector=lambda data,*args:"sample" if data=="ad" else "")
        self.assertEqual(report.kept,["normal1","normal2","normal3"])
        self.assertEqual(report.removed,[(1,"sample")])
    def test_no_reliable_match_or_scan_failure_prevents_upload(self):
        with patch("eh_ad_filter.ocr_binary",return_value="ocr"):
            with self.assertRaises(AdFilterError):scan_images(["one","two"],self.tagged,fetcher=lambda u:b"x",inspector=lambda *args:"")
            with self.assertRaises(AdFilterError):scan_images(["one","two"],self.tagged,fetcher=Mock(side_effect=TimeoutError()))
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
