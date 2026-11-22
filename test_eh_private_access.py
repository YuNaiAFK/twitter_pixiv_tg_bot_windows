import ast
import os
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch


class EHPrivateAccessTests(unittest.TestCase):
    def test_private_requires_admin_or_explicit_id_and_groups_are_unchanged(self):
        tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "eh_chat_allowed")
        ns = {"os": os, "AUTHORIZATION": SimpleNamespace(eh_private_users={5}), "user_is_admin": lambda uid: uid == 1}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<eh-access>", "exec"), ns)
        allowed = ns["eh_chat_allowed"]
        with patch.dict(os.environ, {"EH_PRIVATE_USER_IDS": " 2, 4 "}):
            self.assertTrue(allowed(1, "private"))
            self.assertTrue(allowed(2, "private"))
            self.assertTrue(allowed(5, "private"))
            self.assertFalse(allowed(3, "private"))
            for chat_type in ("group", "supergroup"):
                self.assertTrue(allowed(3, chat_type))
        with patch.dict(os.environ, {"EH_PRIVATE_USER_IDS": ""}):
            self.assertFalse(allowed(2, "private"))
