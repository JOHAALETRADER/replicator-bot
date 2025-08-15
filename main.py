import os
import json
import logging
import re
from typing import Optional, List, Tuple, Dict

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import aiohttp

# -------------------- Config desde Variables de Entorno --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Canal -> Canal ya existente
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")
DEST_CHANNEL   = os.getenv("DEST_CHANNEL")

# Traducción
TRANSLATE  = os.getenv("TRANSLATE", "false").lower() == "true"
TRANSLATOR = os.getenv("TRANSLATOR", "none").lower()  # "deepl" | "libre" | "none"
DEEPL_API_KEY   = os.getenv("DEEPL_API_KEY", "").strip()
DEEPL_API_HOST  = os.getenv("DEEPL_API_HOST", "api-free.deepl.com").strip()  # api-free.deepl.com | api.deepl.com
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate").strip()

TARGET_LANG     = os.getenv("TARGET_LANG", "EN").upper()
SOURCE_LANG     = os.getenv("SOURCE_LANG", "ES").upper()
FORMALITY       = os.getenv("FORMALITY", "default")  # less | default | more
FORCE_TRANSLATE = os.getenv("FORCE_TRANSLATE", "false").lower() == "true"
TRANSLATE_BUTTONS = os.getenv("TRANSLATE_BUTTONS", "true").lower() == "true"

# Admin y alertas
ADMIN_ID    = int(os.getenv("ADMIN_ID", "0") or "0")
ERROR_ALERT = os.getenv("ERROR_ALERT", "true").lower() == "true"

# Glosario DeepL
GLOSSARY_ID  = os.getenv("GLOSSARY_ID", "").strip()
GLOSSARY_TSV = os.getenv("GLOSSARY_TSV", "").strip()

# DeepL: idiomas con formality
FORMALITY_LANGS = {"DE","FR","IT","ES","NL","PL","PT-PT","PT-BR","RU","JA"}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("replicator")

# -------------------- Mapeos de Temas (con tus IDs) --------------------
# Nota: los chat_id de grupos deben ir en formato Telegram: negativos con -100...
#     t.me/c/<absid>/<topic>/<msg>  -> chat_id = -100<absid>, topic_id = <topic>
# Múltiples destinos: lista de tuplas (dest_chat_id, dest_topic_id)

# GRUPO 1
G1 = -1001946870620
TOPIC_CHAT_ES          = (G1, 1)
TOPIC_CHAT_EN          = (G1, 20605)

TOPIC_RES_JT_ES        = (G1, 129)
TOPIC_RES_JT_EN        = (G1, 20607)

TOPIC_RES_ALUMNOS      = (G1, 2890)
TOPIC_RETIROS_VIP      = (G1, 17373)
TOPIC_VIP_RESULTS_EN   = (G1, 20611)

TOPIC_ESTRATEGIAS      = (G1, 8)
TOPIC_PLAN_RIESGO      = (G1, 11)
TOPIC_TRADING_PLAN_EN  = (G1, 20613)

TOPIC_NOTICIAS_SORTEOS = (G1, 9)
TOPIC_UPDATES_PRIZES   = (G1, 20616)

# GRUPO 2
G2 = -1002127373425
TOPIC_RES_TEAMS_ES     = (G2, 3)
TOPIC_RES_TEAMS_EN     = (G2, 4096)
TOPIC_RES_ALUMNOS_VIP  = (G2, 2)
TOPIC_VIP_RESULTS_RISK = (G2, 4098)

# GRUPO 3
G3 = -1002131156976
TOPIC_BINARY_ES        = (G3, 3)
TOPIC_BINARY_EN        = (G3, 5571)

TOPIC_BINANCE_ES       = (G3, 5337)
TOPIC_BINANCE_EN       = (G3, 5573)

TOPIC_FOREX_BIAS       = (G3, 4)
TOPIC_NEWS_ANALYSIS    = (G3, 272)
TOPIC_MARKET_EN        = (G3, 5576)

