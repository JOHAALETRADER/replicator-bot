import os
import json
import logging
import re
from typing import Optional, List, Tuple, Dict

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    MessageEntity,
)
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)
import aiohttp

# ================== CONFIG / ENV ==================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Canal → Canal (opcional). Acepta @usuario o ID (-100...).
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "").strip()
DEST_CHANNEL   = os.getenv("DEST_CHANNEL", "").strip()

# Mapeo de temas (opcional) JSON:
# {
#   "-1001946870620": { "1": 20605, "129": 20607, "2890": 20611, "17373": 20611, "8": 20613, "11": 20613, "9": 20616 },
#   "-1002131156976": { "4": 5576, "272": 5576, "3": 5571, "5337": 5573, "2": 5579 }
# }
TOPIC_MAPPING = os.getenv("TOPIC_MAPPING", "").strip()

TRANSLATE  = os.getenv("TRANSLATE", "false").lower() == "true"
TRANSLATOR = os.getenv("TRANSLATOR", "none").lower()  # "deepl" | "libre" | "none"

DEEPL_API_KEY  = os.getenv("DEEPL_API_KEY", "").strip()
DEEPL_API_HOST = os.getenv("DEEPL_API_HOST", "api-free.deepl.com").strip()  # api-free.deepl.com | api.deepl.com
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate").strip()

TARGET_LANG     = os.getenv("TARGET_LANG", "EN").upper()
SOURCE_LANG     = os.getenv("SOURCE_LANG", "ES").upper()
FORMALITY       = os.getenv("FORMALITY", "default")  # less | default | more
FORCE_TRANSLATE = os.getenv("FORCE_TRANSLATE", "false").lower() == "true"

# Traducir SOLO el texto visible de los botones (conservando URL/callback)
TRANSLATE_BUTTONS = os.getenv("TRANSLATE_BUTTONS", "true").lower() == "true"

# Alertas a admin
ERROR_ALERT = os.getenv("ERROR_ALERT", "true").lower() == "true"
ADMIN_ID    = os.getenv("ADMIN_ID", "").strip()  # tu user id numérico

# Glosario DeepL (opcional)
GLOSSARY_ID  = os.getenv("GLOSSARY_ID", "").strip()
GLOSSARY_TSV = os.getenv("GLOSSARY_TSV", "").strip()

# DeepL solo soporta "formality" en:
FORMALITY_LANGS = {"DE","FR","IT","ES","NL","PL","PT-PT","PT-BR","RU","JA"}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("replicator")

# ================== HELPERS ==================

def _ensure_env():
    if not BOT_TOKEN:
        raise RuntimeError("Falta BOT_TOKEN")

def _parse_topic_mapping(raw: str) -> Dict[str, Dict[str, int]]:
    if not raw:
        return {}
    try:
        m = json.loads(raw)
        # normalizamos a str keys
        out = {}
        for chat_id, mapping in m.items():
            out[str(chat_id)] = {str(k): int(v) for k, v in mapping.items()}
        return out
    except Exception as e:
        logger.warning("TOPIC_MAPPING inválido: %s", e)
        return {}

TOPIC_MAP = _parse_topic_mapping(TOPIC_MAPPING)

def _same_channel(update: Update) -> bool:
    """¿El update viene del channel elegido para canal→canal?"""
    if not SOURCE_CHANNEL:
        return False
    if not update.channel_post:
        return False
    chat = update.channel_post.chat
    if SOURCE_CHANNEL.startswith("@"):
        uname = (chat.username or "").lower()
        return ("@" + uname) == SOURCE_CHANNEL.lower()
    else:
        return str(chat.id) == SOURCE_CHANNEL

def _chat_is_mapped(chat_id: int) -> bool:
    return str(chat_id) in TOPIC_MAP

def _map_topic(chat_id: int, source_thread_id: Optional[int]) -> Optional[int]:
    """Devuelve el topic destino para (chat, topic_or_general). None si no hay mapeo."""
    m = TOPIC_MAP.get(str(chat_id))
    if not m:
        return None
    # En algunos clientes, ‘General’ llega como None o 0 → tratamos ambos como "1" si existe
    norm = source_thread_id if source_thread_id not in (None, 0) else 1
    dst = m.get(str(norm)) or m.get(str(source_thread_id or ""))
    return int(dst) if dst is not None else None

