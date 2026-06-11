import os
import json
import httpx
import threading
import schedule
import time
from flask import Flask, request, jsonify, render_template
from datetime import datetime, timedelta

import checklist
import vision
import competitors

app = Flask(__name__)

WB_API_TOKEN = os.environ.get("WB_API_TOKEN", "").strip()


def _normalize_webhook(url):
    """Приводит URL вебхука к базовому виду .../rest/<id>/<токен>.

    Отрезает случайно скопированный с конца метод (например /profile.json),
    из-за которого все REST-запросы ломаются с ERROR_METHOD_NOT_FOUND.
    """
    url = url.strip().rstrip("/")
    last = url.rsplit("/", 1)[-1] if "/" in url else ""
    # токен вебхука не содержит точки, а метод (profile.json) — содержит
    if "." in last:
        url = url.rsplit("/", 1)[0]
    return url


B24_WEBHOOK = _normalize_webhook(os.environ.get("B24_WEBHOOK", ""))

# ID пользователя Татьяны в Битрикс24 — для личных уведомлений о бюджете кампаний
TATIANA_USER_ID = os.environ.get("TATIANA_USER_ID", "232").strip()
# Порог остатка бюджета (₽), ниже которого шлём уведомление
BUDGET_THRESHOLD = int(os.environ.get("BUDGET_THRESHOLD", "100"))
# ID чат-бота в Битрикс24 — если задан, сообщения шлются от имени бота
B24_BOT_ID = os.environ.get("B24_BOT_ID", "").strip()

# ===================== БИТРИКС =====================

def send_b24_message(dialog_id, text, from_bot=False):
    """Отправляет сообщение в Битрикс. Возвращает (status_code, тело_ответа).

    from_bot=True и заданный B24_BOT_ID — сообщение уходит от имени бота
    (imbot.message.add), иначе от имени владельца вебхука (im.message.add).
    """
    try:
        if from_bot and B24_BOT_ID:
            url = f"{B24_WEBHOOK}/imbot.message.add.json"
            payload = {"BOT_ID": B24_BOT_ID, "DIALOG_ID": dialog_id, "MESSAGE": text}
        else:
            url = f"{B24_WEBHOOK}/im.message.add.json"
            payload = {"DIALOG_ID": dialog_id, "MESSAGE": text}
        resp = httpx.post(url, json=payload, timeout=10)
        print(f"Ответ Битрикс: {resp.status_code} {resp.text[:200]}")
        return resp.status_code, resp.text
    except Exception as e:
        print(f"Ошибка отправки: {e}")
        return None, str(e)

def register_b24_bot():
    """Регистрирует чат-бота в Битрикс24. Возвращает (status_code, тело_ответа).

    В теле ответа поле result — это BOT_ID, который нужно прописать
    в переменную окружения B24_BOT_ID.
    """
    try:
        url = f"{B24_WEBHOOK}/imbot.register.json"
        payload = {
            "CODE": "joto_wb_monitor",
            "TYPE": "B",
            "EVENT_MESSAGE_ADD": "",
            "EVENT_WELCOME_MESSAGE": "",
            "EVENT_BOT_DELETE": "",
            "OPENLINE": "N",
            "PROPERTIES": {
                "NAME": "JOTO Монитор",
                "COLOR": "AZURE",
                "WORK_POSITION": "Реклама и CTR Wildberries",
            },
        }
        resp = httpx.post(url, json=payload, timeout=10)
        print(f"Регистрация бота: {resp.status_code} {resp.text[:300]}")
        return resp.status_code, resp.text
    except Exception as e:
        print(f"Ошибка регистрации бота: {e}")
        return None, str(e)

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
        send_b24_message(TATIANA_USER_ID, msg, from_bot=True)
        print(f"Отправлено {len(alerts)} уведомлений Татьяне")
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
        send_b24_message(TATIANA_USER_ID, msg, from_bot=True)
        print(f"Отправлено {len(alerts)} уведомлений Татьяне")
    else:
        print(f"Кампаний с бюджетом ниже {BUDGET_THRESHOLD} ₽ не найдено")

# ===================== ЧЕК-ЛИСТ КАРТОЧЕК =====================

