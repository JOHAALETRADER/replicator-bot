import os
import html
import logging
import re
from typing import Optional, Tuple, List, Dict, Any

import aiohttp
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
    MessageEntity,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
    Chat,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

# ================== CONFIG BÁSICA ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Traducción
TRANSLATE = os.getenv("TRANSLATE", "true").lower() == "true"
TRANSLATOR = "deepl"
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "384e6eb2-922a-43ce-8e8f-7cd3ac0047b7").strip()
DEEPL_API_HOST = os.getenv("DEEPL_API_HOST", "api-free.deepl.com").strip()

SOURCE_LANG = os.getenv("SOURCE_LANG", "ES").upper()
TARGET_LANG = os.getenv("TARGET_LANG", "EN").upper()
FORMALITY = os.getenv("FORMALITY", "default")
FORCE_TRANSLATE = os.getenv("FORCE_TRANSLATE", "false").lower() == "true"
TRANSLATE_BUTTONS = os.getenv("TRANSLATE_BUTTONS", "true").lower() == "true"

# Glosario DeepL
GLOSSARY_ID = os.getenv("GLOSSARY_ID", "").strip()
GLOSSARY_TSV = os.getenv("GLOSSARY_TSV", "").strip()  # si no está, usamos el DEFAULT_GLOSSARY_TSV

# Alertas
ERROR_ALERT = os.getenv("ERROR_ALERT", "true").lower() == "true"
ADMIN_ID = int(os.getenv("ADMIN_ID", "5958154558") or "0")

# Logging
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
log = logging.getLogger("replicator")

# ================== CANAL → CANAL ==================
CHANNEL_MAP: Dict[str, str] = {
    "@JohaaleTrader_es": "@JohaaleTrader_en",
}

# ================== GRUPOS / TEMAS ==================
# Tus grupos
G1 = -1001946870620
G4 = -1002725606859
G2 = -1002131156976
G5 = -1002569975479
G3 = -1002127373425

# Formato: (src_chat, src_thread) -> (dst_chat, dst_thread, only_sender_id | None)
TOPIC_ROUTES: Dict[Tuple[int, int], Tuple[int, int, Optional[int]]] = {
    # Grupo 1 → Grupo 4
    (G1, 129):   (G4, 8,   None),          # Resultados JT Traders → JT Wins
    (G1, 1):     (G4, 10,  5958154558),    # Chat → Chat Room (solo tú)
    (G1, 2890):  (G4, 6,   None),          # Resultados Alumnos → VIP Results & Payouts
    (G1, 17373): (G4, 6,   None),          # Retiros VIP → VIP Results & Payouts
    (G1, 8):     (G4, 2,   None),          # Estrategias/Archivos → Trading Plan & Risk
    (G1, 11):    (G4, 2,   None),          # Plan & Gestión de Riesgo → Trading Plan & Risk
    (G1, 9):     (G4, 12,  None),          # Noticias y Sorteos → Updates & Prizes

    # Grupo 2 → Grupo 5
    (G2, 2):     (G5, 2,   None),          # Synthetic Index Signals → Synthetic Index Signals
    (G2, 5337):  (G5, 8,   None),          # Binance Master Signals → Binance Pro Signals
    (G2, 3):     (G5, 10,  None),          # Binary Signals → Binary Trade Signals
    (G2, 4):     (G5, 5,   None),          # Forex Bias → Market Insights & Analysis
    (G2, 272):   (G5, 5,   None),          # Noticias y Análisis → Market Insights & Analysis

    # Grupo 3 (mismo grupo)
    (G3, 3):     (G3, 4096, None),         # Resultados JT Traders Teams → JT Wins
    (G3, 2):     (G3, 4098, None),         # Resultados Alumnos VIP → VIP Results & Risk
}

# ================== HEURÍSTICA DE IDIOMA ==================
_EN_COMMON = re.compile(r"\b(the|and|for|with|from|to|of|in|on|is|are|you|we|they|buy|sell|trade|signal|profit|setup|account)\b", re.I)
_ES_MARKERS = re.compile(r"[áéíóúñ¿¡]|\b(que|para|porque|hola|gracias|compra|venta|señal|apalancamiento|beneficios)\b", re.I)

