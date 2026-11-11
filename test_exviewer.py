import ast
import asyncio
import datetime
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from telethon import types
import exviewer_import as external
import exviewer_login as login

URL = "https://e-hentai.org/g/123/abc/"


def results(token="abc"):
    text = f"😀 預覽 來源：https://exhentai.org/g/123/{token}"
    entity = types.MessageEntityTextUrl(3, 2, "https://t.me/iv?url=https%3A%2F%2Ftelegra.ph%2Fwork&rhash=x")
    return [NS(result=NS(send_message=types.BotInlineMessageText(text, entities=[entity])))]


class ExviewerTests(unittest.IsolatedAsyncioTestCase):
    def test_only_exact_gallery_and_existing_telegraph_preview(self):
        self.assertIsNone(external.existing_result(results("def"), URL))
        found = external.existing_result(results(), URL)
        self.assertEqual(found.text, results()[0].result.send_message.message)
        self.assertEqual(found.preview_url, "https://telegra.ph/work")
        self.assertEqual(found.entities[0].offset, 3)
        self.assertEqual(found.entities[0].type, "text_link")
        self.assertIn("t.me/iv", found.entities[0].url)

    def test_membership_notice_or_unrelated_result_is_not_imported(self):
        payload = types.BotInlineMessageText("請先加入會員", entities=[])
        self.assertIsNone(external.existing_result([NS(send_message=payload)], URL))
        self.assertIsNone(external.existing_result(results(), "https://x.com/a/status/1"))

    async def test_private_admin_guard_prevents_any_external_query(self):
        runtime = {"user_is_admin": lambda user: user == 1}
        with patch.object(external, "lookup", new_callable=AsyncMock) as lookup:
            self.assertFalse(await external.reuse(NS(chat=NS(type="channel")), NS(), URL, 1, runtime))
            self.assertFalse(await external.reuse(NS(chat=NS(type="private")), NS(), URL, 2, runtime))
            lookup.assert_not_called()

    async def test_result_is_edited_without_forward_and_offers_eh_channel(self):
        runtime = {"BASE_DIR": Path("."), "user_is_admin": lambda user: True,
            "ehentai": NS(proxy_url=lambda: ""), "EH_CHANNEL_PUBLISHER": NS()}
        message = NS(chat=NS(type="private"))
        status = NS(edit_text=AsyncMock())
        with patch.object(external, "lookup", AsyncMock(return_value=external.existing_result(results(), URL))), \
                patch("channel_publish.offer_eh_publish", new_callable=AsyncMock) as offer:
            self.assertTrue(await external.reuse(message, status, URL, 1, runtime))
            self.assertIsNone(status.edit_text.call_args.kwargs["parse_mode"])
            keyboard = status.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
            self.assertEqual(keyboard[1][0].callback_data, "eh|cancel")
            buttons = keyboard[0]
            self.assertEqual([button.callback_data for button in buttons], ["eh|zip1280|123|abc", "eh|ziporig|123|abc"])
            self.assertTrue(status.edit_text.call_args.kwargs["link_preview_options"].show_above_text)
            offer.assert_awaited_once_with(message, status, 1, runtime["EH_CHANNEL_PUBLISHER"], source_url=URL)

    async def test_no_result_leaves_original_parser_in_control(self):
        runtime = {"BASE_DIR": Path("."), "user_is_admin": lambda user: True, "ehentai": NS(proxy_url=lambda: "")}
        status = NS(edit_text=AsyncMock())
        with patch.object(external, "lookup", AsyncMock(return_value=None)):
            self.assertFalse(await external.reuse(NS(chat=NS(type="private")), status, URL, 1, runtime))
            status.edit_text.assert_not_called()

    async def test_missing_session_does_not_connect(self):
        with tempfile.TemporaryDirectory() as directory, patch("telethon.TelegramClient") as client:
            self.assertIsNone(await external.lookup(URL, Path(directory)))
            client.assert_not_called()

    async def test_eh_entry_returns_before_local_fetch_on_hit(self):
        tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_ehentai_gallery")
        namespace = {"send_status": AsyncMock(return_value=NS()), "gallery_cache_lookup": Mock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<eh-entry>", "exec"), namespace)
        with patch.object(external, "reuse", AsyncMock(return_value=True)):
            self.assertTrue(await namespace["handle_ehentai_gallery"](NS(), URL, NS(id=1)))
            namespace["gallery_cache_lookup"].assert_not_called()

    async def test_eh_entry_shows_normal_menu_when_no_external_result(self):
        tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_ehentai_gallery")
        status = NS(edit_text=AsyncMock())
        namespace = {"send_status": AsyncMock(return_value=status), "gallery_cache_lookup": Mock(return_value={"gallery": {}}),
            "gallery_from_payload": Mock(return_value=NS(is_adult=False, gid="123", token="abc")),
            "gallery_cache_age": lambda url: 0, "GALLERY_META_TTL_SECONDS": 60, "logger": Mock(),
            "build_gallery_info": lambda gallery: "正常畫廊資料", "source_link_options": lambda: None,
            "gallery_keyboard": lambda *a: "normal-menu"}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<eh-entry>", "exec"), namespace)
        with patch.object(external, "reuse", AsyncMock(return_value=False)):
            self.assertTrue(await namespace["handle_ehentai_gallery"](NS(), URL, NS(id=1)))
            self.assertEqual(status.edit_text.call_args.kwargs["reply_markup"], "normal-menu")

    async def test_qr_token_migration_and_login_success(self):
        class Client:
            _switch_dc = AsyncMock()
            _on_login = AsyncMock()
            async def __call__(self, request):
                return types.auth.LoginTokenSuccess(NS(user=NS()))
        client = Client()
        qr = NS(_resp=types.auth.LoginToken(datetime.datetime.now(datetime.timezone.utc), b"test"))
        self.assertFalse(await login.resolve_qr_response(client, qr, {}))
        qr._resp = types.auth.LoginTokenMigrateTo(2, b"test")
        self.assertTrue(await login.resolve_qr_response(client, qr, {}))
        client._switch_dc.assert_awaited_once_with(2)

    async def test_login_respects_flood_wait(self):
        from telethon.errors import FloodWaitError
        rpc = AsyncMock(side_effect=[FloodWaitError(None, capture=2), 123])
        with patch.object(login.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await login.retry_login_rpc(rpc, {}), 123)
            self.assertEqual(sleep.await_count, 3)


    async def test_group_members_can_reuse_but_never_offer_channel_publish(self):
        runtime={"BASE_DIR":Path("."),"user_is_admin":lambda user:user==1,"ehentai":NS(proxy_url=lambda:"")}
        for kind in ("group","supergroup"):
            for user in (1,2):
                with patch.object(external,"lookup",AsyncMock(return_value=external.existing_result(results(),URL))), patch("channel_publish.offer_eh_publish",new_callable=AsyncMock) as offer:
                    status=NS(edit_text=AsyncMock())
                    self.assertTrue(await external.reuse(NS(chat=NS(type=kind)),status,URL,user,runtime))
                    status.edit_text.assert_awaited_once()
                    offer.assert_not_called()


    async def test_cancel_hides_only_keyboard(self):
        tree=ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="handle_gallery_callback")
        ns={"Update":object,"ContextTypes":NS(DEFAULT_TYPE=object),"parse_gallery_callback":lambda data:("cancel","",""),"user_is_allowed":lambda *a:True}
        exec(compile(ast.Module(body=[node],type_ignores=[]),"<cancel>","exec"),ns)
        message=NS(chat_id=1,chat=NS(type="private"),edit_reply_markup=AsyncMock(),edit_text=AsyncMock(),delete=AsyncMock())
        query=NS(data="eh|cancel",answer=AsyncMock())
        await ns["handle_gallery_callback"](NS(callback_query=query,effective_message=message,effective_user=NS(id=1)),NS())
        message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
        message.edit_text.assert_not_called()
        message.delete.assert_not_called()
