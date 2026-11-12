"""One-time, explicitly requested repair of the English launch post's button."""
import json
from pathlib import Path
from telegram import InlineKeyboardButton,InlineKeyboardMarkup
from telegram.error import BadRequest

CHANNEL_USERNAME='@JohaaleTrader_en'
CHANNEL_ID=-1002774183911
MESSAGE_ID=2566
URL='https://t.me/JOHAALETRADER_bot?start=panel_en'

async def repair(application,data_dir,log):
    marker=Path(data_dir)/'panel-button-en-2566-fixed.json'
    if marker.exists():
        log.info('PANEL BUTTON REPAIR 2566: already completed')
        return
    try:
        chat=await application.bot.get_chat(CHANNEL_USERNAME)
        if int(chat.id)!=CHANNEL_ID:
            raise ValueError('English channel identity mismatch; nothing edited')
        keyboard=InlineKeyboardMarkup([[InlineKeyboardButton('🖥 Discover the panel',url=URL)]])
        try:
            result=await application.bot.edit_message_reply_markup(chat_id=CHANNEL_ID,message_id=MESSAGE_ID,reply_markup=keyboard)
            if getattr(result,'reply_markup',None)!=keyboard:
                raise ValueError('Telegram did not return the requested keyboard; repair not marked complete')
        except BadRequest as exc:
            if 'message is not modified' not in str(exc).lower():raise
            # This exact keyboard is already present, as confirmed by Telegram.
        marker.parent.mkdir(parents=True,exist_ok=True)
        temporary=marker.with_suffix('.tmp')
        temporary.write_text(json.dumps({'channel_id':CHANNEL_ID,'message_id':MESSAGE_ID,'url':URL,'verified_by':'Telegram editMessageReplyMarkup response'}))
        temporary.replace(marker)
        log.info('PANEL BUTTON REPAIR 2566 VERIFIED | channel=%s | url=%s | text and media untouched',CHANNEL_ID,URL)
    except Exception as exc:
        log.warning('PANEL BUTTON REPAIR 2566 FAILED | no completion marker | %s',exc)
