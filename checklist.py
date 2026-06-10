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
from concurrent.futures import ThreadPoolExecutor

import vision
import wb_public

WB_API_TOKEN = os.environ.get("WB_API_TOKEN", "").strip()

CONTENT_API = "https://content-api.wildberries.ru"
FEEDBACKS_API = "https://feedbacks-api.wildberries.ru"

# Сколько заполненных характеристик считаем достаточным для «зелёной» метрики
CHARS_THRESHOLD = int(os.environ.get("CHECKLIST_CHARS_MIN", "10"))
# Минимальный рейтинг для «зелёной» метрики
RATING_MIN = float(os.environ.get("CHECKLIST_RATING_MIN", "4.5"))

# Префиксы артикулов, которые считаем тестовыми и не показываем в чек-листе
TEST_PREFIXES = tuple(
    p.strip().lower()
    for p in os.environ.get("CHECKLIST_TEST_PREFIXES", "тест,test").split(",")
    if p.strip()
)

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
    ("recommendations","Рекомендации",      "manual"),
    ("rating",         "Рейтинг",           "auto"),
    ("seo",            "СЕО",               "auto"),
    ("promo_block",    "Блокировка акций",  "manual"),
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

def _is_test_card(card):
    """Тестовая/черновая карточка — по префиксу артикула (vendorCode)."""
    if not TEST_PREFIXES:
        return False
    vc = (card.get("vendorCode") or "").strip().lower()
    return vc.startswith(TEST_PREFIXES)


def _photo_url(photo):
    """Ссылка на большое изображение из объекта фото Content API."""
    if isinstance(photo, dict):
        return photo.get("big") or next(iter(photo.values()), None)
    return photo if isinstance(photo, str) else None


def _auto_metrics(card, fb):
    """Считает автоматические метрики по данным карточки и отзывов."""
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


def _publish(items):
    """Складывает отсортированный снимок чек-листа в кэш (для дашборда)."""
    snapshot = sorted(items, key=lambda i: i["score"])
    with _lock:
        _cache["items"] = snapshot
        _cache["summary"] = _summarize(snapshot)
        _cache["checked_at"] = datetime.now().strftime("%d.%m.%Y, %H:%M")


def _enrich(item, card, ov):
    """Медленная часть по одному артикулу: сетка (vision) + публичная карточка."""
    # Сетка на 4-м фото — vision (фолбэк на ручную отметку)
    if vision.enabled():
        photos = card.get("photos") or []
        if len(photos) >= 4:
            grid = vision.detect_size_grid(_photo_url(photos[3]))
            if grid is not None:
                item["metrics"]["grid_4th"] = grid
    # Сертификаты и рекомендации — парсинг публичной карточки WB
    pub = wb_public.get_public_signals(item["nm_id"])
    if pub.get("certificates") is not None:
        item["metrics"]["certificates"] = pub["certificates"]
    # Рич-контент — точный признак has_rich из публичной карточки
    if pub.get("rich_content") is not None:
        item["metrics"]["rich_content"] = pub["rich_content"]
    _recalc_item(item)


def compute_checklist():
    """Пересчёт чек-листа: сначала быстрые авто-метрики, потом параллельное
    обогащение медленными (сетка/сертификаты/рекомендации)."""
    global _computing
    with _lock:
        if _computing:
            return dict(_cache)
        _computing = True
    try:
        cards = _fetch_cards()
        feedbacks = _fetch_feedbacks_stats()
        overrides = _load_overrides()

        # Фаза 1 — быстрые авто-метрики, таблица показывается сразу
        items, ctx = [], []
        for card in cards:
            nm_id = card.get("nmID")
            if not nm_id:
                continue
            # пропускаем тестовые/черновые карточки (артикул "тест", "test1" и т.п.)
            if _is_test_card(card):
                continue
            fb = feedbacks.get(nm_id, {})
            metrics = _auto_metrics(card, fb)
            ov = overrides.get(str(nm_id), {})
            # медленные метрики пока из ручных отметок (уточнятся в фазе 2)
            for key in MANUAL_KEYS:
                metrics[key] = bool(ov.get(key, False))
            ordered = {k: metrics.get(k, False) for k, _, _ in METRICS}
            item = {
                "nm_id": nm_id,
                "name": card.get("title") or card.get("vendorCode") or str(nm_id),
                "vendor_code": card.get("vendorCode") or "",
                "metrics": ordered,
            }
            _recalc_item(item)
            items.append(item)
            ctx.append((item, card, ov))

        _publish(items)  # дашборд уже показывает данные
        print(f"Чек-лист: фаза 1 готова, артикулов {len(items)}")

        # Фаза 2 — медленное обогащение параллельно
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda c: _enrich(*c), ctx))

        _publish(items)
        with _lock:
            summary = dict(_cache["summary"])
        print(f"Чек-лист готов: {summary}")
        return get_cached()
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


def diagnose():
    """Проверяет доступ токена к нужным WB API и возвращает коды ответов.

    Помогает понять, каких скоупов не хватает у WB_API_TOKEN
    (Контент / Цены / Отзывы), если чек-лист пустой.
    """
    headers = {"Authorization": WB_API_TOKEN}
    out = {"token_set": bool(WB_API_TOKEN), "token_len": len(WB_API_TOKEN)}

    # Контент — карточки
    try:
        r = httpx.post(
            f"{CONTENT_API}/content/v2/get/cards/list",
            headers=headers,
            json={"settings": {"cursor": {"limit": 10}, "filter": {"withPhoto": -1}}},
            timeout=30,
        )
        out["content_status"] = r.status_code
        if r.status_code == 200:
            cards = r.json().get("cards") or []
            out["content_cards"] = len(cards)
            out["content_sample"] = [
                {"nmID": c.get("nmID"), "vendorCode": c.get("vendorCode")} for c in cards[:3]
            ]
        else:
            out["content_error"] = r.text[:300]
    except Exception as e:
        out["content_exc"] = str(e)

    # Отзывы
    try:
        r = httpx.get(
            f"{FEEDBACKS_API}/api/v1/feedbacks",
            headers=headers, params={"isAnswered": "true", "take": 10, "skip": 0}, timeout=30,
        )
        out["feedbacks_status"] = r.status_code
        if r.status_code != 200:
            out["feedbacks_error"] = r.text[:200]
    except Exception as e:
        out["feedbacks_exc"] = str(e)

    return out
