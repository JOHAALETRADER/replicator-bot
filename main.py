import os
import html
import logging
import re
import unicodedata
import asyncio
import io
import sqlite3
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any, Callable, Awaitable

import aiohttp
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
    MessageEntity,
    Chat,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import TimedOut, NetworkError, RetryAfter, BadRequest, Forbidden
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

# ================== CONFIG BÁSICA ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Traducción (global por defecto)
TRANSLATE = os.getenv("TRANSLATE", "true").lower() == "true"
TRANSLATOR = "deepl"
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "").strip()
DEEPL_API_HOST = os.getenv("DEEPL_API_HOST", "api-free.deepl.com").strip()

SOURCE_LANG = os.getenv("SOURCE_LANG", "ES").upper()
TARGET_LANG = os.getenv("TARGET_LANG", "EN").upper()
FORMALITY = os.getenv("FORMALITY", "default")
FORCE_TRANSLATE = os.getenv("FORCE_TRANSLATE", "false").lower() == "true"
TRANSLATE_BUTTONS = os.getenv("TRANSLATE_BUTTONS", "true").lower() == "true"
# Audio → Texto (STT) + Texto (DeepL) + Audio (TTS)
AUDIO_TRANSLATE = os.getenv("AUDIO_TRANSLATE", "true").lower() == "true"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
OPENAI_STT_MODEL = os.getenv("OPENAI_STT_MODEL", "whisper-1").strip()
# Modelos comunes: tts-1 / tts-1-hd / gpt-4o-tts (según tu cuenta)
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "tts-1").strip()
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "alloy").strip()
OPENAI_TTS_FORMAT = os.getenv("OPENAI_TTS_FORMAT", "mp3").strip()
OPENAI_TIMEOUT_SEC = float(os.getenv("OPENAI_TIMEOUT_SEC", "60") or "60")

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
G1 = -1001946870620  # origen ES (tu link /c/1946870620)
G4 = -1002725606859  # espejo EN (tu link /c/2725606859)
G2 = -1002131156976
G5 = -1002569975479
G3 = -1002127373425

# ← Tu ID (ya NO se usa para filtrar Chat ES→EN)
CHAT_OWNER_ID = 5958164558
# ← ID del “Anonymous Admin” de Telegram
ANON_ADMIN_ID = 1087968824

# (src_chat, src_thread) -> (dst_chat, dst_thread, only_sender_id | None)
TOPIC_ROUTES: Dict[Tuple[int, int], Tuple[int, int, Optional[int]]] = {
    # Grupo 1 → Grupo 4
    (G1, 129): (G4, 8, None),
    (G1, 1): (G4, 10, None),  # ✅ Chat → Chat Room (replica TODOS)
    (G1, 2890): (G4, 6, None),
    (G1, 17373): (G4, 6, None),
    (G1, 8): (G4, 2, None),
    (G1, 11): (G4, 2, None),
    (G1, 9): (G4, 12, None),

    # Grupo 2 → Grupo 5
    (G2, 2): (G5, 2, None),
    (G2, 5337): (G5, 8, None),
    (G2, 3): (G5, 10, None),
    (G2, 4): (G5, 5, None),
    (G2, 272): (G5, 5, None),

    # Grupo 3 (mismo grupo)
    (G3, 3): (G3, 4096, None),
    (G3, 2): (G3, 4098, None),  # ES → EN dentro del mismo grupo (si el origen es directo)
}

# ================== FAN-OUT OPCIONAL ==================
FANOUT_ROUTES: Dict[Tuple[int, int], List[Tuple[int, int]]] = {
    (G1, 2890): [(G3, 2), (G3, 4098)],
    (G1, 17373): [(G3, 2), (G3, 4098)],
    (G1, 129): [(G3, 3), (G3, 4096)],
}

# ================== OVERRIDE DE TRADUCCIÓN POR RUTA ==================
NO_TRANSLATE_ROUTES: set[Tuple[int, int, int, int]] = {
    # G1 → G3#2 en ES
    (G1, 2890, G3, 2),
    (G1, 17373, G3, 2),
    (G1, 129, G3, 3),
}

# ================== REPLY FIJO PARA FANOUT (solo rutas específicas) ==================
# Mapea (src_chat, src_thread, dst_chat, dst_thread) -> reply_to_message_id fijo en destino.
# Nota: si el mensaje no existe, hacemos fallback a enviar sin reply (no rompe el flujo).
FIXED_REPLY_TO: Dict[Tuple[int, int, int, int], int] = {}


