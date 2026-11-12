import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import exviewer_import as external

class PageValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_or_removed_page_rejected(self):
        with patch("ehentai.telegraph_page_images",return_value=[]),patch("ehentai.check_image_urls") as check:
            self.assertFalse(await external.valid_result("https://telegra.ph/work"))
            check.assert_not_called()
    async def test_all_images_required(self):
        for report,expected in ((NS(ok=True,stopped_early=False,checked=2),True),
                                 (NS(ok=False,stopped_early=True,checked=1),False),
                                 (NS(ok=True,stopped_early=False,checked=1),False)):
            with patch("ehentai.telegraph_page_images",return_value=["https://img.test/1","https://img.test/2"]),patch("ehentai.check_image_urls",return_value=report) as check:
                self.assertEqual(await external.valid_result("https://telegra.ph/work"),expected)
                self.assertTrue(check.call_args.kwargs["deep"])
    async def test_exception_and_timeout_fall_back(self):
        for error in (TimeoutError(),RuntimeError()):
            with patch("ehentai.telegraph_page_images",side_effect=error):
                self.assertFalse(await external.valid_result("https://telegra.ph/work"))
    async def test_invalid_result_does_not_edit_message_or_offer_publication(self):
        status=NS(edit_text=AsyncMock())
        runtime={"BASE_DIR":Path("."),"user_is_admin":lambda uid:True,"ehentai":NS(proxy_url=lambda:"")}
        with patch.object(external,"lookup",AsyncMock(return_value=external.ExistingResult("preview",[],"https://telegra.ph/dead"))),patch.object(external,"valid_result",AsyncMock(return_value=False)),patch("channel_publish.offer_eh_publish",new_callable=AsyncMock) as offer:
            self.assertFalse(await external.reuse(NS(chat=NS(type="private")),status,"https://exhentai.org/g/1/abc/",1,runtime))
            status.edit_text.assert_not_awaited();offer.assert_not_awaited()
    async def test_non_telegraph_url_never_fetched(self):
        with patch("ehentai.telegraph_page_images") as read:
            self.assertFalse(await external.valid_result("https://elsewhere.test/work"))
            read.assert_not_called()
