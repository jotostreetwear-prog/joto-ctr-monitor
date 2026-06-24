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
    """Список видеофайлов в публичной папке, включая вложенные подпапки.

    Возвращает [{"name", "path", "size"}]. path — путь внутри публичной папки,
    его потом передаём в download_url(). Пишет в логи, что нашёл (для диагностики).
    """
    items = []
    _collect(public_url, None, items, depth=0)
    print(f"Яндекс.Диск: итого найдено видео — {len(items)}")
    return items


def _collect(public_url, path, items, depth):
    """Рекурсивно обходит папку (и подпапки) и собирает видеофайлы."""
    if depth > 4:
        return
    offset = 0
    while True:
        params = {"public_key": public_url, "limit": 200, "offset": offset}
        if path:
            params["path"] = path
        resp = httpx.get(API, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"Яндекс.Диск: ошибка списка {resp.status_code} "
                  f"{resp.text[:200]} (path={path})")
            return
        data = resp.json()
        # Если публичная ссылка ведёт на одиночный файл, а не папку
        if data.get("type") == "file" and not data.get("_embedded"):
            name = data.get("name", "")
            if _is_video(data):
                items.append({"name": name, "path": data.get("path", ""),
                              "size": data.get("size")})
            return
        embedded = data.get("_embedded", {})
        batch = embedded.get("items", []) or []
        for it in batch:
            name = it.get("name", "")
            t = it.get("type")
            if t == "dir":
                print(f"Яндекс.Диск: папка {name} — захожу внутрь")
                _collect(public_url, it.get("path"), items, depth + 1)
            elif t == "file":
                video = _is_video(it)
                print(f"Яндекс.Диск: файл {name} "
                      f"media_type={it.get('media_type')} -> видео={video}")
                if video:
                    items.append({"name": name, "path": it.get("path", ""),
                                  "size": it.get("size")})
        total = embedded.get("total", len(batch))
        offset += len(batch)
        if not batch or offset >= total:
            break


def _is_video(it):
    """Похож ли элемент Диска на видеофайл (по media_type, mime или расширению)."""
    name = (it.get("name") or "").lower()
    return (it.get("media_type") == "video"
            or (it.get("mime_type") or "").startswith("video/")
            or name.endswith(VIDEO_EXT))


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