# ================== ANTI-LOOP: NO replicar desde destinos ==================
DEST_TOPIC_SET: set[Tuple[int, int]] = set()
for (_src_chat, _src_thread), (_dst_chat, _dst_thread, _only_sender) in TOPIC_ROUTES.items():
    DEST_TOPIC_SET.add((_dst_chat, _dst_thread))

def is_destination_topic(chat_id: int, thread_id: Optional[int]) -> bool:
    tid = 1 if (thread_id in (None, 0)) else thread_id
    return (chat_id, tid) in DEST_TOPIC_SET


# ================== DEDUP: evita procesar el mismo msg varias veces ==================
DEDUP_TTL_SECONDS = float(os.getenv("DEDUP_TTL_SECONDS", "120") or "120")
_seen_msgs: Dict[Tuple[int, int], float] = {}

def seen_recent(chat_id: int, message_id: int) -> bool:
    now = asyncio.get_event_loop().time()
    key = (int(chat_id), int(message_id))

    # limpieza ocasional
    if len(_seen_msgs) > 2000:
        cutoff = now - DEDUP_TTL_SECONDS
        for k in list(_seen_msgs.keys()):
            if _seen_msgs.get(k, 0) < cutoff:
                _seen_msgs.pop(k, None)

    t = _seen_msgs.get(key)
    if t and (now - t) < DEDUP_TTL_SECONDS:
        return True

    _seen_msgs[key] = now
    return False


# ================== HEURÍSTICA DE IDIOMA ==================
_EN_COMMON = re.compile(
    r"\b(the|and|for|with|from|to|of|in|on|is|are|you|we|they|buy|sell|trade|signal|profit|setup|account)\b",
    re.I
)
_ES_MARKERS = re.compile(r"[áéíóúñ¿¡]|\b(que|para|porque|hola|gracias|compra|venta|señal|apalancamiento|beneficios)\b", re.I)


def probably_english(text: str) -> bool:
    if _ES_MARKERS.search(text):
        return False
    if _EN_COMMON.search(text):
        return True
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
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
        if e.type in ("bold",):
            meta["tag"] = "b"
        elif e.type in ("italic",):
            meta["tag"] = "i"
        elif e.type in ("underline",):
            meta["tag"] = "u"
        elif e.type in ("strikethrough",):
            meta["tag"] = "s"
        elif e.type in ("code",):
            meta["tag"] = "code"
        elif e.type == "text_link" and e.url:
            meta["tag"] = "a"
            meta["href"] = e.url
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
            out.append(safe)
            continue
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

# ================== TRADUCCIÓN (DEEPL + GLOSARIO) ==================
DEEPL_FORMALITY_LANGS = {"DE", "FR", "IT", "ES", "NL", "PL", "PT-PT", "PT-BR", "RU", "JA"}
_glossary_id_mem: Optional[str] = None  # cache en memoria para esta ejecución


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



# ================== HIGIENE DE TEXTO PARA TRADUCCIÓN ==================
_ZERO_WIDTH_RE = re.compile(r"[\u200B-\u200D\uFEFF]")
_EMOJI_SPACING_RE = re.compile(r"([^\s])([🔥💥📊🤖📲🪙✍️↓✅❌])")

def clean_for_translation(t: str) -> str:
    """
    Limpieza *suave* antes de DeepL para evitar artefactos (palabras pegadas, emojis unidos,
    caracteres invisibles). No toca enlaces ni estructura del mensaje.
    """
    if not t:
        return t

    # Normaliza unicode y elimina caracteres invisibles
    t = unicodedata.normalize("NFKC", t)
    t = _ZERO_WIDTH_RE.sub("", t)

    # Protege URLs para no "partirlas" al separar emojis/símbolos
    url_map = {}
    def _url_repl(m):
        key = f"__URL{len(url_map)}__"
        url_map[key] = m.group(0)
        return key

    t = re.sub(r"https?://\S+|t\.me/\S+", _url_repl, t)

    # Separa emojis pegados a palabras (evita 'forsatechnical', '❤️MaSee', etc.)
    # Rango emoji más común + símbolos varios usados en copy
    emoji_chars = r"\U0001F300-\U0001FAFF\u2600-\u27BF"
    t = re.sub(rf"(\w)([{emoji_chars}])", r"\1 \2", t)
    t = re.sub(rf"([{emoji_chars}])(\w)", r"\1 \2", t)

    # Compacta espacios
    t = re.sub(r"[ \t]+", " ", t).strip()

    # Restaura URLs
    for k, v in url_map.items():
        t = t.replace(k, v)

    return t