def _dest_chat_for_channel() -> str:
    """Devuelve chat_id destino para canal→canal; acepta @usuario o ID."""
    return DEST_CHANNEL or ""

async def alert_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    if ERROR_ALERT and ADMIN_ID:
        try:
            await context.bot.send_message(chat_id=int(ADMIN_ID), text=f"⚠️ {text[:3900]}")
        except Exception:
            pass

# --- Heurística para evitar retraducción si ya está en inglés ---
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

def _split_chunks(text: str, limit: int = 4096) -> List[str]:
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    while text:
        parts.append(text[:limit])
        text = text[limit:]
    return parts

# ============ MarkdownV2 safe building (para “enlaces bonitos”) ============

_MD_V2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"

def _esc(s: str) -> str:
    return re.sub(r"([%s])" % re.escape(_MD_V2_SPECIALS), r"\\\1", s)

def _extract_text_links(text: str, entities: Optional[List[MessageEntity]]) -> List[Tuple[int,int,str]]:
    """
    Devuelve lista de (offset, length, url) para entities tipo text_link.
    """
    out: List[Tuple[int,int,str]] = []
    if not entities:
        return out
    for e in entities:
        try:
            if e.type == "text_link" and e.url is not None:
                out.append((e.offset, e.length, e.url))
        except Exception:
            continue
    # Telegram envía offsets en utf-16 code units, pero Python maneja codepoints.
    # En la práctica con PTB v20 suele venir correcto; si alguna vez falla,
    # evitamos crashear.
    return out

def _apply_link_tokens(text: str, links: List[Tuple[int,int,str]]) -> Tuple[str, List[Tuple[str, str, str]]]:
    """
    Reemplaza cada rango de text_link por un token __L{i}__.
    Devuelve (texto_con_tokens, [(token, visible_text, url), ...]).
    Asumimos que los offsets no se solapan y vienen ordenados (Telegram lo hace).
    """
    if not links:
        return text, []
    pieces = []
    last = 0
    mapping = []
    for i, (off, ln, url) in enumerate(links):
        token = f"__L{i}__"
        visible = text[off:off+ln]
        pieces.append(text[last:off])
        pieces.append(token)
        mapping.append((token, visible, url))
        last = off+ln
    pieces.append(text[last:])
    return "".join(pieces), mapping

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
    form.add_field("entries", GLOSSARY_TSV, filename="glossary.tsv", content_type="text/tab-separated-values")
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
        elif TRANSLATOR == "libre" and LIBRETRANSLATE_URL:
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
                    login_url=getattr(b, "login_url", None),
                    pay=getattr(b, "pay", None),
                )
            )
        new_rows.append(new_row)
    return InlineKeyboardMarkup(new_rows)

async def build_markdown_with_links(
    text: str,
    entities: Optional[List[MessageEntity]],
) -> str:
    """
    Reconstruye el texto en MarkdownV2 preservando “enlaces bonitos” (text_link),
    traduciendo SOLO el título visible y manteniendo la URL.
    """
    links = _extract_text_links(text, entities)
    if not TRANSLATE:
        # Solo rearmamos con tokens para convertir text_link a [texto](url)
        if not links:
            return _esc(text)
        tokened, mapping = _apply_link_tokens(text, links)
        out = _esc(tokened)
        for token, label, url in mapping:
            label_md = _esc(label)
            repl = f"[{label_md}]({url})"
            out = out.replace(_esc(token), repl)
        return out

    if not links:
        # No hay text_link. Solo traducir y escapar.
        t = await translate_text(text, TARGET_LANG)
        return _esc(t)

    # 1) Metemos tokens
    tokened, mapping = _apply_link_tokens(text, links)
    # 2) Traducción del texto con tokens
    translated_main = await translate_text(tokened, TARGET_LANG)
    # 3) Traducción individual de cada label (mejor semántica)
    translated_labels = []
    for _, label, _ in mapping:
        translated_labels.append(await translate_text(label, TARGET_LANG))
    # 4) Escapamos y reemplazamos tokens por [label](url)
    out = _esc(translated_main)
    for (token, _, url), lbl_tr in zip(mapping, translated_labels):
        repl = f"[{_esc(lbl_tr)}]({url})"
        out = out.replace(_esc(token), repl)
    return out

