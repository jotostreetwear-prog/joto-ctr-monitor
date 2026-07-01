"""Мониторинг СПП (скидки постоянного покупателя) по товарам WB.

СПП вычисляем из публичной карточки card.wb.ru: цена без СПП (basic) и итоговая
цена с СПП для покупателя (product/total). СПП% = (basic - client) / basic * 100.

Предыдущие значения СПП храним в файле (переживает редеплой на Railway), чтобы
ловить изменения. Уведомление шлётся, когда СПП изменилась на порог (в п.п.)
и больше — в любую сторону.
"""

import os
import json
import time
import httpx
import wb_public
import checklist

# Порог изменения СПП в процентных пунктах для уведомления (по умолчанию 3)
SPP_THRESHOLD = float(os.environ.get("SPP_THRESHOLD", "3"))

_VOL = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
SPP_STATE = (
    os.environ.get("SPP_STATE")
    or (os.path.join(_VOL, "spp_state.json") if _VOL
        else "/data/spp_state.json" if os.path.isdir("/data")
        else "spp_state.json")
)


def compute_spp(seller, client):
    """СПП% = (цена продавца − цена покупателя) / цена продавца."""
    if not seller or not client or seller <= 0:
        return None
    return round((seller - float(client)) / float(seller) * 100.0, 1)


# WB-Кошелёк — доп. скидка сверх СПП, которую учитывает кабинет/Эвирма (в %).
# Даёт точное совпадение с кабинетом. Настраивается переменной SPP_WALLET_PCT.
SPP_WALLET_PCT = float(os.environ.get("SPP_WALLET_PCT", "2.5"))


def prices_full_map():
    """{nmID(str): {sizeID(str): цена продавца после скидки ₽}} по всему кабинету."""
    out = {}
    headers = {"Authorization": checklist.WB_PRICES_TOKEN}
    offset = 0
    while True:
        try:
            r = httpx.get(
                f"{checklist.PRICES_API}/api/v2/list/goods/filter",
                headers=headers, params={"limit": 1000, "offset": offset}, timeout=30,
            )
        except Exception as e:
            print(f"СПП: prices ошибка {e}")
            break
        if r.status_code != 200:
            print(f"СПП: prices {r.status_code}: {r.text[:150]}")
            break
        goods = (r.json().get("data") or {}).get("listGoods") or []
        if not goods:
            break
        for g in goods:
            sizes = {}
            for s in g.get("sizes") or []:
                dp = s.get("discountedPrice") or s.get("price")
                if dp:
                    sizes[str(s.get("sizeID"))] = dp
            if sizes:
                out[str(g.get("nmID"))] = sizes
        if len(goods) < 1000:
            break
        offset += 1000
        time.sleep(0.6)
    return out


def public_instock(nm_id):
    """(sizeID, цена покупателя ₽) для размера В НАЛИЧИИ из публичной карточки WB.
    Кабинет считает СПП по цене товара, который реально продаётся."""
    prod = wb_public.fetch_detail(nm_id)
    if not isinstance(prod, dict):
        return None, None
    for s in prod.get("sizes") or []:
        p = (s or {}).get("price")
        if isinstance(p, dict) and p.get("product"):
            return str(s.get("optionId")), p["product"] / 100.0
    return None, None


def spp_for(nm_id, sizes=None):
    """СПП по артикулу как в кабинете: база — цена продавца ТОГО ЖЕ размера,
    что в продаже; цена покупателя — с учётом WB-Кошелька.
    Возвращает {nm_id, seller, client, spp} или None. sizes — {sizeID: цена}."""
    if sizes is None:
        sizes = prices_full_map().get(str(nm_id)) or {}
    if not sizes:
        return None
    size_id, client = public_instock(nm_id)
    if client is None:
        return None
    base = sizes.get(size_id) or min(sizes.values())  # цена того же размера
    client_adj = client * (1 - SPP_WALLET_PCT / 100.0)  # учёт WB-Кошелька
    val = compute_spp(base, client_adj)
    if val is None:
        return None
    return {
        "nm_id": nm_id,
        "seller": round(float(base), 2),
        "client": round(client_adj, 2),
        "spp": val,
    }