TOPIC_SYNTH_ES         = (G3, 2)
TOPIC_SYNTH_EN         = (G3, 5579)

# Mapeo maestro: (src_chat, src_topic) -> [ (dst_chat, dst_topic), ... ]
TOPIC_MAPPING: Dict[Tuple[int, int], List[Tuple[int, int]]] = {
    # Grupo 1
    TOPIC_CHAT_ES:          [TOPIC_CHAT_EN],   # Solo mensajes del admin (ver ADMIN_ONLY)
    TOPIC_RES_JT_ES:        [TOPIC_RES_JT_EN],
    TOPIC_RES_ALUMNOS:      [TOPIC_VIP_RESULTS_EN],
    TOPIC_RETIROS_VIP:      [TOPIC_VIP_RESULTS_EN],
    TOPIC_ESTRATEGIAS:      [TOPIC_TRADING_PLAN_EN],
    TOPIC_PLAN_RIESGO:      [TOPIC_TRADING_PLAN_EN],
    TOPIC_NOTICIAS_SORTEOS: [TOPIC_UPDATES_PRIZES],

    # Grupo 2
    TOPIC_RES_TEAMS_ES:     [TOPIC_RES_TEAMS_EN],
    TOPIC_RES_ALUMNOS_VIP:  [TOPIC_VIP_RESULTS_RISK],

    # Grupo 3
    TOPIC_BINARY_ES:        [TOPIC_BINARY_EN],
    TOPIC_BINANCE_ES:       [TOPIC_BINANCE_EN],
    TOPIC_FOREX_BIAS:       [TOPIC_MARKET_EN],
    TOPIC_NEWS_ANALYSIS:    [TOPIC_MARKET_EN],
    TOPIC_SYNTH_ES:         [TOPIC_SYNTH_EN],
}

# Tópicos que replican SOLO si el autor es el admin
ADMIN_ONLY: List[Tuple[int, int]] = [
    TOPIC_CHAT_ES,  # CHAT ES -> CHAT ROOM EN (solo tú)
]

# -------------------- Utilidades --------------------
def _ensure_env():
    missing = []
    if not BOT_TOKEN:      missing.append("BOT_TOKEN")
    if not SOURCE_CHANNEL: missing.append("SOURCE_CHANNEL")
    if not DEST_CHANNEL:   missing.append("DEST_CHANNEL")
    if missing:
        raise RuntimeError("Faltan variables: " + ", ".join(missing))

def _chat_matches_source_channel(update: Update) -> bool:
    # Para canal -> canal
    if not update.channel_post:
        return False
    chat = update.channel_post.chat
    if SOURCE_CHANNEL.startswith("@"):
        uname = (chat.username or "").lower()
        return ("@" + uname) == SOURCE_CHANNEL.lower()
    else:
        return str(chat.id) == SOURCE_CHANNEL

_EN_COMMON = re.compile(
    r"\b(the|and|for|with|from|to|of|in|on|is|are|you|we|they|buy|sell|trade|signal|profit|setup)\b",
    re.I
)
_ES_MARKERS = re.compile(
    r"[áéíóúñ¿¡]|\b(que|para|porque|hola|gracias|con|sin|desde|hoy|mañana|ayer|compra|venta|señal|apalancamiento|beneficios)\b",
    re.I
)

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
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    while text:
        parts.append(text[:limit])
        text = text[limit:]
    return parts

async def translate_text(text: Optional[str], target_lang: str = None) -> str:
    if not text or not TRANSLATE:
        return text or ""

    if (not FORCE_TRANSLATE) and _probably_english(text):
        return text

    tgt = (target_lang or TARGET_LANG or "EN").upper()
    src = SOURCE_LANG or "ES"

    try:
        if TRANSLATOR == "deepl" and DEEPL_API_KEY:
            await _deepl_create_glossary_if_needed()
            url = f"https://{DEEPL_API_HOST}/v2/translate"
            data = {"auth_key": DEEPL_API_KEY, "text": text, "target_lang": tgt, "source_lang": src}
            if tgt in FORMALITY_LANGS:
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
                    web_app=getattr(b, "web_app", None)
                )
            )
        new_rows.append(new_row)
    return InlineKeyboardMarkup(new_rows)