def postprocess_translation_en(t: str) -> str:
    """
    Pulido mínimo para que el inglés suene natural en contexto trading/comunidad,
    sin reescribir el mensaje ni alterar el sentido.
    """
    if not t:
        return t

    # 1) Limpia artefactos raros si alguna vez aparecen
    t = t.replace("\\1", "").replace("\\2", "")

    # 2) Naturalidad para lives (trading)
    # DeepL a veces traduce 'conectarme al live' como 'connect to live'
    t = re.sub(r"\bconnect to (the )?live\b", "go live", t, flags=re.IGNORECASE)

    # 3) Frases comunes: hacerlas más naturales
    t = re.sub(r"\bwithout fail\b", "for sure", t, flags=re.IGNORECASE)
    t = re.sub(r"\bwith all the energy to continue growing together on this path\b",
               "with full energy to keep growing together", t, flags=re.IGNORECASE)

    # 4) Ajustes suaves adicionales (sin cambiar contenido)
    t = t.replace("to take them", "to take these trades")
    t = t.replace("technical analysis to take", "technical analysis to trade")

    # 5) Typos frecuentes si el input viene con ruido
    t = re.sub(r"\bGhank\b", "Thank", t)
    t = re.sub(r"\bMaSee\b", "See", t)

    # 6) Espaciado alrededor de corazones y emojis comunes (por si queda alguno pegado)
    t = re.sub(r"([A-Za-z])([❤️🔥📈🙏💻⚡])", r"\1 \2", t)
    t = re.sub(r"([❤️🔥📈🙏💻⚡])([A-Za-z])", r"\1 \2", t)

    # 7) Compacta espacios
    t = re.sub(r"[ \t]{2,}", " ", t).strip()
    return t


def build_html_no_translate(text: str, entities: List[MessageEntity]) -> str:
    return build_html(entities_to_html(text, entities or []))


async def translate_buttons(markup: Optional[InlineKeyboardMarkup], *, do_translate: bool) -> Optional[InlineKeyboardMarkup]:
    if not markup or not TRANSLATE_BUTTONS or not getattr(markup, "inline_keyboard", None):
        return markup
    if not do_translate:
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


# ================== MAPEO DE REPLY/EDITS (SQLite persistente) ==================
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DB_PATH = Path(os.getenv("REPL_DB_PATH", str(DATA_DIR / "replicator_map.db")))

_DB_CONN: Optional[sqlite3.Connection] = None


def db_init():
    global _DB_CONN
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _DB_CONN = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    _DB_CONN.execute("""
        CREATE TABLE IF NOT EXISTS msg_map (
            src_chat INTEGER NOT NULL,
            src_msg  INTEGER NOT NULL,
            dst_chat INTEGER NOT NULL,
            dst_msg  INTEGER NOT NULL,
            PRIMARY KEY (src_chat, src_msg, dst_chat)
        )
    """)
    _DB_CONN.execute("CREATE INDEX IF NOT EXISTS idx_src ON msg_map (src_chat, src_msg)")
    _DB_CONN.commit()


def db_save_map(src_chat: int, src_msg: int, dst_chat: int, dst_msg: int):
    if not _DB_CONN:
        db_init()
    try:
        _DB_CONN.execute(
            "INSERT OR REPLACE INTO msg_map (src_chat, src_msg, dst_chat, dst_msg) VALUES (?, ?, ?, ?)",
            (int(src_chat), int(src_msg), int(dst_chat), int(dst_msg))
        )
        _DB_CONN.commit()
    except Exception as e:
        log.warning("db_save_map failed: %s", e)


def db_get_dst_msg(src_chat: int, src_msg: int, dst_chat: int) -> Optional[int]:
    if not _DB_CONN:
        db_init()
    try:
        cur = _DB_CONN.execute(
            "SELECT dst_msg FROM msg_map WHERE src_chat=? AND src_msg=? AND dst_chat=? LIMIT 1",
            (int(src_chat), int(src_msg), int(dst_chat))
        )
        row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


# ================== PREFIJO "👤 Nombre:" ==================
def sender_display_name(msg: Message) -> str:
    # Anonymous admin / sender_chat
    if getattr(msg, "sender_chat", None):
        try:
            return (msg.sender_chat.title or "Anonymous").strip()
        except Exception:
            return "Anonymous"
    if msg.from_user:
        try:
            return (msg.from_user.full_name or msg.from_user.first_name or "Usuario").strip()
        except Exception:
            return "Usuario"
    return "Usuario"


def prefix_block(name: str) -> str:
    name = (name or "Usuario").strip()
    return f"👤 Nombre: {name}\n\n"


