import os
import logging
from typing import Optional, List, Dict, Any

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
)
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import aiohttp
import re
import json

# ========= Variables de entorno =========
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Canal → canal
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")  # @usuario_canal o id
DEST_CHANNEL   = os.getenv("DEST_CHANNEL")    # @usuario_canal o id

# Traducción
TRANSLATE   = os.getenv("TRANSLATE", "false").lower() == "true"
TRANSLATOR  = os.getenv("TRANSLATOR", "none").lower()  # "deepl"|"libre"|"none"
DEEPL_API_KEY  = os.getenv("DEEPL_API_KEY", "").strip()
DEEPL_API_HOST = os.getenv("DEEPL_API_HOST", "api-free.deepl.com").strip()
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate").strip()

TARGET_LANG     = os.getenv("TARGET_LANG", "EN").upper()
SOURCE_LANG     = os.getenv("SOURCE_LANG", "ES").upper()
FORMALITY       = os.getenv("FORMALITY", "default")  # less|default|more
FORCE_TRANSLATE = os.getenv("FORCE_TRANSLATE", "false").lower() == "true"
TRANSLATE_BUTTONS = os.getenv("TRANSLATE_BUTTONS", "false").lower() == "true"

# Glosario DeepL
GLOSSARY_ID  = os.getenv("GLOSSARY_ID", "").strip()
GLOSSARY_TSV = os.getenv("GLOSSARY_TSV", "").strip()

# Errores → DM
ADMIN_ID    = os.getenv("ADMIN_ID", "").strip()
ERROR_ALERT = os.getenv("ERROR_ALERT", "false").lower() == "true"

# Mapeo de TEMAS: JSON string -> dict
# Ejemplo de valor:
# {"-1001946870620":{"1":20605,"129":20607}}
TOPIC_MAPPING_STR = os.getenv("TOPIC_MAPPING", "{}")
try:
    TOPIC_MAPPING: Dict[str, Dict[str, int]] = json.loads(TOPIC_MAPPING_STR)
except Exception:
    TOPIC_MAPPING = {}

# ========= logging =========
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("replicator")

# ========= util =========
def _ensure_env():
    missing = []
    if not BOT_TOKEN: missing.append("BOT_TOKEN")
    if not SOURCE_CHANNEL: missing.append("SOURCE_CHANNEL")
    if not DEST_CHANNEL: missing.append("DEST_CHANNEL")
    if missing:
        raise RuntimeError("Faltan variables: " + ", ".join(missing))

def _chat_matches_source(update: Update) -> bool:
    chat = update.channel_post.chat
    if SOURCE_CHANNEL.startswith("@"):
        uname = (chat.username or "").lower()
        return ("@" + uname) == SOURCE_CHANNEL.lower()
    else:
        return str(chat.id) == SOURCE_CHANNEL

_EN_COMMON = re.compile(r"\b(the|and|for|with|from|to|of|in|on|is|are|you|we|they|buy|sell|trade|signal|profit|setup)\b", re.I)
_ES_MARKERS = re.compile(r"[áéíóúñ¿¡]|\b(que|para|porque|hola|gracias|con|sin|desde|hoy|mañana|ayer|compra|venta|señal|apalancamiento|beneficios)\b", re.I)

def _probably_english(text: str) -> bool:
    if _ES_MARKERS.search(text): return False
    if _EN_COMMON.search(text):  return True
    letters = [c for c in text if c.isalpha()]
    if not letters: return False
    ascii_letters = [c for c in letters if ord(c) < 128]
    return (len(ascii_letters) / max(1, len(letters))) > 0.85

async def _deepl_create_glossary_if_needed() -> Optional[str]:
    global GLOSSARY_ID
    if TRANSLATOR != "deepl" or not DEEPL_API_KEY: return None
    if GLOSSARY_ID: return GLOSSARY_ID
    if not GLOSSARY_TSV: return None

    url = f"https://{DEEPL_API_HOST}/v2/glossaries"
    form = aiohttp.FormData()
    form.add_field("name", "Trading ES-EN (Auto)")
    form.add_field("source_lang", "ES")
    form.add_field("target_lang", "EN")
    form.add_field("entries", GLOSSARY_TSV, filename="glossary.tsv",
                   content_type="text/tab-separated-values")

    headers = {"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"}
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

def _split_chunks(text: str, limit: int = 4096) -> List[str]:
    if len(text) <= limit: return [text]
    parts: List[str] = []
    while text:
        parts.append(text[:limit]); text = text[limit:]
    return parts

async def translate_text(text: Optional[str], target_lang: str = None) -> str:
    if not text or not TRANSLATE: return text or ""
    if (not FORCE_TRANSLATE) and _probably_english(text): return text

    tgt = (target_lang or TARGET_LANG or "EN").upper()
    src = SOURCE_LANG or "ES"
    try:
        if TRANSLATOR == "deepl" and DEEPL_API_KEY:
            await _deepl_create_glossary_if_needed()
            url = f"https://{DEEPL_API_HOST}/v2/translate"
            data: Dict[str, Any] = {
                "auth_key": DEEPL_API_KEY,
                "text": text,
                "target_lang": tgt,
                "source_lang": src,
            }
            if tgt in {"DE","FR","IT","ES","NL","PL","PT-PT","PT-BR","RU","JA"}:
                data["formality"] = FORMALITY
            if GLOSSARY_ID:
                data["glossary_id"] = GLOSSARY_ID

            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=data) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        logger.warning("DeepL translate HTTP %s: %s", resp.status, body)
                        return text
                    js = await resp.json()
                    return js["translations"][0]["text"]
        elif TRANSLATOR == "libre":
            url = LIBRETRANSLATE_URL
            data = {"q": text, "source": "auto", "target": tgt.lower().split("-")[0], "format": "text"}
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=data) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        logger.warning("LibreTranslate HTTP %s: %s", resp.status, body)
                        return text
                    js = await resp.json()
                    return js.get("translatedText", text)
    except Exception as e:
        logger.warning("Fallo al traducir: %s", e)
    return text

