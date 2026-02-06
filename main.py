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
SOURCE_LANG = os.getenv("SOURCE_LANG", "ES").strip().upper()
TARGET_LANG = os.getenv("TARGET_LANG", "EN").strip().upper()
FORCE_TRANSLATE = os.getenv("FORCE_TRANSLATE", "false").lower() == "true"
FORMALITY = os.getenv("DEEPL_FORMALITY", "prefer_more").strip()

GLOSSARY_ID = os.getenv("DEEPL_GLOSSARY_ID", "").strip()
GLOSSARY_TSV = os.getenv("DEEPL_GLOSSARY_TSV", "").strip()

# DeepL formality supported languages
DEEPL_FORMALITY_LANGS = {"DE", "FR", "IT", "ES", "NL", "PL", "PT", "RU"}

# ================== LOG ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("replicator")

# ================== GRUPOS / TOPICS ==================
# Deben ser enteros (ids negativos) tal cual los reporta Telegram (chat.id)
G1 = int(os.getenv("G1", "-1001946870620"))
G2 = int(os.getenv("G2", "-1002127373425"))
G3 = int(os.getenv("G3", "-1002127373425"))  # si usas mismo grupo para topics internos
G4 = int(os.getenv("G4", "-1001958300000"))
G5 = int(os.getenv("G5", "-1001958311111"))

# Admin IDs
ADMIN_ID = int(os.getenv("ADMIN_ID", "5958164558"))
# si aplica para filtrar Chat ES→EN)
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
    # ✅ G1#129 también se replica a G3#3 (ES)
    (G1, 129): [(G3, 3)],
}

# ================== OVERRIDE DE TRADUCCIÓN POR RUTA ==================
NO_TRANSLATE_ROUTES: set[Tuple[int, int, int, int]] = {
    # G1 → G3#2 en ES
    (G1, 2890, G3, 2),
    (G1, 17373, G3, 2),
    # ✅ G1#129 → G3#3 en ES
    (G1, 129, G3, 3),
}

# ================== ANTI-LOOP: NO replicar desde destinos ==================
DEST_TOPIC_SET: set[Tuple[int, int]] = set()
for (_src_chat, _src_thread), (_dst_chat, _dst_thread, _only_sender) in TOPIC_ROUTES.items():
    DEST_TOPIC_SET.add((_dst_chat, _dst_thread))

def is_destination_topic(chat_id: int, thread_id: Optional[int]) -> bool:
    if thread_id is None:
        thread_id = 1
    return (chat_id, thread_id) in DEST_TOPIC_SET

# ================== DEDUP ==================
_SEEN_DB = Path("seen.db")
_seen_conn = sqlite3.connect(_SEEN_DB)
_seen_conn.execute(
    "CREATE TABLE IF NOT EXISTS seen (chat_id INTEGER, msg_id INTEGER, ts INTEGER, PRIMARY KEY(chat_id,msg_id))"
)
_seen_conn.commit()

def seen_recent(chat_id: int, msg_id: int, *, ttl_seconds: int = 600) -> bool:
    now = int(asyncio.get_event_loop().time())
    cur = _seen_conn.cursor()
    cur.execute("SELECT ts FROM seen WHERE chat_id=? AND msg_id=?", (chat_id, msg_id))
    row = cur.fetchone()
    if row and (now - int(row[0]) < ttl_seconds):
        return True
    cur.execute("INSERT OR REPLACE INTO seen(chat_id,msg_id,ts) VALUES (?,?,?)", (chat_id, msg_id, now))
    _seen_conn.commit()
    cur.execute("DELETE FROM seen WHERE ts < ?", (now - ttl_seconds,))
    _seen_conn.commit()
    return False

# ================== TRADUCCIÓN ==================
_ES_MARKERS = re.compile(r"[áéíóúñÁÉÍÓÚÑ]|\\b(el|la|los|las|para|porque|señales|sesiones|canal|pide|acceso)\\b", re.I)
_EN_COMMON = re.compile(r"\\b(the|and|for|with|profit|session|share|channel|request|access)\\b", re.I)

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



