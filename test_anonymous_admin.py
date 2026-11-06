"""匿名管理員僅能操作本群授權，不取得 bot 全域權限。"""
import ast
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock


class AnonymousAdminTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "admin_only")
        self.ns = {"Update": object, "ContextTypes": NS(DEFAULT_TYPE=object),
                   "user_is_admin": lambda uid: uid == 42, "AUTHORIZATION": NS(admins={42})}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<admin-wrapper>", "exec"), self.ns)
        self.message = NS(sender_chat=NS(id=-1001), is_automatic_forward=False, reply_text=AsyncMock())
        self.update = NS(effective_user=NS(id=1087968824), effective_message=self.message,
                         effective_chat=NS(id=-1001, type="supergroup"))

    def run_command(self, name, args=None):
        handler = AsyncMock()
        handler.__name__ = name
        asyncio.run(self.ns["admin_only"](handler)(self.update, NS(args=args)))
        return handler

    def test_anonymous_admin_can_grant_and_revoke_current_group(self):
        for name in ("grant_command", "revoke_command"):
            self.run_command(name).assert_awaited_once()

    def test_anonymous_without_effective_user_is_recognized(self):
        self.update.effective_user = None
        self.run_command("grant_command").assert_awaited_once()

    def test_anonymous_cannot_target_other_ids(self):
        self.run_command("grant_command", ["123"]).assert_not_awaited()

    def test_anonymous_cannot_use_global_commands(self):
        for name in ("promote_command", "restart_command", "cookies_command", "authorized_command"):
            self.run_command(name).assert_not_awaited()

    def test_other_channel_is_not_anonymous_group_admin(self):
        self.message.sender_chat.id = -1002
        self.run_command("grant_command").assert_not_awaited()

    def test_forward_is_not_admin(self):
        self.message.is_automatic_forward = True
        self.run_command("grant_command").assert_not_awaited()

    def test_private_chat_is_not_anonymous_admin(self):
        self.update.effective_chat.type = "private"
        self.run_command("grant_command").assert_not_awaited()

    def test_configured_admin_keeps_global_permissions(self):
        self.message.sender_chat = None
        self.update.effective_user.id = 42
        self.run_command("restart_command").assert_awaited_once()

    def test_anonymous_group_responses_do_not_expose_global_lists(self):
        tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        report = Mock(side_effect=AssertionError("global report must stay private"))
        auth = NS(admins={42}, groups={-1001}, group_locked=False,
                  grant_group=lambda group: False,
                  revoke_group=lambda group: auth.groups.remove(group) or True)
        self.ns.update(AUTHORIZATION=auth, authorization_report=report,
                       save_authorization=lambda: True, authorization_reply=AsyncMock())
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name in {"grant_command", "revoke_command"}:
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<group-handler>", "exec"), self.ns)
        for name in ("grant_command", "revoke_command"):
            asyncio.run(self.ns["admin_only"](self.ns[name])(self.update, NS(args=[])))
        report.assert_not_called()
        self.assertTrue(auth.group_locked)
        self.assertEqual(auth.groups, set())


if __name__ == "__main__":
    unittest.main()