def cap_with_prefix(prefix: str, cap_html: str, max_len: int = 1024) -> str:
    out = (prefix + cap_html).strip()
    if len(out) <= max_len:
        return out
    return out[: max_len - 1] + "…"


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


def map_topic(src_chat_id: int, src_thread_id: Optional[int], sender_id: Optional[int]) -> Optional[Tuple[int, int]]:
    """
    Mapea (chat, thread) → (chat, thread). Reglas:
      1) Coincidencia exacta en TOPIC_ROUTES.
      2) Si thread_id es None/0, normaliza a 1 y vuelve a buscar.
    """
    if src_thread_id is not None:
        route = TOPIC_ROUTES.get((src_chat_id, src_thread_id))
        if route:
            dst_chat, dst_thread, only_sender = route
            if only_sender:
                if sender_id == only_sender:
                    return (dst_chat, dst_thread)
                return None
            return (dst_chat, dst_thread)

    tid = 1 if (src_thread_id in (None, 0)) else src_thread_id
    route = TOPIC_ROUTES.get((src_chat_id, tid))
    if route:
        dst_chat, dst_thread, only_sender = route
        if only_sender:
            if sender_id == only_sender:
                return (dst_chat, dst_thread)
            return None
        return (dst_chat, dst_thread)

    return None


def route_no_translate(src_chat: int, src_thread: Optional[int], dst_chat: int, dst_thread: int) -> bool:
    tid = src_thread if src_thread is not None else 1
    return (src_chat, tid, dst_chat, dst_thread) in NO_TRANSLATE_ROUTES


async def alert_error(context: ContextTypes.DEFAULT_TYPE, text: str):
    if ERROR_ALERT and ADMIN_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ {text[:3800]}")
        except Exception:
            pass


# ================== FIX: RETRIES / TIMEOUTS ==================
async def call_with_retry(
    label: str,
    fn: Callable[[], Awaitable[Any]],
    *,
    tries: int = 4,
    base_delay: float = 1.2,
):
    last_exc: Exception | None = None
    for i in range(1, tries + 1):
        try:
            return await fn()
        except RetryAfter as e:
            last_exc = e
            wait_s = float(getattr(e, "retry_after", 1.0))
            log.warning("[%s] RetryAfter %ss (intento %s/%s)", label, wait_s, i, tries)
            await asyncio.sleep(wait_s + 0.2)
        except (TimedOut, NetworkError) as e:
            last_exc = e
            wait = base_delay * (2 ** (i - 1))
            log.warning("[%s] Timeout/NetworkError (intento %s/%s). Esperando %.1fs. Err=%s", label, i, tries, wait, e)
            await asyncio.sleep(wait)
        except BadRequest as e:
            log.error("[%s] BadRequest: %s", label, e)
            raise
        except Forbidden as e:
            log.error("[%s] Forbidden: %s", label, e)
            raise
        except Exception as e:
            last_exc = e
            wait = base_delay * (2 ** (i - 1))
            log.warning("[%s] Error inesperado (intento %s/%s). Esperando %.1fs. Err=%s", label, i, tries, wait, e)
            await asyncio.sleep(wait)

    if last_exc:
        raise last_exc


# ================== HELPERS REPLY/LOOP ==================
def is_from_bot(msg: Message, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        if msg.from_user and context.bot and msg.from_user.id == context.bot.id:
            return True
    except Exception:
        pass
    return False


def resolve_reply_to_id(src_msg: Message, dst_chat: int) -> Optional[int]:
    try:
        r = getattr(src_msg, "reply_to_message", None)
        if not r:
            return None
        return db_get_dst_msg(src_msg.chat.id, r.message_id, dst_chat)
    except Exception:
        return None


# ================== REPLICACIÓN ==================
async def send_text(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | str,
    thread_id: Optional[int],
    msg: Message,
    *,
    do_translate: bool,
    reply_to_message_id: Optional[int] = None,
) -> Optional[Message]:
    name = sender_display_name(msg)
    pref = prefix_block(name)

    if do_translate and TRANSLATE:
        html_text, _ = await translate_visible_html(msg.text or "", msg.entities or [])
    else:
        html_text = build_html_no_translate(msg.text or "", msg.entities or [])

    html_text = pref + html_text
    kb = await translate_buttons(msg.reply_markup, do_translate=do_translate and TRANSLATE)

    sent = await call_with_retry(
        "send_message",
        lambda: context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=html_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=kb,
            reply_to_message_id=reply_to_message_id,
        ),
    )
    return sent