# -------------------- Handlers --------------------
async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Replica canal -> canal (con traducción opcional)."""
    msg = update.channel_post
    if not msg:
        return

    try:
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
            translated_markup = await translate_inline_keyboard(msg.reply_markup, TARGET_LANG)
            for i, chunk in enumerate(_split_chunks(translated)):
                await context.bot.send_message(
                    chat_id=DEST_CHANNEL,
                    text=chunk,
                    parse_mode=None,
                    disable_web_page_preview=True,
                    reply_markup=translated_markup if i == 0 else None
                )
        else:
            translated_caption = await translate_text(caption_plain or "", TARGET_LANG)
            translated_markup = await translate_inline_keyboard(msg.reply_markup, TARGET_LANG)
            await context.bot.copy_message(
                chat_id=DEST_CHANNEL,
                from_chat_id=msg.chat.id,
                message_id=msg.message_id,
                caption=translated_caption if translated_caption else None,
                parse_mode=None if translated_caption else None,
                reply_markup=translated_markup
            )
    except Exception as e:
        logger.exception("Error canal->canal")
        if ERROR_ALERT and ADMIN_ID:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Error canal→canal: {e}")

async def handle_group_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Replica entre temas de grupos (supergrupos) según TOPIC_MAPPING."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type not in ("supergroup", "group"):
        return
    if not getattr(msg, "is_topic_message", False):
        return

    src = (chat.id, msg.message_thread_id or 0)
    destinations = TOPIC_MAPPING.get(src)
    if not destinations:
        return

    # Filtro: solo mensajes del admin en ciertos tópicos
    if src in ADMIN_ONLY and ADMIN_ID:
        author = msg.from_user.id if msg.from_user else None
        if author != ADMIN_ID:
            return

    try:
        text_plain = msg.text or ""
        caption_plain = msg.caption or ""

        # Traducción si procede
        translated_markup = await translate_inline_keyboard(msg.reply_markup, TARGET_LANG)

        if text_plain.strip():
            out_text = await translate_text(text_plain, TARGET_LANG)
            chunks = _split_chunks(out_text)
            for (dst_chat, dst_topic) in destinations:
                for i, chunk in enumerate(chunks):
                    await context.bot.send_message(
                        chat_id=dst_chat,
                        message_thread_id=dst_topic,
                        text=chunk,
                        parse_mode=None,
                        disable_web_page_preview=True,
                        reply_markup=translated_markup if i == 0 else None
                    )
        else:
            out_caption = await translate_text(caption_plain or "", TARGET_LANG)
            for (dst_chat, dst_topic) in destinations:
                await context.bot.copy_message(
                    chat_id=dst_chat,
                    message_thread_id=dst_topic,
                    from_chat_id=chat.id,
                    message_id=msg.message_id,
                    caption=out_caption if out_caption else None,
                    parse_mode=None if out_caption else None,
                    reply_markup=translated_markup
                )

    except Exception as e:
        logger.exception("Error en réplica de grupos/temas")
        if ERROR_ALERT and ADMIN_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"⚠️ Error en réplica de temas {src}: {e}"
                )
            except Exception:
                pass

# -------------------- Router principal --------------------
async def on_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.channel_post and _chat_matches_source_channel(update):
            await handle_channel_post(update, context)
            return

        # Mensajes en grupos/supergrupos con tópicos
        if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
            await handle_group_post(update, context)
            return

    except Exception as e:
        logger.exception("Error en on_update")
        if ERROR_ALERT and ADMIN_ID:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Error general: {e}")

# -------------------- Main --------------------
def main():
    _ensure_env()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, on_update))
    app.run_polling(allowed_updates=["channel_post", "message"], poll_interval=1.2, stop_signals=None)

if __name__ == "__main__":
    main()