def probably_english(text: str) -> bool:
    if _ES_MARKERS.search(text): return False
    if _EN_COMMON.search(text):  return True
    letters = [c for c in text if c.isalpha()]
    if not letters: return False
    ascii_letters = [c for c in letters if ord(c) < 128]
    return (len(ascii_letters) / max(1, len(letters))) > 0.85

# ================== ENTIDADES HTML ==================
SAFE_TAGS = {"b", "strong", "i", "em", "u", "s", "del", "code", "pre", "a"}

def escape(t: str) -> str:
    return html.escape(t, quote=False)

def entities_to_html(text: str, entities: List[MessageEntity]) -> List[Tuple[str, Dict[str, Any]]]:
    if not entities:
        return [(text, {})]
    entities = sorted(entities, key=lambda e: e.offset)
    res: List[Tuple[str, Dict[str, Any]]] = []
    idx = 0
    for e in entities:
        if e.offset > idx:
            res.append((text[idx:e.offset], {}))
        frag = text[e.offset:e.offset + e.length]
        meta: Dict[str, Any] = {}
        if e.type in ("bold",): meta["tag"] = "b"
        elif e.type in ("italic",): meta["tag"] = "i"
        elif e.type in ("underline",): meta["tag"] = "u"
        elif e.type in ("strikethrough",): meta["tag"] = "s"
        elif e.type in ("code",): meta["tag"] = "code"
        elif e.type in ("pre",): meta["tag"] = "pre"
        elif e.type == "text_link" and e.url:
            meta["tag"] = "a"; meta["href"] = e.url
        else:
            meta = {}
        res.append((frag, meta))
        idx = e.offset + e.length
    if idx < len(text):
        res.append((text[idx:], {}))
    return res

def build_html(fragments: List[Tuple[str, Dict[str, Any]]]) -> str:
    out: List[str] = []
    for frag, meta in fragments:
        safe = escape(frag)
        tag = meta.get("tag")
        if not tag:
            out.append(safe); continue
        if tag == "a":
            href = html.escape(meta.get("href", ""), quote=True)
            out.append(f'<a href="{href}">{safe}</a>')
        elif tag in SAFE_TAGS:
            out.append(f"<{tag}>{safe}</{tag}>")
        else:
            out.append(safe)
    return "".join(out)

# ================== GLOSARIO (DEFAULT) ==================
# Si no defines GLOSSARY_ID ni GLOSSARY_TSV, creamos un glosario automático con estos términos.
DEFAULT_GLOSSARY_TSV = """\
JOHAALETRADER\tJOHAALETRADER
JT TRADERS\tJT TRADERS
JT TRADERS TEAMS\tJT TRADERS TEAMS
JT TRADERS MASTERMIND\tJT TRADERS MASTERMIND
Binomo\tBinomo
binary options\tbinary options
setup\tsetup
signal\tsignal
signals\tsignals
entry\tentry
stop loss\tstop loss
take profit\ttake profit
TP\tTP
SL\tSL
risk management\trisk management
trailing stop\ttrailing stop
win rate\twin rate
candlestick\tcandlestick
EMA\tEMA
SMA\tSMA
RSI\tRSI
MACD\tMACD
breakout\tbreakout
pullback\tpullback
order block\torder block
liquidity\tliquidity
spread\tspread
hedging\thedging
derivatives\tderivatives
leverage\tleverage
support\tsupport
resistance\tresistance
market structure\tmarket structure
bullish\tbullish
bearish\tbearish
"""

# ================== TRADUCCIÓN (DEEPL + GLOSARIO) ==================
DEEPL_FORMALITY_LANGS = {"DE","FR","IT","ES","NL","PL","PT-PT","PT-BR","RU","JA"}

_glossary_id_mem: Optional[str] = None  # cache en memoria para esta ejecución

