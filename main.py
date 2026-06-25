import os
import json
import httpx
import threading
import schedule
import time
from flask import Flask, request, jsonify, render_template
from datetime import datetime, timedelta, timezone

import checklist
import vision
import competitors
import vacations
import meetings

app = Flask(__name__)

# Москва (UTC+3) — Railway работает в UTC
MSK = timezone(timedelta(hours=3))

# Лимит длины одного сообщения Битрикс (с запасом)
BITRIX_MSG_LIMIT = int(os.environ.get("BITRIX_MSG_LIMIT", "15000"))


def _split_for_bitrix(text, limit=None):
    """Разбивает длинный текст на части по лимиту Битрикс, не разрывая строки."""
    limit = limit or BITRIX_MSG_LIMIT
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


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

# Релей joto-agent — ЕДИНЫЙ канал отправки всех уведомлений: сообщения уходят
# в Битрикс от имени бота JOTO. POST на JOTO_AGENT_RELAY_URL с токеном.
JOTO_AGENT_RELAY_URL = os.environ.get("JOTO_AGENT_RELAY_URL", "").strip().rstrip("/")
JOTO_AGENT_RELAY_TOKEN = os.environ.get("JOTO_AGENT_RELAY_TOKEN", "").strip()

# ===================== БИТРИКС =====================

def _send_via_relay(dialog_id, text):
    """Отправка через релей joto-agent (от имени бота JOTO). (status, тело)."""
    try:
        resp = httpx.post(
            JOTO_AGENT_RELAY_URL,
            headers={"Authorization": f"Bearer {JOTO_AGENT_RELAY_TOKEN}"},
            json={"dialog_id": str(dialog_id), "message": text},
            timeout=15,
        )
        print(f"Релей JOTO: {resp.status_code} {resp.text[:200]}")
        return resp.status_code, resp.text
    except Exception as e:
        print(f"Ошибка релея JOTO: {e}")
        return None, str(e)


def send_b24_message(dialog_id, text, from_bot=False):
    """Единая отправка всех уведомлений сервиса в Битрикс через бота JOTO.

    Все сообщения (checklist, vacations, competitors, meetings, бюджет и пр.)
    уходят через релей joto-agent (JOTO_AGENT_RELAY_URL). Аргумент from_bot
    оставлен для совместимости со старыми вызовами и ни на что не влияет.

    Если релей не настроен — запасной путь через im.message.add (B24_WEBHOOK),
    чтобы сервис не падал в окружении без релея.
    """
    if JOTO_AGENT_RELAY_URL and JOTO_AGENT_RELAY_TOKEN:
        return _send_via_relay(dialog_id, text)
    try:
        url = f"{B24_WEBHOOK}/im.message.add.json"
        payload = {"DIALOG_ID": dialog_id, "MESSAGE": text}
        resp = httpx.post(url, json=payload, timeout=10)
        print(f"Ответ Битрикс (запасной путь): {resp.status_code} {resp.text[:200]}")
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

def budget_block():
    """Возвращает текст блока «низкий бюджет кампаний» или None."""
    if not WB_API_TOKEN:
        return None
    adverts = get_active_adverts()
    if not adverts:
        return None
    alerts = []
    for adv in adverts:
        budget = get_advert_budget(adv["id"])
        print(f"Кампания {adv['name']} (ID {adv['id']}): бюджет={budget} ₽")
        if budget is not None and budget < BUDGET_THRESHOLD:
            alerts.append(
                f"🔴 «{adv['name']}» (ID {adv['id']}): остаток {budget} ₽ — "
                "срочное пополнение!"
            )
        time.sleep(0.5)  # бережём лимиты WB API
    if not alerts:
        return None
    return "💰 *Низкий бюджет рекламных кампаний WB:*\n" + "\n".join(alerts)


def check_budgets():
    """Real-time проверка бюджета — шлёт Татьяне отдельным сообщением при низком
    остатке (срочно, не ждём утреннего дайджеста)."""
    print(f"Проверка бюджетов: {datetime.now()}")
    if not B24_WEBHOOK or not TATIANA_USER_ID:
        print("Бюджет: нет вебхука или получателя")
        return
    block = budget_block()
    if block:
        send_b24_message(TATIANA_USER_ID, block, from_bot=True)
        print("Бюджет: уведомление отправлено Татьяне")
    else:
        print(f"Кампаний с бюджетом ниже {BUDGET_THRESHOLD} ₽ не найдено")


