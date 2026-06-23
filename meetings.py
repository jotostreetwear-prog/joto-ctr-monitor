"""Напоминания о задачах перед созвоном.

Логика:
  • созвон и его участники берутся из календаря Битрикс24
    (calendar.event.get) — события на сегодня, чьё название подходит под
    MEETING_NAME_FILTER (по умолчанию «созвон/планёрка/стендап/…»);
  • по каждому участнику события тянем его активные задачи из Битрикс24
    (tasks.task.list по ответственному, статус < «Завершена»);
  • за MEETING_REMIND_BEFORE_MIN минут до начала каждому участнику уходит
    личное напоминание со списком его задач — чтобы он подготовился к
    созвону и начинал доклад со своих задач.

Чтобы по одному созвону напоминание не ушло дважды, ключи отправленных
событий хранятся в notified-файле на диске (как в графике отпусков).
Время сравниваем в МСК (Railway работает в UTC).

Нужные права входящего вебхука Битрикс (B24_WEBHOOK): calendar, task, user, im.
Состав участников и время берутся из календаря, поэтому отдельный список
сотрудников вести не требуется.
"""

import os
import re
import json
import httpx
from datetime import datetime, timezone, timedelta

# Московское время (UTC+3) — Railway работает в UTC
MSK = timezone(timedelta(hours=3))


def _now_msk():
    return datetime.now(MSK)


# ===================== КОНФИГ =====================

def _normalize_webhook(url):
    """То же, что в main.py: отрезает случайно скопированный метод (…/profile.json)."""
    url = (url or "").strip().rstrip("/")
    last = url.rsplit("/", 1)[-1] if "/" in url else ""
    if "." in last:
        url = url.rsplit("/", 1)[0]
    return url


WEBHOOK = _normalize_webhook(os.environ.get("B24_WEBHOOK", ""))

# Чей календарь читаем, чтобы найти событие созвона.
#   MEETING_CALENDAR_TYPE  — тип календаря: user | group | company_calendar
#   MEETING_CALENDAR_OWNER — id владельца (для user — id сотрудника-организатора,
#                            для group — id рабочей группы). По умолчанию — Татьяна.
CALENDAR_TYPE = os.environ.get("MEETING_CALENDAR_TYPE", "user").strip() or "user"
CALENDAR_OWNER = os.environ.get(
    "MEETING_CALENDAR_OWNER",
    os.environ.get("TATIANA_USER_ID", "232"),
).strip()
# Необязательный id раздела календаря (section) — если нужно сузить.
CALENDAR_SECTION = os.environ.get("MEETING_CALENDAR_SECTION", "").strip()

# По каким словам в названии события считаем его «созвоном». Пусто — берём все
# события на сегодня. Сравнение по подстроке, регистр не важен.
NAME_FILTER = tuple(
    v.strip().lower()
    for v in os.environ.get(
        "MEETING_NAME_FILTER",
        "созвон,планёрк,планерк,стендап,летучк,совещан,daily,sync,митап,встреч",
    ).split(",")
    if v.strip()
)

# За сколько минут до начала созвона слать напоминание.
REMIND_BEFORE_MIN = int(os.environ.get("MEETING_REMIND_BEFORE_MIN", "30"))
# Сколько задач максимум показывать в напоминании.
TASK_LIMIT = int(os.environ.get("MEETING_TASK_LIMIT", "15"))
# Напоминать только по будням (пн–пт).
WEEKDAYS_ONLY = os.environ.get("MEETING_WEEKDAYS_ONLY", "1").strip().lower() in (
    "1", "true", "yes", "да",
)

# Где хранить ключи уже отправленных напоминаний (переживает редеплои на Railway).
_RAILWAY_VOL = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
NOTIFIED_PATH = (
    os.environ.get("MEETINGS_NOTIFIED")
    or (os.path.join(_RAILWAY_VOL, "meetings_notified.json") if _RAILWAY_VOL
        else "/data/meetings_notified.json" if os.path.isdir("/data")
        else "meetings_notified.json")
)


# ===================== БИТРИКС API =====================

