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

import yandex

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

# Каталог для постоянного хранения токенов и очереди.
# На Railway указывай путь смонтированного тома (Volume), напр. DATA_DIR=/data —
# тогда токены и очередь переживут передеплой/перезапуск сервиса.
DATA_DIR = (os.environ.get("DATA_DIR", ".").strip() or ".")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError as e:
    print(f"TikTok: не удалось создать DATA_DIR={DATA_DIR}: {e}")
    DATA_DIR = "."

TOKENS_FILE = os.path.join(DATA_DIR, "tiktok_tokens.json")
QUEUE_FILE = os.path.join(DATA_DIR, "tiktok_queue.json")

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


def _new_item(**fields):
    item = {
        "id": int(time.time() * 1000),
        "source": "url",          # "url" (PULL_FROM_URL) или "yandex" (FILE_UPLOAD)
        "video_url": "",          # для source=url
        "yandex_public_key": "",  # для source=yandex
        "yandex_path": "",        # для source=yandex
        "name": "",
        "caption": "",
        "publish_at": "",
        "status": "scheduled",
        "publish_id": None,
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    item.update(fields)
    return item


def add_to_queue(video_url, caption, publish_at):
    """Ставит ролик в очередь по прямой ссылке (PULL_FROM_URL).

    publish_at — строка ISO в UTC (например 2026-06-15T18:30). Сравнивается с
    текущим UTC-временем сервера. МСК = UTC+3, учитывай при выборе времени.
    """
    with _lock:
        queue = get_queue()
        # небольшая пауза, чтобы id не совпали при пакетном добавлении
        time.sleep(0.001)
        queue.append(_new_item(
            source="url",
            video_url=video_url.strip(),
            caption=(caption or "").strip(),
            publish_at=publish_at.strip(),
        ))
        _write_json(QUEUE_FILE, queue)
    return True


def import_yandex_folder(folder_url, start_at, interval_hours, caption_template=""):
    """Импортирует видео из публичной папки Яндекс.Диска в очередь.

    Каждому ролику назначается время: start_at, start_at+interval, +2*interval …
    Уже добавленные ранее файлы (по пути) пропускаются. В подписи можно
    использовать {name} — подставится имя файла без расширения.
    Возвращает (количество_добавленных, всего_в_папке).
    """
    from datetime import timedelta
    folder_url = folder_url.strip()
    videos = yandex.list_videos(folder_url)
    start = _parse_dt(start_at) or datetime.now(timezone.utc)
    try:
        step = timedelta(hours=float(interval_hours))
    except (TypeError, ValueError):
        step = timedelta(hours=24)

    with _lock:
        queue = get_queue()
        existing = {i.get("yandex_path") for i in queue if i.get("source") == "yandex"}
        added = 0
        slot = start
        for v in videos:
            if v["path"] in existing:
                continue
            stem = v["name"].rsplit(".", 1)[0]
            caption = (caption_template or "").replace("{name}", stem)
            time.sleep(0.001)
            queue.append(_new_item(
                source="yandex",
                yandex_public_key=folder_url,
                yandex_path=v["path"],
                name=v["name"],
                caption=caption.strip(),
                publish_at=slot.isoformat(timespec="minutes"),
            ))
            existing.add(v["path"])
            slot = slot + step
            added += 1
        _write_json(QUEUE_FILE, queue)
    return added, len(videos)


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

def _post_info(caption):
    return {
        "title": caption or "",
        "privacy_level": PRIVACY,
        "disable_duet": False,
        "disable_comment": False,
        "disable_stitch": False,
    }


def _init(token, payload):
    """Дёргает publish/video/init. Возвращает (ok, data_или_ошибка)."""
    resp = httpx.post(
        INIT_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    data = resp.json()
    err = (data.get("error") or {}).get("code")
    if err and err != "ok":
        msg = (data.get("error") or {}).get("message", "")
        print(f"TikTok: init ошибка {err}: {msg}")
        return False, f"{err}: {msg}"
    return True, data.get("data") or {}


def publish_item(item):
    """Публикует ролик нужным способом. Возвращает (ok, publish_id|ошибка)."""
    token = valid_access_token()
    if not token:
        return False, "Нет действующего токена — переавторизуй TikTok"
    try:
        if item.get("source") == "yandex":
            return _publish_yandex(token, item)
        return _publish_pull(token, item)
    except Exception as e:
        print(f"TikTok: исключение при публикации: {e}")
        return False, str(e)


def _publish_pull(token, item):
    """Публикация по прямой ссылке (PULL_FROM_URL) — домен должен быть подтверждён."""
    ok, data = _init(token, {
        "post_info": _post_info(item.get("caption")),
        "source_info": {"source": "PULL_FROM_URL", "video_url": item["video_url"]},
    })
    if not ok:
        return False, data
    publish_id = data.get("publish_id")
    return (True, publish_id) if publish_id else (False, "нет publish_id")


# Размеры чанков по правилам TikTok: видео ≤ 64 МБ грузим одним куском,
# крупнее — кусками по 10 МБ (последний кусок забирает остаток).
_MB = 1024 * 1024


def _publish_yandex(token, item):
    """Скачивает файл с Яндекс.Диска и заливает в TikTok (FILE_UPLOAD)."""
    href = yandex.download_url(item["yandex_public_key"], item["yandex_path"])
    if not href:
        return False, "не удалось получить ссылку на файл Яндекс.Диска"
    video = yandex.download_bytes(href)
    size = len(video)
    if size == 0:
        return False, "пустой файл"

    if size <= 64 * _MB:
        chunk_size, total_chunks = size, 1
    else:
        chunk_size = 10 * _MB
        total_chunks = size // chunk_size  # последний кусок добирает остаток

    ok, data = _init(token, {
        "post_info": _post_info(item.get("caption")),
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
    })
    if not ok:
        return False, data
    publish_id = data.get("publish_id")
    upload_url = data.get("upload_url")
    if not publish_id or not upload_url:
        return False, "init не вернул upload_url"

    for idx in range(total_chunks):
        start = idx * chunk_size
        end = size - 1 if idx == total_chunks - 1 else start + chunk_size - 1
        chunk = video[start:end + 1]
        put = httpx.put(
            upload_url,
            content=chunk,
            headers={
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(len(chunk)),
            },
            timeout=300,
        )
        if put.status_code not in (200, 201, 206):
            return False, f"загрузка чанка {idx}: {put.status_code} {put.text[:200]}"
    return True, publish_id


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