def send_daily_digest():
    """ЕДИНЫЙ утренний дайджест Татьяне: все блоки (чек-лист, бюджет, ...) —
    ОДНИМ сообщением через бота JOTO. Длинный текст бьём по лимиту Битрикс."""
    print(f"Дайджест: сборка {datetime.now()}")
    if not B24_WEBHOOK or not TATIANA_USER_ID:
        print("Дайджест: нет вебхука или получателя")
        return {"ok": False, "error": "no_webhook_or_recipient"}

    # Бюджет рекламы НЕ включаем — он шлётся отдельно в реальном времени.
    header = f"📊 *Сводка JOTO на {datetime.now(MSK).strftime('%d.%m.%Y')}*"
    blocks = [header]
    for builder in (lambda: checklist_block(force_compute=True),):
        try:
            b = builder()
            if b:
                blocks.append(b)
        except Exception as e:
            print(f"Дайджест: ошибка блока: {e}")

    if len(blocks) == 1:
        blocks.append("На сегодня заметных уведомлений нет ✅")

    text = "\n\n".join(blocks)
    parts = _split_for_bitrix(text)
    sent = 0
    for part in parts:
        status, _ = send_b24_message(TATIANA_USER_ID, part, from_bot=True)
        if status == 200:
            sent += 1
        time.sleep(0.3)
    print(f"Дайджест: отправлено частей {sent}/{len(parts)}")
    return {"ok": True, "parts": len(parts), "sent": sent}

# ===================== ЧЕК-ЛИСТ КАРТОЧЕК =====================

def checklist_block(force_compute=False):
    """Возвращает текст блока «чек-лист карточек» для дайджеста или None."""
    if not WB_API_TOKEN:
        return None
    data = checklist.compute_checklist() if force_compute else checklist.get_cached()
    if not data.get("items"):
        data = checklist.compute_checklist()
    items = data.get("items") or []
    s = data.get("summary") or {}
    if not items:
        return None

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
    return "\n".join(lines)


def notify_checklist(force_compute=False):
    """Считает чек-лист и шлёт Татьяне сводку (отдельным сообщением)."""
    print(f"Чек-лист: уведомление {datetime.now()}")
    block = checklist_block(force_compute=force_compute)
    if not block:
        print("Чек-лист: нет данных для уведомления")
        return
    if not B24_WEBHOOK:
        print("Чек-лист: нет B24_WEBHOOK — не шлём")
        return
    send_b24_message(TATIANA_USER_ID, block, from_bot=True)
    print("Чек-лист: сводка отправлена Татьяне")


# ===================== ГРАФИК ОТПУСКОВ =====================

# ID руководителя/HR для сводки о разосланных уведомлениях (по умолчанию — Татьяна).
VACATIONS_SUMMARY_TO = os.environ.get("VACATIONS_SUMMARY_TO", TATIANA_USER_ID).strip()


