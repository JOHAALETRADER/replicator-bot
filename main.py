import os, logging, html, re, asyncio, aiohttp
from typing import Optional, List, Dict, Tuple

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, MessageEntity
)
from telegram.ext import (
    Application, ContextTypes, MessageHandler, filters, AIORateLimiter
)

# ============ CONFIG (env con fallback a tus valores) ============

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Canal ES -> EN
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "@JohaaleTrader_es").strip()
DEST_CHANNEL   = os.getenv("DEST_CHANNEL",   "@JohaaleTrader_en").strip()

# Traducción
TRANSLATE  = os.getenv("TRANSLATE", "true").lower() == "true"
TRANSLATOR = os.getenv("TRANSLATOR", "deepl").lower()  # deepl | libre | none
DEEPL_API_KEY  = os.getenv("DEEPL_API_KEY", "384e6eb2-922a-43ce-8e8f-7cd3ac0047b7").strip()
DEEPL_API_HOST = os.getenv("DEEPL_API_HOST", "api-free.deepl.com").strip()
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate")

TARGET_LANG     = os.getenv("TARGET_LANG", "EN").upper()
SOURCE_LANG     = os.getenv("SOURCE_LANG", "ES").upper()
FORMALITY       = os.getenv("FORMALITY", "default")  # less | default | more
FORCE_TRANSLATE = os.getenv("FORCE_TRANSLATE", "false").lower() == "true"
TRANSLATE_BUTTONS = os.getenv("TRANSLATE_BUTTONS", "true").lower() == "true"
GLOSSARY_ID  = os.getenv("GLOSSARY_ID", "").strip()
GLOSSARY_TSV = os.getenv("GLOSSARY_TSV", "").strip()

# Alertas a Admin
ADMIN_ID   = int(os.getenv("ADMIN_ID", "5958154558"))
ERROR_ALERT = os.getenv("ERROR_ALERT", "true").lower() == "true"

# Mapeo de temas (chat_id -> {src_thread: dst_thread})
# Importante: chat_id de supergrupos privados es -100 + número de t.me/c/<ID>
TOPIC_MAPPING: Dict[str, Dict[str, int]] = {
    # GRUPO 1
    "-1001946870620": {
        "1": 20605,        # CHAT -> Chat Room (libre)
        "129": 20607,      # Resultados JT Traders -> JT Wins
        "2890": 20611,     # Result Alumnos -> VIP Results & Payouts
        "17373": 20611,    # Retiros VIP -> VIP Results & Payouts
        "8": 20613,        # Estrategias Arch -> Trading Plan & Risk
        "11": 20613,       # Plan y Gestión -> Trading Plan & Risk
        "9": 20616,        # Noticias y Sorteos -> Updates & Prizes
    },
    # GRUPO 2
    "-1002127373425": {
        "3": 4096,         # Resultados Teams -> JT Wins
        "2": 4098,         # Resultados Alumnos VIP -> VIP Results & Risk
    },
    # GRUPO 3
    "-1002131156976": {
        "3": 5571,         # Binary Signals -> Binary Trade Signals
        "5337": 5573,      # Binance Master -> Binance Pro
        "4": 5576,         # Forex Bias -> Market Insights & Analysis
        "272": 5576,       # Noticias y Análisis -> Market Insights & Analysis
        "2": 5579,         # Indices Syntheticos -> Synthetic Index Signals
    },
}

# DeepL soporta "formality" en estos idiomas:
FORMALITY_LANGS = {"DE","FR","IT","ES","NL","PL","PT-PT","PT-BR","RU","JA"}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("replicator")


# ================== UTIL ==================

def _is_username(s: str) -> bool:
    return isinstance(s, str) and s.startswith("@")

def _chat_matches_source_channel(update: Update) -> bool:
    if not update.channel_post:
        return False
    chat = update.channel_post.chat
    if _is_username(SOURCE_CHANNEL):
        return ("@" + (chat.username or "")).lower() == SOURCE_CHANNEL.lower()
    return str(chat.id) == SOURCE_CHANNEL

def _dest_id_for_channel() -> str | int:
    # Permite username o ID numérico
    if _is_username(DEST_CHANNEL):
        return DEST_CHANNEL
    try:
        return int(DEST_CHANNEL)
    except Exception:
        return DEST_CHANNEL

def _escape_html(s: str) -> str:
    return html.escape(s, quote=False)

