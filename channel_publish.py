"""管理員私聊的 Twitter／e-hentai 解析結果，經確認後複製至頻道。"""
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

logger = logging.getLogger(__name__)
CHOICE_SECONDS = 300
MEDIA_REPLIES = {"reply_photo", "reply_video", "reply_animation", "reply_document", "reply_media_group"}


def can_post(member):
    return member.status == "creator" or (
        member.status == "administrator" and bool(getattr(member, "can_post_messages", False)))


class CapturedMessage:
    def __init__(self, message, user_id, publisher):
        self.original = message
        self.user_id = user_id
        self.publisher = publisher
        self.media_ids = []

    def __getattr__(self, name):
        original = getattr(self.original, name)
        if name not in MEDIA_REPLIES:
            return original

        async def reply(*args, **kwargs):
            result = await original(*args, **kwargs)
            messages = result if isinstance(result, (tuple, list)) else [result]
            self.media_ids.extend(message.message_id for message in messages if message is not None)
            return result
        return reply


def capture_message(message, user_id, platform, publisher):
    if (publisher is not None and platform == "twitter" and message.chat.type == "private"
            and publisher.is_admin(user_id)):
        return CapturedMessage(message, user_id, publisher)
    return message


async def offer_publish(message):
    if isinstance(message, CapturedMessage) and message.media_ids:
        try:
            await message.publisher.offer(message)
        except Exception as exc:
            # 頻道選項失敗不能讓已成功的私人解析被當成解析失敗、重送媒體。
            logger.warning("無法提供頻道傳送選項：%s", type(exc).__name__)


@dataclass
class Choice:
    user_id: int
    chat_id: int
    media_ids: list
    channels: set
    expires_at: float
    prompt: object
    task: object = None


class ChannelPublisher:
    def __init__(self, path, is_admin, *, label="X／Twitter", command_name="channels", prefix="cp", auto_register=True):
        self.label, self.command_name, self.prefix = label, command_name, prefix
        self.auto_register = auto_register
        self.path = Path(path)
        self.is_admin = is_admin
        self.channels = {}
        self.pending = {}
        self.tasks = set()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.channels = {int(key): str(title) for key, title in data.items() if int(key) < 0}
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, AttributeError):
            logger.warning(f"頻道設定無法讀取；請用 /{self.command_name} 重新登記。")

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.channels, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    async def membership(self, update, context):
        event = update.my_chat_member
        if not event or event.chat.type != "channel":
            return
        channel_id = event.chat.id
        if not can_post(event.new_chat_member):
            if channel_id in self.channels:
                self.channels.pop(channel_id)
                self.save()
        elif self.auto_register and self.is_admin(event.from_user.id):
            self.channels[channel_id] = event.chat.title or str(channel_id)
            self.save()

    async def command(self, update, context):
        user, chat, message = update.effective_user, update.effective_chat, update.effective_message
        if not user or chat.type != "private" or not self.is_admin(user.id):
            return
        args = context.args
        if len(args) == 2 and args[0] == "remove":
            try:
                channel_id = int(args[1])
            except ValueError:
                return await message.reply_text(f"請使用 /{self.command_name} remove -100頻道ID。")
            self.channels.pop(channel_id, None)
            self.save()
            return await message.reply_text("已移除該頻道的傳送選項。")
        if len(args) == 1:
            target = args[0]
            if target.startswith("-100") and target[1:].isdigit():
                target = int(target)
            elif not target.startswith("@"):
                return await message.reply_text(f"請使用 /{self.command_name} @頻道名稱 或 /{self.command_name} -100頻道ID。")
            try:
                channel = await context.bot.get_chat(target)
                member = await context.bot.get_chat_member(channel.id, context.bot.id)
                if channel.type != "channel" or not can_post(member):
                    return await message.reply_text("請先將 bot 設為該頻道的管理員，並開啟「發布訊息」權限。")
                self.channels[channel.id] = channel.title or str(channel.id)
                self.save()
                return await message.reply_text(f"已登記頻道：{self.channels[channel.id]}")
            except TelegramError:
                return await message.reply_text("無法登記頻道，請確認名稱／ID，以及 bot 的發布權限。")
        lines = [f"{title}（{channel_id}）" for channel_id, title in self.channels.items()]
        await message.reply_text("已登記頻道：\n" + ("\n".join(lines) or "尚無頻道") +
            ("\n\n將 bot 加入頻道並授予發布權限即可自動登記。\n" if self.auto_register
             else "\n\n請先授予 bot 頻道發布權限，再使用下列指令登記。\n") +
            f"也可使用 /{self.command_name} @頻道名稱 或 /{self.command_name} -100頻道ID 登記，"
            f"/{self.command_name} remove -100頻道ID 移除。")

    async def offer(self, message):
        if not self.is_admin(message.user_id):
            return
        if not self.channels:
            await message.original.reply_text("尚無可傳送的頻道。請先將 bot 加入頻道並授予發布權限，"
                                              f"或用 /{self.command_name} 登記；本次結果不會自動傳送。")
            return
        token = os.urandom(8).hex()
        rows = [[InlineKeyboardButton(f"傳送到 {title}", callback_data=f"{self.prefix}|{token}|{channel_id}")]
                for channel_id, title in self.channels.items()]
        rows.append([InlineKeyboardButton("不傳送", callback_data=f"{self.prefix}|{token}|skip")])
        prompt = await message.original.reply_text(f"要將這次 {self.label} 解析結果傳送到頻道嗎？\n"
            "5 分鐘內未選擇，預設不傳送。", reply_markup=InlineKeyboardMarkup(rows))
        choice = Choice(message.user_id, message.chat_id, sorted(set(message.media_ids)),
                        set(self.channels), asyncio.get_running_loop().time() + CHOICE_SECONDS, prompt)
        self.pending[token] = choice
        task = asyncio.create_task(self.expire(token, choice))
        choice.task = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def expire(self, token, choice):
        await asyncio.sleep(max(0, choice.expires_at - asyncio.get_running_loop().time()))
        if self.pending.get(token) is choice:
            self.pending.pop(token)
            try:
                await choice.prompt.edit_text("已逾時，本次結果未傳送到頻道。", reply_markup=None)
            except TelegramError:
                pass

    async def choose(self, update, context):
        query, user, chat = update.callback_query, update.effective_user, update.effective_chat
        if not user or chat.type != "private" or not self.is_admin(user.id):
            return await query.answer("此選項僅供管理員在私聊使用。", show_alert=True)
        parts = (query.data or "").split("|")
        choice = self.pending.get(parts[1]) if len(parts) == 3 and parts[0] == self.prefix else None
        if (choice is None or choice.user_id != user.id or choice.chat_id != chat.id
                or choice.prompt.message_id != update.effective_message.message_id):
            return await query.answer("這個選項已失效。", show_alert=True)
        if asyncio.get_running_loop().time() >= choice.expires_at:
            return await query.answer("已逾時，本次不傳送。", show_alert=True)
        action = parts[2]
        if action != "skip":
            try:
                channel_id = int(action)
            except ValueError:
                return await query.answer("頻道選項無效。", show_alert=True)
            if channel_id not in choice.channels or channel_id not in self.channels:
                return await query.answer("該頻道已移除。", show_alert=True)
        # 在任何網路操作前消耗選項，避免快速連點造成重複發布。
        self.pending.pop(parts[1])
        choice.task.cancel()
        await query.answer()
        if action == "skip":
            return await choice.prompt.edit_text("本次結果不傳送到頻道。", reply_markup=None)
        try:
            await choice.prompt.edit_text("正在傳送到頻道……", reply_markup=None)
            member = await context.bot.get_chat_member(channel_id, context.bot.id)
            if not can_post(member):
                return await choice.prompt.edit_text("bot 已無頻道發布權限，本次未傳送。", reply_markup=None)
            copied = 0
            for start in range(0, len(choice.media_ids), 100):
                ids = choice.media_ids[start:start + 100]
                result = await context.bot.copy_messages(chat_id=channel_id, from_chat_id=choice.chat_id,
                                                         message_ids=ids)
                copied += len(result)
                if len(result) != len(ids):
                    break
            if copied != len(choice.media_ids):
                await choice.prompt.edit_text(f"頻道僅收到 {copied}/{len(choice.media_ids)} 則媒體，"
                    "請先檢查頻道；未自動重送。", reply_markup=None)
            else:
                await choice.prompt.edit_text(f"已傳送到頻道：{self.channels.get(channel_id, channel_id)}。",
                                             reply_markup=None)
        except TelegramError as exc:
            logger.warning("頻道傳送失敗：%s", type(exc).__name__)
            await choice.prompt.edit_text("頻道傳送未完成或回應逾時，請先檢查頻道；未自動重送，避免重複發布。",
                                         reply_markup=None)

    async def shutdown(self, app):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.pending.clear()


