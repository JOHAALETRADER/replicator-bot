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
CHANNEL_MAP: Dict[Any, Any] = {
    "@johaaletrader_es": "@johaaletrader_en",
}

ENV_SRC = (os.getenv("SOURCE_CHANNEL", "") or "").strip() or None
ENV_DST = (os.getenv("DEST_CHANNEL", "") or "").strip() or None

def _norm_chan(x: Any) -> tuple[Optional[str], Optional[int]]:
    if x is None:
        return (None, None)
    if isinstance(x, int):
        return (None, x)
    s = str(x).strip()
    if not s:
        return (None, None)
    if s.startswith("-100") and s[4:].isdigit():
        try:
            return (None, int(s))
        except Exception:
            return (None, None)
    if s.startswith("@"):
        return (s.lower(), None)
    return ("@" + s.lower(), None)

ENV_SRC_UNAME, ENV_SRC_ID = _norm_chan(ENV_SRC)
ENV_DST_UNAME, ENV_DST_ID = _norm_chan(ENV_DST)

# ================== GRUPOS / TEMAS ==================
G1 = -1001946870620
G4 = -1002725606859
G2 = -1002131156976
G5 = -1002569975479
G3 = -1002127373425

# ← Tu ID para filtrar el Chat
CHAT_OWNER_ID = 5958164558
# ← ID del “Anonymous Admin” de Telegram (por si Telegram oculta el remitente)
ANON_ADMIN_ID = 1087968824

# (src_chat, src_thread) -> (dst_chat, dst_thread, only_sender_id | None)
TOPIC_ROUTES: Dict[Tuple[int, int], Tuple[int, int, Optional[int]]] = {
    # Grupo 1 → Grupo 4 (como ya estaba)
    (G1, 129):   (G4, 8,   None),
    (G1, 1):     (G4, 10,  CHAT_OWNER_ID),   # Chat → Chat Room (solo tú)
    (G1, 2890):  (G4, 6,   None),
    (G1, 17373): (G4, 6,   None),
    (G1, 8):     (G4, 2,   None),
    (G1, 11):    (G4, 2,   None),
    (G1, 9):     (G4, 12,  None),

    # Grupo 2 → Grupo 5 (como ya estaba)
    (G2, 2):     (G5, 2,   None),
    (G2, 5337):  (G5, 8,   None),
    (G2, 3):     (G5, 10,  None),
    (G2, 4):     (G5, 5,   None),
    (G2, 272):   (G5, 5,   None),

    # Grupo 3 (mismo grupo, como ya estaba)
    (G3, 3):     (G3, 4096, None),
    (G3, 2):     (G3, 4098, None),  # ← NUEVO: de G3#2 a G3#4098 (traducir a EN)
}

# Fan-out adicional (se mantiene vacío salvo los casos que quieras duplicar)
FANOUT_ROUTES: Dict[Tuple[int, int], List[Tuple[int, int]]] = {
    # ← NUEVO: duplicar G1#2890 y G1#17373 también hacia G3#2
    (G1, 2890): [(G3, 2)],
    (G1, 17373): [(G3, 2)],
}

