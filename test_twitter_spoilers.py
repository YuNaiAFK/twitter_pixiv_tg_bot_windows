import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from telegram import InputMediaPhoto, InputMediaDocument
from twitter_spoilers import sensitive_media

class RatingTests(unittest.TestCase):
    def test_ratings_and_missing_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/"image.jpg"
            for data,expected in (({"sensitive":True},True),({"sensitive":False},False),
                ({"sensitive":False,"sensitive_flags":{"nudity":True}},True),
                ({"possibly_sensitive":True},True),({},True),([],True)):
                p.with_name(p.name+".json").write_text(json.dumps(data))
                self.assertEqual(sensitive_media([p]),expected)
            p.with_name(p.name+".json").unlink()
            self.assertTrue(sensitive_media([p]))
    def test_one_sensitive_file_masks_whole_post(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=[Path(directory)/name for name in ("a.jpg","b.jpg")]
            for p,flag in zip(paths,(False,True)):
                p.with_name(p.name+".json").write_text(json.dumps({"sensitive":flag}))
            self.assertTrue(sensitive_media(paths))

class SenderTests(unittest.IsolatedAsyncioTestCase):
    def harness(self):
        from test_pixiv_filenames import PixivFilenameTests
        h=PixivFilenameTests();h.setUp();self.addCleanup(h.doCleanups)
        h.ns.update(InputMediaPhoto=InputMediaPhoto,InputMediaDocument=InputMediaDocument,
                    build_caption=lambda url:url,_reply_media_group=AsyncMock())
        return h
    async def test_photo_video_gif_and_normal_content(self):
        h=self.harness();m=NS(reply_photo=AsyncMock(),reply_video=AsyncMock(),reply_animation=AsyncMock())
        for name,sender in (("a.jpg",m.reply_photo),("a.mp4",m.reply_video),("a.gif",m.reply_animation)):
            await h.ns["send_single_media"](m,h.source(name,metadata=False),"https://x.com/u/status/1",h.root,has_spoiler=True)
            self.assertTrue(sender.await_args.kwargs["has_spoiler"])
        await h.ns["send_single_media"](m,h.source("safe.jpg",metadata=False),"https://x.com/u/status/1",h.root)
        self.assertFalse(m.reply_photo.await_args.kwargs["has_spoiler"])
    async def test_album_and_caption_retry_keep_mask(self):
        h=self.harness();h.ns["_reply_media_group"].side_effect=[RuntimeError("caption refused"),None]
        p=h.source("a.jpg",metadata=False)
        await h.ns["send_media_album"](NS(),[p,p],"https://x.com/u/status/1",has_spoiler=True)
        for call in h.ns["_reply_media_group"].await_args_list:
            self.assertTrue(all(item.has_spoiler for item in call.args[1]))
    async def test_single_fallback_keeps_mask(self):
        h=self.harness();h.ns["send_media_album"]=AsyncMock(side_effect=RuntimeError("refused"))
        h.ns["send_single_media"]=AsyncMock(return_value=True)
        p=h.source("a.jpg",metadata=False)
        await h.ns["send_original_files"](NS(),[p,p],"https://x.com/u/status/1",h.root,as_document=False,has_spoiler=True)
        self.assertTrue(all(call.kwargs["has_spoiler"] for call in h.ns["send_single_media"].await_args_list))
    async def test_cache_replay_uses_saved_rating_and_old_cache_masks(self):
        h=self.harness();p=h.source("a.jpg",metadata=False)
        h.ns.update(cached_media_files=lambda payload:[p],make_work_dir=lambda:h.root/"scratch",
                    send_original_files=AsyncMock(return_value=(1,"",[])))
        for payload,expected in (({"twitter_sensitive":True},True),({"twitter_sensitive":False},False),({},True)):
            await h.ns["send_cached_media"](NS(),NS(edit_text=AsyncMock()),"https://x.com/u/status/1",payload,"twitter","")
            self.assertEqual(h.ns["send_original_files"].await_args.kwargs["has_spoiler"],expected)
    async def test_command_writes_metadata_and_cache_persists_rating(self):
        h=self.harness();url="https://x.com/u/status/1"
        command=h.ns["build_gallery_dl_command"](url,h.root,"twitter")
        self.assertLess(command.index("--write-metadata"),command.index("--"))
        p=h.source("a.jpg",metadata=False);p.with_name(p.name+".json").write_text('{"sensitive": true}')
        h.ns["cache_media_files"]=lambda files,url:["cached.jpg"]
        self.assertTrue(h.ns["media_cache_payload"]([p],url)["twitter_sensitive"])

    async def test_oversized_sensitive_photo_not_downgraded_to_document(self):
        h=self.harness();h.ns.update(PHOTO_LIMIT_BYTES=1,downscale_photo=lambda *args:None,send_status=AsyncMock())
        import asyncio
        h.ns["asyncio"]=asyncio
        message=NS(reply_document=AsyncMock())
        ok=await h.ns["send_single_media"](message,h.source("large.jpg",metadata=False),"https://x.com/u/status/1",h.root,has_spoiler=True)
        self.assertFalse(ok)
        message.reply_document.assert_not_awaited()
        h.ns["send_status"].assert_awaited_once()
