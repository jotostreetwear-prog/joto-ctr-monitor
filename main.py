import os
import json
import httpx
import schedule
import time
from datetime import datetime, timedelta
from flask import Flask, jsonify
import threading

app = Flask(__name__)

WB_API_TOKEN = os.environ.get("WB_API_TOKEN", "").strip()
B24_WEBHOOK = os.environ.get("B24_WEBHOOK", "").strip()
DIALOG_ID = "chat2024"

previous_ctr = {}


def get_wb_ctr():
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    headers = {
        "Authorization": f"Bearer {WB_API_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "period": {
            "begin": yesterday,
            "end": today
        },
        "orderBy": {
            "field": "openCardCount",
            "mode": "desc"
        },
        "page": 1
    }

    try:
        resp = httpx.post(
            "https://seller-analytics-api.wildberries.ru/api/v2/nm-report/detail",
            json=payload,
            headers=headers,
            timeout=30
        )
        print(f"WB API ответ: {resp.status_code}")
        return resp.json()
    except Exception as e:
        print(f"Ошибка WB API: {e}")
        return None


def send_b24_message(text: str):
    try:
        resp = httpx.post(
            f"{B24_WEBHOOK}/im.message.add.json",
            json={"DIALOG_ID": DIALOG_ID, "MESSAGE": text},
            timeout=10
        )
        print(f"Битрикс ответ: {resp.status_code}")
    except Exception as e:
        print(f"Ошибка отправки: {e}")


def check_ctr():
    global previous_ctr
    print(f"Проверяю CTR... {datetime.now()}")

    data = get_wb_ctr()
    if not data:
        return

    cards = data.get("data", {}).get("cards", [])
    if not cards:
        print(f"Нет карточек. Ответ: {json.dumps(data, ensure_ascii=False)[:300]}")
        return

    alerts = []

    for card in cards:
        nm_id = str(card.get("nmID", ""))
        vendor_code = card.get("vendorCode", nm_id)
        name = card.get("imtName", vendor_code)

        statistics = card.get("statistics", {})
        period_stats = statistics.get("selectedPeriod", {})

        open_card = period_stats.get("openCardCount", 0)
        view_count = period_stats.get("searchResultSuperpositionCount", 0)

        if view_count > 0:
            current_ctr = round((open_card / view_count) * 100, 2)
        else:
            current_ctr = 0

        print(f"{vendor_code}: показы={view_count}, клики={open_card}, CTR={current_ctr}%")

        if nm_id in previous_ctr:
            prev = previous_ctr[nm_id]
            drop = prev - current_ctr
            if drop >= 1.0:
                alerts.append(
                    f"Артикул: {vendor_code}\n"
                    f"Название: {name}\n"
                    f"CTR: {prev}% -> {current_ctr}% (снижение на {round(drop, 2)}%)"
                )

        previous_ctr[nm_id] = current_ctr

    if alerts:
        message = "Снижение CTR более чем на 1%\n\n" + "\n\n".join(alerts)
        send_b24_message(message)
        print(f"Отправлено {len(alerts)} уведомлений")
    else:
        print("Снижений CTR >= 1% не найдено")


def run_scheduler():
    schedule.every().day.at("09:00").do(check_ctr)
    print("Планировщик запущен — проверка каждый день в 09:00")
    while True:
        schedule.run_pending()
        time.sleep(60)


@app.route("/", methods=["GET"])
def index():
    return "CTR Monitor работает"


@app.route("/check-now", methods=["GET"])
def check_now():
    threading.Thread(target=check_ctr, daemon=True).start()
    return jsonify({"ok": True, "message": "Проверка запущена"})


@app.route("/status", methods=["GET"])
def status():
    return jsonify({"ctr_data": previous_ctr, "count": len(previous_ctr)})


if __name__ == "__main__":
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
