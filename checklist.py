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
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

import vision
import wb_public

# Московское время (UTC+3) — Railway работает в UTC
MSK = timezone(timedelta(hours=3))


def _now_msk():
    return datetime.now(MSK)

WB_API_TOKEN = os.environ.get("WB_API_TOKEN", "").strip()
# Отдельный токен для цен (категория «Цены и скидки»). Если не задан —
# используем общий WB_API_TOKEN.
WB_PRICES_TOKEN = os.environ.get("WB_PRICES_TOKEN", "").strip() or WB_API_TOKEN

CONTENT_API = "https://content-api.wildberries.ru"
PRICES_API = "https://discounts-prices-api.wildberries.ru"
FEEDBACKS_API = "https://feedbacks-api.wildberries.ru"

# Минимум фото в карточке для «зелёной» метрики (меньше — красный)
PHOTO_MIN = int(os.environ.get("CHECKLIST_PHOTO_MIN", "6"))
# Сколько первых фото сканировать на размерную сетку (Gemini Vision)
GRID_SCAN_LIMIT = int(os.environ.get("CHECKLIST_GRID_SCAN", "12"))
# Сколько заполненных характеристик считаем достаточным для «зелёной» метрики
CHARS_THRESHOLD = int(os.environ.get("CHECKLIST_CHARS_MIN", "10"))
# Минимальный рейтинг для «зелёной» метрики
RATING_MIN = float(os.environ.get("CHECKLIST_RATING_MIN", "4.5"))

# СЕО считается «зелёным» по наличию заголовка и описания (а не по длине —
# короткие заголовки часто делают намеренно, чтобы не цеплять лишние ключи).
# При желании пороги можно поднять через переменные окружения.
SEO_TITLE_MIN = int(os.environ.get("CHECKLIST_SEO_TITLE_MIN", "1"))
SEO_DESC_MIN = int(os.environ.get("CHECKLIST_SEO_DESC_MIN", "1"))

# Префиксы артикулов, которые считаем тестовыми и не показываем в чек-листе
TEST_PREFIXES = tuple(
    p.strip().lower()
    for p in os.environ.get("CHECKLIST_TEST_PREFIXES", "тест,test").split(",")
    if p.strip()
)

# Фильтр по тегам WB: оставляем только карточки с тегом, содержащим одно из слов
# (по умолчанию «актив» и «новин» — активные и новинки). Пусто = показывать все.
TAG_KEYWORDS = tuple(
    k.strip().lower()
    for k in os.environ.get("CHECKLIST_TAG_KEYWORDS", "актив,новин").split(",")
    if k.strip()
)

# Файл с ручными отметками по «визуальным» метрикам: {nmID: {metric_key: bool}}
# Где хранить ручные отметки. Railway сообщает путь подключённого тома в
# RAILWAY_VOLUME_MOUNT_PATH — пишем туда (отметки переживают редеплои).
# Фолбэк: /data, если есть, иначе локальный временный файл.
_RAILWAY_VOL = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
OVERRIDES_PATH = (
    os.environ.get("CHECKLIST_OVERRIDES")
    or (os.path.join(_RAILWAY_VOL, "overrides.json") if _RAILWAY_VOL
        else "/data/overrides.json" if os.path.isdir("/data")
        else "overrides.json")
)

