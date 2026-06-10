"""Анализ конкурентов на Wildberries по публичным данным.

На вход — текст со ссылками на карточки WB или номерами nm. Для каждого
артикула тянем публичные данные (цена, рейтинг, отзывы, остаток, контент)
и даём оценку продаж/выручки по числу отзывов (помечается как оценочная).
"""

import os
import re
import json
import httpx
from concurrent.futures import ThreadPoolExecutor

import wb_public

NM_RE = re.compile(r"(\d{6,})")
# Сколько покупателей в среднем оставляют отзыв (для грубой оценки продаж)
REVIEW_RATE = 0.05  # ~5% -> множитель 20

# Наш бренд — для пометки «наш товар» в сравнении
OWN_BRAND = os.environ.get("OWN_BRAND", "JOTO").strip().lower()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"


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


def _characteristics(cj):
    """Словарь {характеристика: значение} из публичной карточки."""
    out = {}
    if not isinstance(cj, dict):
        return out
    for opt in cj.get("options") or []:
        name = opt.get("name")
        val = opt.get("value")
        if not name or not val:
            continue
        out[name] = ", ".join(map(str, val)) if isinstance(val, list) else str(val)
    return out


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
    chars = {}
    if isinstance(cj, dict):
        photos = (cj.get("media") or {}).get("photo_count")
        rich = cj.get("has_rich")
        grid = wb_public._has_size_grid(cj)
        desc_len = len(cj.get("description") or "")
        brand = brand or (cj.get("selling") or {}).get("brand_name")
        name = name or cj.get("imt_name")
        chars = _characteristics(cj)

    # Остаток — сумма по всем размерам и складам (в ответе v4 нет totalQuantity)
    stock = product.get("totalQuantity")
    if stock is None:
        stock = sum(
            st.get("qty", 0)
            for s in (product.get("sizes") or [])
            for st in (s.get("stocks") or [])
        )

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
        "is_ours": OWN_BRAND in (brand or "").lower(),
        "supplier_id": product.get("supplierId"),
        "url": f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
        "price": price,
        "rating": round(rating, 2) if rating else None,
        "feedbacks": feedbacks,
        "stock": stock,
        "recommendations": (cj.get("has_seller_recommendations") if isinstance(cj, dict) else None),
        "photos": photos,
        "rich": rich,
        "grid": grid,
        "desc_len": desc_len,
        "content": content,        # 0..4
        "chars": chars,            # полные характеристики
        "orders_est": orders_est,  # оценочно, накопленные
        "revenue_est": revenue_est,
    }


def _avg(values, ndigits=0):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    a = round(sum(vals) / len(vals), ndigits)
    return int(a) if ndigits == 0 else a


def _rule_verdict(ours, comp):
    """Текстовый вывод по правилам (если Gemini недоступен)."""
    if not comp:
        return "Добавь товары конкурентов (не нашего бренда), чтобы было с чем сравнивать."
    if not ours:
        return ("Добавь в список свой товар (бренда «{}»), чтобы сравнить с конкурентами — "
                "тогда покажу, где мы сильнее/слабее.").format(OWN_BRAND.upper())
    op, cp = _avg([i["price"] for i in ours]), _avg([i["price"] for i in comp])
    orat, crat = _avg([i["rating"] for i in ours], 1), _avg([i["rating"] for i in comp], 1)
    ofb, cfb = _avg([i["feedbacks"] for i in ours]), _avg([i["feedbacks"] for i in comp])
    oc, cc = _avg([i["content"] for i in ours]), _avg([i["content"] for i in comp])
    L = []
    if op and cp:
        diff = round((op - cp) / cp * 100)
        L.append(f"• Цена: у нас ≈{op}₽, у конкурентов ≈{cp}₽ "
                 f"({'дороже' if diff>0 else 'дешевле'} на {abs(diff)}%).")
    if orat and crat:
        L.append(f"• Рейтинг: у нас {orat}, у конкурентов {crat}.")
    if ofb is not None and cfb is not None:
        L.append(f"• Отзывы (≈масштаб продаж): у нас ≈{ofb}, у конкурентов ≈{cfb}.")
    if oc is not None and cc is not None:
        L.append(f"• Контент карточек: у нас {oc}/4, у конкурентов {cc}/4.")
    return "\n".join(L) if L else "Недостаточно данных для сравнения."


