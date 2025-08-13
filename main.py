import os
import logging
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import aiohttp
import re

# -------- Config desde Variables de Entorno --------
BOT_TOKEN = os.getenv("BOT_TOKEN")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")   # Ej: "@tu_canal_es" o "-1001234567890"
DEST_CHANNEL = os.getenv("DEST_CHANNEL")       # Ej: "@tu_canal_en" o "-100987654321"

TRANSLATE = os.getenv("TRANSLATE", "false").lower() == "true"
TRANSLATOR = os.getenv("TRANSLATOR", "none").lower()  # "deepl", "libre" o "none"

# DeepL / LibreTranslate
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY")
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate")

# Ajustes de traducción
TARGET_LANG = os.getenv("TARGET_LANG", "EN")          # "EN", "EN-US", "EN-GB", etc.
FORMALITY   = os.getenv("FORMALITY", "less")          # "less" | "default" | "more"
GLOSSARY_ID = os.getenv("GLOSSARY_ID", "").strip()    # opcional (DeepL)

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


# --- Heurística ligera para evitar retraducir si ya está en inglés ---
_EN_COMMON = re.compile(r"\b(the|and|for|with|from|to|of|in|on|is|are|you|we|they|buy|sell|trade|signal|profit|setup)\b", re.I)
_ES_MARKERS = re.compile(r"[áéíóúñ¿¡]|\\b(que|para|porque|hola|gracias|con|sin|desde|hoy|mañana|ayer|compra|venta|señal|apalancamiento|beneficios)\\b", re.I)

def _probably_english(text: str) -> bool:
    # Si tiene acentos/ñ o palabras muy típicas del español, NO es inglés
    if _ES_MARKERS.search(text):
        return False
    # Si contiene varias palabras comunes del inglés, asumimos inglés
    if _EN_COMMON.search(text):
        return True
    # Ratio de ASCII simple como apoyo
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    ascii_letters = [c for c in letters if ord(c) < 128]
    return (len(ascii_letters) / max(1, len(letters))) > 0.85


async def translate_text(text: Optional[str], target_lang: str = None) -> str:
    """
    Traduce TEXTO PLANO (no HTML).
    - DeepL si TRANSLATOR=deepl y hay DEEPL_API_KEY (con formality y glosario opcional).
    - LibreTranslate si TRANSLATOR=libre (endpoint configurable).
    - Evita traducir si parece ya en inglés.
    """
    if not text or not TRANSLATE:
        return text or ""

    # Evitar retraducción innecesaria (si ya parece inglés)
    if _probably_english(text):
        return text

    tgt = (target_lang or TARGET_LANG or "EN").upper()

    try:
        if TRANSLATOR == "deepl" and DEEPL_API_KEY:
            url = "https://api-free.deepl.com/v2/translate"
            data = {
                "auth_key": DEEPL_API_KEY,
                "text": text,
                "target_lang": tgt,          # EN, EN-US, EN-GB...
                "formality": FORMALITY,      # less | default | more
            }
            if GLOSSARY_ID:
                data["glossary_id"] = GLOSSARY_ID

            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=data) as resp:
                    js = await resp.json()
                    # DeepL devuelve {"translations":[{"text": "..."}]}
                    return js["translations"][0]["text"]

        elif TRANSLATOR == "libre":
            url = LIBRETRANSLATE_URL
            data = {
                "q": text,
                "source": "auto",    # intenta detectar
                "target": tgt.lower().split("-")[0],  # "en"
                "format": "text"
            }
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=data) as resp:
                    js = await resp.json()
                    return js.get("translatedText", text)

    except Exception as e:
        logger.warning("Fallo al traducir: %s", e)

    return text  # fallback sin cambio


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.channel_post or not _chat_matches_source(update):
        return

    msg = update.channel_post

    # Sin traducción: copia 1:1
    if not TRANSLATE:
        await context.bot.copy_message(
            chat_id=DEST_CHANNEL,
            from_chat_id=msg.chat.id,
            message_id=msg.message_id,
        )
        return

    # Usamos TEXTO PLANO (no *_html) para evitar romper formato con el traductor
    text_plain = msg.text or ""
    caption_plain = msg.caption or ""

    if text_plain.strip():
        translated = await translate_text(text_plain, TARGET_LANG)
        await context.bot.send_message(
            chat_id=DEST_CHANNEL,
            text=translated,
            parse_mode=None,  # sin HTML para evitar conflictos
            disable_web_page_preview=getattr(msg, "has_protected_content", False),
        )
    else:
        translated_caption = await translate_text(caption_plain or "", TARGET_LANG)
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

