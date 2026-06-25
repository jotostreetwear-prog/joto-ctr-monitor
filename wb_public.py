"""Парсинг публичной карточки Wildberries — для метрик, которых нет в официальном
API: сертификаты и «Рекомендации продавца».

ВАЖНО: это парсинг внешнего фронта WB, он хрупкий — WB периодически меняет
структуру ответов и хосты basket. Поэтому всё обёрнуто в try/except и при любой
неопределённости возвращает None («не удалось определить»), а вызывающий код
откатывается на ручную отметку. Эндпоинт /checklist/debug-public показывает
сырые ответы, чтобы донастроить селекторы на боевом окружении.
"""

import httpx

# v2 отключён WB (404 с дек.2025) — рабочий публичный эндпоинт цены/рейтинга — v4
CARD_DETAIL = "https://card.wb.ru/cards/v4/detail"

_UA = {"User-Agent": "Mozilla/5.0 (compatible; JOTO-checklist/1.0)"}

# Кэш найденного basket-хоста по vol (чтобы не сканировать повторно)
_basket_cache = {}


def _basket_host(nm_id):
    """Хост basket по vol = nmID // 100000. Диапазоны растут со временем."""
    vol = nm_id // 100000
    table = [
        (143, 1), (287, 2), (431, 3), (719, 4), (1007, 5), (1061, 6),
        (1115, 7), (1169, 8), (1313, 9), (1601, 10), (1655, 11), (1919, 12),
        (2045, 13), (2189, 14), (2405, 15), (2685, 16), (2925, 17), (3115, 18),
        (3325, 19), (3464, 20), (3603, 21), (3742, 22), (3881, 23), (4020, 24),
    ]
    for upper, n in table:
        if vol <= upper:
            return f"basket-{n:02d}.wbbasket.ru"
    return "basket-25.wbbasket.ru"


def fetch_card_json(nm_id):
    """Сырой публичный card.json. Хост ищем перебором, если расчётный не подошёл
    (для «высоких» артикулов таблица хостов устаревает)."""
    vol = nm_id // 100000
    part = nm_id // 1000

    # порядок хостов: из кэша → расчётный → полный перебор
    hosts = []
    if vol in _basket_cache:
        hosts.append(_basket_cache[vol])
    hosts.append(_basket_host(nm_id))
    hosts += [f"basket-{n:02d}.wbbasket.ru" for n in range(1, 46)]

    seen = set()
    for host in hosts:
        if host in seen:
            continue
        seen.add(host)
        url = f"https://{host}/vol{vol}/part{part}/{nm_id}/info/ru/card.json"
        try:
            r = httpx.get(url, headers=_UA, timeout=12, follow_redirects=True)
            if r.status_code == 200:
                _basket_cache[vol] = host
                return r.json()
        except Exception:
            continue
    print(f"WB public card.json {nm_id} -> не найден ни на одном basket-хосте")
    return None


def fetch_detail(nm_id):
    """Публичная витрина карточки (card.wb.ru v4): цена, рейтинг, отзывы."""
    try:
        r = httpx.get(
            CARD_DETAIL,
            params={"appType": 1, "curr": "rub", "dest": -1257786, "spp": 30, "nm": nm_id},
            headers=_UA, timeout=20, follow_redirects=True,
        )
        if r.status_code == 200:
            j = r.json()
            # v4 отдаёт products на верхнем уровне, прежние версии — под "data"
            products = (j.get("data") or {}).get("products") or j.get("products") or []
            return products[0] if products else None
        print(f"WB public detail {nm_id} -> {r.status_code}")
    except Exception as e:
        print(f"WB public detail {nm_id} ошибка: {e}")
    return None


def _has_certificate(card_json):
    """True/False/None — нашли ли признак сертификата в публичной карточке."""
    if not isinstance(card_json, dict):
        return None
    # Прямое поле, если WB его отдаёт
    cert = card_json.get("certificate")
    if isinstance(cert, dict):
        return bool(cert.get("verified") or cert.get("id") or cert)
    if isinstance(cert, bool):
        return cert
    # Иногда сведения о сертификате/декларации лежат среди характеристик
    for opt in card_json.get("options") or []:
        name = (opt.get("name") or "").lower()
        if "сертификат" in name or "декларац" in name:
            return bool(opt.get("value"))
    # card.json получили, но признака нет — считаем, что не подтверждён
    return False


def _seller_recommendations(cj):
    """True/False — есть ли блок «Рекомендации продавца» (поле card.json)."""
    if not isinstance(cj, dict) or "has_seller_recommendations" not in cj:
        return None
    return bool(cj.get("has_seller_recommendations"))


def _has_size_grid(cj):
    """True/False — заполнена ли в карточке размерная сетка (sizes_table)."""
    if not isinstance(cj, dict):
        return None
    st = cj.get("sizes_table") or {}
    for row in st.get("values") or []:
        # есть хотя бы одна строка с заполненными деталями (обхваты/размеры)
        if any((str(d).strip() for d in (row.get("details") or []))):
            return True
    return False


def get_public_signals(nm_id):
    """Сигналы из публичной карточки WB: рич-контент, размерная сетка,
    рекомендации продавца, отметка товара на фото (всё — из card.json)."""
    cj = fetch_card_json(nm_id)
    rich = cj.get("has_rich") if isinstance(cj, dict) and "has_rich" in cj else None
    tags = cj.get("has_photo_tags") if isinstance(cj, dict) and "has_photo_tags" in cj else None
    return {
        "rich_content": rich,
        "grid_4th": _has_size_grid(cj),
        "recommendations": _seller_recommendations(cj),
        "photo_tags": (bool(tags) if tags is not None else None),
    }


def debug_dump(nm_id):
    """Сырые публичные ответы по артикулу — для донастройки селекторов."""
    cj = fetch_card_json(nm_id)
    return {
        "nm_id": nm_id,
        "basket_host": _basket_host(nm_id),
        "card_json": cj,
        "detail": fetch_detail(nm_id),
        "recommendations": _seller_recommendations(cj),
    }
