import os
import html
import logging
import re
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
}

# ================== OVERRIDE DE TRADUCCIÓN POR RUTA ==================
NO_TRANSLATE_ROUTES: set[Tuple[int, int, int, int]] = {
    # G1 → G3#2 en ES
    (G1, 2890, G3, 2),
    (G1, 17373, G3, 2),
}

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
hedging\hedging
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


async def deepl_translate(text: str, *, session: aiohttp.ClientSession) -> str:
    if not text.strip():
        return text
    if not TRANSLATE or not DEEPL_API_KEY:
        return text
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
        return js["translations"][0]["text"]

# ================== OPENAI STT/TTS (AUDIO) ==================
async def openai_transcribe(audio_bytes: bytes, filename: str, mime: str, *, language_hint: str) -> str:
    """
    Speech-to-text con OpenAI (Whisper). Devuelve texto en el idioma original.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("Falta OPENAI_API_KEY para transcribir audio.")
    url = f"{OPENAI_BASE_URL}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    form = aiohttp.FormData()
    form.add_field("model", OPENAI_STT_MODEL)
    # Whisper usa language como 'es', 'en', etc. (mejor esfuerzo)
    if language_hint:
        form.add_field("language", language_hint.lower())
    form.add_field("file", audio_bytes, filename=filename, content_type=mime or "application/octet-stream")

    timeout = aiohttp.ClientTimeout(total=OPENAI_TIMEOUT_SEC)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, data=form) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"OpenAI STT HTTP {resp.status}: {body[:400]}")
            js = await resp.json()
            return (js.get("text") or "").strip()

async def openai_tts(text_en: str) -> bytes:
    """
    Text-to-speech con OpenAI. Devuelve bytes de audio (mp3 por defecto).
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("Falta OPENAI_API_KEY para generar audio (TTS).")
    if not text_en.strip():
        return b""
    url = f"{OPENAI_BASE_URL}/audio/speech"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_TTS_MODEL,
        "voice": OPENAI_TTS_VOICE,
        "input": text_en,
        "format": OPENAI_TTS_FORMAT,
    }

    timeout = aiohttp.ClientTimeout(total=OPENAI_TIMEOUT_SEC)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"OpenAI TTS HTTP {resp.status}: {body[:400]}")
            return await resp.read()

async def replicate_audio_with_translation(
    context: ContextTypes.DEFAULT_TYPE,
    src_msg: Message,
    dest_chat_id: int | str,
    dest_thread_id: Optional[int],
    *,
    do_translate: bool,
):
    """Replica voice/audio manteniendo el audio ORIGINAL y agregando SOLO el texto traducido en el caption.

    - No genera TTS (no cambia voz).
    - El texto va pegado al audio (caption), no en un mensaje aparte.
    - Si falla STT/DeepL, hace fallback a la réplica normal.
    """
    try:
        if not AUDIO_TRANSLATE or not do_translate:
            await replicate_media_with_album_support(context, src_msg, dest_chat_id, dest_thread_id, do_translate=do_translate)
            return

        is_voice = bool(getattr(src_msg, "voice", None))
        is_audio = bool(getattr(src_msg, "audio", None))
        if not (is_voice or is_audio):
            await replicate_media_with_album_support(context, src_msg, dest_chat_id, dest_thread_id, do_translate=do_translate)
            return

        if not OPENAI_API_KEY:
            await replicate_media_with_album_support(context, src_msg, dest_chat_id, dest_thread_id, do_translate=do_translate)
            return

        # Descargar bytes desde Telegram
        file_id = src_msg.voice.file_id if is_voice else src_msg.audio.file_id
        tg_file = await context.bot.get_file(file_id)
        audio_url = tg_file.file_path

        timeout = aiohttp.ClientTimeout(total=max(30, int(OPENAI_TIMEOUT_SEC or 60)))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(audio_url) as r:
                if r.status != 200:
                    raise RuntimeError(f"Telegram get_file HTTP {r.status}")
                audio_bytes = await r.read()

            # STT (OpenAI): devuelve texto en el idioma original
            # Nota: openai_transcribe abre su propia sesión por dentro.
            transcript = await openai_transcribe(
                audio_bytes=audio_bytes,
                filename=("voice.ogg" if is_voice else "audio.bin"),
                mime=("audio/ogg" if is_voice else "application/octet-stream"),
                language_hint=(SOURCE_LANG or "ES").lower(),
            )
            transcript = (transcript or "").strip()
            if not transcript:
                await replicate_media_with_album_support(context, src_msg, dest_chat_id, dest_thread_id, do_translate=do_translate)
                return

            # Traducir a EN con DeepL
            translated = await deepl_translate(transcript, session=session)
            translated = (translated or transcript).strip()

        # Caption máximo ~1024. Dejamos margen.
        caption = translated[:1000]

        kb = await translate_buttons(src_msg.reply_markup, do_translate=True)

        # Enviar audio ORIGINAL con caption traducido
        if is_voice:
            await context.bot.send_voice(
                chat_id=dest_chat_id,
                message_thread_id=dest_thread_id,
                voice=file_id,
                caption=caption,
                reply_markup=kb,
            )
        else:
            await context.bot.send_audio(
                chat_id=dest_chat_id,
                message_thread_id=dest_thread_id,
                audio=file_id,
                caption=caption,
                reply_markup=kb,
            )

    except Exception as e:
        log.warning("Audio caption-translate fallback (msg %s): %s", getattr(src_msg, "message_id", "?"), e)
        await replicate_media_with_album_support(context, src_msg, dest_chat_id, dest_thread_id, do_translate=do_translate)


async def replicate_media_with_album_support(
    context: ContextTypes.DEFAULT_TYPE,
    src_msg: Message,
    dest_chat_id: int | str,
    dest_thread_id: Optional[int],
    *,
    do_translate: bool,
):
    mgid = getattr(src_msg, "media_group_id", None)
    if not mgid:
        reply_to_id = resolve_reply_to_id(src_msg, int(dest_chat_id)) if isinstance(dest_chat_id, int) else None
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
):
    # Anti-loop interno: si ya es del bot, no repliques
    if is_from_bot(src_msg, context):
        return

    reply_to_id = None
    if isinstance(dest_chat_id, int):
        reply_to_id = resolve_reply_to_id(src_msg, dest_chat_id)

    
    # --- AUDIO: transcribir + traducir + reenviar como audio EN + texto EN ---
    if (getattr(src_msg, "voice", None) or getattr(src_msg, "audio", None)) and do_translate:
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
        context, src_msg, dest_chat_id, dest_thread_id, do_translate=do_translate
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
            await replicate_message(context, msg, dst_chat, dst_thread, do_translate=do_translate_main)
        except Exception as e:
            log.warning("Fallo ruta principal %s#%s -> %s#%s: %s", chat.id, thread_id, dst_chat, dst_thread, e)
            await alert_error(context, f"Ruta principal fallo: {chat.id}#{thread_id} -> {dst_chat}#{dst_thread}\n{e}")

        tid_norm = thread_id if thread_id is not None else 1
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
                await replicate_message(context, msg, extra_chat, extra_thread, do_translate=do_translate_extra)
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
