"""授權完成只清除 bot 成功回覆，失敗提示保留。"""
import ast
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock


class CleanupTests(unittest.TestCase):
    def setUp(self):
        node = next(n for n in ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig")).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "authorization_reply")
        self.reply = object()
        self.message = NS(reply_text=AsyncMock(return_value=self.reply))
        self.sleep = AsyncMock()
        self.cleanup = AsyncMock()
        self.ns = {"asyncio": NS(sleep=self.sleep), "DELETE_STATUS_MESSAGE": True,
                   "cleanup_request_messages": self.cleanup}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<authorization-reply>", "exec"), self.ns)

    def test_success_deletes_only_bot_reply_after_five_seconds(self):
        asyncio.run(self.ns["authorization_reply"](self.message, "已授權"))
        self.sleep.assert_awaited_once_with(5)
        self.cleanup.assert_awaited_once_with(self.reply)

    def test_write_failure_keeps_warning(self):
        asyncio.run(self.ns["authorization_reply"](self.message, "寫入失敗", saved=False))
        self.cleanup.assert_not_called()
        self.sleep.assert_not_called()

    def test_disabled_cleanup_keeps_reply(self):
        self.ns["DELETE_STATUS_MESSAGE"] = False
        asyncio.run(self.ns["authorization_reply"](self.message, "已授權"))
        self.cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