async def deepl_create_glossary_if_needed() -> Optional[str]:
    """
    Crea un glosario en DeepL si no hay GLOSSARY_ID. Usa GLOSSARY_TSV o DEFAULT_GLOSSARY_TSV.
    """
    global _glossary_id_mem, GLOSSARY_ID
    if not TRANSLATE or not DEEPL_API_KEY:
        return None
    if GLOSSARY_ID:
        _glossary_id_mem = GLOSSARY_ID
        return GLOSSARY_ID

    entries = (GLOSSARY_TSV or DEFAULT_GLOSSARY_TSV).strip()
    if not entries:
        return None

    url = f"https://{DEEPL_API_HOST}/v2/glossaries"
    form = aiohttp.FormData()
    form.add_field("name", "Trading ES-EN (Auto)")
    form.add_field("source_lang", SOURCE_LANG or "ES")
    form.add_field("target_lang", TARGET_LANG or "EN")
    form.add_field("entries", entries, filename="glossary.tsv", content_type="text/tab-separated-values")

    headers = {"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"}
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, data=form) as resp:
                body = await resp.text()
                if resp.status != 200:
                    log.warning("DeepL glossary create HTTP %s: %s", resp.status, body)
                    return None
                js = await resp.json()
                gid = js.get("glossary_id", "")
                if gid:
                    _glossary_id_mem = gid
                    GLOSSARY_ID = gid
                    log.info("DeepL glossary created: %s", gid)
                    return gid
    except Exception as e:
        log.warning("DeepL glossary create failed: %s", e)
    return None

async def deepl_translate(text: str, *, session: aiohttp.ClientSession) -> str:
    if not text.strip():
        return text
    if not TRANSLATE or not DEEPL_API_KEY:
        return text
    if not FORCE_TRANSLATE and probably_english(text):
        return text

    # asegurar glosario disponible (si procede)
    gid = _glossary_id_mem or GLOSSARY_ID
    if gid is None:
        gid = ""
    if not gid and (GLOSSARY_TSV or DEFAULT_GLOSSARY_TSV):
        try:
            gid = await deepl_create_glossary_if_needed() or ""
        except Exception:
            gid = ""

    url = f"https://{DEEPL_API_HOST}/v2/translate"
    data = {
        "auth_key": DEEPL_API_KEY,
        "text": text,
        "source_lang": SOURCE_LANG,
        "target_lang": TARGET_LANG,
    }
    if TARGET_LANG in DEEPL_FORMALITY_LANGS:
        data["formality"] = FORMALITY
    if gid:
        data["glossary_id"] = gid

    async with session.post(url, data=data) as r:
        b = await r.text()
        if r.status != 200:
            log.warning("DeepL HTTP %s: %s", r.status, b)
            return text
        js = await r.json()
        return js["translations"][0]["text"]

async def translate_visible_html(text: str, entities: List[MessageEntity]) -> Tuple[str, List[MessageEntity]]:
    frags = entities_to_html(text, entities or [])
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        new_frags: List[Tuple[str, Dict[str, Any]]] = []
        for frag, meta in frags:
            if meta.get("tag") in {None, "b", "i", "u", "s", "code", "pre", "a"}:
                new_text = await deepl_translate(frag, session=session)
                new_frags.append((new_text, meta))
            else:
                new_frags.append((frag, meta))
    html_text = build_html(new_frags)
    return html_text, []

async def translate_buttons(markup: Optional[InlineKeyboardMarkup]) -> Optional[InlineKeyboardMarkup]:
    if not markup or not TRANSLATE_BUTTONS or not getattr(markup, "inline_keyboard", None):
        return markup
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        rows: List[List[InlineKeyboardButton]] = []
        for row in markup.inline_keyboard:
            new_row: List[InlineKeyboardButton] = []
            for b in row:
                label = await deepl_translate(b.text or "", session=session)
                new_row.append(
                    InlineKeyboardButton(
                        text=(label or "")[:64],
                        url=b.url,
                        callback_data=b.callback_data,
                        switch_inline_query=b.switch_inline_query,
                        switch_inline_query_current_chat=b.switch_inline_query_current_chat,
                        web_app=getattr(b, "web_app", None),
                        login_url=getattr(b, "login_url", None),
                    )
                )
            rows.append(new_row)
        return InlineKeyboardMarkup(rows)

