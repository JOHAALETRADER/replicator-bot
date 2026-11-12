import unittest,tempfile,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
from telegram.error import BadRequest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from panel_repair_2566 import repair,CHANNEL_ID,URL
class Tests(unittest.IsolatedAsyncioTestCase):
 async def test_only_exact_post_keyboard_is_edited_once(self):
  bot=SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(id=CHANNEL_ID)))
  async def edit(**kw):return SimpleNamespace(reply_markup=kw['reply_markup'])
  bot.edit_message_reply_markup=AsyncMock(side_effect=edit)
  with tempfile.TemporaryDirectory() as d:
   await repair(SimpleNamespace(bot=bot),d,Mock())
   kw=bot.edit_message_reply_markup.call_args.kwargs
   self.assertEqual((kw['chat_id'],kw['message_id']),(CHANNEL_ID,2566));self.assertEqual(set(kw),{'chat_id','message_id','reply_markup'})
   self.assertEqual(kw['reply_markup'].inline_keyboard[0][0].url,URL)
   await repair(SimpleNamespace(bot=bot),d,Mock());bot.edit_message_reply_markup.assert_awaited_once()
 async def test_wrong_channel_or_failed_edit_never_marks_complete(self):
  for mismatch in (True,False):
   bot=SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(id=123 if mismatch else CHANNEL_ID)),edit_message_reply_markup=AsyncMock(side_effect=BadRequest('not enough rights')))
   with tempfile.TemporaryDirectory() as d:
    await repair(SimpleNamespace(bot=bot),d,Mock())
    self.assertEqual(list(Path(d).iterdir()),[])
    if mismatch:bot.edit_message_reply_markup.assert_not_awaited()
 async def test_already_correct_is_confirmed_without_republishing(self):
  bot=SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(id=CHANNEL_ID)),edit_message_reply_markup=AsyncMock(side_effect=BadRequest('Message is not modified')))
  with tempfile.TemporaryDirectory() as d:
   await repair(SimpleNamespace(bot=bot),d,Mock());self.assertTrue((Path(d)/'panel-button-en-2566-fixed.json').exists())