def _b24(method, payload=None, method_get=False):
    """Вызов REST-метода Битрикс через вебхук. Возвращает result или None."""
    if not WEBHOOK:
        print("Созвоны: нет B24_WEBHOOK")
        return None
    url = f"{WEBHOOK}/{method}.json"
    try:
        if method_get:
            resp = httpx.get(url, params=payload or {}, timeout=30)
        else:
            resp = httpx.post(url, json=payload or {}, timeout=30)
        if resp.status_code != 200:
            print(f"Созвоны: {method} HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            print(f"Созвоны: {method} ошибка {data.get('error')}: "
                  f"{data.get('error_description')}")
            return None
        return data.get("result") if isinstance(data, dict) else data
    except Exception as e:
        print(f"Созвоны: {method} исключение {e}")
        return None


def _parse_dt(s):
    """Разбирает дату/время из Битрикс в datetime с таймзоной МСК."""
    if not s:
        return None
    s = str(s).strip()
    try:
        dt = datetime.fromisoformat(s)
        return dt.astimezone(MSK) if dt.tzinfo else dt.replace(tzinfo=MSK)
    except Exception:
        pass
    base = s.replace("T", " ").split("+")[0].strip()
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
              "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(base, f).replace(tzinfo=MSK)
        except Exception:
            continue
    return None


def _name_matches(name):
    if not NAME_FILTER:
        return True
    n = (name or "").lower()
    return any(kw in n for kw in NAME_FILTER)


def _attendee_ids(event):
    """Достаёт id участников события из ATTENDEES_CODES (['U232','U226',...])."""
    codes = (event.get("ATTENDEES_CODES") or event.get("attendeesCodes")
             or event.get("attendees") or [])
    ids = []
    for c in codes:
        m = re.match(r"U(\d+)", str(c))
        if m:
            ids.append(m.group(1))
    host = event.get("MEETING_HOST") or event.get("meetingHost")
    if host and str(host) not in ids:
        ids.append(str(host))
    # уникальные, с сохранением порядка
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def fetch_today_events():
    """События-созвоны на сегодня. Возвращает список dict:

        {id, name, start(datetime МСК), end, attendee_ids:[...]}
    отсортированный по времени начала.
    """
    today = _now_msk().strftime("%Y-%m-%d")
    payload = {"type": CALENDAR_TYPE, "ownerId": CALENDAR_OWNER,
               "from": today, "to": today}
    if CALENDAR_SECTION:
        payload["section"] = [CALENDAR_SECTION]

    res = _b24("calendar.event.get", payload)
    if res is None:
        return []
    if isinstance(res, dict):
        res = res.get("items") or res.get("events") or []

    events = []
    for e in res or []:
        start = _parse_dt(e.get("DATE_FROM") or e.get("dateFrom"))
        if not start:
            continue
        name = e.get("NAME") or e.get("name") or "Созвон"
        if not _name_matches(name):
            continue
        events.append({
            "id": str(e.get("ID") or e.get("id") or ""),
            "name": name,
            "start": start,
            "end": _parse_dt(e.get("DATE_TO") or e.get("dateTo")),
            "attendee_ids": _attendee_ids(e),
        })
    events.sort(key=lambda x: x["start"])
    return events


_name_cache = {}


def get_user_name(uid):
    """Имя сотрудника по id Битрикс (с кэшем)."""
    uid = str(uid)
    if uid in _name_cache:
        return _name_cache[uid]
    res = _b24("user.get", {"ID": uid})
    name = ""
    if isinstance(res, list) and res:
        u = res[0]
        name = " ".join(x for x in [u.get("NAME"), u.get("LAST_NAME")] if x).strip()
    _name_cache[uid] = name
    return name


def get_user_names(ids):
    return {uid: get_user_name(uid) for uid in ids}


_TASK_SELECT = ["ID", "TITLE", "DEADLINE", "STATUS", "RESPONSIBLE_ID"]


def get_user_tasks(uid):
    """Активные задачи сотрудника (статус < «Завершена»), по дедлайну."""
    res = _b24("tasks.task.list", {
        "filter": {"RESPONSIBLE_ID": uid, "<STATUS": 5},
        "select": _TASK_SELECT,
        "order": {"DEADLINE": "asc"},
    })
    if isinstance(res, dict):
        raw = res.get("tasks") or []
    elif isinstance(res, list):
        raw = res
    else:
        raw = []

    tasks = []
    for t in raw[:TASK_LIMIT]:
        tasks.append({
            "id": t.get("id") or t.get("ID"),
            "title": t.get("title") or t.get("TITLE"),
            "deadline": t.get("deadline") or t.get("DEADLINE"),
            "status": t.get("status") or t.get("STATUS"),
        })
    return tasks


