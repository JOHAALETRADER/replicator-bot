# main.py
import os
import json
import logging
import html
import re
from typing import Optional, List, Tuple, Dict

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, Message, MessageEntity
)
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import aiohttp

# ========================= Config desde variables =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Canal → canal (opcional, deja vacío si no quieres esta réplica)
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "").strip()   # @usuario_canal o id numérico
DEST_CHANNEL   = os.getenv("DEST_CHANNEL", "").strip()

# Grupos con temas (opcional). JSON:
# {
#   "-1001946870620": {               # chat_id del grupo origen (numérico, con signo)
#     "1": {"dest_chat_id": -1001946870620, "dest_thread_id": 20605},   # CHAT → CHAT ROOM
#     "129": {"dest_chat_id": -1001946870620, "dest_thread_id": 20607}, # etc...
#     "*": {"dest_chat_id": -100xxxxx, "dest_thread_id": 123}           # comodín (opcional)
#   },
#   "-1002131156976": { ... }
# }
TOPIC_MAPPING = {}
_raw_map = os.getenv("TOPIC_MAPPING", "").strip()
if _raw_map:
    try:
        TOPIC_MAPPING = json.loads(_raw_map)
    except Exception:
        TOPIC_MAPPING = {}

# Traducción
TRANSLATE  = os.getenv("TRANSLATE", "true").lower() == "true"
TRANSLATOR = os.getenv("TRANSLATOR", "deepl").lower()  # "deepl" | "libre" | "none"
DEEPL_API_KEY  = os.getenv("DEEPL_API_KEY", "").strip()
DEEPL_API_HOST = os.getenv("DEEPL_API_HOST", "api-free.deepl.com").strip()
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate").strip()

TARGET_LANG     = os.getenv("TARGET_LANG", "EN").upper()
SOURCE_LANG     = os.getenv("SOURCE_LANG", "ES").upper()
FORMALITY       = os.getenv("FORMALITY", "default")
FORCE_TRANSLATE = os.getenv("FORCE_TRANSLATE", "false").lower() == "true"

# Traducción de botones (sólo el texto visible, mantiene URLs/callbacks)
TRANSLATE_BUTTONS = os.getenv("TRANSLATE_BUTTONS", "true").lower() == "true"

# Glosario DeepL (opcional)
GLOSSARY_ID  = os.getenv("GLOSSARY_ID", "").strip()
GLOSSARY_TSV = os.getenv("GLOSSARY_TSV", "").strip()

# Alertas
ADMIN_ID   = os.getenv("ADMIN_ID", "").strip()  # tu ID numérico de Telegram
ERROR_ALERT = os.getenv("ERROR_ALERT", "true").lower() == "true"

# Para formality en DeepL
FORMALITY_LANGS = {"DE","FR","IT","ES","NL","PL","PT-PT","PT-BR","RU","JA"}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
logger = logging.getLogger("replicator")


# ========================= Utilidades =========================
def _ensure_env():
    if not BOT_TOKEN:
        raise RuntimeError("Falta BOT_TOKEN")

def _id_like(x: str) -> int:
    """
    Convierte @usuario o cadena numérica en int para chat_id cuando aplica.
    Si empieza con @, devolvemos None porque Telegram espera string para username.
    """
    x = (x or "").strip()
    if not x:
        return None
    if x.startswith("@"):
        return None
    try:
        return int(x)
    except Exception:
        return None

def _same_channel(update: Update) -> bool:
    """
    ¿El post viene del canal configurado en SOURCE_CHANNEL?
    Acepta username @ o id numérico.
    """
    if not update.channel_post:
        return False
    chat = update.channel_post.chat
    if SOURCE_CHANNEL.startswith("@"):
        uname = (chat.username or "").lower()
        return ("@" + uname) == SOURCE_CHANNEL.lower()
    else:
        return str(chat.id) == SOURCE_CHANNEL

# Heurística simple para detectar si ya está en inglés
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
                    logger.warning("DeepL glossary response without ID: %s", txt)
                return GLOSSARY_ID or None
    except Exception as e:
        logger.warning("DeepL glossary create failed: %s", e)
        return None

