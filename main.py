import os
import re
import html
import json
import logging
from typing import Optional, List, Tuple, Dict

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)
import aiohttp

# ========= Config por ENV =========
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Canal -> Canal (tu flujo existente)
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")  # ej: -100123..., o @mi_canal
DEST_CHANNEL   = os.getenv("DEST_CHANNEL")    # ej: -100456..., o @destino

# Traducción
TRANSLATE  = os.getenv("TRANSLATE", "true").lower() == "true"
TRANSLATOR = os.getenv("TRANSLATOR", "deepl").lower()  # deepl | libre | none
DEEPL_API_KEY  = os.getenv("DEEPL_API_KEY", "").strip()
DEEPL_API_HOST = os.getenv("DEEPL_API_HOST", "api-free.deepl.com").strip()
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate").strip()

SOURCE_LANG = os.getenv("SOURCE_LANG", "ES").upper()
TARGET_LANG = os.getenv("TARGET_LANG", "EN").upper()
FORMALITY   = os.getenv("FORMALITY", "default")  # less | default | more
FORCE_TRANSLATE = os.getenv("FORCE_TRANSLATE", "false").lower() == "true"
TRANSLATE_BUTTONS = os.getenv("TRANSLATE_BUTTONS", "true").lower() == "true"

# Glosario DeepL (opcional)
GLOSSARY_ID  = os.getenv("GLOSSARY_ID", "").strip()
GLOSSARY_TSV = os.getenv("GLOSSARY_TSV", "").strip()

# Admin para errores
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0") or "0")

# ========= Constantes =========
FORMALITY_LANGS = {"DE","FR","IT","ES","NL","PL","PT-PT","PT-BR","RU","JA"}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("replicator")

# ========= Mapeo de Temas (según tu listado) =========
# Nota: t.me/c/<abs_id>/<topic>/<msg>. El chat_id real = -100<abs_id>
def _abs_to_chat(abs_id: int) -> int:
    return -100 * 10**(len(str(abs_id))) - abs_id  # no sirve, mejor formemos directo
# Atajo: ya te los dejo como enteros listos.

ROUTES: List[Dict] = [
    # ------- GRUPO 1 -------
    # chat_id = -1001946870620
    # CHAT (1) -> CHAT ROOM (20605), solo si es tu user
    {"chat_id": -1001946870620, "src_topic": 1, "dst_topic": 20605, "only_from_user": 5958154558},

    # RESULTADOS JT TRADERS (129) -> JT Wins (20607)
    {"chat_id": -1001946870620, "src_topic": 129, "dst_topic": 20607},
    # RESULTADOS ALUMNOS (2890) -> VIP Results & Payouts (20611)
    {"chat_id": -1001946870620, "src_topic": 2890, "dst_topic": 20611},
    # RETIROS VIP (17373) -> VIP Results & Payouts (20611)
    {"chat_id": -1001946870620, "src_topic": 17373, "dst_topic": 20611},

    # ESTRATEGIAS ARCHIVOS (8) -> Trading Plan & Risk (20613)
    {"chat_id": -1001946870620, "src_topic": 8, "dst_topic": 20613},
    # PLAN Y GESTIÓN DE RIESGO (11) -> Trading Plan & Risk (20613)
    {"chat_id": -1001946870620, "src_topic": 11, "dst_topic": 20613},

    # NOTICIAS Y SORTEOS (9) -> Updates & Prizes (20616)
    {"chat_id": -1001946870620, "src_topic": 9, "dst_topic": 20616},

    # ------- GRUPO 2 -------
    # chat_id = -1002127373425
    # RESULTADOS JT TRADERS TEAMS (3) -> JT Wins (4096)
    {"chat_id": -1002127373425, "src_topic": 3, "dst_topic": 4096},
    # RESULTADOS ALUMNOS VIP (2) -> VIP Results & Risk (4098)
    {"chat_id": -1002127373425, "src_topic": 2, "dst_topic": 4098},

    # ------- GRUPO 3 -------
    # chat_id = -1002131156976
    # BINARY SIGNALS (3) -> Binary Trade Signals (5571)
    {"chat_id": -1002131156976, "src_topic": 3, "dst_topic": 5571},
    # Binance Master Signals (5337) -> Binance Pro Signals (5573)
    {"chat_id": -1002131156976, "src_topic": 5337, "dst_topic": 5573},
    # Forex Bias (4) -> Market Insights & Analysis (5576)
    {"chat_id": -1002131156976, "src_topic": 4, "dst_topic": 5576},
    # Noticias y Análisis (272) -> Market Insights & Analysis (5576)
    {"chat_id": -1002131156976, "src_topic": 272, "dst_topic": 5576},
    # INDICES SYNTHETICOS (2) -> Synthetic Index Signals (5579)
    {"chat_id": -1002131156976, "src_topic": 2, "dst_topic": 5579},
]

