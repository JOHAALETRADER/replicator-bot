import os, re, json, logging, aiohttp, traceback
from typing import Optional, List, Dict

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    Message, Chat, MessageEntity
)
from telegram.ext import (
    Application, MessageHandler, ContextTypes, filters
)

# ================== CONFIG (ENV) ==================
BOT_TOKEN        = os.getenv("BOT_TOKEN", "").strip()

# Canal a canal (ya lo tenías)
SOURCE_CHANNEL   = os.getenv("SOURCE_CHANNEL", "").strip()
DEST_CHANNEL     = os.getenv("DEST_CHANNEL", "").strip()

# Traducción
TRANSLATE        = os.getenv("TRANSLATE", "false").lower() == "true"
TRANSLATOR       = os.getenv("TRANSLATOR", "none").lower()       # "deepl" | "libre" | "none"
DEEPL_API_KEY    = os.getenv("DEEPL_API_KEY", "").strip()
DEEPL_API_HOST   = os.getenv("DEEPL_API_HOST", "api-free.deepl.com").strip()
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate").strip()
TARGET_LANG      = os.getenv("TARGET_LANG", "EN").upper()
SOURCE_LANG      = os.getenv("SOURCE_LANG", "ES").upper()
FORMALITY        = os.getenv("FORMALITY", "default")
FORCE_TRANSLATE  = os.getenv("FORCE_TRANSLATE", "false").lower() == "true"
TRANSLATE_BUTTONS= os.getenv("TRANSLATE_BUTTONS", "true").lower() == "true"  # traduce SOLO el texto de botones

# Glosario DeepL
GLOSSARY_ID      = os.getenv("GLOSSARY_ID", "").strip()
GLOSSARY_TSV     = os.getenv("GLOSSARY_TSV", "").strip()

# Mapeo de temas: JSON en env TOPIC_MAPPING
# Ejemplo (UNA SOLA LÍNEA aceptada): {"-1001946870620":{"1":"20605","129":"20607",...},"-100213...":{...}}
TOPIC_MAPPING_RAW = os.getenv("TOPIC_MAPPING", "{}")
try:
    TOPIC_MAPPING: Dict[str, Dict[str,str]] = json.loads(TOPIC_MAPPING_RAW) if TOPIC_MAPPING_RAW else {}
except Exception:
    TOPIC_MAPPING = {}

# Alertas de error
ADMIN_ID   = os.getenv("ADMIN_ID", "").strip()        # tu user id (e.g., 5958154558)
ERROR_ALERT= os.getenv("ERROR_ALERT", "true").lower() == "true"

# ================== LOG ===========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("replicator")

# ================== HELPERS =======================
def need_vars(*pairs):
    missing = [name for name, val in pairs if not val]
    if missing:
        raise RuntimeError("Faltan variables: " + ", ".join(missing))

_EN_COMMON = re.compile(r"\b(the|and|for|with|from|to|of|in|on|is|are|you|we|they|buy|sell|trade|signal|profit|setup)\b", re.I)
_ES_MARKERS = re.compile(r"[áéíóúñ¿¡]|\b(que|para|porque|hola|gracias|con|sin|desde|hoy|mañana|ayer|compra|venta|señal|apalancamiento|beneficios)\b", re.I)

def probably_english(text: str) -> bool:
    if _ES_MARKERS.search(text): return False
    if _EN_COMMON.search(text):  return True
    letters = [c for c in text if c.isalpha()]
    if not letters: return False
    ascii_letters = [c for c in letters if ord(c) < 128]
    return (len(ascii_letters) / max(1, len(letters))) > 0.85