def debug_full(nm):
    """Диагностика по товару: сопоставление размера, цены и итоговая СПП."""
    pmap = prices_full_map()
    sizes = pmap.get(str(nm)) or {}
    size_id, client = public_instock(nm)
    base = sizes.get(size_id) or (min(sizes.values()) if sizes else None)
    out = {
        "nm_id": nm,
        "size_id_in_stock": size_id,
        "seller_price_same_size": base,
        "client_no_wallet": client,
        "wallet_pct": SPP_WALLET_PCT,
        "all_sizes_prices": sizes,
        "prices_count": len(pmap),
    }
    res = spp_for(nm, sizes=sizes)
    if res:
        out["client_with_wallet"] = res["client"]
        out["spp"] = res["spp"]
    return out


def raw_price_good(nm):
    """Полный объект товара из Prices API WB (все поля, все размеры)."""
    headers = {"Authorization": checklist.WB_PRICES_TOKEN}
    offset = 0
    while True:
        try:
            r = httpx.get(
                f"{checklist.PRICES_API}/api/v2/list/goods/filter",
                headers=headers, params={"limit": 1000, "offset": offset}, timeout=30,
            )
        except Exception as e:
            return {"error": str(e)}
        if r.status_code != 200:
            return {"error": f"{r.status_code}: {r.text[:200]}"}
        goods = (r.json().get("data") or {}).get("listGoods") or []
        if not goods:
            break
        for g in goods:
            if str(g.get("nmID")) == str(nm):
                return g
        if len(goods) < 1000:
            break
        offset += 1000
        time.sleep(0.6)
    return {"error": "nm не найден в Prices API"}


def raw_dump(nm):
    """Все сырые данные по товару — чтобы найти поле СПП."""
    prod = wb_public.fetch_detail(nm)
    return {
        "nm_id": nm,
        "prices_api_good": raw_price_good(nm),
        "public_sizes": (prod.get("sizes") if isinstance(prod, dict) else None),
        "public_extended": (prod.get("extended") if isinstance(prod, dict) else None),
    }


def load_state():
    try:
        with open(SPP_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"СПП: не прочитал {SPP_STATE}: {e}")
        return {}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(SPP_STATE) or ".", exist_ok=True)
        with open(SPP_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"СПП: не сохранил {SPP_STATE}: {e}")
        return False


def check_changes(products, seed=False):
    """products — список dict {nm_id, name}. Считает СПП по каждому, сравнивает
    с прошлым значением. Возвращает список изменений:
        {nm_id, name, old, new, delta}
    где |delta| >= SPP_THRESHOLD. seed=True — только запомнить, без изменений.
    """
    state = load_state()
    prices = prices_full_map()  # цены продавца по размерам — один раз
    changes = []
    for it in products:
        nm = str(it.get("nm_id") or it.get("nmID") or "")
        if not nm:
            continue
        cur = spp_for(nm, sizes=prices.get(nm))
        time.sleep(0.15)  # бережём публичный API WB
        if cur is None:
            continue
        new_spp = cur["spp"]
        prev = state.get(nm)
        old_spp = prev.get("spp") if isinstance(prev, dict) else prev
        state[nm] = {"spp": new_spp, "client": cur["client"], "seller": cur["seller"]}
        if seed or old_spp is None:
            continue
        delta = round(new_spp - old_spp, 1)
        if abs(delta) >= SPP_THRESHOLD:
            seller = cur.get("seller") or 0
            new_client = cur.get("client") or 0
            old_client = round(seller * (1 - old_spp / 100.0), 0) if seller else 0
            changes.append({
                "nm_id": nm,
                "name": it.get("name") or nm,
                "old": old_spp,
                "new": new_spp,
                "delta": delta,
                "seller": seller,
                "old_client": old_client,
                "new_client": round(new_client, 0),
            })
    save_state(state)
    return changes


def recommendation(delta):
    """Совет по изменению СПП. delta < 0 — СПП упала (цена для покупателя выросла)."""
    if delta <= 0:
        return ("⚠️ Цена для покупателя выросла — возможен спад заказов. "
                "Совет: снизить свою цену/скидку продавца или зайти в акцию, "
                "чтобы удержать итоговую цену; либо усилить рекламу.")
    return ("✅ Цена для покупателя упала (за счёт WB) — товар стал привлекательнее. "
            "Совет: усилить рекламу/поднять ставки для роста заказов, "
            "либо аккуратно поднять свою цену, сохранив привлекательность.")