async def copy_with_caption(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | str,
    thread_id: Optional[int],
    msg: Message,
    *,
    do_translate: bool,
    reply_to_message_id: Optional[int] = None,
) -> Optional[Message]:
    name = sender_display_name(msg)
    pref = prefix_block(name)

    cap_text = msg.caption or ""
    cap_entities = msg.caption_entities or []

    if cap_text.strip():
        if do_translate and TRANSLATE:
            cap_html, _ = await translate_visible_html(cap_text, cap_entities)
        else:
            cap_html = build_html_no_translate(cap_text, cap_entities)

        cap_html = cap_with_prefix(pref, cap_html, max_len=1024)
        kb = await translate_buttons(msg.reply_markup, do_translate=do_translate and TRANSLATE)

        sent = await call_with_retry(
            "copy_message_caption",
            lambda: context.bot.copy_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                from_chat_id=msg.chat.id,
                message_id=msg.message_id,
                caption=cap_html,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
                reply_to_message_id=reply_to_message_id,
            ),
        )
        return sent

    sent = await call_with_retry(
        "copy_message",
        lambda: context.bot.copy_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            from_chat_id=msg.chat.id,
            message_id=msg.message_id,
            reply_to_message_id=reply_to_message_id,
        ),
    )
    return sent


# --------- SOPORTE DE ÁLBUM (media_group) ---------
MEDIA_GROUP_BUFFER: Dict[Tuple[int, str, Any, Optional[int], bool], List[Message]] = {}
MEDIA_GROUP_TASKS: Dict[Tuple[int, str, Any, Optional[int], bool], Any] = {}
MEDIA_GROUP_DELAY = 0.6  # segundos


def _msg_has_photo(msg: Message) -> bool:
    return bool(getattr(msg, "photo", None))


def _msg_has_video(msg: Message) -> bool:
    return bool(getattr(msg, "video", None))


def _msg_has_document(msg: Message) -> bool:
    return bool(getattr(msg, "document", None))


def _msg_has_audio(msg: Message) -> bool:
    return bool(getattr(msg, "audio", None))


def _msg_build_input_media(
    msg: Message,
    *,
    caption_html: Optional[str],
) -> Optional[InputMediaPhoto | InputMediaVideo | InputMediaDocument | InputMediaAudio]:
    if _msg_has_photo(msg):
        fid = msg.photo[-1].file_id
        return InputMediaPhoto(media=fid, caption=caption_html, parse_mode=ParseMode.HTML if caption_html else None)
    if _msg_has_video(msg):
        return InputMediaVideo(media=msg.video.file_id, caption=caption_html, parse_mode=ParseMode.HTML if caption_html else None)
    if _msg_has_document(msg):
        return InputMediaDocument(media=msg.document.file_id, caption=caption_html, parse_mode=ParseMode.HTML if caption_html else None)
    if _msg_has_audio(msg):
        return InputMediaAudio(media=msg.audio.file_id, caption=caption_html, parse_mode=ParseMode.HTML if caption_html else None)
    return None


async def _flush_media_group(context: ContextTypes.DEFAULT_TYPE, key: Tuple[int, str, Any, Optional[int], bool]):
    try:
        msgs = MEDIA_GROUP_BUFFER.pop(key, [])
        MEDIA_GROUP_TASKS.pop(key, None)
        if not msgs:
            return

        msgs.sort(key=lambda m: m.message_id)

        _, _, dst_chat, dst_thread, do_translate = key

        cap_text = ""
        cap_entities: List[MessageEntity] = []
        first_src_msg: Optional[Message] = None
        for m in msgs:
            if (m.caption or "").strip():
                cap_text = m.caption or ""
                cap_entities = m.caption_entities or []
                first_src_msg = m
                break

        first_caption_html: Optional[str] = None
        if cap_text:
            name = sender_display_name(first_src_msg or msgs[0])
            pref = prefix_block(name)
            if do_translate and TRANSLATE:
                first_caption_html, _ = await translate_visible_html(cap_text, cap_entities)
            else:
                first_caption_html = build_html_no_translate(cap_text, cap_entities)
            first_caption_html = cap_with_prefix(pref, first_caption_html, max_len=1024)

        media_list: List[InputMediaPhoto | InputMediaVideo | InputMediaDocument | InputMediaAudio] = []
        first_used = False
        for m in msgs:
            cap = first_caption_html if not first_used else None
            im = _msg_build_input_media(m, caption_html=cap)
            if im:
                media_list.append(im)
                if cap is not None:
                    first_used = True

        if not media_list:
            return

        sent_msgs = await call_with_retry(
            "send_media_group",
            lambda: context.bot.send_media_group(
                chat_id=dst_chat,
                message_thread_id=dst_thread,
                media=media_list,
            ),
        )

        if sent_msgs and isinstance(sent_msgs, list) and isinstance(dst_chat, int):
            for i, sm in enumerate(msgs):
                if i < len(sent_msgs):
                    db_save_map(sm.chat.id, sm.message_id, int(dst_chat), sent_msgs[i].message_id)

    except Exception as e:
        log.exception("Error enviando media group %s: %s", key, e)
        await alert_error(context, f"media_group error: {e}")