# ================== MAPEO ==================
def map_channel(src_chat: Chat) -> Optional[str]:
    uname = (src_chat.username or "").lower()
    if not uname:
        return None
    return CHANNEL_MAP.get(f"@{uname}")

def map_topic(src_chat_id: int, src_thread_id: Optional[int], sender_id: Optional[int]) -> Optional[Tuple[int,int]]:
    if not src_thread_id:
        return None
    route = TOPIC_ROUTES.get((src_chat_id, src_thread_id))
    if not route:
        return None
    dst_chat, dst_thread, only_sender = route
    if only_sender and sender_id and sender_id != only_sender:
        return None
    return (dst_chat, dst_thread)

async def alert_error(context: ContextTypes.DEFAULT_TYPE, text: str):
    if ERROR_ALERT and ADMIN_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ {text[:3800]}")
        except Exception:
            pass

# ================== REPLICACIÓN ==================
async def send_translated_text(context: ContextTypes.DEFAULT_TYPE, chat_id: int | str, thread_id: Optional[int], msg: Message):
    html_text, _ = await translate_visible_html(msg.text or "", msg.entities or [])
    kb = await translate_buttons(msg.reply_markup)
    await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=html_text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=kb,
    )

async def copy_with_translated_caption(context: ContextTypes.DEFAULT_TYPE, chat_id: int | str, thread_id: Optional[int], msg: Message):
    cap_text = msg.caption or ""
    cap_entities = msg.caption_entities or []
    if cap_text.strip():
        cap_html, _ = await translate_visible_html(cap_text, cap_entities)
        kb = await translate_buttons(msg.reply_markup)
        await context.bot.copy_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            from_chat_id=msg.chat.id,
            message_id=msg.message_id,
            caption=cap_html,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
    else:
        await context.bot.copy_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            from_chat_id=msg.chat.id,
            message_id=msg.message_id,
        )

async def replicate_message(context: ContextTypes.DEFAULT_TYPE, src_msg: Message, dest_chat_id: int | str, dest_thread_id: Optional[int]):
    if src_msg.text:
        await send_translated_text(context, dest_chat_id, dest_thread_id, src_msg)
        return
    await copy_with_translated_caption(context, dest_chat_id, dest_thread_id, src_msg)

# ================== HANDLERS ==================
async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.channel_post:
            return
        msg = update.channel_post
        dst = map_channel(msg.chat)
        if not dst:
            return
        log.info("Channel %s → %s | msg %s", msg.chat.username, dst, msg.message_id)
        await replicate_message(context, msg, dst, None)
    except Exception as e:
        log.exception("Error on_channel_post")
        await alert_error(context, f"on_channel_post: {e}")

async def on_group_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.effective_message
        chat = update.effective_chat
        if not msg or not chat:
            return
        if chat.type not in (ChatType.SUPERGROUP, ChatType.GROUP):
            return
        thread_id = msg.message_thread_id
        sender_id = msg.from_user.id if msg.from_user else None
        route = map_topic(chat.id, thread_id, sender_id)
        if not route:
            return
        dst_chat, dst_thread = route
        log.info("Group %s#%s → %s#%s | msg %s", chat.id, thread_id, dst_chat, dst_thread, msg.message_id)
        await replicate_message(context, msg, dst_chat, dst_thread)
    except Exception as e:
        log.exception("Error on_group_post")
        await alert_error(context, f"on_group_post: {e}")

# ================== MAIN ==================
def ensure_env():
    if not BOT_TOKEN:
        raise RuntimeError("Falta BOT_TOKEN")

def main():
    ensure_env()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, on_group_post))
    log.info("Replicator iniciado. Translate=%s, Buttons=%s", TRANSLATE, TRANSLATE_BUTTONS)
    app.run_polling(allowed_updates=["channel_post", "message"], poll_interval=1.2, stop_signals=None)

if __name__ == "__main__":
    main()
