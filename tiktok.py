"""Автопубликация роликов в TikTok через официальный Content Posting API.

Цепочка работы:
  1. Один раз авторизуем аккаунт по OAuth (кнопка «Подключить TikTok» на странице
     /tiktok). TikTok возвращает access_token + refresh_token — храним в файле.
  2. Складываем ролики в очередь (видео по публичному URL + подпись + время).
  3. Планировщик раз в минуту дёргает process_due(): берёт «созревшие» по времени
     записи и публикует их через Direct Post.

Режим публикации задаётся переменной TIKTOK_PRIVACY:
  SELF_ONLY          — приватно/черновик (работает в sandbox до аудита) ← по умолчанию
  PUBLIC_TO_EVERYONE — публично в ленту (доступно после одобрения аудита приложения)

Видео отдаётся TikTok по ссылке (PULL_FROM_URL). Домен ссылки должен быть
подтверждён в настройках приложения (URL properties), иначе init вернёт ошибку.
"""

import os
import json
import time
import threading
from datetime import datetime, timezone

import httpx

CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
# Базовый публичный адрес приложения (Railway), например https://app.up.railway.app
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
# Режим приватности публикуемых видео (см. модульный docstring)
PRIVACY = os.environ.get("TIKTOK_PRIVACY", "SELF_ONLY").strip()

REDIRECT_URI = f"{PUBLIC_BASE_URL}/tiktok/callback" if PUBLIC_BASE_URL else ""
SCOPES = "user.info.basic,video.publish,video.upload"

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

TOKENS_FILE = "tiktok_tokens.json"
QUEUE_FILE = "tiktok_queue.json"

_lock = threading.Lock()


def enabled():
    """Заданы ли ключи приложения и публичный адрес для редиректа."""
    return bool(CLIENT_KEY and CLIENT_SECRET and REDIRECT_URI)


# ===================== ХРАНИЛИЩЕ (простые JSON-файлы) =====================

def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_tokens():
    return _read_json(TOKENS_FILE, {})


def _save_tokens(data):
    _write_json(TOKENS_FILE, data)


def is_connected():
    return bool(_load_tokens().get("refresh_token"))


# ===================== OAUTH =====================

def auth_url(state="joto"):
    """Ссылка, по которой пользователь подтверждает доступ к своему аккаунту."""
    from urllib.parse import urlencode
    params = {
        "client_key": CLIENT_KEY,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code):
    """Меняет одноразовый code (из callback) на access/refresh токены."""
    resp = httpx.post(
        TOKEN_URL,
        data={
            "client_key": CLIENT_KEY,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    data = resp.json()
    if not data.get("access_token"):
        print(f"TikTok: ошибка обмена code: {resp.status_code} {resp.text[:300]}")
        return False, data
    _store_token_response(data)
    return True, data


def _store_token_response(data):
    now = int(time.time())
    tokens = {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "open_id": data.get("open_id"),
        "scope": data.get("scope"),
        # expires_in — секунды жизни access_token (обычно 86400)
        "access_expires_at": now + int(data.get("expires_in", 0)),
    }
    _save_tokens(tokens)


def _refresh():
    """Обновляет access_token по refresh_token. Возвращает True при успехе."""
    tokens = _load_tokens()
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return False
    resp = httpx.post(
        TOKEN_URL,
        data={
            "client_key": CLIENT_KEY,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    data = resp.json()
    if not data.get("access_token"):
        print(f"TikTok: ошибка refresh: {resp.status_code} {resp.text[:300]}")
        return False
    _store_token_response(data)
    return True


def valid_access_token():
    """Действующий access_token (обновляет за 2 минуты до истечения), либо None."""
    tokens = _load_tokens()
    if not tokens.get("access_token"):
        return None
    if int(time.time()) >= int(tokens.get("access_expires_at", 0)) - 120:
        if not _refresh():
            return None
        tokens = _load_tokens()
    return tokens.get("access_token")


def account_info():
    """Инфа об авторизованном авторе (имя, лимиты приватности) — для UI и проверок."""
    token = valid_access_token()
    if not token:
        return {"connected": False}
    try:
        resp = httpx.post(
            CREATOR_INFO_URL,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            timeout=30,
        )
        data = resp.json().get("data", {})
        return {
            "connected": True,
            "nickname": data.get("creator_nickname"),
            "privacy_options": data.get("privacy_level_options", []),
            "max_video_seconds": data.get("max_video_post_duration_sec"),
        }
    except Exception as e:
        print(f"TikTok: creator_info ошибка: {e}")
        return {"connected": True, "error": str(e)}


# ===================== ОЧЕРЕДЬ ПУБЛИКАЦИЙ =====================

def get_queue():
    return _read_json(QUEUE_FILE, [])


def add_to_queue(video_url, caption, publish_at):
    """Ставит ролик в очередь.

    publish_at — строка ISO в UTC (например 2026-06-15T18:30). Сравнивается с
    текущим UTC-временем сервера. МСК = UTC+3, учитывай при выборе времени.
    """
    with _lock:
        queue = get_queue()
        queue.append({
            "id": int(time.time() * 1000),
            "video_url": video_url.strip(),
            "caption": (caption or "").strip(),
            "publish_at": publish_at.strip(),
            "status": "scheduled",
            "publish_id": None,
            "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        _write_json(QUEUE_FILE, queue)
    return True


def remove_from_queue(item_id):
    with _lock:
        queue = [i for i in get_queue() if i.get("id") != item_id]
        _write_json(QUEUE_FILE, queue)
    return True


def _update_item(item_id, **fields):
    with _lock:
        queue = get_queue()
        for i in queue:
            if i.get("id") == item_id:
                i.update(fields)
                break
        _write_json(QUEUE_FILE, queue)


# ===================== ПУБЛИКАЦИЯ =====================

def publish_item(item):
    """Создаёт задачу публикации Direct Post. Возвращает (ok, publish_id|ошибка)."""
    token = valid_access_token()
    if not token:
        return False, "Нет действующего токена — переавторизуй TikTok"

    payload = {
        "post_info": {
            "title": item.get("caption", ""),
            "privacy_level": PRIVACY,
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": item["video_url"],
        },
    }
    try:
        resp = httpx.post(
            INIT_URL,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        data = resp.json()
        err = (data.get("error") or {}).get("code")
        if err and err != "ok":
            msg = (data.get("error") or {}).get("message", "")
            print(f"TikTok: init ошибка {err}: {msg}")
            return False, f"{err}: {msg}"
        publish_id = (data.get("data") or {}).get("publish_id")
        if not publish_id:
            return False, f"нет publish_id: {resp.text[:200]}"
        return True, publish_id
    except Exception as e:
        print(f"TikTok: исключение при публикации: {e}")
        return False, str(e)


def process_due():
    """Запускается планировщиком: публикует все записи, у которых время пришло."""
    if not enabled() or not is_connected():
        return
    now = datetime.now(timezone.utc)
    for item in get_queue():
        if item.get("status") != "scheduled":
            continue
        when = _parse_dt(item.get("publish_at"))
        if when is None or when > now:
            continue
        print(f"TikTok: публикую #{item['id']} (план {item.get('publish_at')})")
        ok, result = publish_item(item)
        if ok:
            _update_item(item["id"], status="published", publish_id=result, error=None)
            print(f"TikTok: ролик #{item['id']} отправлен, publish_id={result}")
        else:
            _update_item(item["id"], status="error", error=str(result))
            print(f"TikTok: ролик #{item['id']} ошибка: {result}")


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        # время без зоны считаем UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None
