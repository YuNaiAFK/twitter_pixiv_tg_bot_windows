"""頻道歷史連結檢查；不連線、不傳送真實訊息。"""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from channel_links import normalize_link, message_links, latest_message_id, scan_links


class LinkTests(unittest.IsolatedAsyncioTestCase):
    def test_platform_aliases_and_tracking_are_same_work(self):
        self.assertEqual(normalize_link("https://twitter.com/a/status/123?s=20"), normalize_link("https://x.com/b/status/123"))
        self.assertEqual(normalize_link("https://exhentai.org/g/123/abc/?p=2"), normalize_link("https://e-hentai.org/g/123/abc/"))
        self.assertNotEqual(normalize_link("https://x.com/a/status/123"), normalize_link("https://x.com/a/status/124"))
        self.assertEqual(normalize_link("https://telegra.ph/%E4%BD%9C%E5%93%81#x"), normalize_link("http://telegra.ph/作品"))

    def test_plain_and_hidden_links_with_utf16_offsets(self):
        from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl
        text = "😀 即時預覽 https://x.com/a/status/123?s=20"
        start = len("😀 即時預覽 ".encode("utf-16-le")) // 2
        entities = [MessageEntityTextUrl(3, 4, "https://telegra.ph/work"), MessageEntityUrl(start, len(text.split()[-1]))]
        self.assertEqual(message_links(NS(message=text, entities=entities)), {"telegra.ph/work", "twitter:123"})
        self.assertEqual(message_links(NS(caption="即時預覽", caption_entities=(NS(url="https://e-hentai.org/g/1/abc/"),))), {"eh:1:abc"})

    async def test_latest_id_from_channel_snapshot(self):
        client = AsyncMock(side_effect=[NS(full_chat=NS(read_inbox_max_id=1, read_outbox_max_id=0)), NS(dialog=NS(top_message=305))])
        self.assertEqual(await latest_message_id(client, "peer"), 305)

    async def test_latest_id_follows_all_update_pages(self):
        client = AsyncMock(side_effect=[NS(full_chat=NS(read_inbox_max_id=1, read_outbox_max_id=0)),
            NS(new_messages=[NS(id=3)], other_updates=[], pts=10, final=False),
            NS(new_messages=[NS(id=7)], other_updates=[], pts=15, final=True)])
        self.assertEqual(await latest_message_id(client, "peer"), 7)
        self.assertEqual(client.call_args_list[-1].args[0].pts, 10)

    async def test_old_hidden_link_is_found_after_all_newer_batches(self):
        async def messages(peer, ids):
            return [NS(id=2, message="即時預覽", entities=[NS(url="https://telegra.ph/work")])] if 2 in ids else []
        client = NS(get_messages=AsyncMock(side_effect=messages))
        with patch("channel_links.latest_message_id", AsyncMock(return_value=305)):
            self.assertEqual(await scan_links(client, "peer", {"telegra.ph/work"}), 2)
        self.assertEqual(client.get_messages.await_count, 4)

    async def test_no_match_including_deleted_messages(self):
        client = NS(get_messages=AsyncMock(return_value=[None, NS(id=1, message="https://x.com/a/status/999")]))
        with patch("channel_links.latest_message_id", AsyncMock(return_value=100)):
            self.assertIsNone(await scan_links(client, "peer", {"twitter:123"}))


class IndexTests(unittest.IsolatedAsyncioTestCase):
    async def test_incremental_edit_delete_and_restart_without_history_read(self):
        import tempfile,json
        from pathlib import Path
        from channel_links import indexed_links
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"index.json"
            path.write_text(json.dumps({"version":1,"pts":10,"messages":{"1":["twitter:123"],"2":["twitter:456"]}}))
            edit=type("UpdateEditChannelMessage",(),{})()
            edit.message=NS(id=1,message="https://x.com/a/status/999")
            delete=type("UpdateDeleteChannelMessages",(),{})()
            delete.messages=[2]
            client=AsyncMock(return_value=NS(pts=12,final=True,new_messages=[],other_updates=[edit,delete]))
            self.assertEqual(await indexed_links(client,"peer",{"twitter:999"},path),1)
            self.assertEqual(client.call_args.args[0].pts,10)
            client.get_messages.assert_not_called()
            client.return_value=NS(pts=12,final=True,new_messages=[],other_updates=[])
            self.assertIsNone(await indexed_links(client,"peer",{"twitter:123","twitter:456"},path))
            self.assertEqual(client.call_args.args[0].pts,12)
            client.get_messages.assert_not_called()