# Index rápido
TOPIC_MAP: Dict[Tuple[int,int], Dict] = {
    (r["chat_id"], r["src_topic"]): r for r in ROUTES
}

# ========= Utilidades =========
def _ensure_env():
    missing = []
    if not BOT_TOKEN:      missing.append("BOT_TOKEN")
    if not SOURCE_CHANNEL: missing.append("SOURCE_CHANNEL")
    if not DEST_CHANNEL:   missing.append("DEST_CHANNEL")
    if missing:
        raise RuntimeError("Faltan variables: " + ", ".join(missing))

def _id_from_channel(chat) -> str:
    """Devuelve '@user' o el ID numérico como string, para comparar con envs."""
    if isinstance(chat.id, int):
        return str(chat.id)
    return str(chat.username or "")

def _chat_matches_source(update: Update) -> bool:
    chat = update.channel_post.chat
    if str(SOURCE_CHANNEL).startswith("@"):
        uname = (chat.username or "").lower()
        return ("@" + uname) == SOURCE_CHANNEL.lower()
    else:
        return str(chat.id) == str(SOURCE_CHANNEL)

# Heurística: evitar retraducir si ya es inglés
_EN_COMMON = re.compile(r"\b(the|and|for|with|from|to|of|in|on|is|are|you|we|they|buy|sell|trade|signal|profit|setup)\b", re.I)
_ES_MARKERS = re.compile(r"[áéíóúñ¿¡]|\b(que|para|porque|hola|gracias|con|sin|desde|hoy|mañana|ayer|compra|venta|señal|apalancamiento|beneficios)\b", re.I)

def _probably_english(text: str) -> bool:
    if _ES_MARKERS.search(text): return False
    if _EN_COMMON.search(text):  return True
    letters = [c for c in text if c.isalpha()]
    if not letters: return False
    ascii_letters = [c for c in letters if ord(c) < 128]
    return (len(ascii_letters) / max(1, len(letters))) > 0.85

def _split_chunks(text: str, limit: int = 4096) -> List[str]:
    if len(text) <= limit: return [text]
    parts: List[str] = []
    while text:
        parts.append(text[:limit])
        text = text[limit:]
    return parts

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

# ============ Enlaces bonitos (TEXT_LINK) ============
def _mask_text_links(original_text: str, entities) -> Tuple[str, List[Tuple[str, str]]]:
    """
    Reemplaza cada TEXT_LINK por un marcador __LkN__ y devuelve:
    - texto con marcadores
    - lista [(placeholder, url), ...]
    """
    if not entities:
        return original_text, []

    # Construimos sobre índices; ordenar por offset ascendente
    items = []
    for ent in entities:
        if getattr(ent, "type", None) == "text_link":
            items.append((ent.offset, ent.length, ent.url))
    if not items:
        return original_text, []

    items.sort(key=lambda x: x[0])
    result = []
    pos = 0
    placeholders = []
    idx = 0
    for off, length, url in items:
        if off > pos:
            result.append(original_text[pos:off])
        placeholder = f"__Lk{idx}__"
        result.append(placeholder)
        placeholders.append((placeholder, original_text[off:off+length], url))
        pos = off + length
        idx += 1
    result.append(original_text[pos:])
    return "".join(result), [(ph, txt, url) for (ph, txt, url) in placeholders]

async def _rebuild_html_with_links(masked_text: str, placeholders: List[Tuple[str, str, str]]) -> str:
    """
    Toma el texto MASKED ya traducido; escapa HTML y sustituye marcadores por <a href="...">texto traducido</a>.
    """
    escaped = html.escape(masked_text)
    # traducimos cada título del link por separado
    for i, (ph, link_text, url) in enumerate(placeholders):
        translated_title = await translate_text(link_text, TARGET_LANG) if TRANSLATE else link_text
        anchor = f'<a href="{html.escape(url, quote=True)}">{html.escape(translated_title)}</a>'
        escaped = escaped.replace(html.escape(ph), anchor)
    return escaped

