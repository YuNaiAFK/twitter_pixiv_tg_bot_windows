import ast
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
from eh_origin import should_ignore

class OriginTests(unittest.TestCase):
    def setUp(self):
        self.runtime={"EH_CHANNEL_PUBLISHER":NS(channels={-1001:"EH"}),"CHANNEL_PUBLISHER":NS(channels={-1002:"X"})}
    def test_bound_channel_and_discussion_forward(self):
        for message in (NS(chat=NS(id=-1001)),NS(sender_chat=NS(id=-1002)),
                        NS(forward_origin=NS(chat=NS(id=-1001))),
                        NS(forward_origin=NS(sender_chat=NS(id=-1001))),NS(forward_from_chat=NS(id=-1001))):
            self.assertTrue(should_ignore(message,self.runtime))
    def test_designated_bot(self):
        bot=NS(is_bot=True,username="ExViewer_Bot")
        for message in (NS(from_user=bot),NS(via_bot=bot),NS(forward_origin=NS(sender_user=bot)),NS(forward_from=bot)):
            self.assertTrue(should_ignore(message,self.runtime))
    def test_unrelated_and_hidden_sources_not_guessed(self):
        for message in (NS(),NS(chat=NS(id=-1003)),NS(from_user=NS(is_bot=True,username="another_bot")),
                        NS(forward_origin=NS(sender_user_name="exviewer_bot")),NS(text="來自 @exviewer_bot")):
            self.assertFalse(should_ignore(message,self.runtime))

class EarlyGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_before_authorization_cooldown_and_status(self):
        tree=ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig"))
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="handle_message")
        ns={"Update":object,"ContextTypes":NS(DEFAULT_TYPE=object),"extract_urls":lambda t:[t],
            "gallery_url":lambda t:t,"user_is_allowed":lambda *args: self.fail("authorization should not run"),
            "send_status":AsyncMock(),**{"EH_CHANNEL_PUBLISHER":NS(channels={-1001:"EH"})}}
        exec(compile(ast.Module(body=[node],type_ignores=[]),"<early-gate>","exec"),ns)
        message=NS(text="https://e-hentai.org/g/1/abc/",caption=None,forward_origin=NS(chat=NS(id=-1001)))
        await ns["handle_message"](NS(effective_message=message,effective_user=NS(id=1)),NS())
        ns["send_status"].assert_not_awaited()