def _gemini_verdict(items):
    """Аналитический вывод через Gemini. None, если ключа нет или ошибка."""
    if not GEMINI_API_KEY:
        return None
    rows = []
    for i in items:
        tag = "НАШ" if i["is_ours"] else "конкурент"
        rows.append(
            f"[{tag}] {i['brand']} «{i['name']}»: цена {i['price']}₽, рейтинг {i['rating']}, "
            f"отзывов {i['feedbacks']}, контент {i['content']}/4 "
            f"(фото={i['photos']}, рич={i['rich']}, сетка={i['grid']}), "
            f"оценка накопл. продаж ≈{i['orders_est']}"
        )
    prompt = (
        "Ты аналитик маркетплейсов Wildberries. Ниже данные по нашим товарам и конкурентам.\n\n"
        + "\n".join(rows) +
        "\n\nКратко и по делу (на русском) сделай вывод для продавца:\n"
        "1) Чем конкуренты берут (цена/контент/отзывы) — 2-4 пункта.\n"
        "2) Где мы сильнее и где слабее.\n"
        "3) 3 конкретные рекомендации, что улучшить.\n"
        "Чётко разделяй факты (цена, рейтинг, отзывы) и оценки (продажи). "
        "Без воды, маркированными пунктами."
    )
    try:
        resp = httpx.post(
            f"{GEMINI_URL}/{GEMINI_MODEL}:generateContent",
            params={"key": GEMINI_API_KEY},
            headers={"content-type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.4, "maxOutputTokens": 700}},
            timeout=60,
        )
        if resp.status_code != 200:
            print(f"Конкуренты/Gemini: {resp.status_code}: {resp.text[:200]}")
            return None
        cands = resp.json().get("candidates") or []
        if not cands:
            return None
        parts = (cands[0].get("content") or {}).get("parts") or [{}]
        return parts[0].get("text", "").strip() or None
    except Exception as e:
        print(f"Конкуренты/Gemini: ошибка {e}")
        return None


def debug(nm):
    """Перебирает кандидатов публичных эндпоинтов WB и basket-хостов,
    чтобы найти рабочие (v2 card.wb.ru отключён с дек.2025)."""
    ua = {"User-Agent": "Mozilla/5.0 (compatible; JOTO/1.0)"}
    vol, part = nm // 100000, nm // 1000
    out = {"nm": nm, "vol": vol, "price_endpoints": {}, "basket_scan": None}

    # Кандидаты эндпоинтов цены/рейтинга/отзывов
    candidates = [
        ("v2_card", "https://card.wb.ru/cards/v2/detail",
         {"appType": 1, "curr": "rub", "dest": -1257786, "spp": 30, "nm": nm}),
        ("v1_card", "https://card.wb.ru/cards/v1/detail",
         {"appType": 1, "curr": "rub", "dest": -1257786, "nm": nm}),
        ("v2_ucard", "https://u-card.wb.ru/cards/v2/detail",
         {"appType": 1, "curr": "rub", "dest": -1257786, "spp": 30, "nm": nm}),
        ("v4_card", "https://card.wb.ru/cards/v4/detail",
         {"appType": 1, "curr": "rub", "dest": -1257786, "spp": 30, "nm": nm}),
    ]
    for tag, url, params in candidates:
        try:
            r = httpx.get(url, params=params, headers=ua, timeout=15)
            info = {"status": r.status_code}
            if r.status_code == 200:
                try:
                    prods = (r.json().get("data") or {}).get("products") or r.json().get("products") or []
                    info["products"] = len(prods)
                    if prods:
                        p = prods[0]
                        info["sample"] = {k: p.get(k) for k in
                                          ("name", "salePriceU", "reviewRating", "feedbacks", "sizes")}
                except Exception as e:
                    info["parse_err"] = str(e)
            out["price_endpoints"][tag] = info
        except Exception as e:
            out["price_endpoints"][tag] = {"exc": repr(e)}

    # Скан basket-хостов для card.json (находим правильный для этого vol)
    for n in range(1, 46):
        host = f"basket-{n:02d}.wbbasket.ru"
        url = f"https://{host}/vol{vol}/part{part}/{nm}/info/ru/card.json"
        try:
            r = httpx.get(url, headers=ua, timeout=8)
            if r.status_code == 200:
                out["basket_scan"] = {"host": host, "n": n}
                break
        except Exception:
            continue
    out["basket_computed"] = wb_public._basket_host(nm)
    return out


def analyze(text):
    nms = extract_nm_ids(text)
    if not nms:
        return {"items": [], "error": "Не нашёл артикулов. Вставь ссылки на карточки WB или номера nm."}
    with ThreadPoolExecutor(max_workers=8) as ex:
        items = list(ex.map(_analyze_one, nms))

    ours = [i for i in items if i["is_ours"]]
    comp = [i for i in items if not i["is_ours"]]
    verdict = _gemini_verdict(items) or _rule_verdict(ours, comp)

    return {
        "items": items,
        "count": len(items),
        "verdict": verdict,
        "verdict_ai": bool(GEMINI_API_KEY),
    }
