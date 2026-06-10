"""Определение размерной сетки на 4-м фото карточки через Claude Vision.

Скачивает изображение по ссылке из Content API и спрашивает у vision-модели,
есть ли на нём размерная сетка (таблица размеров). Результат кэшируется по
URL фото, чтобы не дёргать модель при каждом пересчёте чек-листа.
"""

import os
import json
import base64
import hashlib
import httpx

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# Дешёвая и быстрая модель достаточно для бинарного «да/нет»
VISION_MODEL = os.environ.get("VISION_MODEL", "claude-haiku-4-5-20251001")
CACHE_PATH = os.environ.get("VISION_CACHE", "vision_cache.json")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ALLOWED_MEDIA = {"image/jpeg", "image/png", "image/gif", "image/webp"}

PROMPT = (
    "Это одно из фото карточки товара на Wildberries. "
    "На изображении есть размерная сетка — таблица размеров "
    "(колонки/строки с размерами S/M/L или числами и обхватами/длиной в см)? "
    "Ответь строго одним словом: да или нет."
)


def enabled():
    return bool(ANTHROPIC_API_KEY)


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


def detect_size_grid(photo_url):
    """True/False — есть ли размерная сетка. None, если определить не удалось."""
    if not ANTHROPIC_API_KEY or not photo_url:
        return None

    cache = _load_cache()
    key = hashlib.sha1(photo_url.encode()).hexdigest()
    if key in cache:
        return cache[key]

    try:
        img = httpx.get(photo_url, timeout=30, follow_redirects=True)
        if img.status_code != 200:
            print(f"Vision: фото {photo_url} -> {img.status_code}")
            return None
        media_type = (img.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
        if media_type not in _ALLOWED_MEDIA:
            media_type = "image/jpeg"
        b64 = base64.b64encode(img.content).decode()

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
        result = text.startswith("да") or text.startswith("yes")
        cache[key] = result
        _save_cache(cache)
        return result
    except Exception as e:
        print(f"Vision: ошибка анализа {photo_url}: {e}")
        return None
