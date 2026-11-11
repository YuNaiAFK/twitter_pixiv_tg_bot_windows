import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from channel_reuse import reuse
from channel_links import ChannelCheckError

class ReuseTests(unittest.IsolatedAsyncioTestCase):
    def setup_case(self, found=12, private=True):
        self.bot=NS(copy_message=AsyncMock(return_value=NS(message_id=99)))
        self.message=NS(chat=NS(id=7,type="private" if private else "supergroup"), get_bot=lambda:self.bot)
        self.status=NS(delete=AsyncMock())
        self.publisher=NS(channels={-1001:"EH"},duplicate_check=AsyncMock(return_value=found),is_admin=lambda uid:uid==1)
        return {"EH_CHANNEL_PUBLISHER":self.publisher}
    async def test_match_copies_without_forward(self):
        runtime=self.setup_case()
        with patch("channel_publish.offer_eh_publish",new_callable=AsyncMock) as offer:
            self.assertTrue(await reuse(self.message,self.status,"https://e-hentai.org/g/123/abc/",1,runtime))
            self.publisher.duplicate_check.assert_awaited_once_with(-1001,7,[],"https://e-hentai.org/g/123/abc/")
            self.assertEqual(self.bot.copy_message.await_args.kwargs["message_id"],12)
            self.status.delete.assert_awaited_once()
            offer.assert_awaited_once()
    async def test_miss_falls_back(self):
        runtime=self.setup_case(None)
        self.assertFalse(await reuse(self.message,self.status,"https://e-hentai.org/g/123/abc/",2,runtime))
        self.bot.copy_message.assert_not_awaited()
    async def test_scan_error_falls_back(self):
        runtime=self.setup_case(); self.publisher.duplicate_check.side_effect=ChannelCheckError("offline")
        self.assertFalse(await reuse(self.message,self.status,"https://e-hentai.org/g/123/abc/",1,runtime))
    async def test_cleanup_failure_does_not_duplicate(self):
        runtime=self.setup_case();self.status.delete.side_effect=RuntimeError()
        with patch("channel_publish.offer_eh_publish",new_callable=AsyncMock,side_effect=RuntimeError()):
            self.assertTrue(await reuse(self.message,self.status,"https://e-hentai.org/g/123/abc/",1,runtime))
    async def test_group_no_publication_prompt(self):
        runtime=self.setup_case(private=False)
        self.assertTrue(await reuse(self.message,self.status,"https://e-hentai.org/g/123/abc/",2,runtime))
    async def test_copy_failure_falls_back(self):
        runtime=self.setup_case();self.bot.copy_message.side_effect=RuntimeError()
        self.assertFalse(await reuse(self.message,self.status,"https://e-hentai.org/g/123/abc/",1,runtime))
