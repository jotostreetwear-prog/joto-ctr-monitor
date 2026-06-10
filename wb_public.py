"""Парсинг публичной карточки Wildberries — для метрик, которых нет в официальном
API: сертификаты и «Рекомендации продавца».

ВАЖНО: это парсинг внешнего фронта WB, он хрупкий — WB периодически меняет
структуру ответов и хосты basket. Поэтому всё обёрнуто в try/except и при любой
неопределённости возвращает None («не удалось определить»), а вызывающий код
откатывается на ручную отметку. Эндпоинт /checklist/debug-public показывает
сырые ответы, чтобы донастроить селекторы на боевом окружении.
"""

import httpx

CARD_DETAIL = "https://card.wb.ru/cards/v2/detail"
# Эндпоинт «Рекомендации продавца» (similar/recommended товары продавца)
SELLER_RECOM = "https://recom.wb.ru/recom/recommended"

_UA = {"User-Agent": "Mozilla/5.0 (compatible; JOTO-checklist/1.0)"}


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
    """Сырой публичный card.json карточки (описание, опции, иногда сертификат)."""
    vol = nm_id // 100000
    part = nm_id // 1000
    host = _basket_host(nm_id)
    url = f"https://{host}/vol{vol}/part{part}/{nm_id}/info/ru/card.json"
    try:
        r = httpx.get(url, headers=_UA, timeout=20, follow_redirects=True)
        if r.status_code == 200:
            return r.json()
        print(f"WB public card.json {nm_id} -> {r.status_code}")
    except Exception as e:
        print(f"WB public card.json {nm_id} ошибка: {e}")
    return None


def fetch_detail(nm_id):
    """Публичная витрина карточки (card.wb.ru): рейтинг, отзывы и пр."""
    try:
        r = httpx.get(
            CARD_DETAIL,
            params={"appType": 1, "curr": "rub", "dest": -1257786, "nm": nm_id},
            headers=_UA, timeout=20, follow_redirects=True,
        )
        if r.status_code == 200:
            products = (r.json().get("data") or {}).get("products") or []
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


def _has_seller_recommendations(nm_id):
    """True/False/None — есть ли блок «Рекомендации продавца»."""
    try:
        r = httpx.get(
            SELLER_RECOM, params={"nm": nm_id},
            headers=_UA, timeout=20, follow_redirects=True,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        # структура может отличаться — считаем рекомендации найденными,
        # если в ответе есть непустой список товаров
        products = (data.get("data") or {}).get("products") or data.get("products") or []
        return len(products) > 0
    except Exception as e:
        print(f"WB public recom {nm_id} ошибка: {e}")
        return None


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
    """Сигналы из публичной карточки WB: rich_content и наличие размерной сетки.

    Рекомендации и сертификаты через публичный API надёжно не достаются —
    они вынесены в ручные отметки, поэтому здесь их нет.
    """
    cj = fetch_card_json(nm_id)
    rich = cj.get("has_rich") if isinstance(cj, dict) and "has_rich" in cj else None
    return {
        "rich_content": rich,
        "grid_4th": _has_size_grid(cj),
    }


def debug_dump(nm_id):
    """Сырые публичные ответы по артикулу — для донастройки селекторов."""
    return {
        "nm_id": nm_id,
        "basket_host": _basket_host(nm_id),
        "card_json": fetch_card_json(nm_id),
        "detail": fetch_detail(nm_id),
        "recom_signal": _has_seller_recommendations(nm_id),
    }
