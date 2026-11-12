"""沿用 EH 發布頻道中已有相同來源連結的訊息。"""
import logging
import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from channel_links import ChannelCheckError

logger = logging.getLogger(__name__)


async def reuse(message, status, source_url, user_id, runtime):
    if runtime.get("EH_AD_FILTER_REQUIRED", False):
        return False
    publisher = runtime.get("EH_CHANNEL_PUBLISHER")
    if publisher is None or not publisher.channels or publisher.duplicate_check is None:
        return False
    if message.chat.type not in {"private", "group", "supergroup"}:
        return False
    match = re.search(r"/g/(\d+)/([a-fA-F0-9]+)", source_url)
    if not match:
        return False
    gid, token = match.groups()
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("官方壓縮包 · 1280", callback_data=f"eh|zip1280|{gid}|{token}"),
        InlineKeyboardButton("官方壓縮包 · 原圖", callback_data=f"eh|ziporig|{gid}|{token}"),
    ], [InlineKeyboardButton("取消", callback_data="eh|cancel")]])
    from channel_links import ChannelLinkChecker
    batched = isinstance(publisher.duplicate_check, ChannelLinkChecker)
    if batched:
        try:
            match = await publisher.duplicate_check.find_first(publisher.channels, source_url)
        except ChannelCheckError as exc:
            logger.warning("EH 頻道批次查詢失敗（%s）；繼續其他來源", exc)
            return False
        if match is None:
            return False
        targets = [match]
    else:
        targets = [(cid, None) for cid in publisher.channels]
    for channel_id, prefetched in targets:
        try:
            # 與發布查重共用同一份增量索引，直接比對正規化 EH 來源連結。
            found = prefetched if batched else await publisher.duplicate_check(channel_id, message.chat.id, [], source_url)
        except ChannelCheckError as exc:
            logger.warning("EH 頻道查詢未完成（%s）；繼續其他來源", exc)
            continue
        if found is None:
            continue
        try:
            copied = await message.get_bot().copy_message(
                chat_id=message.chat.id, from_chat_id=channel_id, message_id=found,
                reply_markup=keyboard)
        except Exception as exc:
            logger.warning("EH 頻道結果複製未完成（%s）；沿用正常解析", type(exc).__name__)
            return False
        from eh_idle import arm
        arm(message.get_bot(), message.chat.id, copied.message_id, hide_only=True)
        # 複製成功後不再重新解析，避免狀態清理／發布提示失敗造成重複結果。
        try:
            await status.delete()
        except Exception:
            logger.warning("EH 頻道結果已複製；讀取提示未能刪除")
        from channel_publish import offer_eh_publish
        try:
            await offer_eh_publish(message, copied, user_id, publisher, source_url=source_url,
                                   excluded_channels={channel_id})
        except Exception as exc:
            logger.warning("EH 頻道結果已複製；發布選項建立失敗（%s）", type(exc).__name__)
        return True
    return False
