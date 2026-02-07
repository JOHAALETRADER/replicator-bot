
# ===== TRANSLATION OPTIMIZATION LAYER (SAFE PATCH) =====
# Drop‑in helper functions to improve DeepL translation quality
# without modifying routing / fanout logic.

import re

EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002700-\U000027BF"
    "\U000024C2-\U0001F251"
    "]+", flags=re.UNICODE
)

def separate_emojis(text: str) -> str:
    return EMOJI_PATTERN.sub(lambda m: f" {m.group(0)} ", text)

TYPO_MAP = {
    "ghank": "thank",
    "forsatechnical": "technical",
    "forsatechnical analysis": "technical analysis",
}

def normalize_typos(text: str) -> str:
    low = text.lower()
    for k, v in TYPO_MAP.items():
        if k in low:
            text = re.sub(k, v, text, flags=re.IGNORECASE)
    return text

URL_PATTERN = re.compile(r'https?://\S+')

def protect_urls(text):
    urls = URL_PATTERN.findall(text)
    placeholders = {}
    for i, url in enumerate(urls):
        ph = f"__URL{i}__"
        text = text.replace(url, ph)
        placeholders[ph] = url
    return text, placeholders

def restore_urls(text, placeholders):
    for ph, url in placeholders.items():
        text = text.replace(ph, url)
    return text

def preprocess_for_translation(text: str):
    text = separate_emojis(text)
    text = normalize_typos(text)
    text, urls = protect_urls(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text, urls

def postprocess_translation(text: str, urls):
    text = restore_urls(text, urls)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
