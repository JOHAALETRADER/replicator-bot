import ast,unittest,sys,copy
from pathlib import Path
from types import SimpleNamespace
from typing import Optional,List
from unittest.mock import AsyncMock,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as K
from telegram.error import BadRequest
import aiohttp
from panel_buttons import panel_url,has_panel_button,localize_panel_markup
SOURCE=Path(__file__).resolve().parents[1]/'main.py'
ES='https://t.me/JOHAALETRADER_bot?start=panel'
EN='https://t.me/JOHAALETRADER_bot?start=panel_en'
def keyboard():return K([[B('Conoce el panel',url=ES),B('Other',url='https://example.com')],[B('Callback',callback_data='keep')]])
async def retry(name,fn):return await fn()
def load():
 env=dict(Optional=Optional,List=List,InlineKeyboardMarkup=K,InlineKeyboardButton=B,ContextTypes=SimpleNamespace(DEFAULT_TYPE=object),Update=object,Message=object,TRANSLATE_BUTTONS=True,TRANSLATE=True,TARGET_LANG='EN',aiohttp=aiohttp,localize_panel_markup=localize_panel_markup,has_panel_button=has_panel_button,BadRequest=BadRequest,
 deepl_translate=AsyncMock(return_value='Translated label'),translate_visible_html=AsyncMock(return_value=('English text',None)),build_html_no_translate=lambda *a:'Original text',sender_display_name=lambda *a:'Owner',prefix_block=lambda *a:'',cap_with_prefix=lambda pref,text,**kw:text,
 db_get_dst_msg=Mock(return_value=77),map_channel=Mock(return_value=-100222),call_with_retry=retry,log=Mock(),alert_error=AsyncMock(),ParseMode=SimpleNamespace(HTML='HTML'))
 names={'translate_buttons','replicate_edit','on_channel_panel_edit','replicate_message'}
 body=[n for n in ast.parse(SOURCE.read_text()).body if isinstance(n,ast.AsyncFunctionDef) and n.name in names]
 exec(compile(ast.Module(body=body,type_ignores=[]),'<real replicator functions>','exec'),env);return env