def notify_checklist(force_compute=False):
    """Считает чек-лист и шлёт Татьяне сводку по готовности карточек."""
    print(f"Чек-лист: уведомление {datetime.now()}")
    if not WB_API_TOKEN:
        print("Чек-лист: нет WB_API_TOKEN")
        return

    data = checklist.compute_checklist() if force_compute else checklist.get_cached()
    if not data.get("items"):
        data = checklist.compute_checklist()
    items = data.get("items") or []
    s = data.get("summary") or {}
    if not items:
        print("Чек-лист: нет данных для уведомления")
        return

    worst = [i for i in items if i["score"] < 100][:10]
    lines = [
        f"📋 *Чек-лист карточек WB* (на {data.get('checked_at','')})",
        f"Средний балл: *{s.get('avg_score',0)}%*",
        f"Готовы: {s.get('ready',0)} • С недочётами: {s.get('with_issues',0)} • "
        f"Всего артикулов: {s.get('total',0)}",
    ]
    if worst:
        lines.append("\nТоп артикулов с недочётами:")
        for i in worst:
            lines.append(f"🔴 {i['name']} (nm {i['nm_id']}) — {i['score']}%")

    if not B24_WEBHOOK:
        print("Чек-лист: нет B24_WEBHOOK — не шлём")
        return
    send_b24_message(TATIANA_USER_ID, "\n".join(lines), from_bot=True)
    print(f"Чек-лист: сводка отправлена Татьяне ({len(worst)} с недочётами)")


@app.route("/competitors", methods=["GET", "POST"])
def competitors_page():
    return render_template("competitors.html")


@app.route("/competitors/analyze", methods=["POST"])
def competitors_analyze():
    body = request.get_json(silent=True) or {}
    return jsonify(competitors.analyze(body.get("text", "")))


@app.route("/competitors/debug", methods=["GET"])
def competitors_debug():
    nm = request.args.get("nm", type=int)
    if not nm:
        return jsonify({"error": "укажите ?nm=<артикул>"}), 400
    return jsonify(competitors.debug(nm))


@app.route("/checklist", methods=["GET", "POST"])
def checklist_page():
    # POST приходит, когда страница открыта как приложение в Битрикс24 (iframe)
    return render_template("checklist.html")


# Путь установки для Битрикс24. Отдаёт ту же страницу чек-листа: она и
# завершает установку (BX24.installFinish), и сразу показывает таблицу —
# поэтому неважно, какой путь прописан в настройках приложения.
@app.route("/checklist/install", methods=["GET", "POST"])
def checklist_install():
    return render_template("checklist.html")


@app.route("/checklist/data", methods=["GET"])
def checklist_data():
    data = checklist.get_cached()
    return jsonify({
        "checked_at": data.get("checked_at"),
        "items": data.get("items", []),
        "summary": data.get("summary", {}),
        "metrics": checklist.metrics_meta(),
        "computing": checklist.is_computing(),
        "vision": vision.enabled(),
        "vision_mode": vision.mode(),
    })


@app.route("/checklist/refresh", methods=["POST", "GET"])
def checklist_refresh():
    threading.Thread(target=checklist.compute_checklist, daemon=True).start()
    return jsonify({"ok": True, "message": "Пересчёт чек-листа запущен"})


@app.route("/checklist/overrides-info", methods=["GET"])
def checklist_overrides_info():
    return jsonify(checklist.overrides_info())


@app.route("/checklist/override", methods=["POST"])
def checklist_override():
    body = request.get_json(silent=True) or {}
    ok = checklist.set_override(body.get("nm_id"), body.get("metric"), body.get("value"))
    return jsonify({"ok": ok})


@app.route("/checklist/notify-now", methods=["GET"])
def checklist_notify_now():
    threading.Thread(target=notify_checklist, kwargs={"force_compute": True}, daemon=True).start()
    return jsonify({"ok": True, "message": "Сводка чек-листа будет отправлена Татьяне"})


@app.route("/checklist/debug", methods=["GET"])
def checklist_debug():
    """Диагностика доступа WB_API_TOKEN к Контенту/Ценам/Отзывам."""
    return jsonify(checklist.diagnose())


@app.route("/checklist/debug-card", methods=["GET"])
def checklist_debug_card():
    """Сырая первая карточка Content API — для сверки имён полей (фото, видео)."""
    return jsonify(checklist.debug_first_card())