async def replicate_media_with_album_support(
    context: ContextTypes.DEFAULT_TYPE,
    src_msg: Message,
    dest_chat_id: int | str,
    dest_thread_id: Optional[int],
    *,
    do_translate: bool,
    forced_reply_to_message_id: Optional[int] = None,
):
    mgid = getattr(src_msg, "media_group_id", None)
    if not mgid:
        reply_to_id = forced_reply_to_message_id if forced_reply_to_message_id is not None else (resolve_reply_to_id(src_msg, int(dest_chat_id)) if isinstance(dest_chat_id, int) else None)
        sent = await copy_with_caption(
            context, dest_chat_id, dest_thread_id, src_msg,
            do_translate=do_translate, reply_to_message_id=reply_to_id
        )
        if sent and isinstance(dest_chat_id, int):
            db_save_map(src_msg.chat.id, src_msg.message_id, int(dest_chat_id), sent.message_id)
        return

    key = (src_msg.chat.id, str(mgid), dest_chat_id, dest_thread_id, bool(do_translate))
    bucket = MEDIA_GROUP_BUFFER.setdefault(key, [])
    bucket.append(src_msg)

    async def _delayed_flush():
        await asyncio.sleep(MEDIA_GROUP_DELAY)
        await _flush_media_group(context, key)

    task = MEDIA_GROUP_TASKS.get(key)
    if task and not task.done():
        return
    MEDIA_GROUP_TASKS[key] = asyncio.create_task(_delayed_flush())


async def replicate_message(
    context: ContextTypes.DEFAULT_TYPE,
    src_msg: Message,
    dest_chat_id: int | str,
    dest_thread_id: Optional[int],
    *,
    do_translate: bool,
    forced_reply_to_message_id: Optional[int] = None,
):
    # Anti-loop interno: si ya es del bot, no repliques
    if is_from_bot(src_msg, context):
        return
    # forced_reply_to_message_id:
    #   None -> normal behavior (may map replies)
    #   0    -> explicitly disable replying in destination
    reply_to_id = None if forced_reply_to_message_id == 0 else forced_reply_to_message_id
    if reply_to_id is None and forced_reply_to_message_id != 0 and isinstance(dest_chat_id, int):
        reply_to_id = resolve_reply_to_id(src_msg, dest_chat_id)

    
    # --- AUDIO: transcribir + traducir + reenviar como audio EN + texto EN ---
    if (getattr(src_msg, "voice", None) or getattr(src_msg, "audio", None)):
        try:
            await replicate_audio_with_translation(context, src_msg, dest_chat_id, dest_thread_id, do_translate=do_translate)
            return
        except Exception as e:
            # Si falla STT/TTS, hacemos fallback al comportamiento original (copiar audio)
            log.warning("Audio translate fallback (msg %s): %s", src_msg.message_id, e)
    if src_msg.text:
        sent = await send_text(
            context, dest_chat_id, dest_thread_id, src_msg,
            do_translate=do_translate, reply_to_message_id=reply_to_id
        )
        if sent and isinstance(dest_chat_id, int):
            db_save_map(src_msg.chat.id, src_msg.message_id, dest_chat_id, sent.message_id)
        return

    await replicate_media_with_album_support(
        context, src_msg, dest_chat_id, dest_thread_id, do_translate=do_translate, forced_reply_to_message_id=reply_to_id
    )

    mgid = getattr(src_msg, "media_group_id", None)
    if mgid and reply_to_id and (src_msg.caption or "").strip() and isinstance(dest_chat_id, int):
        name = sender_display_name(src_msg)
        pref = prefix_block(name)
        cap_text = src_msg.caption or ""
        cap_entities = src_msg.caption_entities or []
        if do_translate and TRANSLATE:
            cap_html, _ = await translate_visible_html(cap_text, cap_entities)
        else:
            cap_html = build_html_no_translate(cap_text, cap_entities)
        cap_html = cap_with_prefix(pref, cap_html, max_len=3500)
        await call_with_retry(
            "reply_album_caption",
            lambda: context.bot.send_message(
                chat_id=dest_chat_id,
                message_thread_id=dest_thread_id,
                text=cap_html,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_to_message_id=reply_to_id,
            )
        )