_EN_COMMON = re.compile(r"\b(the|and|for|with|from|to|of|in|on|is|are|you|we|they|buy|sell|trade|signal|profit|setup)\b", re.I)
_ES_MARKERS = re.compile(r"[áéíóúñ¿¡]|\b(que|para|porque|hola|gracias|con|sin|desde|hoy|mañana|ayer|compra|venta|señal)\b", re.I)
def _probably_english(text: str) -> bool:
    if _ES_MARKERS.search(text): return False
    if _EN_COMMON.search(text):  return True
    letters = [c for c in text if c.isalpha()]
    if not letters: return False
    ascii_letters = [c for c in letters if ord(c) < 128]
    return (len(ascii_letters) / max(1, len(letters))) > 0.85

def _norm_thread_id(th: Optional[int]) -> int:
    # Algunos clientes mandan None/0; tomamos 1 como “General/Chat”
    if not th or th == 0:
        return 1
    return th


# ============== DEEPL / LIBRE ==============

async def _deepl_create_glossary_if_needed() -> Optional[str]:
    global GLOSSARY_ID
    if TRANSLATOR != "deepl" or not DEEPL_API_KEY:
        return None
    if GLOSSARY_ID or not GLOSSARY_TSV:
        return GLOSSARY_ID or None

    url = f"https://{DEEPL_API_HOST}/v2/glossaries"
    form = aiohttp.FormData()
    form.add_field("name", "Trading ES-EN (Auto)")
    form.add_field("source_lang", "ES")
    form.add_field("target_lang", "EN")
    form.add_field("entries", GLOSSARY_TSV, filename="glossary.tsv",
                   content_type="text/tab-separated-values")

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.post(url, data=form, headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"}) as r:
                js = await r.json()
                GLOSSARY_ID = js.get("glossary_id", "")
                if GLOSSARY_ID:
                    logger.info("DeepL glossary created: %s", GLOSSARY_ID)
                return GLOSSARY_ID or None
    except Exception as e:
        logger.warning("Glossary create failed: %s", e)
        return None

async def translate_text(text: str) -> str:
    if not text or not TRANSLATE or TRANSLATOR == "none":
        return text
    if (not FORCE_TRANSLATE) and _probably_english(text):
        return text

    if TRANSLATOR == "deepl" and DEEPL_API_KEY:
        await _deepl_create_glossary_if_needed()
        url = f"https://{DEEPL_API_HOST}/v2/translate"
        data = {
            "auth_key": DEEPL_API_KEY,
            "text": text,
            "target_lang": TARGET_LANG,
            "source_lang": SOURCE_LANG,  # para poder usar glosario
        }
        if TARGET_LANG in FORMALITY_LANGS:
            data["formality"] = FORMALITY
        if GLOSSARY_ID:
            data["glossary_id"] = GLOSSARY_ID

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                async with s.post(url, data=data) as r:
                    js = await r.json()
                    return js["translations"][0]["text"]
        except Exception as e:
            logger.warning("DeepL error: %s", e)
            return text

    if TRANSLATOR == "libre":
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                async with s.post(LIBRETRANSLATE_URL, data={
                    "q": text, "source": "auto",
                    "target": TARGET_LANG.lower().split("-")[0],
                    "format": "text"
                }) as r:
                    js = await r.json()
                    return js.get("translatedText", text)
        except Exception as e:
            logger.warning("Libre error: %s", e)
            return text

    return text


# ======== Construcción de HTML preservando “enlaces bonitos” ========

def _extract_text_links(text: str, entities: Optional[List[MessageEntity]]) -> Tuple[str, List[Tuple[str, str]]]:
    """
    Devuelve:
      - texto con marcadores __LINKi__ en lugar de cada text_link
      - lista [(visible_text, url), ...] en el mismo orden
    """
    if not text or not entities:
        return text, []

    links = []
    chunks = []
    last = 0
    # Filtramos sólo text_link; otros estilos los ignoramos adrede
    for e in sorted([e for e in entities if e.type == MessageEntity.TEXT_LINK], key=lambda x: x.offset):
        if e.length <= 0: 
            continue
        # segmento antes del link
        chunks.append(text[last:e.offset])
        # marcador
        idx = len(links)
        chunks.append(f"__LINK{idx}__")
        visible = text[e.offset:e.offset + e.length]
        links.append((visible, e.url))
        last = e.offset + e.length
    chunks.append(text[last:])
    return "".join(chunks), links

async def build_html_with_links(text: str, entities: Optional[List[MessageEntity]]) -> str:
    """Traduce y construye HTML preservando links (text_link) y traduciendo su título."""
    base_with_tokens, links = _extract_text_links(text, entities)

    # Traducción del cuerpo (con tokens) y de cada título de link por separado
    translated_body = await translate_text(base_with_tokens)
    translated_links = []
    for visible, url in links:
        tl = await translate_text(visible)
        translated_links.append((tl, url))

    # escapamos todo (para parse_mode=HTML), luego sustituimos tokens por <a>
    safe = _escape_html(translated_body)
    for i, (ttl, url) in enumerate(translated_links):
        anchor = f'<a href="{html.escape(url, quote=True)}">{_escape_html(ttl)}</a>'
        safe = safe.replace(f"__LINK{i}__", anchor)
    return safe


# ======== Botones (sólo traducimos el texto) ========

async def translate_inline_keyboard(markup: Optional[InlineKeyboardMarkup]) -> Optional[InlineKeyboardMarkup]:
    if not TRANSLATE_BUTTONS or not markup or not getattr(markup, "inline_keyboard", None):
        return markup
    new_rows: List[List[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        new_row: List[InlineKeyboardButton] = []
        for b in row:
            try:
                new_text = await translate_text(b.text or "")
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


# ================== Handlers ==================

async def alert_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    if not ERROR_ALERT or not ADMIN_ID:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ {text[:3900]}")
    except Exception:
        pass

def _mapped_dest_thread(chat_id: int, src_thread_id: Optional[int]) -> Optional[int]:
    m = TOPIC_MAPPING.get(str(chat_id))
    if not m:
        return None
    src_norm = _norm_thread_id(src_thread_id)
    # doble intento por str/int
    return m.get(str(src_norm)) or m.get(src_norm)

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = msg.chat
    if chat.type not in ("supergroup", "group"):
        return
    if msg.from_user and msg.from_user.is_bot:
        return  # evita bucles

    dest_thread = _mapped_dest_thread(chat.id, msg.message_thread_id)
    if not dest_thread:
        return  # no hay mapeo = no se replica

    try:
        markup = await translate_inline_keyboard(msg.reply_markup)

        if msg.text:
            html_text = await build_html_with_links(msg.text, msg.entities)
            await context.bot.send_message(
                chat_id=chat.id,
                message_thread_id=dest_thread,
                text=html_text,
                parse_mode="HTML",
                disable_web_page_preview=False,
                reply_markup=markup
            )
        elif msg.caption:
            html_cap = await build_html_with_links(msg.caption, msg.caption_entities)
            # Media: copiamos el mensaje para conservar la media, pero con caption traducido
            await context.bot.copy_message(
                chat_id=chat.id,
                message_thread_id=dest_thread,
                from_chat_id=chat.id,
                message_id=msg.message_id,
                caption=html_cap,
                parse_mode="HTML",
                reply_markup=markup
            )
        else:
            # Otros (stickers, etc.) -> copia 1:1
            await context.bot.copy_message(
                chat_id=chat.id,
                message_thread_id=dest_thread,
                from_chat_id=chat.id,
                message_id=msg.message_id,
                reply_markup=markup
            )

    except Exception as e:
        logger.exception("Error replicando en grupo")
        await alert_admin(context, f"Grupo {chat.id} hilo {msg.message_thread_id}: {e}")

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.channel_post or not _chat_matches_source_channel(update):
        return
    msg = update.channel_post
    try:
        markup = await translate_inline_keyboard(msg.reply_markup)
        dest = _dest_id_for_channel()

        if msg.text:
            html_text = await build_html_with_links(msg.text, msg.entities)
            await context.bot.send_message(
                chat_id=dest,
                text=html_text,
                parse_mode="HTML",
                disable_web_page_preview=False,
                reply_markup=markup
            )
        elif msg.caption:
            html_cap = await build_html_with_links(msg.caption, msg.caption_entities)
            await context.bot.copy_message(
                chat_id=dest,
                from_chat_id=msg.chat.id,
                message_id=msg.message_id,
                caption=html_cap,
                parse_mode="HTML",
                reply_markup=markup
            )
        else:
            await context.bot.copy_message(
                chat_id=dest,
                from_chat_id=msg.chat.id,
                message_id=msg.message_id,
                reply_markup=markup
            )

    except Exception as e:
        logger.exception("Error replicando canal")
        await alert_admin(context, f"Canal → canal: {e}")


# ================== MAIN ==================

def _ensure_token():
    if not BOT_TOKEN:
        raise RuntimeError("Falta BOT_TOKEN")

def main():
    _ensure_token()
    app = Application.builder().token(BOT_TOKEN).rate_limiter(AIORateLimiter()).build()

    # Grupos/temas
    app.add_handler(MessageHandler(filters.ChatType.SUPERGROUP & ~filters.StatusUpdate.ALL, handle_group_message))
    app.add_handler(MessageHandler(filters.ChatType.GROUP & ~filters.StatusUpdate.ALL,      handle_group_message))
    # Canales
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))

    logger.info("Replicador listo.")
    app.run_polling(allowed_updates=["message", "channel_post"], poll_interval=1.2, stop_signals=None)

if __name__ == "__main__":
    main()
