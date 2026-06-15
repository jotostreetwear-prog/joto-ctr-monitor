"""Чтение публичной папки Яндекс.Диска через Public Resources API (без авторизации).

Используется ботом, чтобы получить список видео в папке и прямую ссылку на
скачивание конкретного файла — дальше байты заливаются в TikTok (FILE_UPLOAD).

ВАЖНО: домены Яндекса должны быть в allowlist окружения, где крутится бот.
На Railway интернет открыт, поэтому там работает; в сборочной среде Claude —
нет (поэтому отсюда папку прочитать нельзя, только на задеплоенном боте).
"""

import httpx

API = "https://cloud-api.yandex.net/v1/disk/public/resources"
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm")


def list_videos(public_url):
    """Список видеофайлов верхнего уровня папки.

    Возвращает [{"name", "path", "size"}]. path — путь внутри публичной папки,
    его потом передаём в download_url().
    """
    items = []
    offset = 0
    while True:
        resp = httpx.get(
            API,
            params={"public_key": public_url, "limit": 200, "offset": offset},
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"Яндекс.Диск: ошибка списка {resp.status_code} {resp.text[:200]}")
            break
        embedded = resp.json().get("_embedded", {})
        batch = embedded.get("items", []) or []
        for it in batch:
            if it.get("type") != "file":
                continue
            name = it.get("name", "")
            is_video = (it.get("media_type") == "video"
                        or name.lower().endswith(VIDEO_EXT))
            if not is_video:
                continue
            items.append({
                "name": name,
                "path": it.get("path", ""),
                "size": it.get("size"),
            })
        total = embedded.get("total", len(batch))
        offset += len(batch)
        if not batch or offset >= total:
            break
    return items


def download_url(public_url, path):
    """Свежая прямая ссылка на скачивание файла по пути внутри публичной папки."""
    resp = httpx.get(
        f"{API}/download",
        params={"public_key": public_url, "path": path},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Яндекс.Диск: download {resp.status_code} {resp.text[:200]}")
        return None
    return resp.json().get("href")


def download_bytes(href):
    """Скачивает файл по прямой ссылке (следует редиректам). Возвращает bytes."""
    resp = httpx.get(href, timeout=180, follow_redirects=True)
    resp.raise_for_status()
    return resp.content
