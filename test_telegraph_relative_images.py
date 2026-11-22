import unittest
from types import SimpleNamespace as NS
import ehentai

class RelativePreviewTests(unittest.TestCase):
    def test_relative_absolute_and_protocol_relative_images(self):
        markup="""<article><img src="/file/a.jpg"><img src="file/b.png"><img src="//cdn.example/c.jpg"><img src="https://img.example/d.jpg?a=1&amp;b=2"><img src="/file/a.jpg"></article>"""
        client=NS(get=lambda url:NS(status_code=200,text=markup))
        self.assertEqual(ehentai.telegraph_page_images("some-page",http_client=client),[
            "https://telegra.ph/file/a.jpg","https://telegra.ph/file/b.png","https://cdn.example/c.jpg","https://img.example/d.jpg?a=1&b=2"])
    def test_invalid_schemes_empty_and_proxy_sources_not_accepted(self):
        markup="""<img src=""><img src="javascript:alert(1)"><img src="data:image/png,abc"><img src="file:///tmp/a"><img src="https://t.me/proxy"><script src="/file/not-an-image"></script>"""
        self.assertEqual(ehentai.telegraph_page_images("page",http_client=NS(get=lambda url:NS(status_code=200,text=markup))),[])
    def test_removed_page_still_fails(self):
        self.assertEqual(ehentai.telegraph_page_images("gone",http_client=NS(get=lambda url:NS(status_code=404,text='<img src="/file/a.jpg">'))),[])

    def test_confirmed_missing_is_distinct_from_temporary_failure(self):
        for status in (404, 410):
            client = NS(get=lambda url: NS(status_code=status))
            with self.assertRaises(ehentai.TelegraphPageMissing):
                ehentai.telegraph_page_images("gone", http_client=client, raise_if_missing=True)
        for status in (429, 502, 503):
            client = NS(get=lambda url: NS(status_code=status))
            self.assertEqual(ehentai.telegraph_page_images(
                "page", http_client=client, raise_if_missing=True), [])
