"""Определение размерной сетки на 4-м фото карточки.

Режим выбирается автоматически по доступным ключам (от точного к запасному):
  1. Google Gemini — бесплатный тариф, хорошая точность. GEMINI_API_KEY.
  2. Claude Vision — точно, но платно. ANTHROPIC_API_KEY.
  3. OCR (Tesseract) — бесплатно, без ключей, точность пониже.

Результат кэшируется по URL фото, чтобы не анализировать одно и то же повторно.
"""

import os
import io
import json
import base64
import hashlib
import shutil
import threading
import httpx

# Кэш фото анализируется из нескольких потоков (фаза 2 чек-листа) — защищаем файл
_cache_lock = threading.Lock()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
VISION_MODEL = os.environ.get("VISION_MODEL", "claude-haiku-4-5-20251001")
# Кэш результатов распознавания. По умолчанию — на Railway-том (переживает
# редеплои), иначе /data или локальный файл. Можно переопределить VISION_CACHE.
_VISION_VOL = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
CACHE_PATH = (
    os.environ.get("VISION_CACHE")
    or (os.path.join(_VISION_VOL, "vision_cache.json") if _VISION_VOL
        else "/data/vision_cache.json" if os.path.isdir("/data")
        else "vision_cache.json")
)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_ALLOWED_MEDIA = {"image/jpeg", "image/png", "image/gif", "image/webp"}

PROMPT = (
    "Это одно из фото карточки товара на Wildberries. "
    "На изображении есть размерная сетка — таблица размеров "
    "(колонки/строки с размерами S/M/L или числами и обхватами/длиной в см)? "
    "Ответь строго одним словом: да или нет."
)

# Слова-маркеры размерной таблицы для OCR-режима
SIZE_KEYWORDS = [
    "размер", "обхват", "талия", "бедр", "груд", "длина", "ширина",
    "рост", "стелька", "размерная", "таблица размеров", " см", "size",
]


def _ocr_available():
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
        return shutil.which("tesseract") is not None
    except Exception:
        return False


def enabled():
    """Есть ли вообще способ определить сетку автоматически."""
    return bool(GEMINI_API_KEY) or bool(ANTHROPIC_API_KEY) or _ocr_available()


def mode():
    if GEMINI_API_KEY:
        return "gemini"
    if ANTHROPIC_API_KEY:
        return "vision"
    if _ocr_available():
        return "ocr"
    return "off"


def _load_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"Vision: не удалось сохранить кэш: {e}")


def _download(photo_url):
    r = httpx.get(photo_url, timeout=30, follow_redirects=True)
    if r.status_code != 200:
        print(f"Vision: фото {photo_url} -> {r.status_code}")
        return None, None
    media_type = (r.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if media_type not in _ALLOWED_MEDIA:
        media_type = "image/jpeg"
    return r.content, media_type


def _ocr_grid(content):
    """True/False по OCR. None при ошибке распознавания."""
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(io.BytesIO(content))
        text = pytesseract.image_to_string(img, lang="rus+eng").lower()
        hits = sum(1 for k in SIZE_KEYWORDS if k in text)
        return hits >= 2
    except Exception as e:
        print(f"Vision/OCR: ошибка распознавания: {e}")
        return None


def _gemini_grid(content, media_type):
    """True/False по Google Gemini. None при ошибке."""
    try:
        b64 = base64.b64encode(content).decode()
        resp = httpx.post(
            f"{GEMINI_URL}/{GEMINI_MODEL}:generateContent",
            params={"key": GEMINI_API_KEY},
            headers={"content-type": "application/json"},
            json={
                "contents": [{
                    "parts": [
                        {"inline_data": {"mime_type": media_type, "data": b64}},
                        {"text": PROMPT},
                    ],
                }],
                "generationConfig": {"maxOutputTokens": 8, "temperature": 0},
            },
            timeout=60,
        )
        if resp.status_code != 200:
            print(f"Vision/Gemini: {resp.status_code}: {resp.text[:200]}")
            return None
        cands = resp.json().get("candidates") or []
        if not cands:
            return None
        parts = (cands[0].get("content") or {}).get("parts") or [{}]
        text = parts[0].get("text", "").strip().lower()
        return text.startswith("да") or text.startswith("yes")
    except Exception as e:
        print(f"Vision/Gemini: ошибка анализа: {e}")
        return None


def _vision_grid(content, media_type):
    """True/False по Claude Vision. None при ошибке."""
    try:
        b64 = base64.b64encode(content).decode()
        resp = httpx.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": VISION_MODEL,
                "max_tokens": 8,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": media_type, "data": b64,
                        }},
                        {"type": "text", "text": PROMPT},
                    ],
                }],
            },
            timeout=60,
        )
        if resp.status_code != 200:
            print(f"Vision: Anthropic {resp.status_code}: {resp.text[:200]}")
            return None
        text = (resp.json().get("content") or [{}])[0].get("text", "").strip().lower()
        return text.startswith("да") or text.startswith("yes")
    except Exception as e:
        print(f"Vision: ошибка анализа: {e}")
        return None


def detect_size_grid(photo_url):
    """True/False — есть ли размерная сетка. None, если определить не удалось."""
    if not photo_url or not enabled():
        return None

    key = hashlib.sha1(photo_url.encode()).hexdigest()
    with _cache_lock:
        cache = _load_cache()
        if key in cache:
            return cache[key]

    try:
        content, media_type = _download(photo_url)
        if content is None:
            return None
        if GEMINI_API_KEY:
            result = _gemini_grid(content, media_type)
        elif ANTHROPIC_API_KEY:
            result = _vision_grid(content, media_type)
        else:
            result = _ocr_grid(content)
        if result is not None:
            with _cache_lock:
                cache = _load_cache()
                cache[key] = result
                _save_cache(cache)
        return result
    except Exception as e:
        print(f"Vision: ошибка анализа {photo_url}: {e}")
        return None