# ================== HIGIENE DE TEXTO PARA TRADUCCIÓN ==================
_ZERO_WIDTH_RE = re.compile(r"[\u200B-\u200D\uFEFF]")

# Emojis comunes del copy (si van pegados a una palabra, DeepL a veces se confunde)
_EMOJI_SPACING_RE = re.compile(r"([^\s])([🔥💥📊🤖📲🪙✍️↓✅❌])")

def clean_for_translation(t: str) -> str:
    if not t:
        return t
    # Normaliza unicode y elimina caracteres invisibles
    t = unicodedata.normalize("NFKC", t)
    t = _ZERO_WIDTH_RE.sub("", t)
    # Separa emojis pegados a palabras
    t = _EMOJI_SPACING_RE.sub(r"\1 \2", t)
    # Compacta espacios
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()

def postprocess_translation(t: str) -> str:
    # Ajustes suaves para que suene natural (sin cambiar el sentido)
    if not t:
        return t
    t = t.replace("to take them", "to take these trades")
    t = t.replace("technical analysis to take", "technical analysis to trade")
    return t


# ================== ENTIDADES HTML ==================
SAFE_TAGS = {"b", "strong", "i", "em", "u", "s", "del", "code", "pre", "a"}


def escape(text: str) -> str:
    return html.escape(text or "")


def sanitize_html(html_text: str) -> str:
    if not html_text:
        return html_text
    # muy básico: elimina tags no seguros
    return re.sub(r"</?([a-zA-Z0-9]+)(?:\\s[^>]*)?>", lambda m: m.group(0) if m.group(1).lower() in SAFE_TAGS else "", html_text)


# ================== GLOSSARY ==================
DEFAULT_GLOSSARY_TSV = """\
sesión\t session
sesiones\t sessions
señales\t signals
pide acceso\t request access
canal\t channel
broker\t broker
binomo\t Binomo
"""

_glossary_id_mem: Optional[str] = None


async def deepl_create_glossary_if_needed() -> Optional[str]:
    global _glossary_id_mem
    if _glossary_id_mem:
        return _glossary_id_mem
    tsv = GLOSSARY_TSV or DEFAULT_GLOSSARY_TSV
    if not tsv.strip():
        return None
    url = f"https://{DEEPL_API_HOST}/v2/glossaries"
    data = {
        "auth_key": DEEPL_API_KEY,
        "name": "tg_replicator_glossary",
        "source_lang": SOURCE_LANG,
        "target_lang": TARGET_LANG,
        "entries_format": "tsv",
        "entries": tsv,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=data) as r:
            b = await r.text()
            if r.status != 200:
                log.warning("DeepL glossary HTTP %s: %s", r.status, b)
                return None
            js = await r.json()
            _glossary_id_mem = js.get("glossary_id")
            return _glossary_id_mem


async def deepl_translate(text: str, *, session: aiohttp.ClientSession) -> str:
    if not text.strip():
        return text
    if not TRANSLATE or not DEEPL_API_KEY:
        return text
    # Limpieza mínima para evitar traducciones raras (emojis pegados, chars invisibles, etc.)
    text = clean_for_translation(text)

    # ✅ opción A: si ya es inglés y no forzamos, se deja tal cual
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
        out = js["translations"][0]["text"]
        return postprocess_translation(out)

# ================== OPENAI STT/TTS (AUDIO) ====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_STT_MODEL = os.getenv("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe").strip()
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts").strip()
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "alloy").strip()

# ================== HELPERS DE RUTEO ==================
def map_topic(src_chat: int, src_thread: Optional[int], sender_id: Optional[int]) -> Optional[Tuple[int, int]]:
    if src_thread is None:
        src_thread = 1
    route = TOPIC_ROUTES.get((src_chat, src_thread))
    if not route:
        return None
    dst_chat, dst_thread, only_sender = route
    if only_sender is not None and sender_id != only_sender:
        return None
    return (dst_chat, dst_thread)

def get_fanout(src_chat: int, src_thread: Optional[int]) -> List[Tuple[int, int]]:
    if src_thread is None:
        src_thread = 1
    return FANOUT_ROUTES.get((src_chat, src_thread), [])