def check_vacations(send_seed=False, force=None):
    """Читает график отпусков и шлёт сотрудникам уведомления о согласованных.

    send_seed=True — принудительно уведомить все строки (даже на первом
    запуске). force — переотправить, игнорируя защиту от дублей:
      • force="all" — всем подходящим строкам;
      • force="<ID>" — только сотруднику с этим ID Битрикс.
    """
    force = (str(force).strip() if force else "")
    print(f"Отпуска: проверка {datetime.now()}")

    if not B24_WEBHOOK:
        print("Отпуска: нет B24_WEBHOOK — некуда слать")
        return

    data = vacations.fetch_rows()
    if not data.get("ok"):
        print(f"Отпуска: не удалось прочитать таблицу — {data.get('error')}")
        return

    rows = data["rows"]
    # уведомляем на этапах «на согласовании» (pending) и «согласовано» (approved)
    to_notify = [r for r in rows if r.get("stage")]
    print(f"Отпуска: всего строк {len(rows)}, к уведомлению {len(to_notify)} "
          f"(согласовано {sum(1 for r in to_notify if r['stage']=='approved')}, "
          f"на согласовании {sum(1 for r in to_notify if r['stage']=='pending')})")

    notified = vacations.load_notified()
    first_run = notified is None
    if first_run:
        notified = set()

    # Первый запуск без принудительной отправки — просто запоминаем текущие
    # строки, чтобы не разослать всем разом исторические отпуска.
    if first_run and not send_seed:
        for r in to_notify:
            notified.add(r["key"])
        vacations.save_notified(notified)
        print(f"Отпуска: первый запуск — запомнил {len(to_notify)} строк, "
              "уведомления не слал. Новые статусы будут уведомляться.")
        return

    sent, skipped_no_id, failed = [], [], []
    for r in to_notify:
        forced = force and (force == "all" or force == r["bitrix_id"])
        if r["key"] in notified and not forced:
            continue
        if not r.get("has_dates"):
            continue  # «без отпуска» / нет дат — уведомлять не о чем
        if not r["bitrix_id"]:
            skipped_no_id.append(r)
            continue
        status, _ = send_b24_message(
            r["bitrix_id"], vacations.employee_message(r), from_bot=True,
        )
        if status == 200:
            notified.add(r["key"])
            sent.append(r)
        else:
            failed.append(r)

    if sent or skipped_no_id:
        vacations.save_notified(notified)

    print(f"Отпуска: отправлено {len(sent)}, без ID {len(skipped_no_id)}, "
          f"ошибок {len(failed)}")

    # Короткая сводка руководителю/HR (если кому-то реально ушли уведомления).
    if sent and VACATIONS_SUMMARY_TO:
        lines = ["🌴 *Уведомления об отпусках разосланы (JOTO):*", ""]
        for r in sent:
            period = r.get("period") or f"{r['start']}–{r['end']}".strip("–")
            mark = "✅ согласовано" if r.get("stage") == "approved" else "📝 на согласовании"
            lines.append(f"{mark}: {r['name'] or r['bitrix_id']} ({period})")
        if skipped_no_id:
            lines.append("")
            lines.append("⚠️ Без ID Битрикс (не отправлено): "
                         + ", ".join(r["name"] or "?" for r in skipped_no_id))
        send_b24_message(VACATIONS_SUMMARY_TO, "\n".join(lines), from_bot=True)


# ===================== НАПОМИНАНИЯ ПЕРЕД СОЗВОНОМ =====================

def check_meetings(force=False, only_uid=None):
    """Шлёт участникам созвона их задачи перед началом встречи.

    Событие и состав участников берутся из календаря Битрикс24, задачи —
    из Битрикс по ответственному. Напоминание уходит за
    MEETING_REMIND_BEFORE_MIN минут до начала (force=True — сразу, для теста).

    only_uid — если задан, рассылка идёт ТОЛЬКО этому сотруднику (тест-режим:
    не спамим всю команду, не сохраняем отметки об отправке).
    """
    print(f"Созвоны: проверка {datetime.now()}"
          + (f" (тест только для {only_uid})" if only_uid else ""))
    test_mode = only_uid is not None
    if test_mode:
        force = True

    if not B24_WEBHOOK:
        print("Созвоны: нет B24_WEBHOOK — некуда слать")
        return {"ok": False, "error": "no_webhook"}

    now = meetings._now_msk()
    if meetings.WEEKDAYS_ONLY and now.weekday() >= 5 and not force:
        print("Созвоны: выходной — пропуск")
        return {"ok": True, "skipped": "weekend"}

    events = meetings.fetch_today_events()
    if not events:
        print("Созвоны: подходящих событий на сегодня нет")
        return {"ok": True, "events": 0}

    notified = meetings.load_notified()

    # 1) Собираем созвоны, по которым сейчас пора напомнить
    due_events = []
    for ev in events:
        remind_at = ev["start"] - timedelta(minutes=meetings.REMIND_BEFORE_MIN)
        due = force or (remind_at <= now < ev["start"])
        if not due:
            continue
        if meetings.event_key(ev) in notified and not force:
            continue
        due_events.append(ev)

    if not due_events:
        print("Созвоны: подходящих по времени созвонов сейчас нет")
        return {"ok": True, "meetings": 0, "sent": 0}

    # 2) По КАЖДОМУ созвону — отдельное сообщение каждому участнику (за 30 мин
    #    до начала этого созвона). Приветствие добавляем только один раз на
    #    человека (при самом первом напоминании), потом не повторяем.
    sent = 0
    announced_now = set()  # кому приветствие уже добавили в этом запуске
    for ev in due_events:
        targets = ev["attendee_ids"]
        if test_mode:
            targets = [u for u in targets if str(u) == str(only_uid)]
        if not targets:
            continue
        names = meetings.get_user_names(targets)
        for uid in targets:
            name = names.get(uid, "")
            tasks = meetings.get_user_tasks(uid)
            ann_key = meetings.announced_key(uid)
            include_announce = (uid not in announced_now and
                                (test_mode or ann_key not in notified))
            msg = meetings.combined_employee_message(
                name, [(ev, tasks)], include_announce)
            status, _ = send_b24_message(uid, msg, from_bot=True)
            if status == 200:
                sent += 1
                if include_announce:
                    announced_now.add(uid)
                    if not test_mode:
                        notified.add(ann_key)
            time.sleep(0.3)  # бережём лимиты Битрикс
        if not test_mode:
            notified.add(meetings.event_key(ev))

    if not test_mode:
        meetings.save_notified(notified)

    print(f"Созвоны: созвонов {len(due_events)}, отправлено сообщений {sent}")
    return {"ok": True, "meetings": len(due_events), "sent": sent}