async def deepl_create_glossary_if_needed():
    global GLOSSARY_ID
    if TRANSLATOR != "deepl" or not DEEPL_API_KEY or GLOSSARY_ID or not GLOSSARY_TSV:
        return
    url = f"https://{DEEPL_API_HOST}/v2/glossaries"
    form = aiohttp.FormData()
    form.add_field("name", "Trading ES-EN (Auto)")
    form.add_field("source_lang", "ES")
    form.add_field("target_lang", "EN")
    form.add_field("entries", GLOSSARY_TSV, filename="glossary.tsv",
                   content_type="text/tab-separated-values")
    headers = {"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.post(url, headers=headers, data=form) as r:
                t = await r.text()
                if r.status != 200:
                    log.warning("DeepL glossary create HTTP %s: %s", r.status, t); return
                js = await r.json()
                GLOSSARY_ID = js.get("glossary_id", "")
                if GLOSSARY_ID:
                    log.info("DeepL glossary created: %s", GLOSSARY_ID)
    except Exception as e:
        log.warning("DeepL glossary create failed: %s", e)

async def translate_text(text: Optional[str], target_lang: Optional[str] = None) -> str:
    if not text: return ""
    if not TRANSLATE: return text
    if (not FORCE_TRANSLATE) and probably_english(text):
        return text
    tgt = (target_lang or TARGET_LANG or "EN").upper()
    src = SOURCE_LANG or "ES"

    try:
        if TRANSLATOR == "deepl" and DEEPL_API_KEY:
            await deepl_create_glossary_if_needed()
            url = f"https://{DEEPL_API_HOST}/v2/translate"
            data = {"auth_key": DEEPL_API_KEY, "text": text, "target_lang": tgt, "source_lang": src}
            if tgt in {"DE","FR","IT","ES","NL","PL","PT-PT","PT-BR","RU","JA"}:
                data["formality"] = FORMALITY
            if GLOSSARY_ID:
                data["glossary_id"] = GLOSSARY_ID
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                async with s.post(url, data=data) as r:
                    body = await r.text()
                    if r.status != 200:
                        log.warning("DeepL translate HTTP %s: %s", r.status, body)
                        return text
                    js = await r.json()
                    return js["translations"][0]["text"]

        elif TRANSLATOR == "libre" and LIBRETRANSLATE_URL:
            data = {"q": text, "source": "auto", "target": tgt.lower().split("-")[0], "format": "text"}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                async with s.post(LIBRETRANSLATE_URL, data=data) as r:
                    body = await r.text()
                    if r.status != 200:
                        log.warning("LibreTranslate HTTP %s: %s", r.status, body)
                        return text
                    js = await r.json()
                    return js.get("translatedText", text)

    except Exception as e:
        log.warning("Fallo al traducir: %s", e)

    return text

async def translate_keyboard(markup: Optional[InlineKeyboardMarkup], tgt: str) -> Optional[InlineKeyboardMarkup]:
    if not TRANSLATE_BUTTONS or not markup or not getattr(markup, "inline_keyboard", None):
        return markup
    rows: List[List[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            try:
                new_text = await translate_text(b.text or "", tgt)
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
        rows.append(new_row)
    return InlineKeyboardMarkup(rows)

def map_topic(chat_id: int, source_thread_id: Optional[int]) -> Optional[int]:
    # Normalizamos: en algunos clientes el tema "General" llega como None o 0
    norm_src = source_thread_id
    if source_thread_id in (None, 0):
        norm_src = 1  # asumimos que el General es 1 si existe en el mapping

    m1 = TOPIC_MAPPING.get(str(chat_id)) or {}
    if not norm_src:
        return None
    dst = m1.get(str(norm_src)) or m1.get(str(source_thread_id or ""))  # doble intento
    return int(dst) if dst else None


async def alert_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    if ERROR_ALERT and ADMIN_ID:
        try:
            await context.bot.send_message(chat_id=int(ADMIN_ID), text=f"⚠️ {text[:3900]}")
        except Exception:
            pass

# ================== REPLICACIÓN ===================
async def replicate_message(
    context: ContextTypes.DEFAULT_TYPE,
    src_msg: Message,
    dest_chat_id: int,
    dest_thread_id: Optional[int]
):
    text_plain  = src_msg.text or ""
    caption_pln = src_msg.caption or ""
    markup = src_msg.reply_markup

    if text_plain:
        out_text = await translate_text(text_plain, TARGET_LANG)
        out_kb   = await translate_keyboard(markup, TARGET_LANG)
        await context.bot.send_message(
            chat_id=dest_chat_id,
            message_thread_id=dest_thread_id,
            text=out_text,
            reply_markup=out_kb,
            disable_web_page_preview=True
        )
    elif src_msg.photo or src_msg.video or src_msg.document or src_msg.animation or src_msg.audio or src_msg.voice:
        out_caption = await translate_text(caption_pln, TARGET_LANG) if caption_pln else None
        out_kb      = await translate_keyboard(markup, TARGET_LANG)
        # copiar media con caption traducida
        await context.bot.copy_message(
            chat_id=dest_chat_id,
            from_chat_id=src_msg.chat.id,
            message_id=src_msg.message_id,
            message_thread_id=dest_thread_id,
            caption=out_caption,
            reply_markup=out_kb
        )
    else:
        # otros tipos (stickers, etc.) → copiar sin traducir
        await context.bot.copy_message(
            chat_id=dest_chat_id,
            from_chat_id=src_msg.chat.id,
            message_id=src_msg.message_id,
            message_thread_id=dest_thread_id
        )

# --------- CANALES: SOURCE_CHANNEL → DEST_CHANNEL
def channel_matches(update: Update) -> bool:
    if not update.channel_post: return False
    chat = update.channel_post.chat
    if SOURCE_CHANNEL.startswith("@"):
        uname = (chat.username or "").lower()
        return ("@" + uname) == SOURCE_CHANNEL.lower()
    else:
        return str(chat.id) == SOURCE_CHANNEL

async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.channel_post or not channel_matches(update):
            return
        msg = update.channel_post
        await replicate_message(context, msg, int(DEST_CHANNEL), None)
    except Exception as e:
        log.exception("Error on_channel_post")
        await alert_admin(context, f"Error canal→canal: {e}\n{traceback.format_exc()[:1500]}")

# --------- GRUPOS/TEMAS: según TOPIC_MAPPING
async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:  # sólo mensajes normales de grupo/tema
            return
        msg: Message = update.message
        chat: Chat   = msg.chat
        if chat.type not in ("supergroup", "group"):
            return

        # Necesitamos un destino para este grupo/tema
        dest_thread = map_topic(chat.id, msg.message_thread_id)
        if dest_thread is None:
            return  # no mapeado → se ignora

        # Destino SIEMPRE es el MISMO grupo (réplica tema→tema)
        await replicate_message(context, msg, chat.id, dest_thread)

    except Exception as e:
        log.exception("Error on_group_message")
        await alert_admin(context, f"Error grupo/tema: {e}\n{traceback.format_exc()[:1500]}")

# ================== MAIN ==========================
def main():
    need_vars(("BOT_TOKEN", BOT_TOKEN))
    app = Application.builder().token(BOT_TOKEN).build()

    # canales
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))

    # grupos/temas
    app.add_handler(MessageHandler(filters.ChatType.SUPERGROUP | filters.ChatType.GROUP, on_group_message))

    app.run_polling(
        allowed_updates=["message","channel_post"],
        poll_interval=1.3,
        stop_signals=None
    )

if __name__ == "__main__":
    main()