class Tests(unittest.IsolatedAsyncioTestCase):
 def test_only_exact_panel_destination_changes(self):
  for url in ['https://example.com?start=panel','https://t.me/OTHER_bot?start=panel','https://t.me/JOHAALETRADER_bot?start=PREMIUM100','https://t.me/JOHAALETRADER_bot?start=validar_id','https://t.me/JOHAALETRADER_bot?start=panel&start=other',None]:self.assertIsNone(panel_url(url),url)
  self.assertEqual(panel_url(ES),EN);self.assertEqual(panel_url(EN),EN)
 def test_preserves_rows_and_all_unrelated_buttons(self):
  src=keyboard();before=src.to_dict();out=localize_panel_markup(src,english=True)
  self.assertEqual(out.inline_keyboard[0][0].url,EN);self.assertEqual(out.inline_keyboard[0][0].text,'🖥 Discover the panel')
  self.assertEqual(out.inline_keyboard[0][1].to_dict(),src.inline_keyboard[0][1].to_dict());self.assertEqual(out.inline_keyboard[1][0].to_dict(),src.inline_keyboard[1][0].to_dict())
  self.assertEqual(src.to_dict(),before);self.assertEqual(localize_panel_markup(out,english=True),out)
 def test_spanish_and_empty_markup_untouched(self):
  src=keyboard();self.assertIs(localize_panel_markup(src,english=False),src)
  self.assertIsNone(localize_panel_markup(None,english=True));self.assertFalse(has_panel_button(None))
 async def test_real_translator_localizes_url_and_label(self):
  e=load();out=await e['translate_buttons'](keyboard(),do_translate=True)
  self.assertEqual(out.inline_keyboard[0][0].url,EN);self.assertEqual(out.inline_keyboard[0][0].text,'🖥 Discover the panel')
  self.assertEqual(out.inline_keyboard[0][1].url,'https://example.com')
 async def test_spanish_route_never_rewrites_url(self):
  e=load();src=keyboard();self.assertIs(await e['translate_buttons'](src,do_translate=False),src)
 async def test_panel_route_works_without_general_label_translation(self):
  e=load();e['TRANSLATE_BUTTONS']=False
  out=await e['translate_buttons'](keyboard(),do_translate=True);self.assertEqual(out.inline_keyboard[0][0].url,EN)
  e['deepl_translate'].assert_not_awaited()
 async def test_text_and_caption_edits_keep_localized_panel_button(self):
  for text,caption in [('Hola',None),(None,'Hola')]:
   e=load();bot=SimpleNamespace(edit_message_text=AsyncMock(),edit_message_caption=AsyncMock())
   msg=SimpleNamespace(chat=SimpleNamespace(id=-100111),message_id=10,text=text,caption=caption,entities=[],caption_entities=[],reply_markup=keyboard())
   await e['replicate_edit'](SimpleNamespace(bot=bot),msg,-100222,None,do_translate=True)
   method=bot.edit_message_text if text else bot.edit_message_caption
   self.assertEqual(method.call_args.kwargs['reply_markup'].inline_keyboard[0][0].url,EN)
 async def test_non_panel_edits_keep_previous_behavior(self):
  e=load();bot=SimpleNamespace(edit_message_text=AsyncMock())
  msg=SimpleNamespace(chat=SimpleNamespace(id=-100111),message_id=10,text='Hola',caption=None,entities=[],reply_markup=K([[B('Other',url='https://example.com')]]))
  await e['replicate_edit'](SimpleNamespace(bot=bot),msg,-100222,None,do_translate=True)
  self.assertNotIn('reply_markup',bot.edit_message_text.call_args.kwargs)
 async def test_channel_edit_changes_only_existing_copy_markup(self):
  e=load();bot=SimpleNamespace(edit_message_reply_markup=AsyncMock())
  msg=SimpleNamespace(chat=SimpleNamespace(id=-100111),message_id=10,reply_markup=keyboard())
  await e['on_channel_panel_edit'](SimpleNamespace(edited_channel_post=msg),SimpleNamespace(bot=bot))
  bot.edit_message_reply_markup.assert_awaited_once();kw=bot.edit_message_reply_markup.call_args.kwargs
  self.assertEqual((kw['chat_id'],kw['message_id']),(-100222,77));self.assertEqual(kw['reply_markup'].inline_keyboard[0][0].url,EN)
 async def test_channel_non_panel_unmapped_and_repeat_are_safe(self):
  e=load();bot=SimpleNamespace(edit_message_reply_markup=AsyncMock())
  msg=SimpleNamespace(chat=SimpleNamespace(id=-100111),message_id=10,reply_markup=None)
  await e['on_channel_panel_edit'](SimpleNamespace(edited_channel_post=msg),SimpleNamespace(bot=bot));bot.edit_message_reply_markup.assert_not_awaited()
  msg.reply_markup=keyboard();e['db_get_dst_msg'].return_value=None
  await e['on_channel_panel_edit'](SimpleNamespace(edited_channel_post=msg),SimpleNamespace(bot=bot));bot.edit_message_reply_markup.assert_not_awaited()
  e['db_get_dst_msg'].return_value=77;bot.edit_message_reply_markup.side_effect=BadRequest('Message is not modified')
  await e['on_channel_panel_edit'](SimpleNamespace(edited_channel_post=msg),SimpleNamespace(bot=bot));e['alert_error'].assert_not_awaited()
 async def test_panel_copies_to_username_keep_edit_mapping(self):
  for panel in (True,False):
   e=load();e.update(is_from_bot=lambda *a:False,translated_job_kind=lambda *a:None,AUDIO_TRANSLATE=False,resolve_reply_to_id=lambda *a:None,send_text=AsyncMock(return_value=SimpleNamespace(message_id=77)),db_save_map=Mock())
   bot=SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(id=-100222)))
   msg=SimpleNamespace(chat=SimpleNamespace(id=-100111),message_id=10,text='Hola',reply_markup=keyboard() if panel else None)
   await e['replicate_message'](SimpleNamespace(bot=bot),msg,'@johaaletrader_en',None,do_translate=True)
   if panel:
    e['db_save_map'].assert_called_once_with(-100111,10,-100222,77)
    bot.get_chat.assert_awaited_once()
   else:
    bot.get_chat.assert_not_awaited();e['db_save_map'].assert_not_called()
 async def test_username_lookup_failure_preserves_existing_copy_behavior(self):
  e=load();e.update(is_from_bot=lambda *a:False,translated_job_kind=lambda *a:None,AUDIO_TRANSLATE=False,resolve_reply_to_id=lambda *a:None,send_text=AsyncMock(return_value=SimpleNamespace(message_id=77)),db_save_map=Mock())
  bot=SimpleNamespace(get_chat=AsyncMock(side_effect=RuntimeError('temporary metadata failure')))
  msg=SimpleNamespace(chat=SimpleNamespace(id=-100111),message_id=10,text='Hola',reply_markup=keyboard())
  await e['replicate_message'](SimpleNamespace(bot=bot),msg,'@johaaletrader_en',None,do_translate=True)
  e['send_text'].assert_awaited_once();self.assertEqual(e['send_text'].call_args.args[1],'@johaaletrader_en')
 def test_other_replication_functions_unchanged(self):
  before=SOURCE.parent/'main.before.py'
  if not before.exists():self.skipTest('Local comparison only')
  defs=lambda p:{n.name:ast.dump(n) for n in ast.parse(p.read_text()).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))}
  old,new=defs(before),defs(SOURCE)
  for name in old:
   if name not in {'translate_buttons','replicate_edit','replicate_message','main','post_init'}:self.assertEqual(old[name],new[name],name)
if __name__=='__main__':unittest.main()