# ← NUEVO: rutas que deben ir SIN traducción (copiar tal cual)
# En tu caso, solo saltamos la traducción cuando el destino es exactamente G3#2
NO_TRANSLATE_ROUTES: Dict[Tuple[int, int], List[Tuple[int, int]]] = {
    (G1, 2890): [(G3, 2)],
    (G1, 17373): [(G3, 2)],
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

DEEPL_FORMALITY_LANGS = {"DE","FR","IT","ES","NL","PL","PT-PT","PT-BR","RU","JA"}
_glossary_id_mem: Optional[str] = None

async def deepl_create_glossary_if_needed() -> Optional[str]:
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

    gid = _glossary_id_mem or GLOSSARY_ID or ""
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

def escape(t: str) -> str:
    return html.escape(t, quote=False)

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
        elif tag in {"b","strong","i","em","u","s","del","code","pre","a"}:
            out.append(f"<{tag}>{safe}</{tag}>")
        else:
            out.append(safe)
    return "".join(out)

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
        elif e.type == "text_link" and e.url:
            meta["tag"] = "a"; meta["href"] = e.url
        else:
            meta = {}
        res.append((frag, meta))
        idx = e.offset + e.length
    if idx < len(text):
        res.append((text[idx:], {}))
    return res

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

# ← NUEVO: render sin traducir (respeta entidades)
def render_visible_html_no_translate(text: str, entities: List[MessageEntity]) -> Tuple[str, List[MessageEntity]]:
    frags = entities_to_html(text, entities or [])
    html_text = build_html(frags)
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
def map_channel(src_chat: Chat) -> Optional[int | str]:
    src_id = int(src_chat.id)
    src_uname = ("@" + (src_chat.username or "").lower()) if src_chat.username else None

    if ENV_SRC_ID is not None and src_id == ENV_SRC_ID:
        return ENV_DST_ID if ENV_DST_ID is not None else (ENV_DST_UNAME or None)
    if ENV_SRC_UNAME and src_uname and src_uname == ENV_SRC_UNAME:
        return ENV_DST_ID if ENV_DST_ID is not None else (ENV_DST_UNAME or None)

    if src_id in CHANNEL_MAP:
        return CHANNEL_MAP[src_id]
    if str(src_id) in CHANNEL_MAP:
        return CHANNEL_MAP[str(src_id)]
    if src_uname and src_uname in CHANNEL_MAP:
        return CHANNEL_MAP[src_uname]

    return None

def _route_has_no_translate(src_chat_id: int, src_thread_id_norm: int, dst_chat: int, dst_thread: int) -> bool:
    pairs = NO_TRANSLATE_ROUTES.get((src_chat_id, src_thread_id_norm), [])
    return (dst_chat, dst_thread) in pairs

def map_topic(src_chat_id: int, src_thread_id: Optional[int], sender_id: Optional[int]) -> Optional[Tuple[int,int,Optional[bool]]]:
    """
    Mapea (chat, thread) → (chat, thread) y devuelve (dst_chat, dst_thread, no_translate?).
    - Respeta filtro de remitente (CHAT del G1 solo tú / Anonymous Admin).
    - Añade bandera no_translate si está configurado en NO_TRANSLATE_ROUTES.
    """
    # 1) exacto primero
    if src_thread_id is not None:
        route = TOPIC_ROUTES.get((src_chat_id, src_thread_id))
        if route:
            dst_chat, dst_thread, only_sender = route
            if only_sender:
                if sender_id == only_sender:
                    pass
                elif (src_chat_id == G1 and src_thread_id in (None, 0, 1) and sender_id == ANON_ADMIN_ID):
                    pass
                else:
                    return None
            nt = _route_has_no_translate(src_chat_id, src_thread_id, dst_chat, dst_thread)
            return (dst_chat, dst_thread, nt)

    # 2) normalización General→1
    tid = 1 if (src_thread_id in (None, 0)) else src_thread_id
    route = TOPIC_ROUTES.get((src_chat_id, tid))
    if route:
        dst_chat, dst_thread, only_sender = route
        if only_sender:
            if sender_id == only_sender:
                pass
            elif (src_chat_id == G1 and tid == 1 and sender_id == ANON_ADMIN_ID):
                pass
            else:
                return None
        nt = _route_has_no_translate(src_chat_id, tid, dst_chat, dst_thread)
        return (dst_chat, dst_thread, nt)

    return None

async def alert_error(context: ContextTypes.DEFAULT_TYPE, text: str):
    if ERROR_ALERT and ADMIN_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ {text[:3800]}")
        except Exception:
            pass

# ================== REPLICACIÓN ==================
async def send_translated_text(context: ContextTypes.DEFAULT_TYPE, chat_id: int | str, thread_id: Optional[int], msg: Message, *, no_translate: bool = False):
    if no_translate:
        html_text, _ = render_visible_html_no_translate(msg.text or "", msg.entities or [])
    else:
        html_text, _ = await translate_visible_html(msg.text or "", msg.entities or [])
    kb = await translate_buttons(msg.reply_markup) if not no_translate else msg.reply_markup
    await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=html_text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=kb,
    )

async def copy_with_translated_caption(context: ContextTypes.DEFAULT_TYPE, chat_id: int | str, thread_id: Optional[int], msg: Message, *, no_translate: bool = False):
    cap_text = msg.caption or ""
    cap_entities = msg.caption_entities or []
    if cap_text.strip():
        if no_translate:
            cap_html, _ = render_visible_html_no_translate(cap_text, cap_entities)
            kb = msg.reply_markup
        else:
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

async def replicate_message(context: ContextTypes.DEFAULT_TYPE, src_msg: Message, dest_chat_id: int | str, dest_thread_id: Optional[int], *, no_translate: bool = False):
    if src_msg.text:
        await send_translated_text(context, dest_chat_id, dest_thread_id, src_msg, no_translate=no_translate)
        return
    await copy_with_translated_caption(context, dest_chat_id, dest_thread_id, src_msg, no_translate=no_translate)

# ================== HANDLERS ==================
async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.channel_post:
            return
        msg = update.channel_post
        dst = map_channel(msg.chat)
        if not dst:
            return
        log.info("Channel %s (id=%s) → %s | msg %s", msg.chat.username, msg.chat.id, dst, msg.message_id)
        await replicate_message(context, msg, dst, None, no_translate=False)
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

        if chat.id == G1:
            logging.info(f"[CHAT DEBUG] thread_id={thread_id} sender={sender_id}")

        mapped = map_topic(chat.id, thread_id, sender_id)
        if not mapped:
            return
        dst_chat, dst_thread, no_trans = mapped
        log.info("Group %s#%s → %s#%s | msg %s | no_translate=%s",
                 chat.id, thread_id if thread_id is not None else 1, dst_chat, dst_thread, msg.message_id, no_trans)
        await replicate_message(context, msg, dst_chat, dst_thread, no_translate=bool(no_trans))

        # Fan-out (rutas adicionales, con su propia regla de no-traducción)
        tid_norm = thread_id if thread_id is not None else 1
        extras = FANOUT_ROUTES.get((chat.id, tid_norm), [])
        for extra_chat, extra_thread in extras:
            extra_no_trans = _route_has_no_translate(chat.id, tid_norm, extra_chat, extra_thread)
            log.info("Fanout %s#%s → %s#%s | msg %s | no_translate=%s",
                     chat.id, tid_norm, extra_chat, extra_thread, msg.message_id, extra_no_trans)
            await replicate_message(context, msg, extra_chat, extra_thread, no_translate=bool(extra_no_trans))

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
    log.info("Replicator iniciado. Translate=%s, Buttons=%s | ENV_SRC=%s ENV_DST=%s", TRANSLATE, TRANSLATE_BUTTONS, ENV_SRC, ENV_DST)
    app.run_polling(allowed_updates=["channel_post", "message"], poll_interval=1.2, stop_signals=None)

if __name__ == "__main__":
    main()