@app.route("/meetings/check-now", methods=["GET"])
def meetings_check_now():
    # /meetings/check-now?force=1 — разослать сразу всем, игнорируя время и дубли
    # /meetings/check-now?test=226 — БЕЗОПАСНЫЙ тест: отправить только сотруднику 226
    force = request.args.get("force") in ("1", "true", "yes")
    only_uid = request.args.get("test") or None
    threading.Thread(
        target=check_meetings,
        kwargs={"force": force, "only_uid": only_uid},
        daemon=True,
    ).start()
    return jsonify({
        "ok": True,
        "message": "Проверка созвонов запущена",
        "force": force,
        "test_only_uid": only_uid,
    })


@app.route("/meetings/debug", methods=["GET"])
def meetings_debug():
    """Что бот видит в календаре и какие задачи у участников."""
    return jsonify(meetings.debug())


@app.route("/vacations", methods=["GET", "POST"])
def vacations_page():
    return render_template("vacations.html")


@app.route("/vacations/install", methods=["GET", "POST"])
def vacations_install():
    return render_template("vacations.html")


@app.route("/vacations/data", methods=["GET"])
def vacations_data():
    data = vacations.fetch_rows()
    notified = vacations.load_notified()
    sent_keys = set() if notified is None else notified
    for r in data.get("rows", []):
        r["notified"] = r["key"] in sent_keys
    data["sheet_url"] = vacations.csv_url()
    return jsonify(data)


@app.route("/vacations/check-now", methods=["GET"])
def vacations_check_now():
    # /vacations/check-now?seed=1 — запомнить текущие без отправки (первый запуск)
    # /vacations/check-now?force=226 — переотправить сотруднику с ID 226
    # /vacations/check-now?force=all — переотправить всем (игнорируя дубли)
    seed = request.args.get("seed") in ("1", "true", "yes")
    force = request.args.get("force", "")
    threading.Thread(target=check_vacations,
                     kwargs={"send_seed": seed, "force": force}, daemon=True).start()
    return jsonify({"ok": True, "message": "Проверка графика отпусков запущена",
                    "seed": seed, "force": force})


@app.route("/vacations/debug", methods=["GET"])
def vacations_debug():
    """Что приложение видит в таблице: распознанные колонки и разобранные строки."""
    data = vacations.fetch_rows()
    data["csv_url"] = vacations.csv_url()
    return jsonify(data)


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
    resp = app.make_response(render_template("checklist.html"))
    # Без этого Битрикс/браузер кэширует старую вёрстку (старый JS/столбцы)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# Путь установки для Битрикс24. Отдаёт ту же страницу чек-листа: она и
# завершает установку (BX24.installFinish), и сразу показывает таблицу —
# поэтому неважно, какой путь прописан в настройках приложения.
@app.route("/checklist/install", methods=["GET", "POST"])
def checklist_install():
    return render_template("checklist.html")


@app.route("/checklist/data", methods=["GET"])
def checklist_data():
    data = checklist.get_cached()
    resp = jsonify({
        "checked_at": data.get("checked_at"),
        "items": data.get("items", []),
        "summary": data.get("summary", {}),
        "metrics": checklist.metrics_meta(),
        "computing": checklist.is_computing(),
        "vision": vision.enabled(),
        "vision_mode": vision.mode(),
    })
    # Запрещаем кэш: иначе браузер/Битрикс отдают старый ответ (старый набор
    # столбцов) и новые метрики не появляются, сколько ни обновляй страницу.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


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


@app.route("/checklist/tags", methods=["GET"])
def checklist_tags():
    """Список тегов WB в кабинете и сколько артикулов пройдёт фильтр активных."""
    return jsonify(checklist.tags_overview())


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


