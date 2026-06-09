import os
import json
import httpx
import threading
import schedule
import time
from flask import Flask, request, jsonify
from datetime import datetime, timedelta

app = Flask(__name__)

WB_API_TOKEN = os.environ.get("WB_API_TOKEN", "").strip()
B24_WEBHOOK = os.environ.get("B24_WEBHOOK", "").strip()

# ID пользователя Татьяны в Битрикс24 — для личных уведомлений о бюджете кампаний
TATIANA_USER_ID = os.environ.get("TATIANA_USER_ID", "232").strip()
# Порог остатка бюджета (₽), ниже которого шлём уведомление
BUDGET_THRESHOLD = int(os.environ.get("BUDGET_THRESHOLD", "100"))

# ===================== БИТРИКС =====================

def send_b24_message(dialog_id, text):
    try:
        url = f"{B24_WEBHOOK}/im.message.add.json"
        resp = httpx.post(url, json={"DIALOG_ID": dialog_id, "MESSAGE": text}, timeout=10)
        print(f"Ответ Битрикс: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"Ошибка отправки: {e}")

# ===================== CTR МОНИТОРИНГ =====================

# Хранилище предыдущих значений CTR (в памяти)
previous_ctr = {}

def get_wb_ctr():
    try:
        today = datetime.now().date()
        date_from = (today - timedelta(days=2)).strftime("%Y-%m-%d")
        date_to = today.strftime("%Y-%m-%d")

        url = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"
        headers = {"Authorization": WB_API_TOKEN}
        payload = {
            "dateFrom": date_from,
            "dateTo": date_to,
            "limit": 100,
            "offset": 0,
            "orderBy": {"field": "addToCartCount", "mode": "desc"},
            "selectedPeriod": {
                "begin": date_from,
                "end": date_to
            }
        }

        resp = httpx.post(url, headers=headers, json=payload, timeout=30)
        print(f"WB API статус: {resp.status_code}")
        if resp.status_code != 200:
            print(f"WB API ошибка: {resp.text[:300]}")
            return {}

        data = resp.json()
        items = data.get("data", {}).get("products", []) or data.get("products", []) or []

        result = {}
        for item in items:
            nm_id = item.get("nmID") or item.get("nmId")
            name = item.get("vendorCode", str(nm_id))
            views = item.get("openCardCount", 0) or 0
            clicks = item.get("addToCartCount", 0) or 0
            print(f"{name}: показы={views}, клики={clicks}, CTR={round(clicks/views*100,2) if views>0 else 0}%")
            if nm_id and views > 0:
                result[nm_id] = {
                    "ctr": round(clicks / views * 100, 2),
                    "name": name
                }

        print(f"Получено артикулов: {len(result)}")
        return result

    except Exception as e:
        print(f"Ошибка WB API: {e}")
        return {}

def check_ctr():
    global previous_ctr
    print(f"Проверка CTR: {datetime.now()}")

    if not WB_API_TOKEN or not B24_WEBHOOK:
        print("Нет токенов")
        return

    current = get_wb_ctr()
    if not current:
        print("Нет данных CTR")
        return

    alerts = []

    for nm_id, data in current.items():
        ctr = data["ctr"]
        name = data["name"]

        if nm_id in previous_ctr:
            prev_ctr = previous_ctr[nm_id]["ctr"]
            if prev_ctr > 0 and (prev_ctr - ctr) >= 1.0:
                alerts.append(
                    f"⚠️ {name} (арт. {nm_id}): CTR снизился с {prev_ctr}% до {ctr}% (−{round(prev_ctr-ctr,2)}%)"
                )

    # Сохраняем текущие данные
    previous_ctr = current

    if alerts:
        msg = "📉 *Снижение CTR на Wildberries:*\n\n" + "\n".join(alerts)
        send_b24_message("chat2024", msg)
        print(f"Отправлено {len(alerts)} уведомлений")
    else:
        print("Снижений CTR >= 1% не найдено")

# ===================== БЮДЖЕТ РЕКЛАМНЫХ КАМПАНИЙ =====================

ADV_API_BASE = "https://advert-api.wildberries.ru"

def get_active_adverts():
    """Возвращает список активных кампаний: [{"id": advertId, "name": название}]."""
    headers = {"Authorization": WB_API_TOKEN}

    # 1. Список кампаний, сгруппированных по типу и статусу
    try:
        resp = httpx.get(f"{ADV_API_BASE}/adv/v1/promotion/count", headers=headers, timeout=30)
        print(f"WB Adv count статус: {resp.status_code}")
        if resp.status_code != 200:
            print(f"WB Adv count ошибка: {resp.text[:300]}")
            return []
        groups = resp.json().get("adverts") or []
    except Exception as e:
        print(f"Ошибка WB Adv count: {e}")
        return []

    # Собираем id кампаний со статусом 9 (идут показы)
    advert_ids = []
    for group in groups:
        if group.get("status") != 9:
            continue
        for adv in group.get("advert_list") or []:
            adv_id = adv.get("advertId")
            if adv_id:
                advert_ids.append(adv_id)

    if not advert_ids:
        print("Активных кампаний не найдено")
        return []

    # 2. Названия кампаний (POST принимает не более 50 id за раз)
    names = {}
    for i in range(0, len(advert_ids), 50):
        chunk = advert_ids[i:i + 50]
        try:
            resp = httpx.post(
                f"{ADV_API_BASE}/adv/v1/promotion/adverts",
                headers=headers, json=chunk, timeout=30,
            )
            if resp.status_code == 200:
                for adv in resp.json() or []:
                    names[adv.get("advertId")] = adv.get("name") or str(adv.get("advertId"))
            else:
                print(f"WB Adv adverts ошибка: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"Ошибка WB Adv adverts: {e}")

    return [{"id": adv_id, "name": names.get(adv_id, str(adv_id))} for adv_id in advert_ids]

def get_advert_budget(advert_id):
    """Остаток бюджета кампании в рублях, либо None при ошибке."""
    headers = {"Authorization": WB_API_TOKEN}
    try:
        resp = httpx.get(
            f"{ADV_API_BASE}/adv/v1/budget",
            headers=headers, params={"id": advert_id}, timeout=30,
        )
        if resp.status_code != 200:
            print(f"WB Adv budget {advert_id} ошибка: {resp.status_code} {resp.text[:200]}")
            return None
        return resp.json().get("total")
    except Exception as e:
        print(f"Ошибка WB Adv budget {advert_id}: {e}")
        return None

def check_budgets():
    print(f"Проверка бюджетов: {datetime.now()}")

    if not WB_API_TOKEN or not B24_WEBHOOK:
        print("Нет токенов")
        return

    if not TATIANA_USER_ID:
        print("Не задан TATIANA_USER_ID — некуда слать уведомление о бюджете")
        return

    adverts = get_active_adverts()
    if not adverts:
        print("Нет активных кампаний для проверки бюджета")
        return

    alerts = []
    for adv in adverts:
        budget = get_advert_budget(adv["id"])
        print(f"Кампания {adv['name']} (ID {adv['id']}): бюджет={budget} ₽")
        if budget is not None and budget < BUDGET_THRESHOLD:
            alerts.append(
                f"🔴 Рекламная кампания «{adv['name']}» (ID {adv['id']}): "
                f"остаток бюджета {budget} ₽ — срочное пополнение бюджета!"
            )
        time.sleep(0.5)  # бережём лимиты WB API

    if alerts:
        msg = "💰 *Низкий бюджет рекламных кампаний WB:*\n\n" + "\n".join(alerts)
        send_b24_message(TATIANA_USER_ID, msg)
        print(f"Отправлено {len(alerts)} уведомлений Татьяне")
    else:
        print(f"Кампаний с бюджетом ниже {BUDGET_THRESHOLD} ₽ не найдено")

# ===================== FLASK =====================

@app.route("/", methods=["GET"])
def index():
    return "JOTO CTR Monitor работает ✓"

@app.route("/test-notify", methods=["GET"])
def test_notify():
    send_b24_message("chat2024", "✅ Тест: CTR монитор работает и подключён к этому чату!")
    return jsonify({"ok": True, "message": "Тестовое уведомление отправлено"})

@app.route("/check-now", methods=["GET"])
def check_now():
    threading.Thread(target=check_ctr).start()
    return jsonify({"ok": True, "message": "CTR проверка запущена"})

@app.route("/check-budget-now", methods=["GET"])
def check_budget_now():
    threading.Thread(target=check_budgets).start()
    return jsonify({"ok": True, "message": "Проверка бюджетов запущена"})

# ===================== ЗАПУСК =====================

def run_scheduler():
    schedule.every().day.at("06:00").do(check_ctr)
    # Проверка остатка бюджета кампаний каждый час
    schedule.every().hour.do(check_budgets)
    print("Планировщик запущен — CTR каждый день в 09:00 МСК, бюджет — каждый час")
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
