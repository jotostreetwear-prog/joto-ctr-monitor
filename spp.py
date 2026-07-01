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
import wb_public

# Порог изменения СПП в процентных пунктах для уведомления
SPP_THRESHOLD = float(os.environ.get("SPP_THRESHOLD", "1.5"))

_VOL = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
SPP_STATE = (
    os.environ.get("SPP_STATE")
    or (os.path.join(_VOL, "spp_state.json") if _VOL
        else "/data/spp_state.json" if os.path.isdir("/data")
        else "spp_state.json")
)


def _extract_basic_client(product):
    """Возвращает (basic, client) в рублях из публичной карточки или (None, None).

    basic  — цена без СПП (после скидки продавца);
    client — итоговая цена с СПП (что платит покупатель).
    Цены в публичном API WB — в копейках (×100).
    """
    if not isinstance(product, dict):
        return None, None
    for s in product.get("sizes") or []:
        p = (s or {}).get("price") or {}
        basic = p.get("basic")
        client = p.get("product") or p.get("total")
        if basic and client:
            return basic / 100.0, client / 100.0
    ext = product.get("extended") or {}
    if ext.get("basicPriceU") and ext.get("clientPriceU"):
        return ext["basicPriceU"] / 100.0, ext["clientPriceU"] / 100.0
    return None, None


def spp_for(nm_id):
    """СПП по одному артикулу: {nm_id, basic, client, spp} или None."""
    prod = wb_public.fetch_detail(nm_id)
    basic, client = _extract_basic_client(prod)
    if not basic or not client or basic <= 0:
        return None
    return {
        "nm_id": nm_id,
        "basic": round(basic, 2),
        "client": round(client, 2),
        "spp": round((basic - client) / basic * 100.0, 1),
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
    changes = []
    for it in products:
        nm = str(it.get("nm_id") or it.get("nmID") or "")
        if not nm:
            continue
        cur = spp_for(nm)
        time.sleep(0.15)  # бережём публичный API WB
        if cur is None:
            continue
        new_spp = cur["spp"]
        prev = state.get(nm)
        old_spp = prev.get("spp") if isinstance(prev, dict) else prev
        state[nm] = {"spp": new_spp, "client": cur["client"], "basic": cur["basic"]}
        if seed or old_spp is None:
            continue
        delta = round(new_spp - old_spp, 1)
        if abs(delta) >= SPP_THRESHOLD:
            changes.append({
                "nm_id": nm,
                "name": it.get("name") or nm,
                "old": old_spp,
                "new": new_spp,
                "delta": delta,
            })
    save_state(state)
    return changes