@app.route("/checklist/debug-public", methods=["GET"])
def checklist_debug_public():
    """Сырые публичные ответы WB по артикулу — для донастройки парсинга."""
    import wb_public
    nm = request.args.get("nm", type=int)
    if not nm:
        return jsonify({"error": "укажите ?nm=<артикул>"}), 400
    return jsonify(wb_public.debug_dump(nm))


# ===================== FLASK =====================

@app.route("/", methods=["GET"])
def index():
    return (
        "JOTO CTR Monitor работает ✓<br>"
        "Чек-лист карточек: <a href='/checklist'>/checklist</a><br>"
        "Анализ конкурентов: <a href='/competitors'>/competitors</a>"
    )

@app.route("/test-notify", methods=["GET"])
def test_notify():
    status, body = send_b24_message(
        TATIANA_USER_ID,
        "✅ Тест: CTR-монитор работает. Сюда будут приходить уведомления о снижении CTR по артикулам.",
        from_bot=True,
    )
    return jsonify({
        "ok": status == 200,
        "dialog_id": TATIANA_USER_ID,
        "bitrix_status": status,
        "bitrix_response": body,
    })

@app.route("/check-now", methods=["GET"])
def check_now():
    threading.Thread(target=check_ctr).start()
    return jsonify({"ok": True, "message": "CTR проверка запущена"})

@app.route("/check-budget-now", methods=["GET"])
def check_budget_now():
    threading.Thread(target=check_budgets).start()
    return jsonify({"ok": True, "message": "Проверка бюджетов запущена"})

@app.route("/test-budget-notify", methods=["GET"])
def test_budget_notify():
    # DIALOG_ID можно переопределить в URL: /test-budget-notify?to=232
    dialog_id = request.args.get("to", TATIANA_USER_ID)
    status, body = send_b24_message(
        dialog_id,
        "✅ Тест: уведомления о бюджете рекламных кампаний подключены. "
        "Сюда будут приходить сообщения, когда остаток бюджета кампании станет меньше "
        f"{BUDGET_THRESHOLD} ₽.",
        from_bot=True,
    )
    return jsonify({
        "ok": status == 200,
        "dialog_id": dialog_id,
        "from_bot": bool(B24_BOT_ID),
        "bitrix_status": status,
        "bitrix_response": body,
    })

@app.route("/register-bot", methods=["GET"])
def register_bot():
    status, body = register_b24_bot()
    return jsonify({
        "ok": status == 200,
        "bitrix_status": status,
        "bitrix_response": body,
        "hint": "Возьмите число из поля result и пропишите его в переменную окружения B24_BOT_ID на Railway",
    })

@app.route("/debug-bitrix", methods=["GET"])
def debug_bitrix():
    """Диагностика: какой вебхук используется, чей аккаунт и какие у него права."""
    # маскируем токен в URL вебхука (оставляем домен и id пользователя)
    masked = B24_WEBHOOK
    parts = B24_WEBHOOK.rstrip("/").split("/")
    if len(parts) >= 1:
        parts[-1] = "***токен***"
        masked = "/".join(parts)

    out = {"webhook_url": masked, "bot_id_env": B24_BOT_ID or "(не задан)"}

    # profile — чей это аккаунт
    try:
        r = httpx.get(f"{B24_WEBHOOK}/profile.json", timeout=10)
        out["profile"] = r.json()
    except Exception as e:
        out["profile_error"] = str(e)

    # scope — какие права у вебхука (ищем "im")
    try:
        r = httpx.get(f"{B24_WEBHOOK}/scope.json", timeout=10)
        out["scope"] = r.json()
    except Exception as e:
        out["scope_error"] = str(e)

    return jsonify(out)

# ===================== ЗАПУСК =====================

def run_scheduler():
    schedule.every().day.at("06:00").do(check_ctr)
    # Проверка остатка бюджета кампаний каждые полчаса
    schedule.every(30).minutes.do(check_budgets)
    # Сводка по чек-листу карточек — раз в день
    schedule.every().day.at("07:00").do(notify_checklist)
    print("Планировщик запущен — CTR в 09:00 МСК, бюджет каждые 30 мин, чек-лист в 10:00 МСК")
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    # Первичный расчёт чек-листа в фоне, чтобы дашборд сразу был с данными
    threading.Thread(target=checklist.compute_checklist, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
