"""Анализ конкурентов на Wildberries по публичным данным.

На вход — текст со ссылками на карточки WB или номерами nm. Для каждого
артикула тянем публичные данные (цена, рейтинг, отзывы, остаток, контент)
и даём оценку продаж/выручки по числу отзывов (помечается как оценочная).
"""

import re
from concurrent.futures import ThreadPoolExecutor

import wb_public

NM_RE = re.compile(r"(\d{6,})")
# Сколько покупателей в среднем оставляют отзыв (для грубой оценки продаж)
REVIEW_RATE = 0.05  # ~5% -> множитель 20


def extract_nm_ids(text):
    """Достаёт номера артикулов из текста (ссылки WB или просто числа)."""
    seen = []
    for m in NM_RE.findall(text or ""):
        nm = int(m)
        if nm not in seen:
            seen.append(nm)
    return seen[:30]  # бережём лимиты — не больше 30 за раз


def _price(product):
    """Цена со скидкой в рублях из ответа card.wb.ru (копейки → рубли)."""
    if product.get("salePriceU"):
        return round(product["salePriceU"] / 100)
    for s in product.get("sizes") or []:
        pr = s.get("price") or {}
        v = pr.get("product") or pr.get("total") or pr.get("basic")
        if v:
            return round(v / 100)
    if product.get("priceU"):
        return round(product["priceU"] / 100)
    return None


def _analyze_one(nm):
    product = wb_public.fetch_detail(nm) or {}
    cj = wb_public.fetch_card_json(nm)

    price = _price(product)
    rating = product.get("reviewRating") or product.get("rating") or 0
    feedbacks = product.get("feedbacks") or 0
    brand = product.get("brand")
    name = product.get("name")

    photos = rich = grid = None
    desc_len = 0
    if isinstance(cj, dict):
        photos = (cj.get("media") or {}).get("photo_count")
        rich = cj.get("has_rich")
        grid = wb_public._has_size_grid(cj)
        desc_len = len(cj.get("description") or "")
        brand = brand or (cj.get("selling") or {}).get("brand_name")
        name = name or cj.get("imt_name")

    # Грубая оценка: накопленные продажи ≈ отзывы / доля оставляющих отзыв
    orders_est = round(feedbacks / REVIEW_RATE) if feedbacks else 0
    revenue_est = orders_est * price if (price and orders_est) else None

    # Контент-балл: фото≥10, рич, размерная сетка, описание
    content = sum([
        bool(photos and photos >= 10),
        bool(rich),
        bool(grid),
        desc_len >= 300,
    ])

    return {
        "nm_id": nm,
        "name": name or str(nm),
        "brand": brand or "",
        "supplier_id": product.get("supplierId"),
        "url": f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
        "price": price,
        "rating": round(rating, 2) if rating else None,
        "feedbacks": feedbacks,
        "stock": product.get("totalQuantity"),
        "photos": photos,
        "rich": rich,
        "grid": grid,
        "desc_len": desc_len,
        "content": content,        # 0..4
        "orders_est": orders_est,  # оценочно, накопленные
        "revenue_est": revenue_est,
    }


def analyze(text):
    nms = extract_nm_ids(text)
    if not nms:
        return {"items": [], "error": "Не нашёл артикулов. Вставь ссылки на карточки WB или номера nm."}
    with ThreadPoolExecutor(max_workers=8) as ex:
        items = list(ex.map(_analyze_one, nms))
    return {"items": items, "count": len(items)}
