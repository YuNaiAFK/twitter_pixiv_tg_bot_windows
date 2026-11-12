import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from single_result import ResultStatus, SingleResultMessage

class SingleResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_media_edits_original_and_survives_cleanup(self):
        import io
        from telegram import InputMediaPhoto, InputMediaVideo, InputMediaAnimation, InputMediaDocument
        for method,field,builder in (("reply_photo","photo",InputMediaPhoto),("reply_video","video",InputMediaVideo),
                                     ("reply_animation","animation",InputMediaAnimation),("reply_document","document",InputMediaDocument)):
            original=NS(edit_media=AsyncMock(),edit_text=AsyncMock(),delete=AsyncMock(),message_id=12)
            status=ResultStatus(original);source=NS(media_ids=[])
            message=SingleResultMessage(source,status)
            kwargs={field:io.BytesIO(b"data"),"filename":"original.gif","caption":"title"}
            if method!="reply_document":kwargs["has_spoiler"]=True
            await getattr(message,method)(**kwargs)
            media=original.edit_media.await_args.kwargs["media"]
            self.assertIsInstance(media,builder)
            self.assertEqual(media.media.filename,"original.gif")
            if method!="reply_document":self.assertTrue(media.has_spoiler)
            self.assertEqual(source.media_ids,[12])
            await status.edit_text("完成")
            await status.finish()
            await status.delete()
            original.edit_text.assert_not_awaited();original.delete.assert_not_awaited()
    async def test_album_finishes_by_deleting_status_only_once(self):
        original=NS(delete=AsyncMock())
        status=ResultStatus(original)
        await status.finish();await status.delete()
        original.delete.assert_awaited_once()
    async def test_failed_edit_leaves_error_status_usable(self):
        original=NS(edit_media=AsyncMock(side_effect=RuntimeError("failed")),edit_text=AsyncMock())
        status=ResultStatus(original)
        with self.assertRaises(RuntimeError):
            await SingleResultMessage(NS(),status).reply_photo(photo=b"x")
        await status.edit_text("error")
        original.edit_text.assert_awaited_once_with("error")

    async def test_actual_sender_selects_edit_for_single_but_album_for_multiple(self):
        from test_pixiv_filenames import PixivFilenameTests
        from telegram import InputMediaPhoto,InputMediaDocument
        h=PixivFilenameTests();h.setUp();self.addCleanup(h.doCleanups)
        h.ns.update(InputMediaPhoto=InputMediaPhoto,InputMediaDocument=InputMediaDocument,
                    build_caption=lambda url:url,_reply_media_group=AsyncMock())
        source=NS(media_ids=[])
        status=ResultStatus(NS(edit_media=AsyncMock(),message_id=12,delete=AsyncMock()))
        photo=h.source("a.jpg",metadata=False)
        await h.ns["send_original_files"](source,[photo],"https://x.com/u/status/1",h.root,as_document=False,result_status=status,has_spoiler=True)
        self.assertTrue(status.consumed)
        status.original.edit_media.assert_awaited_once()
        other=ResultStatus(NS(delete=AsyncMock()))
        await h.ns["send_original_files"](NS(),[photo,photo],"https://x.com/u/status/1",h.root,as_document=False,result_status=other)
        self.assertFalse(other.consumed)
        h.ns["_reply_media_group"].assert_awaited_once()
    async def test_pixiv_gif_edits_with_mtproto_and_keeps_filename(self):
        from test_pixiv_filenames import PixivFilenameTests
        from unittest.mock import patch
        h=PixivFilenameTests();h.setUp();self.addCleanup(h.doCleanups)
        h.ns["system_proxy_url"]=lambda:""
        gif=h.source("title_1_p0_2_artist.gif",metadata=False)
        status=ResultStatus(NS(message_id=12))
        source=NS(chat_id=7,message_id=3,media_ids=[])
        with patch("telegram_documents.send_gif_document",new_callable=AsyncMock) as send:
            self.assertTrue(await h.ns["send_single_media"](source,gif,"https://www.pixiv.net/artworks/1",h.root,result_status=status))
        self.assertEqual(send.await_args.kwargs["edit_message_id"],12)
        self.assertEqual(send.await_args.args[0].name,gif.name)
        self.assertTrue(status.consumed)
        self.assertEqual(source.media_ids,[12])

    async def test_partial_failure_notice_survives_cleanup(self):
        original=NS(edit_text=AsyncMock(),delete=AsyncMock())
        status=ResultStatus(original)
        await status.finish("部分傳送失敗")
        await status.delete()
        original.edit_text.assert_awaited_once_with("部分傳送失敗")
        original.delete.assert_not_awaited()
