import os
import logging
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import aiohttp

# -------- Config --------
BOT_TOKEN = os.getenv("BOT_TOKEN")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")   # Ej: "@tu_canal_es" o "-1001234567890"
DEST_CHANNEL = os.getenv("DEST_CHANNEL")       # Ej: "@tu_canal_en" o "-100987654321"
TRANSLATE = os.getenv("TRANSLATE", "false").lower() == "true"
TRANSLATOR = os.getenv("TRANSLATOR", "none").lower()  # "deepl", "libre" o "none"

DEEPL_API_KEY = os.getenv("DEEPL_API_KEY")
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("replicator")

def _ensure_env():
    missing = []
    if not BOT_TOKEN:        missing.append("BOT_TOKEN")
    if not SOURCE_CHANNEL:   missing.append("SOURCE_CHANNEL")
    if not DEST_CHANNEL:     missing.append("DEST_CHANNEL")
    if missing:
        raise RuntimeError("Faltan variables: " + ", ".join(missing))

def _chat_matches_source(update: Update) -> bool:
    chat = update.channel_post.chat
    if SOURCE_CHANNEL.startswith("@"):
        uname = chat.username or ""
        return ("@" + uname.lower()) == SOURCE_CHANNEL.lower()
    else:
        return str(chat.id) == SOURCE_CHANNEL

async def translate_text(text: Optional[str], target_lang: str = "EN") -> str:
    """
    Traduce texto plano (no HTML). Usa DeepL si TRANSLATOR=deepl y hay DEEPL_API_KEY;
    si no, intenta LibreTranslate (endpoint configurable por variable).
    """
    if not text or not TRANSLATE:
        return text or ""
    try:
        if TRANSLATOR == "deepl" and DEEPL_API_KEY:
            # DeepL: no necesita source_lang; target_lang "EN" para inglés
            url = "https://api-free.deepl.com/v2/translate"
            data = {"auth_key": DEEPL_API_KEY, "text": text, "target_lang": target_lang}
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=data) as resp:
                    js = await resp.json()
                    return js["translations"][0]["text"]
        elif TRANSLATOR == "libre":
            # LibreTranslate público (puede estar lento/saturado). Acepta source/target
            url = LIBRETRANSLATE_URL
            data = {"q": text, "source": "es", "target": "en", "format": "text"}
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=data) as resp:
                    js = await resp.json()
                    return js.get("translatedText", text)
    except Exception as e:
        logger.warning("Fallo al traducir: %s", e)
    return text

async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.channel_post or not _chat_matches_source(update):
        return

    msg = update.channel_post

    # Si no hay traducción, copia 1:1
    if not TRANSLATE:
        await context.bot.copy_message(
            chat_id=DEST_CHANNEL,
            from_chat_id=msg.chat.id,
            message_id=msg.message_id,
        )
        return

    # ---- CAMBIO CLAVE: usar TEXTO PLANO en lugar de *_html ----
    text_plain = msg.text or ""
    caption_plain = msg.caption or ""

    if text_plain.strip():
        translated = await translate_text(text_plain, "EN")
        await context.bot.send_message(
            chat_id=DEST_CHANNEL,
            text=translated,
            # No usamos HTML para evitar que caracteres de DeepL rompan el parseo
            parse_mode=None,
            disable_web_page_preview=getattr(msg, "has_protected_content", False),
        )
    else:
        translated_caption = await translate_text(caption_plain or "", "EN")
        await context.bot.copy_message(
            chat_id=DEST_CHANNEL,
            from_chat_id=msg.chat.id,
            message_id=msg.message_id,
            caption=translated_caption if translated_caption else None,
            parse_mode=None if translated_caption else None,
        )

def main():
    _ensure_env()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))
    app.run_polling(allowed_updates=["channel_post"], poll_interval=1.5, stop_signals=None)

if __name__ == "__main__":
    main()
