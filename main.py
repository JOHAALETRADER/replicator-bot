import os
import logging
from typing import Optional

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import aiohttp
import re

# -------- Config desde Variables de Entorno --------
BOT_TOKEN = os.getenv("BOT_TOKEN")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")
DEST_CHANNEL = os.getenv("DEST_CHANNEL")

TRANSLATE  = os.getenv("TRANSLATE", "false").lower() == "true"
TRANSLATOR = os.getenv("TRANSLATOR", "none").lower()  # "deepl" | "libre" | "none"

DEEPL_API_KEY  = os.getenv("DEEPL_API_KEY")
DEEPL_API_HOST = os.getenv("DEEPL_API_HOST", "api-free.deepl.com").strip()  # api-free.deepl.com | api.deepl.com
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate")

TARGET_LANG = os.getenv("TARGET_LANG", "EN").upper()
FORMALITY   = os.getenv("FORMALITY", "less")  # less | default | more
FORCE_TRANSLATE = os.getenv("FORCE_TRANSLATE", "false").lower() == "true"

# GLOSARIO: dos modos
GLOSSARY_ID  = os.getenv("GLOSSARY_ID", "").strip()   # si ya tienes un ID, úsalo
GLOSSARY_TSV = os.getenv("GLOSSARY_TSV", "").strip()  # si NO hay ID, crearemos uno con este TSV

# DeepL solo soporta "formality" en estos idiomas:
FORMALITY_LANGS = {"DE","FR","IT","ES","NL","PL","PT-PT","PT-BR","RU","JA"}

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("replicator")

def _ensure_env():
    missing = []
    if not BOT_TOKEN:      missing.append("BOT_TOKEN")
    if not SOURCE_CHANNEL: missing.append("SOURCE_CHANNEL")
    if not DEST_CHANNEL:   missing.append("DEST_CHANNEL")
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
_ES_MARKERS = re.compile(r"[áéíóúñ¿¡]|\b(que|para|porque|hola|gracias|con|sin|desde|hoy|mañana|ayer|compra|venta|señal|apalancamiento|beneficios)\b", re.I)

def _probably_english(text: str) -> bool:
    if _ES_MARKERS.search(text):
        return False
    if _EN_COMMON.search(text):
        return True
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    ascii_letters = [c for c in letters if ord(c) < 128]
    return (len(ascii_letters) / max(1, len(letters))) > 0.85

async def _deepl_create_glossary_if_needed() -> Optional[str]:
    """
    Si no hay GLOSSARY_ID pero sí GLOSSARY_TSV, crea el glosario en DeepL (host correcto) y devuelve el ID.
    """
    global GLOSSARY_ID
    if TRANSLATOR != "deepl" or not DEEPL_API_KEY:
        return None
    if GLOSSARY_ID:
        return GLOSSARY_ID
    if not GLOSSARY_TSV:
        return None

    url = f"https://{DEEPL_API_HOST}/v2/glossaries"
    form = aiohttp.FormData()
    form.add_field("name", "Trading ES-EN (Auto)")
    form.add_field("source_lang", "ES")
    form.add_field("target_lang", "EN")
    form.add_field("entries", GLOSSARY_TSV, filename="glossary.tsv",
                   content_type="text/tab-separated-values")

    headers = {
        "Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}",
        # MUY IMPORTANTE: enviar el Content-Type del form con el boundary correcto
        "Content-Type": form.content_type,
    }
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, data=form) as resp:
                txt = await resp.text()
                if resp.status != 200:
                    logger.warning("DeepL glossary create HTTP %s: %s", resp.status, txt)
                    return None
                js = await resp.json()
                GLOSSARY_ID = js.get("glossary_id", "")
                if GLOSSARY_ID:
                    logger.info("DeepL glossary created: %s", GLOSSARY_ID)
                else:
                    logger.warning("DeepL glossary creation response without ID: %s", txt)
                return GLOSSARY_ID or None
    except Exception as e:
        logger.warning("DeepL glossary create failed: %s", e)
        return None

async def translate_text(text: Optional[str], target_lang: str = None) -> str:
    """
    Traduce TEXTO PLANO (no HTML).
    - DeepL con glosario (ID ya existente o creado automáticamente).
    - LibreTranslate como alternativa.
    - Evita traducir si ya parece inglés (a menos que FORCE_TRANSLATE=true).
    """
    if not text or not TRANSLATE:
        return text or ""

    if (not FORCE_TRANSLATE) and _probably_english(text):
        return text

    tgt = (target_lang or TARGET_LANG or "EN").upper()

    try:
        if TRANSLATOR == "deepl" and DEEPL_API_KEY:
            # asegúrate de que hay glosario si el usuario nos dio TSV
            await _deepl_create_glossary_if_needed()

            url = f"https://{DEEPL_API_HOST}/v2/translate"
            data = {
                "auth_key": DEEPL_API_KEY,
                "text": text,
                "target_lang": tgt,
            }
            # solo enviar "formality" si el idioma destino lo soporta
            if tgt in FORMALITY_LANGS:
                data["formality"] = FORMALITY

            if GLOSSARY_ID:
                data["glossary_id"] = GLOSSARY_ID

            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=data) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        logger.warning("DeepL translate HTTP %s: %s", resp.status, txt)
                        return text  # fallback: deja el original
                    js = await resp.json()
                    return js["translations"][0]["text"]

        elif TRANSLATOR == "libre":
            url = LIBRETRANSLATE_URL
            data = {"q": text, "source": "auto", "target": tgt.lower().split("-")[0], "format": "text"}
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=data) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        logger.warning("LibreTranslate HTTP %s: %s", resp.status, txt)
                        return text
                    js = await resp.json()
                    return js.get("translatedText", text)

    except Exception as e:
        logger.warning("Fallo al traducir: %s", e)

    return text

async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.channel_post or not _chat_matches_source(update):
        return

    msg = update.channel_post

    if not TRANSLATE:
        await context.bot.copy_message(
            chat_id=DEST_CHANNEL,
            from_chat_id=msg.chat.id,
            message_id=msg.message_id,
        )
        return

    text_plain = msg.text or ""
    caption_plain = msg.caption or ""

    if text_plain.strip():
        translated = await translate_text(text_plain, TARGET_LANG)
        await context.bot.send_message(
            chat_id=DEST_CHANNEL,
            text=translated,
            parse_mode=None,
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