@app.route("/checklist/debug-vision", methods=["GET"])
def checklist_debug_vision():
    """Диагностика распознавания размерной сетки: режим, ключи и результат
    Gemini по каждому фото карточки. ?nm=<артикул> — конкретный товар."""
    nm = request.args.get("nm", type=int)
    return jsonify(checklist.debug_vision(nm))


@app.route("/checklist/clear-vision-cache", methods=["GET", "POST"])
def checklist_clear_vision_cache():
    """Сброс кэша распознавания сетки — чтобы пересчитать карточки заново
    после смены провайдера/модели."""
    removed = vision.clear_cache()
    return jsonify({"ok": True, "removed_entries": removed})


# ===================== FLASK =====================

@app.route("/", methods=["GET"])
def index():
    return (
        "JOTO CTR Monitor работает ✓<br>"
        "Чек-лист карточек: <a href='/checklist'>/checklist</a><br>"
        "График отпусков: <a href='/vacations'>/vacations</a><br>"
        "Анализ конкурентов: <a href='/competitors'>/competitors</a><br>"
        "Созвоны (диагностика): <a href='/meetings/debug'>/meetings/debug</a>"
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

@app.route("/digest/send", methods=["GET"])
def digest_send():
    """Собрать и отправить единый дайджест Татьяне прямо сейчас."""
    result = send_daily_digest()
    return jsonify(result)

@app.route("/test-relay", methods=["GET"])
def test_relay():
    """Пробное сообщение через релей JOTO. /test-relay?dialog_id=226"""
    dialog_id = request.args.get("dialog_id", TATIANA_USER_ID)
    status, body = send_b24_message(
        dialog_id,
        "✅ Тест отправки через бота JOTO. Если видишь это сообщение — "
        "релей работает, уведомления идут от бота JOTO.",
        from_bot=True,
    )
    return jsonify({
        "ok": status == 200,
        "dialog_id": dialog_id,
        "relay_url": JOTO_AGENT_RELAY_URL or "(не задан)",
        "relay_configured": bool(JOTO_AGENT_RELAY_URL and JOTO_AGENT_RELAY_TOKEN),
        "bitrix_status": status,
        "bitrix_response": body,
    })

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
        "via_relay": bool(JOTO_AGENT_RELAY_URL),
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
        "hint": "Устаревшее: отправка идёт через релей JOTO (JOTO_AGENT_RELAY_URL)",
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

    out = {
        "webhook_url": masked,
        "relay_configured": bool(JOTO_AGENT_RELAY_URL and JOTO_AGENT_RELAY_TOKEN),
        "relay_url": JOTO_AGENT_RELAY_URL or "(не задан)",
    }

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
    # Единый утренний дайджест Татьяне (чек-лист и т.п.) — одним сообщением.
    # Бюджет рекламы и CTR остаются отдельными real-time уведомлениями.
    schedule.every().day.at("07:00").do(send_daily_digest)
    # Автоматический пересчёт чек-листа каждые N часов (по умолчанию 1 — раз в час)
    refresh_h = int(os.environ.get("CHECKLIST_REFRESH_HOURS", "1"))
    schedule.every(refresh_h).hours.do(checklist.compute_checklist)
    # Проверка графика отпусков — каждые N минут (по умолчанию 15)
    vac_min = int(os.environ.get("VACATIONS_CHECK_MINUTES", "15"))
    schedule.every(vac_min).minutes.do(check_vacations)
    # Напоминания о задачах перед созвоном — частый опрос календаря, чтобы
    # поймать момент «за N минут до начала» (по умолчанию каждые 5 минут)
    meet_min = int(os.environ.get("MEETING_CHECK_MINUTES", "5"))
    schedule.every(meet_min).minutes.do(check_meetings)
    print(f"Планировщик запущен — CTR в 09:00 МСК, бюджет каждые 30 мин, "
          f"чек-лист: сводка в 10:00 МСК, авто-пересчёт каждые {refresh_h} ч, "
          f"отпуска каждые {vac_min} мин, созвоны каждые {meet_min} мин")
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    # Первичный расчёт чек-листа в фоне, чтобы дашборд сразу был с данными
    threading.Thread(target=checklist.compute_checklist, daemon=True).start()
    # Первичная проверка отпусков (на первом запуске только запомнит согласованные)
    threading.Thread(target=check_vacations, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
