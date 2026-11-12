import ast
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from eh_ad_rules import filter_pages,rule_version

class AdRulesTests(unittest.TestCase):
    def test_known_ranges_preserve_other_pages(self):
        for url,start,end,total in (("https://exhentai.org/g/2051305/d1969d0ee2/",36,41,41),
                                    ("https://e-hentai.org/g/2000612/54a53baa2d/?p=1",38,42,45)):
            pages=list(range(1,total+1))
            self.assertEqual(filter_pages(url,pages,NS(tags={"other":["extraneous ads"]})),[p for p in pages if not start<=p<=end])
            self.assertTrue(rule_version(url,NS(tags={"other":["extraneous ads"]})))
    def test_unrelated_gallery_or_token_not_affected(self):
        for url in ("https://exhentai.org/g/2051305/abc/","https://exhentai.org/g/123/d1969d0ee2/"):
            self.assertEqual(filter_pages(url,list(range(50))),list(range(50)))
            self.assertFalse(rule_version(url))
    def test_new_publish_and_regenerate_filter_before_r2(self):
        tree=ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        for name in ("run_gallery_images","run_regenerate"):
            node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
            calls=[n for n in ast.walk(node) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)]
            filtered=next(n.lineno for n in calls if n.func.id=="filter_pages")
            hosted=next(n.lineno for n in calls if n.func.id=="host_on_r2")
            self.assertLess(filtered,hosted)

class BypassTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_ads_bypass_channel_and_exviewer(self):
        import channel_reuse,exviewer_import
        for module in (channel_reuse,exviewer_import):
            with patch("exviewer_import.lookup",new_callable=AsyncMock) as lookup:
                self.assertFalse(await module.reuse(NS(),NS(),"https://exhentai.org/g/2051305/d1969d0ee2/",1,{"EH_AD_FILTER_REQUIRED":True}))
                lookup.assert_not_awaited()


class TagGateTests(unittest.TestCase):
    def test_no_tag_never_filters_even_known_gallery(self):
        url="https://exhentai.org/g/2051305/d1969d0ee2/"
        for gallery in (None,NS(tags={}),NS(tags={"other":["watermarked","scanmark"]})):
            self.assertEqual(filter_pages(url,list(range(1,42)),gallery),list(range(1,42)))
            self.assertEqual(rule_version(url,gallery),"")
    def test_tag_without_known_rule_does_not_guess_pages(self):
        gallery=NS(tags={"other":["extraneous ads"]})
        self.assertEqual(filter_pages("https://exhentai.org/g/1/abc/",list(range(45)),gallery),list(range(45)))