# Определение метрик: (ключ, подпись, источник)
#   auto   — считается из официального WB API
#   parse  — парсится из публичной карточки WB
#   manual — выставляется вручную в дашборде
# Порядок совпадает с макетом дашборда.
METRICS = [
    ("photo10",        f"Фото ({PHOTO_MIN}+ шт)",  "auto"),
    ("pinned_reviews", "Закреп. отзывы",    "manual"),
    ("photo_reviews",  "Фотоотзывы",        "auto"),
    ("video",          "Видео",             "auto"),
    ("rich_content",   "Рич-контент",       "auto"),
    ("certificates",   "Сертификаты",       "manual"),
    ("barcode",        "Баркод",            "auto"),
    ("grid_4th",       "Сетка на фото",     "parse"),
    ("recommendations","Рекомендации",      "parse"),
    ("rating",         "Рейтинг",           "auto"),
    ("seo",            "СЕО",               "auto"),
    ("promo_block",    "Блокировка акций",  "manual"),
]
# Метрики, которые можно переопределить вручную (всё, что не чистый auto):
# для них ручная отметка — фолбэк, когда парсинг не смог определить.
MANUAL_KEYS = [k for k, _, kind in METRICS if kind != "auto"]
# Значения по умолчанию для ручных метрик (если нет ручной отметки).
# Сертификаты — зелёные по умолчанию (у продавца серты почти на всё),
# исключения отмечаются вручную. Остальные — красные, пока не отметят.
MANUAL_DEFAULTS = {"certificates": True}
TOTAL_METRICS = len(METRICS)

# Метрики, которые можно отмечать/переопределять вручную — все, кроме «Баркод»
# (баркод считается автоматически и надёжно). Авто-значение остаётся базовым,
# ручная отметка его перебивает и сохраняется.
OVERRIDABLE_KEYS = {k for k, _, _ in METRICS if k != "barcode"}