async def translate_text_plain(text: Optional[str], target_lang: str = None) -> str:
    """ Traduce texto PLANO (sin HTML). """
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
            data = {
                "auth_key": DEEPL_API_KEY,
                "text": text,
                "target_lang": tgt,
                "source_lang": src,
            }
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

def _entity_order_key(ent: MessageEntity) -> int:
    return ent.offset

async def translate_with_entities_to_html(
    text: str,
    entities: Optional[List[MessageEntity]]
) -> Tuple[str, str]:
    """
    Traduce preservando formato mediante HTML.
    - Traduce el contenido visible.
    - Para text_link, traduce el título y mantiene la URL.
    - URLs/hashtags/mentions se dejan tal cual.
    Devuelve (html_text, debug_note)
    """
    if not text:
        return "", ""

    ents = entities or []
    # Sólo usaremos entidades que NO se solapen (caso común en Telegram)
    ents = sorted(ents, key=_entity_order_key)

    pieces: List[str] = []
    cursor = 0
    debug = []

    def wrap(kind: str, inner: str, ent: MessageEntity) -> str:
        if kind == "bold":
            return f"<b>{inner}</b>"
        if kind == "italic":
            return f"<i>{inner}</i>"
        if kind == "underline":
            return f"<u>{inner}</u>"
        if kind == "strikethrough":
            return f"<s>{inner}</s>"
        if kind == "code":
            return f"<code>{inner}</code>"
        if kind == "pre":
            return f"<pre>{inner}</pre>"
        if kind == "text_link":
            url = ent.url or ""
            return f"<a href=\"{html.escape(url)}\">{inner}</a>"
        # por defecto, sin wrapper
        return inner

    try:
        for ent in ents:
            start = ent.offset
            end = start + ent.length
            if start < cursor:
                # superposición: abortamos y devolvemos traducción plana sin HTML
                raise RuntimeError("Entities overlapped")

            # Texto normal antes de la entidad
            if start > cursor:
                chunk = text[cursor:start]
                trans = await translate_text_plain(chunk)
                pieces.append(html.escape(trans))

            seg = text[start:end]
            kind = ent.type

            # Entidades que NO debemos traducir su interior
            if kind in ("url", "mention", "hashtag", "cashtag", "bot_command"):
                pieces.append(html.escape(seg))
            else:
                seg_trans = await translate_text_plain(seg)
                wrapped = wrap(kind, html.escape(seg_trans), ent)
                pieces.append(wrapped)

            cursor = end

        # Resto del texto
        if cursor < len(text):
            chunk = text[cursor:]
            trans = await translate_text_plain(chunk)
            pieces.append(html.escape(trans))

        return "".join(pieces), "; ".join(debug)
    except Exception:
        # Fallback simple: traducir todo y escapar
        trans_all = await translate_text_plain(text)
        return html.escape(trans_all), "fallback_plain"