# ================== EDICIONES (AUTO SYNC) ==================
async def replicate_edit(
    context: ContextTypes.DEFAULT_TYPE,
    src_msg: Message,
    dest_chat_id: int,
    dest_thread_id: Optional[int],
    *,
    do_translate: bool,
):
    dst_msg_id = db_get_dst_msg(src_msg.chat.id, src_msg.message_id, dest_chat_id)
    if not dst_msg_id:
        return

    if src_msg.text:
        name = sender_display_name(src_msg)
        pref = prefix_block(name)
        if do_translate and TRANSLATE:
            html_text, _ = await translate_visible_html(src_msg.text or "", src_msg.entities or [])
        else:
            html_text = build_html_no_translate(src_msg.text or "", src_msg.entities or [])
        html_text = pref + html_text

        try:
            await call_with_retry(
                "edit_message_text",
                lambda: context.bot.edit_message_text(
                    chat_id=dest_chat_id,
                    message_id=dst_msg_id,
                    text=html_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                ),
            )
        except BadRequest as e:
            log.warning("edit_message_text failed -> try caption: %s", e)

    cap = (src_msg.caption or "").strip()
    if cap:
        name = sender_display_name(src_msg)
        pref = prefix_block(name)
        if do_translate and TRANSLATE:
            cap_html, _ = await translate_visible_html(src_msg.caption or "", src_msg.caption_entities or [])
        else:
            cap_html = build_html_no_translate(src_msg.caption or "", src_msg.caption_entities or [])
        cap_html = cap_with_prefix(pref, cap_html, max_len=1024)

        await call_with_retry(
            "edit_message_caption",
            lambda: context.bot.edit_message_caption(
                chat_id=dest_chat_id,
                message_id=dst_msg_id,
                caption=cap_html,
                parse_mode=ParseMode.HTML,
            ),
        )


# ================== COMANDOS DE EDICIÓN (opcionales) ==================
ADMIN_SET = {ANON_ADMIN_ID}
if ADMIN_ID:
    ADMIN_SET.add(ADMIN_ID)

PENDING_MEDIA: Dict[int, Dict[str, Any]] = {}


def _is_admin(uid: Optional[int]) -> bool:
    return bool(uid) and (uid in ADMIN_SET)


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(getattr(user, "id", None)):
        return
    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text("Uso: /edit <message_id> <texto nuevo>")
        return
    try:
        msg_id = int(context.args[0])
    except Exception:
        await update.effective_message.reply_text("message_id inválido.")
        return
    new_text = " ".join(context.args[1:]).strip()
    if not new_text:
        await update.effective_message.reply_text("El texto no puede estar vacío.")
        return

    chat_id = update.effective_chat.id
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=new_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await update.effective_message.reply_text("✅ Texto editado.")
        return
    except Exception as e1:
        try:
            await context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=msg_id,
                caption=new_text,
                parse_mode=ParseMode.HTML,
            )
            await update.effective_message.reply_text("✅ Caption editado.")
            return
        except Exception as e2:
            log.warning("edit failed text=%s caption=%s", e1, e2)
            await update.effective_message.reply_text("⚠️ No se pudo editar. Verifica el ID y que el mensaje sea del bot.")