def register(application, runtime):
    from telegram.ext import CallbackQueryHandler, ChatMemberHandler, CommandHandler, filters
    publisher = ChannelPublisher(runtime["BASE_DIR"] / "channel_targets.json", runtime["user_is_admin"])
    runtime["CHANNEL_PUBLISHER"] = publisher
    eh_publisher = ChannelPublisher(runtime["BASE_DIR"] / "eh_channel_targets.json", runtime["user_is_admin"],
        label="e-hentai", command_name="ehchannels", prefix="ecp", auto_register=False)
    runtime["EH_CHANNEL_PUBLISHER"] = eh_publisher
    async def membership(update, context):
        await publisher.membership(update, context)
        await eh_publisher.membership(update, context)
    application.add_handler(ChatMemberHandler(membership, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CommandHandler("channels", publisher.command, filters=filters.ChatType.PRIVATE))
    application.add_handler(CallbackQueryHandler(publisher.choose, pattern=r"^cp\|"))
    application.add_handler(CommandHandler("ehchannels", eh_publisher.command, filters=filters.ChatType.PRIVATE))
    application.add_handler(CallbackQueryHandler(eh_publisher.choose, pattern=r"^ecp\|"))
    previous = application.post_shutdown

    async def shutdown(app):
        await publisher.shutdown(app)
        await eh_publisher.shutdown(app)
        if previous is not None:
            await previous(app)
    application.post_shutdown = shutdown
    return publisher

async def offer_eh_publish(message, status, user_id, publisher):
    if publisher is None or status is None or message.chat.type != "private" or not publisher.is_admin(user_id):
        return
    captured = CapturedMessage(message, user_id, publisher)
    captured.media_ids = [status.message_id]
    await offer_publish(captured)