def _fmt_deadline(deadline):
    dt = _parse_dt(deadline)
    return dt.strftime("%d.%m") if dt else ""


def announce_message(name):
    """Однократное объявление о новом формате созвонов (шлётся каждому участнику
    один раз, перед первым напоминанием)."""
    greet = f"{name}, " if name else ""
    return (
        f"📣 {greet}у нас новый формат созвонов!\n\n"
        "Теперь перед каждой встречей я заранее пришлю тебе список твоих "
        "текущих задач из Битрикс. Созвон начинаем по-новому:\n"
        "1️⃣ сначала каждый проходит по своим задачам;\n"
        "2️⃣ потом — всё остальное.\n\n"
        "Так что приходи на созвон подготовленным по своим задачам 🙌\n\n"
        "_Сообщение от JOTO — сформировано автоматически._"
    )


def announced_key(uid):
    return f"announced:{uid}"


def employee_message(name, event, tasks):
    """Текст личного напоминания сотруднику перед созвоном."""
    when = event["start"].strftime("%H:%M") if event.get("start") else ""
    title = event.get("name") or "Созвон"
    greet = f"{name}, " if name else ""
    head = f"⏰ Напоминание: сегодня в {when} — «{title}»."

    if tasks:
        lines = [f"{greet}подготовься к созвону. Начни доклад со своих задач:"]
        for t in tasks:
            d = _fmt_deadline(t.get("deadline"))
            dl = f" (до {d})" if d else ""
            title_t = (t.get("title") or "").strip() or "Без названия"
            lines.append(f"• {title_t}{dl}")
        body = "\n".join(lines)
    else:
        body = (f"{greet}активных задач в Битрикс не нашлось — "
                "будь готов рассказать о статусе своих работ.")

    return (
        f"{head}\n\n{body}\n\n"
        "_Сообщение от JOTO — сформировано автоматически перед созвоном._"
    )


# ===================== ХРАНИЛИЩЕ ОТПРАВЛЕННЫХ =====================

def event_key(event):
    """Ключ напоминания: дата + id события (одно напоминание на созвон в день)."""
    day = event["start"].strftime("%Y-%m-%d") if event.get("start") else ""
    return f"{day}|{event.get('id', '')}"


def load_notified():
    try:
        with open(NOTIFIED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set(data.get("keys", []))
    except FileNotFoundError:
        return set()
    except Exception as e:
        print(f"Созвоны: не прочитал {NOTIFIED_PATH}: {e}")
        return set()


def save_notified(keys):
    try:
        os.makedirs(os.path.dirname(NOTIFIED_PATH) or ".", exist_ok=True)
        with open(NOTIFIED_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(keys), f, ensure_ascii=False, indent=0)
        return True
    except Exception as e:
        print(f"Созвоны: не сохранил {NOTIFIED_PATH}: {e}")
        return False


# ===================== ДИАГНОСТИКА =====================

def debug():
    """Что бот видит в календаре и какие задачи у участников — для проверки."""
    now = _now_msk()
    events = fetch_today_events()
    out = {
        "now_msk": now.strftime("%Y-%m-%d %H:%M"),
        "weekday": now.weekday(),
        "config": {
            "calendar_type": CALENDAR_TYPE,
            "calendar_owner": CALENDAR_OWNER,
            "name_filter": list(NAME_FILTER),
            "remind_before_min": REMIND_BEFORE_MIN,
            "weekdays_only": WEEKDAYS_ONLY,
            "webhook_set": bool(WEBHOOK),
        },
        "events": [],
    }
    notified = load_notified()
    for ev in events:
        remind_at = ev["start"] - timedelta(minutes=REMIND_BEFORE_MIN)
        names = get_user_names(ev["attendee_ids"])
        out["events"].append({
            "id": ev["id"],
            "name": ev["name"],
            "start": ev["start"].strftime("%Y-%m-%d %H:%M"),
            "remind_at": remind_at.strftime("%Y-%m-%d %H:%M"),
            "already_notified": event_key(ev) in notified,
            "attendees": [
                {"id": uid, "name": names.get(uid, ""),
                 "tasks": [t["title"] for t in get_user_tasks(uid)]}
                for uid in ev["attendee_ids"]
            ],
        })
    return out
