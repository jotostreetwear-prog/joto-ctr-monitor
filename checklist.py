"""Чек-лист готовности карточек Wildberries.

Тянет все карточки кабинета через Content API, считает 13 метрик готовности
и итоговый балл. Часть метрик считается автоматически из API, часть —
визуальные (сетка на 4-м фото, сертификаты и т.п.) — выставляются вручную
галочками в дашборде и хранятся в overrides.json.
"""

import os
import json
import time
import threading
import httpx
from datetime import datetime

import vision
import wb_public

WB_API_TOKEN = os.environ.get("WB_API_TOKEN", "").strip()

CONTENT_API = "https://content-api.wildberries.ru"
PRICES_API = "https://discounts-prices-api.wildberries.ru"
FEEDBACKS_API = "https://feedbacks-api.wildberries.ru"

# Сколько заполненных характеристик считаем достаточным для «зелёной» метрики
CHARS_THRESHOLD = int(os.environ.get("CHECKLIST_CHARS_MIN", "10"))
# Минимальный рейтинг для «зелёной» метрики
RATING_MIN = float(os.environ.get("CHECKLIST_RATING_MIN", "4.5"))

# Файл с ручными отметками по «визуальным» метрикам: {nmID: {metric_key: bool}}
OVERRIDES_PATH = os.environ.get("CHECKLIST_OVERRIDES", "overrides.json")

# Определение метрик: (ключ, подпись, источник)
#   auto   — считается из официального WB API
#   vision — определяется анализом фото через Claude Vision
#   parse  — парсится из публичной карточки WB
#   manual — выставляется вручную в дашборде
# Порядок совпадает с макетом дашборда.
METRICS = [
    ("photo10",        "Фото (10 шт)",      "auto"),
    ("pinned_reviews", "Закреп. отзывы",    "manual"),
    ("photo_reviews",  "Фотоотзывы",        "auto"),
    ("video",          "Видео",             "auto"),
    ("rich_content",   "Рич-контент",       "auto"),
    ("certificates",   "Сертификаты",       "parse"),
    ("barcode",        "Баркод",            "auto"),
    ("characteristics","Характеристики",    "auto"),
    ("grid_4th",       "Сетка на 4-м фото", "vision"),
    ("recommendations","Рекомендации",      "parse"),
    ("price",          "Цена",              "auto"),
    ("rating",         "Рейтинг",           "auto"),
    ("seo",            "СЕО",               "auto"),
]
# Метрики, которые можно переопределить вручную (всё, что не чистый auto):
# для них ручная отметка — фолбэк, когда vision/парсинг не смог определить.
MANUAL_KEYS = [k for k, _, kind in METRICS if kind != "auto"]
TOTAL_METRICS = len(METRICS)

# Кэш последнего результата + блокировка
_cache = {"checked_at": None, "items": [], "summary": {}}
_lock = threading.Lock()
_computing = False


# ===================== Ручные отметки =====================

def _load_overrides():
    try:
        with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_overrides(data):
    try:
        with open(OVERRIDES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"Чек-лист: не удалось сохранить overrides: {e}")


def set_override(nm_id, metric_key, value):
    """Выставить ручную отметку по визуальной метрике."""
    if metric_key not in MANUAL_KEYS:
        return False
    data = _load_overrides()
    nm_key = str(nm_id)
    data.setdefault(nm_key, {})[metric_key] = bool(value)
    _save_overrides(data)
    # обновляем кэш, чтобы дашборд сразу показал новый балл
    with _lock:
        for item in _cache["items"]:
            if str(item["nm_id"]) == nm_key:
                item["metrics"][metric_key] = bool(value)
                _recalc_item(item)
                break
        _cache["summary"] = _summarize(_cache["items"])
    return True


# ===================== WB API =====================