# Белый список характеристик-столбцов (после метрик). Пусто = показывать все.
# Имена должны совпадать с названиями характеристик WB. «Описание» — по описанию.
CHAR_INCLUDE = [
    s.strip() for s in os.environ.get(
        "CHECKLIST_CHAR_INCLUDE",
        "Описание,Пол,Состав,Сезон,Страна производства,Ставка НДС",
    ).split(",") if s.strip()
]

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
        d = os.path.dirname(OVERRIDES_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(OVERRIDES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"Чек-лист: не удалось сохранить overrides: {e}")


def overrides_info():
    """Диагностика хранения ручных отметок: путь, права записи, содержимое."""
    d = os.path.dirname(OVERRIDES_PATH) or "."
    info = {
        "path": OVERRIDES_PATH,
        "railway_volume_mount_path": _RAILWAY_VOL or "(не задан)",
        "data_volume_mounted": os.path.isdir("/data"),
        "dir": d,
        "dir_exists": os.path.isdir(d),
        "file_exists": os.path.exists(OVERRIDES_PATH),
        "overrides": _load_overrides(),
    }
    try:
        os.makedirs(d, exist_ok=True)
        test = os.path.join(d, ".w_test")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        info["write_test"] = "ok"
    except Exception as e:
        info["write_test"] = f"FAIL: {e}"
    return info


def set_override(nm_id, metric_key, value):
    """Ручная отметка: метрика (MANUAL_KEYS) или характеристика (ключ 'char:Название')."""
    is_char = str(metric_key).startswith("char:")
    if not is_char and metric_key not in OVERRIDABLE_KEYS:
        return False
    data = _load_overrides()
    nm_key = str(nm_id)
    data.setdefault(nm_key, {})[metric_key] = bool(value)
    _save_overrides(data)
    # обновляем кэш, чтобы дашборд сразу показал изменение
    with _lock:
        for item in _cache["items"]:
            if str(item["nm_id"]) == nm_key:
                if is_char:
                    name = metric_key[5:]
                    for c in item.get("chars", []):
                        if c["name"] == name:
                            c["filled"] = bool(value)
                            break
                else:
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
    headers = {"Authorization": WB_PRICES_TOKEN}
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
        time.sleep(0.6)  # лимит цен: 10 запросов / 6 сек
    return prices


# Кэш полного списка характеристик по subjectID (названия на русском)
_charcs_cache = {}


def _subject_charcs(subject_id):
    """Все возможные характеристики предмета (subjectID) — названия на русском."""
    if not subject_id:
        return []
    if subject_id in _charcs_cache:
        return _charcs_cache[subject_id]
    headers = {"Authorization": WB_API_TOKEN}
    names = []
    try:
        r = httpx.get(
            f"{CONTENT_API}/content/v2/object/charcs/{subject_id}",
            headers=headers, params={"locale": "ru"}, timeout=30,
        )
        if r.status_code == 200:
            names = [c.get("name") for c in (r.json().get("data") or []) if c.get("name")]
        else:
            print(f"Чек-лист: charcs {subject_id} -> {r.status_code}")
    except Exception as e:
        print(f"Чек-лист: ошибка charcs {subject_id}: {e}")
    _charcs_cache[subject_id] = names
    return names


# Характеристики, которые НЕ показываем в чек-листе (технические/маркетплейсные).
# Совпадение по подстроке (без регистра). Настраивается CHECKLIST_CHAR_EXCLUDE.
CHAR_EXCLUDE = tuple(
    k.strip().lower()
    for k in os.environ.get(
        "CHECKLIST_CHAR_EXCLUDE",
        "ozon,озон,икпу,код тру,баркод,код упаковки,штук в товаре,любимые герои",
    ).split(",")
    if k.strip()
)


def _char_excluded(name):
    n = (name or "").lower()
    return any(k in n for k in CHAR_EXCLUDE)


def _filled_chars(card):
    """Заполненные характеристики карточки: {название: значение}."""
    out = {}
    for ch in card.get("characteristics") or []:
        n, v = ch.get("name"), ch.get("value")
        if n and v and not _char_excluded(n):
            out[n] = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
    return out


def _full_chars(card):
    """Столбцы характеристик. Если задан белый список (CHAR_INCLUDE) — только они,
    в заданном порядке; «Описание» берётся из описания карточки."""
    filled = _filled_chars(card)
    if CHAR_INCLUDE:
        flow = {k.lower(): v for k, v in filled.items()}
        desc = (card.get("description") or "").strip()
        out = []
        for name in CHAR_INCLUDE:
            if name.lower() == "описание":
                out.append({"name": "Описание", "filled": bool(desc),
                            "value": (desc[:100] + "…") if len(desc) > 100 else desc})
            else:
                v = flow.get(name.lower(), "")
                out.append({"name": name, "filled": bool(v), "value": v})
        return out
    # без белого списка — полный список характеристик категории
    names = [n for n in _subject_charcs(card.get("subjectID")) if not _char_excluded(n)]
    if not names:
        return [{"name": n, "filled": True, "value": v} for n, v in filled.items()]
    full = [{"name": n, "filled": n in filled, "value": filled.get(n, "")} for n in names]
    for n, v in filled.items():
        if n not in names:
            full.append({"name": n, "filled": True, "value": v})
    return full


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


def _passes_tag_filter(card):
    """True, если у карточки есть тег WB из списка (активные/новинки)."""
    if not TAG_KEYWORDS:
        return True
    names = " ".join((t.get("name") or "") for t in (card.get("tags") or [])).lower()
    return any(k in names for k in TAG_KEYWORDS)


def _photos(card):
    """Список фото карточки — поле может называться photos или mediaFiles."""
    return card.get("photos") or card.get("mediaFiles") or []


def _photo_url(photo):
    """Ссылка на большое изображение из объекта фото Content API."""
    if isinstance(photo, dict):
        return photo.get("big") or next(iter(photo.values()), None)
    return photo if isinstance(photo, str) else None


def _grid_photo_index(card):
    """Номер фото (с 1) с размерной сеткой. 0 — ни на одном фото сетки нет,
    None — распознавание недоступно/не удалось определить.
    Сканируем фото по очереди и выходим на первом найденном."""
    if not vision.enabled():
        return None
    urls = [u for u in (_photo_url(p) for p in _photos(card)) if u]
    if not urls:
        return None
    determined = False
    for i, url in enumerate(urls[:GRID_SCAN_LIMIT]):
        res = vision.detect_size_grid(url)
        if res is True:
            return i + 1
        if res is not None:
            determined = True
    # все просмотренные фото без сетки → 0; если ни одно не распозналось → None
    return 0 if determined else None


def _auto_metrics(card, fb):
    """Считает автоматические метрики по данным карточки и отзывов."""
    photos = _photos(card)
    video = card.get("video")
    chars = card.get("characteristics") or []
    sizes = card.get("sizes") or []
    title = (card.get("title") or "").strip()
    desc = (card.get("description") or "").strip()

    barcode_ok = bool(sizes) and all((s.get("skus") for s in sizes))
    chars_filled = sum(1 for c in chars if c.get("value"))

    return {
        "photo10": len(photos) >= PHOTO_MIN,
        "photo_reviews": bool(fb.get("has_photo")),
        "video": bool(video),
        "rich_content": len(desc) >= 1000,
        "barcode": barcode_ok,
        "characteristics": chars_filled >= CHARS_THRESHOLD,
        "rating": fb.get("rating", 0) >= RATING_MIN and fb.get("count", 0) > 0,
        "seo": len(title) >= SEO_TITLE_MIN and len(desc) >= SEO_DESC_MIN,
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
        _cache["checked_at"] = _now_msk().strftime("%d.%m.%Y, %H:%M") + " МСК"


def _enrich(item, card, ov):
    """Медленная часть по одному артикулу: данные из публичной карточки WB."""
    pub = wb_public.get_public_signals(item["nm_id"])
    # Рич-контент — точный признак has_rich из публичной карточки
    if pub.get("rich_content") is not None:
        item["metrics"]["rich_content"] = pub["rich_content"]
    # Сетка — реально нарисованная размерная сетка на фото (Gemini Vision);
    # запасной сигнал — наличие заполненной таблицы размеров (sizes_table).
    grid_idx = _grid_photo_index(card)
    if grid_idx is not None:
        item["metrics"]["grid_4th"] = grid_idx > 0
        item["grid_index"] = grid_idx  # 0 — сетки нет; >0 — номер фото с сеткой
    else:
        grid = pub.get("grid_4th")
        if grid is not None:
            item["metrics"]["grid_4th"] = grid
    # Рекомендации продавца — поле has_seller_recommendations из card.json
    if pub.get("recommendations") is not None:
        item["metrics"]["recommendations"] = pub["recommendations"]
    # Полный список характеристик категории (заполнено/не заполнено), на русском
    item["chars"] = _full_chars(card)
    # Ручные отметки менеджера имеют приоритет — применяем последними, чтобы
    # квадратики (метрики и характеристики) не сбрасывались при «Обновить».
    for k, v in (ov or {}).items():
        if k in item["metrics"]:
            item["metrics"][k] = bool(v)
        elif k.startswith("char:"):
            name = k[5:]
            for c in item["chars"]:
                if c["name"] == name:
                    c["filled"] = bool(v)
                    break
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
            # оставляем только активные/новинки по тегам WB
            if not _passes_tag_filter(card):
                continue
            fb = feedbacks.get(nm_id, {})
            metrics = _auto_metrics(card, fb)
            ov = overrides.get(str(nm_id), {})
            # медленные метрики пока из ручных отметок (уточнятся в фазе 2)
            for key in MANUAL_KEYS:
                metrics[key] = bool(ov.get(key, MANUAL_DEFAULTS.get(key, False)))
            ordered = {k: metrics.get(k, False) for k, _, _ in METRICS}
            # Столбцы характеристик (белый список — быстро, без сети)
            chars = _full_chars(card)
            item = {
                "nm_id": nm_id,
                "name": card.get("title") or card.get("vendorCode") or str(nm_id),
                "vendor_code": card.get("vendorCode") or "",
                "category": card.get("subjectName") or "Без категории",
                "chars": chars,
                "metrics": ordered,
                "photo_count": len(_photos(card)),
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


def tags_overview():
    """Какие теги WB есть в кабинете и сколько артикулов останется после фильтра."""
    cards = _fetch_cards()
    counts = {}
    for c in cards:
        for t in c.get("tags") or []:
            n = t.get("name") or "?"
            counts[n] = counts.get(n, 0) + 1
    after = sum(1 for c in cards if not _is_test_card(c) and _passes_tag_filter(c))
    return {
        "total_cards": len(cards),
        "tag_counts": dict(sorted(counts.items(), key=lambda x: -x[1])),
        "keywords": list(TAG_KEYWORDS),
        "after_filter": after,
    }


def debug_first_card():
    """Сырая первая карточка из Content API — чтобы свериться с именами полей."""
    cards = _fetch_cards()
    if not cards:
        return {"total_cards": 0}
    c = cards[0]
    photos, media = c.get("photos"), c.get("mediaFiles")
    return {
        "total_cards": len(cards),
        "keys": sorted(c.keys()),
        "photos_len": len(photos) if isinstance(photos, list) else None,
        "mediaFiles_len": len(media) if isinstance(media, list) else None,
        "video": c.get("video"),
        "title_len": len(c.get("title") or ""),
        "desc_len": len(c.get("description") or ""),
        "subjectName": c.get("subjectName"),
        "raw": c,
    }


def debug_vision(nm_id=None):
    """Диагностика распознавания сетки: режим, ключи и результат по фото.
    nm_id — конкретный артикул; если не задан, берём первую карточку кабинета."""
    out = {
        "vision_mode": vision.mode(),
        "vision_enabled": vision.enabled(),
        "vision_provider_setting": vision.VISION_PROVIDER,
        "gemini_key_set": bool(vision.GEMINI_API_KEY),
        "gemini_model": vision.GEMINI_MODEL,
        "anthropic_key_set": bool(vision.ANTHROPIC_API_KEY),
        "anthropic_model": vision.VISION_MODEL,
        "cache_path": vision.CACHE_PATH,
        "grid_scan_limit": GRID_SCAN_LIMIT,
    }
    try:
        cards = _fetch_cards()
    except Exception as e:
        out["error"] = f"не удалось получить карточки: {e}"
        return out
    card = None
    if nm_id:
        card = next((c for c in cards if c.get("nmID") == nm_id), None)
        if card is None:
            out["error"] = f"карточка {nm_id} не найдена в кабинете"
            return out
    else:
        card = cards[0] if cards else None
    if card is None:
        out["error"] = "нет карточек"
        return out
    out["nm_id"] = card.get("nmID")
    out["vendor_code"] = card.get("vendorCode")
    urls = [u for u in (_photo_url(p) for p in _photos(card)) if u]
    out["photo_count"] = len(urls)
    photos = [vision.debug_photo(u) for u in urls[:GRID_SCAN_LIMIT]]
    out["photos"] = photos
    # Агрегат и номер фото с сеткой выводим из уже полученных ответов,
    # чтобы не сканировать фото повторно (иначе шлюз отдаёт таймаут).
    results = [p.get("result") for p in photos]
    grid_index = next((i + 1 for i, r in enumerate(results) if r is True), None)
    out["grid_photo_index"] = grid_index
    if grid_index is not None:
        out["grid_result"] = True
    elif any(r is not None for r in results):
        out["grid_result"] = False
    else:
        out["grid_result"] = None
    return out


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

    # Цены — статус + сырой пример товара (ищем поле блокировки акций)
    out["prices_token_len"] = len(WB_PRICES_TOKEN)
    out["prices_token_separate"] = WB_PRICES_TOKEN != WB_API_TOKEN
    try:
        r = httpx.get(
            f"{PRICES_API}/api/v2/list/goods/filter",
            headers={"Authorization": WB_PRICES_TOKEN},
            params={"limit": 10, "offset": 0}, timeout=30,
        )
        out["prices_status"] = r.status_code
        if r.status_code == 200:
            goods = (r.json().get("data") or {}).get("listGoods") or []
            out["prices_goods"] = len(goods)
            out["prices_sample"] = goods[0] if goods else None
        else:
            out["prices_error"] = r.text[:300]
    except Exception as e:
        out["prices_exc"] = str(e)

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