async def cmd_editmedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(getattr(user, "id", None)):
        return
    if not context.args or len(context.args) < 1:
        await update.effective_message.reply_text(
            "Uso: /editmedia <message_id>\nDespués envía la nueva foto/video/documento/audio (con caption opcional)."
        )
        return
    try:
        msg_id = int(context.args[0])
    except Exception:
        await update.effective_message.reply_text("message_id inválido.")
        return
    PENDING_MEDIA[user.id] = {"chat_id": update.effective_chat.id, "message_id": msg_id}
    await update.effective_message.reply_text("Ok. Envía ahora el nuevo medio (foto/video/documento/audio).")


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
        await replicate_message(context, msg, dst, None, do_translate=True)
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

        # ✅ Dedup
        if seen_recent(chat.id, msg.message_id):
            return

        # ✅ Anti-loop: si viene desde un tema destino, no replicar
        if is_destination_topic(chat.id, msg.message_thread_id):
            return

        thread_id = msg.message_thread_id
        sender_id = msg.from_user.id if msg.from_user else None
        tid_norm = thread_id if thread_id is not None else 1


        route = map_topic(chat.id, thread_id, sender_id)
        if not route:
            return
        dst_chat, dst_thread = route

        do_translate_main = not route_no_translate(chat.id, thread_id, dst_chat, dst_thread)

        log.info(
            "Group %s#%s → %s#%s | translate=%s | msg %s",
            chat.id,
            thread_id if thread_id is not None else 1,
            dst_chat,
            dst_thread,
            do_translate_main,
            msg.message_id,
        )

        try:
            await replicate_message(context, msg, dst_chat, dst_thread, do_translate=do_translate_main, forced_reply_to_message_id=None)
        except Exception as e:
            log.warning("Fallo ruta principal %s#%s -> %s#%s: %s", chat.id, thread_id, dst_chat, dst_thread, e)
            await alert_error(context, f"Ruta principal fallo: {chat.id}#{thread_id} -> {dst_chat}#{dst_thread}\n{e}")

        extras = FANOUT_ROUTES.get((chat.id, tid_norm), [])
        for extra_chat, extra_thread in extras:
            do_translate_extra = not route_no_translate(chat.id, thread_id, extra_chat, extra_thread)
            log.info(
                "Fanout %s#%s → %s#%s | translate=%s | msg %s",
                chat.id,
                tid_norm,
                extra_chat,
                extra_thread,
                do_translate_extra,
                msg.message_id,
            )
            try:
                reply_fixed = None
                if isinstance(extra_chat, int):
                    reply_fixed = FIXED_REPLY_TO.get((chat.id, tid_norm, int(extra_chat), int(extra_thread)))
                try:
                    await replicate_message(context, msg, extra_chat, extra_thread, do_translate=do_translate_extra, forced_reply_to_message_id=(None if (chat.id == G1 and tid_norm == 129) else reply_fixed))
                except BadRequest as be:
                    if reply_fixed is not None and 'Message to be replied not found' in str(be):
                        # Fallback: enviar sin reply fijo
                        await replicate_message(context, msg, extra_chat, extra_thread, do_translate=do_translate_extra, forced_reply_to_message_id=(0 if (chat.id == G1 and tid_norm == 129) else None))
                    else:
                        raise

            except Exception as e:
                log.warning("Fallo fanout %s#%s -> %s#%s: %s", chat.id, tid_norm, extra_chat, extra_thread, e)
                await alert_error(context, f"Fanout fallo: {chat.id}#{tid_norm} -> {extra_chat}#{extra_thread}\n{e}")

    except Exception as e:
        log.exception("Error on_group_post")
        await alert_error(context, f"on_group_post: {e}")


async def on_group_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.edited_message
        chat = update.effective_chat
        if not msg or not chat:
            return
        if chat.type not in (ChatType.SUPERGROUP, ChatType.GROUP):
            return

        # ✅ Dedup edits
        if seen_recent(chat.id, msg.message_id):
            return

        # ✅ Anti-loop edits
        if is_destination_topic(chat.id, msg.message_thread_id):
            return

        thread_id = msg.message_thread_id
        sender_id = msg.from_user.id if msg.from_user else None

        route = map_topic(chat.id, thread_id, sender_id)
        if not route:
            return
        dst_chat, dst_thread = route
        if not isinstance(dst_chat, int):
            return

        do_translate_main = not route_no_translate(chat.id, thread_id, dst_chat, dst_thread)

        log.info(
            "EDIT Group %s#%s → %s#%s | translate=%s | msg %s",
            chat.id,
            thread_id if thread_id is not None else 1,
            dst_chat,
            dst_thread,
            do_translate_main,
            msg.message_id,
        )

        await replicate_edit(context, msg, dst_chat, dst_thread, do_translate=do_translate_main)

    except Exception as e:
        log.exception("Error on_group_edit")
        await alert_error(context, f"on_group_edit: {e}")


# ================== MAIN ==================
def ensure_env():
    if not BOT_TOKEN:
        raise RuntimeError("Falta BOT_TOKEN")


def main():
    ensure_env()
    db_init()

    request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=20.0,
    )

    app = Application.builder().token(BOT_TOKEN).request(request).build()
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, on_group_post))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.UpdateType.EDITED_MESSAGE, on_group_edit))

    # Opcional
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("editmedia", cmd_editmedia))

    log.info(
        "Replicator iniciado. Translate=%s, Buttons=%s | ENV_SRC=%s ENV_DST=%s | DB=%s | DedupTTL=%ss",
        TRANSLATE,
        TRANSLATE_BUTTONS,
        ENV_SRC,
        ENV_DST,
        str(DB_PATH),
        str(DEDUP_TTL_SECONDS),
    )

    app.run_polling(
        allowed_updates=["channel_post", "message", "edited_message"],
        poll_interval=1.2,
        stop_signals=None,
        drop_pending_updates=True

    )


if __name__ == "__main__":
    main()