def route_no_translate(src_chat: int, src_thread: Optional[int], dst_chat: int, dst_thread: int) -> bool:
    if src_thread is None:
        src_thread = 1
    return (src_chat, src_thread, dst_chat, dst_thread) in NO_TRANSLATE_ROUTES

# ================== REPLICACIÓN DE MEDIOS ==================
async def copy_message_content(
    msg: Message,
    *,
    app: Application,
    dst_chat_id: int,
    dst_thread_id: Optional[int],
    do_translate: bool,
    session: aiohttp.ClientSession,
) -> None:
    caption = msg.caption or ""
    text = msg.text or ""
    entities = msg.entities or []
    cap_entities = msg.caption_entities or []

    # Texto (solo si es mensaje textual)
    if text:
        out_text = text
        if do_translate:
            out_text = await deepl_translate(out_text, session=session)
        await app.bot.send_message(
            chat_id=dst_chat_id,
            message_thread_id=dst_thread_id,
            text=out_text,
        )
        return

    # Foto/Video/Documento/Audio con caption
    out_caption = caption
    if caption and do_translate:
        out_caption = await deepl_translate(out_caption, session=session)

    if msg.photo:
        await app.bot.send_photo(
            chat_id=dst_chat_id,
            message_thread_id=dst_thread_id,
            photo=msg.photo[-1].file_id,
            caption=out_caption or None,
        )
        return

    if msg.video:
        await app.bot.send_video(
            chat_id=dst_chat_id,
            message_thread_id=dst_thread_id,
            video=msg.video.file_id,
            caption=out_caption or None,
        )
        return

    if msg.document:
        await app.bot.send_document(
            chat_id=dst_chat_id,
            message_thread_id=dst_thread_id,
            document=msg.document.file_id,
            caption=out_caption or None,
        )
        return

    if msg.audio:
        await app.bot.send_audio(
            chat_id=dst_chat_id,
            message_thread_id=dst_thread_id,
            audio=msg.audio.file_id,
            caption=out_caption or None,
        )
        return

    if msg.voice:
        await app.bot.send_voice(
            chat_id=dst_chat_id,
            message_thread_id=dst_thread_id,
            voice=msg.voice.file_id,
            caption=out_caption or None,
        )
        return

    # fallback
    if caption:
        await app.bot.send_message(
            chat_id=dst_chat_id,
            message_thread_id=dst_thread_id,
            text=out_caption,
        )

# ================== HANDLER PRINCIPAL ==================
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

        async with aiohttp.ClientSession() as session:
            await copy_message_content(
                msg,
                app=context.application,
                dst_chat_id=dst_chat,
                dst_thread_id=dst_thread,
                do_translate=do_translate_main,
                session=session,
            )

            # Fan-out extra
            fanouts = get_fanout(chat.id, thread_id)
            for (fo_chat, fo_thread) in fanouts:
                do_translate_fo = not route_no_translate(chat.id, thread_id, fo_chat, fo_thread)
                log.info(
                    "Fan-out %s#%s → %s#%s | translate=%s | msg %s",
                    chat.id,
                    thread_id if thread_id is not None else 1,
                    fo_chat,
                    fo_thread,
                    do_translate_fo,
                    msg.message_id,
                )
                await copy_message_content(
                    msg,
                    app=context.application,
                    dst_chat_id=fo_chat,
                    dst_thread_id=fo_thread,
                    do_translate=do_translate_fo,
                    session=session,
                )

    except RetryAfter as e:
        await asyncio.sleep(float(e.retry_after) + 0.5)
    except (TimedOut, NetworkError):
        return
    except (BadRequest, Forbidden) as e:
        log.warning("Telegram error: %s", e)
        return
    except Exception as e:
        log.exception("Unhandled error: %s", e)

# ================== START / HEALTH ==================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Replicador activo.")

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing")
    request = HTTPXRequest(connect_timeout=20, read_timeout=45)
    app = Application.builder().token(BOT_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.ALL, on_message))

    log.info("Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