# ================== REPLICACIÓN ==================

async def replicate_text_message(
    context: ContextTypes.DEFAULT_TYPE,
    src_msg,
    dest_chat_id,
    dest_thread_id: Optional[int],
):
    """
    Textos puros (sin media): usamos send_message en MarkdownV2 para no perder “enlaces bonitos”.
    """
    md = await build_markdown_with_links(src_msg.text or "", src_msg.entities)
    markup = await translate_inline_keyboard(src_msg.reply_markup, TARGET_LANG)
    for chunk in _split_chunks(md, 4000):  # margen para Markdown
        await context.bot.send_message(
            chat_id=dest_chat_id,
            text=chunk,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
            message_thread_id=dest_thread_id,
            reply_markup=markup,
        )
        # Solo ponemos botones en el primer chunk
        markup = None

async def replicate_media_message(
    context: ContextTypes.DEFAULT_TYPE,
    src_msg,
    dest_chat_id,
    dest_thread_id: Optional[int],
):
    """
    Para media copiamos el mensaje y, si hay caption, lo reemplazamos traducido en MarkdownV2.
    """
    caption = src_msg.caption or ""
    caption_md = None
    if caption:
        caption_md = await build_markdown_with_links(caption, src_msg.caption_entities)
    markup = await translate_inline_keyboard(src_msg.reply_markup, TARGET_LANG)

    await context.bot.copy_message(
        chat_id=dest_chat_id,
        from_chat_id=src_msg.chat.id,
        message_id=src_msg.message_id,
        caption=caption_md,
        parse_mode="MarkdownV2" if caption_md else None,
        reply_markup=markup,
        message_thread_id=dest_thread_id,
    )

async def replicate_any(
    context: ContextTypes.DEFAULT_TYPE,
    src_msg,
    dest_chat_id,
    dest_thread_id: Optional[int],
):
    """
    Decide según tipo. Si no es texto ni tiene caption, copiamos tal cual.
    """
    try:
        if src_msg.text:
            await replicate_text_message(context, src_msg, dest_chat_id, dest_thread_id)
        elif src_msg.caption:
            await replicate_media_message(context, src_msg, dest_chat_id, dest_thread_id)
        else:
            # Todo lo demás: copiar 1:1 (polls, stickers, etc.)
            await context.bot.copy_message(
                chat_id=dest_chat_id,
                from_chat_id=src_msg.chat.id,
                message_id=src_msg.message_id,
                message_thread_id=dest_thread_id,
            )
    except Exception as e:
        logger.exception("Error replicando mensaje: %s", e)
        await alert_admin(context, f"Error replicando: {e}")

# ================== HANDLERS ==================

async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Canal → Canal (si SOURCE_CHANNEL y DEST_CHANNEL están definidos).
    """
    try:
        if not _same_channel(update):
            return
        msg = update.channel_post
        dest_chat = _dest_chat_for_channel()
        if not dest_chat:
            return
        await replicate_any(context, msg, dest_chat, None)
    except Exception as e:
        logger.exception("Error on_channel_post: %s", e)
        await alert_admin(context, f"Error canal→canal: {e}")

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Replica entre temas (topic) según TOPIC_MAPPING.
    Si el chat no está mapeado, no hace nada.
    """
    try:
        msg = update.effective_message
        chat = msg.chat
        if not chat or chat.type != "supergroup":
            return
        if not _chat_is_mapped(chat.id):
            return

        src_thread = msg.message_thread_id  # puede ser None (General)
        dest_thread = _map_topic(chat.id, src_thread)
        if dest_thread is None:
            return  # tema no mapeado

        # Misma réplica (dentro del mismo supergrupo) cambiando de topic
        await replicate_any(context, msg, chat.id, dest_thread)

    except Exception as e:
        logger.exception("Error on_group_message: %s", e)
        await alert_admin(context, f"Error en temas: {e}")

# ================== APP ==================

def main():
    _ensure_env()
    app = Application.builder().token(BOT_TOKEN).build()

    # Canal → canal
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))

    # Temas (supergrupos)
    app.add_handler(MessageHandler(filters.ChatType.SUPERGROUP, on_group_message))

    app.run_polling(
        allowed_updates=["message", "channel_post"],
        poll_interval=1.5,
        stop_signals=None
    )

if __name__ == "__main__":
    main()