def _fetch_cards():
    """Все карточки кабинета через Content API (с пагинацией по курсору)."""
    headers = {"Authorization": WB_API_TOKEN}
    cards = []
    cursor = {"limit": 100}
    while True:
        body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
        try:
            resp = httpx.post(
                f"{CONTENT_API}/content/v2/get/cards/list",
                headers=headers, json=body, timeout=60,
            )
        except Exception as e:
            print(f"Чек-лист: ошибка Content API: {e}")
            break
        if resp.status_code != 200:
            print(f"Чек-лист: Content API {resp.status_code}: {resp.text[:300]}")
            break
        data = resp.json()
        batch = data.get("cards") or []
        cards.extend(batch)
        rc = data.get("cursor") or {}
        total = rc.get("total", 0)
        if total < cursor["limit"]:
            break
        cursor = {"limit": 100, "updatedAt": rc.get("updatedAt"), "nmID": rc.get("nmID")}
        time.sleep(0.3)
    print(f"Чек-лист: получено карточек {len(cards)}")
    return cards


def _fetch_prices():
    """nmID -> цена со скидкой (или базовая). Пусто при ошибке/нет скоупа."""
    headers = {"Authorization": WB_API_TOKEN}
    prices = {}
    offset = 0
    while True:
        try:
            resp = httpx.get(
                f"{PRICES_API}/api/v2/list/goods/filter",
                headers=headers, params={"limit": 1000, "offset": offset}, timeout=30,
            )
        except Exception as e:
            print(f"Чек-лист: ошибка Prices API: {e}")
            break
        if resp.status_code != 200:
            print(f"Чек-лист: Prices API {resp.status_code}: {resp.text[:200]}")
            break
        goods = (resp.json().get("data") or {}).get("listGoods") or []
        if not goods:
            break
        for g in goods:
            nm = g.get("nmID")
            sizes = g.get("sizes") or []
            price = 0
            if sizes:
                price = sizes[0].get("discountedPrice") or sizes[0].get("price") or 0
            prices[nm] = price
        if len(goods) < 1000:
            break
        offset += 1000
        time.sleep(0.3)
    return prices


def _fetch_feedbacks_stats():
    """nmID -> {has_photo, rating, count}. Агрегируем по списку отзывов."""
    headers = {"Authorization": WB_API_TOKEN}
    stats = {}
    for is_answered in (True, False):
        skip = 0
        while True:
            try:
                resp = httpx.get(
                    f"{FEEDBACKS_API}/api/v1/feedbacks",
                    headers=headers,
                    params={"isAnswered": str(is_answered).lower(), "take": 5000, "skip": skip},
                    timeout=30,
                )
            except Exception as e:
                print(f"Чек-лист: ошибка Feedbacks API: {e}")
                return stats
            if resp.status_code != 200:
                print(f"Чек-лист: Feedbacks API {resp.status_code}: {resp.text[:200]}")
                return stats
            payload = (resp.json().get("data") or {})
            fbs = payload.get("feedbacks") or []
            if not fbs:
                break
            for fb in fbs:
                nm = (fb.get("productDetails") or {}).get("nmId")
                if not nm:
                    continue
                s = stats.setdefault(nm, {"has_photo": False, "sum": 0, "count": 0})
                if fb.get("photoLinks"):
                    s["has_photo"] = True
                val = fb.get("productValuation") or 0
                if val:
                    s["sum"] += val
                    s["count"] += 1
            if len(fbs) < 5000:
                break
            skip += 5000
            time.sleep(0.3)
    for nm, s in stats.items():
        s["rating"] = round(s["sum"] / s["count"], 2) if s["count"] else 0
    return stats


# ===================== Подсчёт метрик =====================

def _photo_url(photo):
    """Ссылка на большое изображение из объекта фото Content API."""
    if isinstance(photo, dict):
        return photo.get("big") or next(iter(photo.values()), None)
    return photo if isinstance(photo, str) else None


