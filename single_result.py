"""將單一媒體直接填入原進度訊息，保留多檔相簿行為。"""
import logging
from telegram.error import TelegramError
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaAnimation, InputMediaDocument


logger = logging.getLogger(__name__)


class ResultStatus:
    def __init__(self, original):
        self.original = original
        self.consumed = False
        self.removed = False
        self.keep_notice = False

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def edit_text(self, *args, **kwargs):
        if not self.consumed:
            return await self.original.edit_text(*args, **kwargs)

    async def finish(self, notice=None):
        if notice and not self.consumed:
            self.keep_notice = True
            await self.original.edit_text(notice)
        else:
            await self.delete()

    async def delete(self, *args, **kwargs):
        if not self.consumed and not self.removed and not self.keep_notice:
            try:
                await self.original.delete(*args, **kwargs)
                self.removed = True
            except TelegramError as exc:
                logger.warning("媒體已送達，但無法刪除進度訊息（%s）", type(exc).__name__)


class SingleResultMessage:
    def __init__(self, original, status):
        self.original, self.status = original, status

    def __getattr__(self, name):
        return getattr(self.original, name)

    def complete(self):
        self.status.consumed = True
        # 管理員的頻道發布使用原進度訊息 ID，不再依賴新增的媒體訊息。
        ids = getattr(self.original, "media_ids", None)
        if isinstance(ids, list) and self.status.message_id not in ids:
            ids.append(self.status.message_id)

    async def _edit(self, builder, field, kwargs):
        media = kwargs.pop(field)
        markup = kwargs.pop("reply_markup", None)
        filename = kwargs.pop("filename", None)
        if filename is not None:
            from telegram import InputFile
            media = InputFile(media, filename=filename, attach=True)
        result = await self.status.original.edit_media(media=builder(media=media, **kwargs), reply_markup=markup)
        self.complete()
        return result

    async def reply_photo(self, **kwargs):
        return await self._edit(InputMediaPhoto, "photo", kwargs)

    async def reply_video(self, **kwargs):
        return await self._edit(InputMediaVideo, "video", kwargs)

    async def reply_animation(self, **kwargs):
        return await self._edit(InputMediaAnimation, "animation", kwargs)

    async def reply_document(self, **kwargs):
        return await self._edit(InputMediaDocument, "document", kwargs)