async def translate_inline_keyboard(markup: Optional[InlineKeyboardMarkup], target_lang: str) -> Optional[InlineKeyboardMarkup]:
    if not TRANSLATE_BUTTONS or not markup or not getattr(markup, "inline_keyboard", None):
        return markup
    new_rows: List[List[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        new_row: List[InlineKeyboardButton] = []
        for b in row:
            try:
                new_text = await translate_text(b.text or "", target_lang)
            except Exception:
                new_text = b.text or ""
            new_row.append(
                InlineKeyboardButton(
                    text=(new_text or "")[:64],
                    url=b.url,
                    callback_data=b.callback_data,
                    switch_inline_query=b.switch_inline_query,
                    switch_inline_query_current_chat=b.switch_inline_query_current_chat,
                    web_app=getattr(b, "web_app", None),
                )
            )
        new_rows.append(new_row)
    return InlineKeyboardMarkup(new_rows)

async def alert_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    if not (ERROR_ALERT and ADMIN_ID): return
    try:
        await context.bot.send_message(chat_id=int(ADMIN_ID), text=f"⚠️ {text[:3900]}")
    except Exception:
        pass

# ========= Mapeo de temas =========
def map_topic(chat_id: int, source_thread_id: Optional[int]) -> Optional[int]:
    """Devuelve el thread destino para (chat_id, source_thread_id)."""
    norm_src = source_thread_id
    if source_thread_id in (None, 0):
        norm_src = 1  # tratamos "General" como 1 si llega None/0
    m1 = TOPIC_MAPPING.get(str(chat_id)) or {}
    if not norm_src:
        return None
    dst = m1.get(str(norm_src)) or m1.get(str(source_thread_id or ""))
    return int(dst) if dst else None

# ========= Replicación común =========
async def replicate_message(
    context: ContextTypes.DEFAULT_TYPE,
    src_msg: Message,
    dest_chat_id: int,
    dest_thread_id: Optional[int]
):
    text_plain   = src_msg.text or ""
    caption_plain= src_msg.caption or ""
    markup       = src_msg.reply_markup

    # TEXTO puro
    if text_plain.strip():
        out_text = await translate_text(text_plain, TARGET_LANG)
        out_markup = await translate_inline_keyboard(markup, TARGET_LANG)
        chunks = _split_chunks(out_text)
        for i, chunk in enumerate(chunks):
            await context.bot.send_message(
                chat_id=dest_chat_id,
                message_thread_id=dest_thread_id,
                text=chunk,
                disable_web_page_preview=True,
                reply_markup=out_markup if i == 0 else None,
            )
        return

    # MEDIA + caption
    translated_caption = await translate_text(caption_plain or "", TARGET_LANG)
    out_markup = await translate_inline_keyboard(markup, TARGET_LANG)

    # copia 1:1 preservando media; sobreescribimos solo caption/markup
    await context.bot.copy_message(
        chat_id=dest_chat_id,
        from_chat_id=src_msg.chat.id,
        message_id=src_msg.message_id,
        message_thread_id=dest_thread_id,
        caption=translated_caption if translated_caption else None,
        reply_markup=out_markup
    )

# ========= Handlers =========
async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.channel_post or not _chat_matches_source(update):
            return
        msg = update.channel_post
        await replicate_message(
            context,
            msg,
            dest_chat_id = int(DEST_CHANNEL) if str(DEST_CHANNEL).lstrip("-").isdigit() else DEST_CHANNEL,
            dest_thread_id = None
        )
    except Exception as e:
        logger.exception("Error on_channel_post")
        await alert_admin(context, f"Error canal→canal: {e}")

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mensajes en supergrupos con temas."""
    try:
        if not update.message:
            return
        msg = update.message

        # Solo foros/temas: requieren message_thread_id
        thread_id = getattr(msg, "message_thread_id", None)
        if thread_id in (None, 0):
            return  # ignoramos mensajes fuera de temas

        src_chat_id = msg.chat.id  # negativo
        dst_thread = map_topic(src_chat_id, thread_id)
        if not dst_thread:
            return  # este tema no está mapeado

        # Destino: mismo grupo (replica tema→tema)
        await replicate_message(
            context,
            msg,
            dest_chat_id=src_chat_id,
            dest_thread_id=dst_thread
        )
    except Exception as e:
        logger.exception("Error on_group_message")
        await alert_admin(context, f"Error tema→tema: {e}")

# ========= main =========
def main():
    _ensure_env()
    app = Application.builder().token(BOT_TOKEN).build()

    # Canal → canal
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))

    # Grupos con temas (supergrupos/foros)
    app.add_handler(MessageHandler(filters.ChatType.SUPERGROUP & ~filters.StatusUpdate.ALL, on_group_message))

    app.run_polling(
        allowed_updates=["channel_post", "message"],
        poll_interval=1.5,
        stop_signals=None
    )

if __name__ == "__main__":
    main()