def _auto_metrics(card, price, fb):
    """Считает автоматические метрики по данным карточки/цены/отзывов."""
    photos = card.get("photos") or []
    video = card.get("video")
    chars = card.get("characteristics") or []
    sizes = card.get("sizes") or []
    title = (card.get("title") or "").strip()
    desc = (card.get("description") or "").strip()

    barcode_ok = bool(sizes) and all((s.get("skus") for s in sizes))
    chars_filled = sum(1 for c in chars if c.get("value"))

    return {
        "photo10": len(photos) >= 10,
        "photo_reviews": bool(fb.get("has_photo")),
        "video": bool(video),
        "rich_content": len(desc) >= 1000,
        "barcode": barcode_ok,
        "characteristics": chars_filled >= CHARS_THRESHOLD,
        "price": (price or 0) > 0,
        "rating": fb.get("rating", 0) >= RATING_MIN and fb.get("count", 0) > 0,
        "seo": len(title) >= 25 and len(desc) >= 100,
    }


def _recalc_item(item):
    greens = sum(1 for v in item["metrics"].values() if v)
    item["score"] = round(greens / TOTAL_METRICS * 100)
    item["reds"] = TOTAL_METRICS - greens


def _summarize(items):
    n = len(items)
    avg = round(sum(i["score"] for i in items) / n) if n else 0
    ready = sum(1 for i in items if i["score"] == 100)
    reds = sum(i["reds"] for i in items)
    return {
        "avg_score": avg,
        "total": n,
        "ready": ready,
        "with_issues": n - ready,
        "total_reds": reds,
    }


def compute_checklist():
    """Полный пересчёт чек-листа по всем артикулам кабинета."""
    global _computing
    with _lock:
        if _computing:
            return _cache
        _computing = True
    try:
        cards = _fetch_cards()
        prices = _fetch_prices()
        feedbacks = _fetch_feedbacks_stats()
        overrides = _load_overrides()

        items = []
        for card in cards:
            nm_id = card.get("nmID")
            if not nm_id:
                continue
            name = card.get("title") or card.get("vendorCode") or str(nm_id)
            vendor = card.get("vendorCode") or ""
            fb = feedbacks.get(nm_id, {})
            metrics = _auto_metrics(card, prices.get(nm_id), fb)
            ov = overrides.get(str(nm_id), {})

            # Сетка на 4-м фото — Claude Vision (фолбэк на ручную отметку)
            grid = None
            if vision.enabled():
                photos = card.get("photos") or []
                if len(photos) >= 4:
                    grid = vision.detect_size_grid(_photo_url(photos[3]))
            metrics["grid_4th"] = grid if grid is not None else bool(ov.get("grid_4th", False))

            # Сертификаты и рекомендации — парсинг публичной карточки WB
            pub = wb_public.get_public_signals(nm_id)
            metrics["certificates"] = (
                pub["certificates"] if pub["certificates"] is not None
                else bool(ov.get("certificates", False))
            )
            metrics["recommendations"] = (
                pub["recommendations"] if pub["recommendations"] is not None
                else bool(ov.get("recommendations", False))
            )

            # Закреплённые отзывы — только ручная отметка
            metrics["pinned_reviews"] = bool(ov.get("pinned_reviews", False))

            time.sleep(0.2)  # бережём публичные эндпоинты WB
            # упорядочиваем по METRICS
            ordered = {k: metrics.get(k, False) for k, _, _ in METRICS}
            item = {
                "nm_id": nm_id,
                "name": name,
                "vendor_code": vendor,
                "metrics": ordered,
            }
            _recalc_item(item)
            items.append(item)

        items.sort(key=lambda i: i["score"])
        summary = _summarize(items)
        result = {
            "checked_at": datetime.now().strftime("%d.%m.%Y, %H:%M"),
            "items": items,
            "summary": summary,
        }
        with _lock:
            _cache.update(result)
        print(f"Чек-лист готов: {summary}")
        return result
    finally:
        with _lock:
            _computing = False


def get_cached():
    with _lock:
        return dict(_cache)


def is_computing():
    with _lock:
        return _computing


def metrics_meta():
    return [{"key": k, "label": label, "kind": kind} for k, label, kind in METRICS]