# ============ Botones: traducir SOLO el texto ============
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

# ============ Canal -> Canal ============
async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
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
            masked, phs = _mask_text_links(text_plain, msg.entities)
            translated_masked = await translate_text(masked, TARGET_LANG)
            html_out = await _rebuild_html_with_links(translated_masked, phs)
            translated_markup = await translate_inline_keyboard(msg.reply_markup, TARGET_LANG)
            for i, chunk in enumerate(_split_chunks(html_out, 4096)):
                await context.bot.send_message(
                    chat_id=DEST_CHANNEL,
                    text=chunk,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=translated_markup if i == 0 else None
                )
        else:
            # media con caption
            masked, phs = _mask_text_links(caption_plain, msg.caption_entities or [])
            translated_masked = await translate_text(masked, TARGET_LANG)
            html_out = await _rebuild_html_with_links(translated_masked, phs) if translated_masked else None
            translated_markup = await translate_inline_keyboard(msg.reply_markup, TARGET_LANG)

            await context.bot.copy_message(
                chat_id=DEST_CHANNEL,
                from_chat_id=msg.chat.id,
                message_id=msg.message_id,
                caption=html_out if html_out else None,
                parse_mode="HTML" if html_out else None,
                reply_markup=translated_markup
            )

    except Exception as e:
        logger.exception("Error en on_channel_post")
        if ADMIN_USER_ID:
            try:
                await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"❗️Error canal→canal: {e}")
            except Exception:
                pass

# ============ Grupos/Temas ============
def _route_for_message(msg) -> Optional[Dict]:
    if not msg.is_topic_message:
        return None
    key = (msg.chat.id, msg.message_thread_id)
    return TOPIC_MAP.get(key)

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        msg = update.message
        if not msg or not msg.is_topic_message:
            return

        route = _route_for_message(msg)
        if not route:
            return

        # filtro por usuario (CHAT 1 -> CHAT ROOM solo tú)
        only_uid = route.get("only_from_user")
        if only_uid and (not msg.from_user or msg.from_user.id != only_uid):
            return

        dst_chat = msg.chat.id
        dst_thread = route["dst_topic"]

        if not TRANSLATE:
            await context.bot.copy_message(
                chat_id=dst_chat,
                from_chat_id=msg.chat.id,
                message_id=msg.message_id,
                message_thread_id=dst_thread,
            )
            return

        text_plain = msg.text or ""
        caption_plain = msg.caption or ""

        if text_plain.strip():
            masked, phs = _mask_text_links(text_plain, msg.entities)
            translated_masked = await translate_text(masked, TARGET_LANG)
            html_out = await _rebuild_html_with_links(translated_masked, phs)
            translated_markup = await translate_inline_keyboard(msg.reply_markup, TARGET_LANG)

            for i, chunk in enumerate(_split_chunks(html_out, 4096)):
                await context.bot.send_message(
                    chat_id=dst_chat,
                    text=chunk,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=translated_markup if i == 0 else None,
                    message_thread_id=dst_thread,
                )
        else:
            masked, phs = _mask_text_links(caption_plain, msg.caption_entities or [])
            translated_masked = await translate_text(masked, TARGET_LANG)
            html_out = await _rebuild_html_with_links(translated_masked, phs) if translated_masked else None
            translated_markup = await translate_inline_keyboard(msg.reply_markup, TARGET_LANG)

            await context.bot.copy_message(
                chat_id=dst_chat,
                from_chat_id=msg.chat.id,
                message_id=msg.message_id,
                message_thread_id=dst_thread,
                caption=html_out if html_out else None,
                parse_mode="HTML" if html_out else None,
                reply_markup=translated_markup
            )

    except Exception as e:
        logger.exception("Error en on_group_message")
        if ADMIN_USER_ID:
            try:
                await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"❗️Error grupos/temas: {e}")
            except Exception:
                pass

# ============ Arranque ============
def main():
    _ensure_env()
    app = Application.builder().token(BOT_TOKEN).build()

    # Canal -> canal
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))

    # Grupos/temas (foros)
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & (~filters.StatusUpdate.ALL), on_group_message)
    )

    app.run_polling(
        allowed_updates=["channel_post", "message"],
        poll_interval=1.3,
        stop_signals=None,
    )

if __name__ == "__main__":
    main()


