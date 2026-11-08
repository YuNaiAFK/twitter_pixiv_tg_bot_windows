"""來源超連結文字與 Unicode 位置的離線測試。"""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from twitter_captions import linked_caption


class TwitterCaptionTests(unittest.TestCase):
    def test_embeds_url_in_source_label(self):
        url = "https://x.com/user/status/123"
        text, entities = linked_caption("原始連結：" + url, url)
        self.assertEqual(text, "原始連結")
        self.assertEqual(entities[0].url, url)
        self.assertEqual((entities[0].offset, entities[0].length), (0, 4))

    def test_preserves_notes_and_uses_utf16_entity_offsets(self):
        url = "https://twitter.com/user/status/123"
        text, entities = linked_caption("😀說明\n🔗來源：" + url + "\n已壓縮", url)
        self.assertEqual(text, "😀說明\n🔗來源\n已壓縮")
        self.assertEqual((entities[0].offset, entities[0].length), (5, 4))

    def test_no_link_remains_plain_and_empty_label_gets_default(self):
        self.assertEqual(linked_caption("只有說明", "https://x.com/a/status/1"), ("只有說明", None))
        text, entities = linked_caption("https://x.com/a/status/1", "https://x.com/a/status/1")
        self.assertEqual(text, "原始連結")
        self.assertEqual(entities[0].type, "text_link")


class SenderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_photo_video_and_album_senders_embed_link(self):
        from test_pixiv_filenames import PixivFilenameTests
        from telegram import InputMediaDocument, InputMediaPhoto
        harness = PixivFilenameTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        ns = harness.ns
        ns.update(InputMediaPhoto=InputMediaPhoto, InputMediaDocument=InputMediaDocument,
                  build_caption=lambda url: "原始連結：" + url, _reply_media_group=AsyncMock())
        url = "https://x.com/user/status/123"
        message = NS(reply_photo=AsyncMock(), reply_video=AsyncMock())
        photo = harness.source("photo.jpg", metadata=False)
        video = harness.source("video.mp4", metadata=False)
        for file in (photo, video):
            await ns["send_single_media"](message, file, url, harness.root, caption="原始連結：" + url)
        for sender in (message.reply_photo, message.reply_video):
            self.assertEqual(sender.call_args.kwargs["caption"], "原始連結")
            self.assertEqual(sender.call_args.kwargs["caption_entities"][0].url, url)
        await ns["send_media_album"](message, [photo, photo], url)
        media = ns["_reply_media_group"].call_args.args[1]
        self.assertEqual(media[0].caption, "原始連結")
        self.assertEqual(media[0].caption_entities[0].url, url)
        self.assertIsNone(media[1].caption)