async def translate_inline_keyboard(
    markup: Optional[InlineKeyboardMarkup],
    target_lang: str
) -> Optional[InlineKeyboardMarkup]:
    if not TRANSLATE_BUTTONS or not markup or not getattr(markup, "inline_keyboard", None):
        return markup
    new_rows: List[List[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        new_row: List[InlineKeyboardButton] = []
        for b in row:
            try:
                new_text = await translate_text_plain(b.text or "", target_lang)
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

async def alert_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    if not (ERROR_ALERT and ADMIN_ID):
        return
    try:
        await context.bot.send_message(chat_id=int(ADMIN_ID), text=f"⚠️ {text[:3900]}")
    except Exception:
        pass


# ========================= Lógica de mapeo de temas =========================
def map_topic(chat_id: int, source_thread_id: Optional[int]) -> Optional[Tuple[int, Optional[int]]]:
    """
    Dado un chat y un thread_id origen, devuelve (dest_chat_id, dest_thread_id) o None.
    El tema “General” a veces llega como None o 0 → lo normalizamos a 1.
    """
    m = TOPIC_MAPPING.get(str(chat_id)) or {}
    if source_thread_id in (None, 0):
        norm = 1
    else:
        norm = source_thread_id

    # Coincidencia exacta
    item = m.get(str(norm))
    if item and "dest_chat_id" in item:
        return (int(item["dest_chat_id"]), int(item.get("dest_thread_id")) if item.get("dest_thread_id") else None)

    # comodín
    item = m.get("*")
    if item and "dest_chat_id" in item:
        return (int(item["dest_chat_id"]), int(item.get("dest_thread_id")) if item.get("dest_thread_id") else None)

    return None


# ========================= Replicación =========================
async def replicate_message(
    context: ContextTypes.DEFAULT_TYPE,
    src_msg: Message,
    dest_chat_id: int,
    dest_thread_id: Optional[int]
):
    text_plain = src_msg.text or ""
    caption_plain = src_msg.caption or ""
    markup = src_msg.reply_markup

    # Caso 1: Mensaje de TEXTO
    if text_plain:
        html_text, _ = await translate_with_entities_to_html(text_plain, src_msg.entities)
        translated_markup = await translate_inline_keyboard(markup, TARGET_LANG)
        await context.bot.send_message(
            chat_id=dest_chat_id,
            message_thread_id=dest_thread_id,
            text=html_text,
            parse_mode="HTML",
            disable_web_page_preview=False,
            reply_markup=translated_markup
        )
        return

    # Caso 2: Media con caption
    if caption_plain:
        html_caption, _ = await translate_with_entities_to_html(caption_plain, src_msg.caption_entities)
        translated_markup = await translate_inline_keyboard(markup, TARGET_LANG)
        # copiamos el media original pero con caption traducido y los mismos botones (traducidos)
        await context.bot.copy_message(
            chat_id=dest_chat_id,
            message_thread_id=dest_thread_id,
            from_chat_id=src_msg.chat.id,
            message_id=src_msg.message_id,
            caption=html_caption,
            parse_mode="HTML",
            reply_markup=translated_markup
        )
        return

    # Caso 3: otros (stickers, etc.) → copia 1:1
    await context.bot.copy_message(
        chat_id=dest_chat_id,
        message_thread_id=dest_thread_id,
        from_chat_id=src_msg.chat.id,
        message_id=src_msg.message_id,
    )


# ========================= Handlers =========================
async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.channel_post:
            return
        if not SOURCE_CHANNEL or not DEST_CHANNEL:
            return
        if not _same_channel(update):
            return

        src = update.channel_post
        # Destino puede ser @usuario o id numérico
        dest_id_num = _id_like(DEST_CHANNEL)
        if dest_id_num is not None:
            await replicate_message(context, src, dest_id_num, None)
        else:
            # username
            await replicate_message(context, src, DEST_CHANNEL, None)

    except Exception as e:
        logger.exception("Error on_channel_post")
        await alert_admin(context, f"Error on_channel_post: {e}")

async def on_group_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.effective_message
        if not msg or not msg.is_topic_message:
            return  # sólo manejamos mensajes en temas

        src_chat_id = msg.chat.id
        src_thread_id = msg.message_thread_id

        mapped = map_topic(src_chat_id, src_thread_id)
        if not mapped:
            return
        dest_chat_id, dest_thread_id = mapped

        # 🔓 “CHAT” libre: NO filtramos por ADMIN_ID aquí en absoluto
        await replicate_message(context, msg, dest_chat_id, dest_thread_id)

    except Exception as e:
        logger.exception("Error on_group_post")
        await alert_admin(context, f"Error on_group_post: {e}")


def main():
    _ensure_env()
    app = Application.builder().token(BOT_TOKEN).build()

    # Canal → canal
    if SOURCE_CHANNEL and DEST_CHANNEL:
        app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))

    # Grupos con temas (supergrupos)
    app.add_handler(MessageHandler(filters.ChatType.SUPERGROUP, on_group_post))

    app.run_polling(allowed_updates=["channel_post", "message"], poll_interval=1.2, stop_signals=None)


if __name__ == "__main__":
    main()
