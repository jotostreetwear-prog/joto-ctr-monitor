"""График отпусков из Google-таблицы → уведомления сотрудникам в Битрикс24.

Логика:
  • таблица «График отпусков» ведётся в Google Sheets (как и раньше);
  • приложение периодически читает её как CSV (export?format=csv);
  • как только в колонке-статусе у строки появляется «Согласовано»
    (или другое из VACATIONS_APPROVED_VALUES) — сотруднику из колонки
    с ID Битрикс уходит личное уведомление об утверждённом отпуске.

Чтобы один и тот же отпуск не уведомлялся повторно, ключи уже отправленных
строк хранятся в notified-файле на диске (Railway volume). При самом первом
запуске (файла ещё нет) все текущие согласованные строки помечаются как
«уже уведомлённые» без отправки — чтобы не разослать всем разом исторические
отпуска. Дальше шлём только новые согласования.
"""

import os
import re
import csv
import io
import json
import httpx
from datetime import datetime, timezone, timedelta

# Московское время (UTC+3) — Railway работает в UTC
MSK = timezone(timedelta(hours=3))


def _now_msk():
    return datetime.now(MSK)


# ===================== КОНФИГ =====================

# ID Google-таблицы с графиком отпусков. Можно переопределить переменной
# окружения; по умолчанию — таблица, присланная заказчиком.
SHEET_ID = os.environ.get(
    "VACATIONS_SHEET_ID",
    "1W_G0ILrBN2JXM_m_oe13L7-e3JBbI6vv8WTLDoSlj5I",
).strip()
# Конкретный лист внутри книги (gid из URL). По умолчанию первый лист.
SHEET_GID = os.environ.get("VACATIONS_SHEET_GID", "0").strip()
# Полный CSV-URL можно задать напрямую (например ссылка «Опубликовать → CSV»).
SHEET_URL = os.environ.get("VACATIONS_SHEET_URL", "").strip()

# Значения статуса, считающиеся «согласовано» (сравнение по подстроке, регистр не важен).
APPROVED_VALUES = tuple(
    v.strip().lower()
    for v in os.environ.get(
        "VACATIONS_APPROVED_VALUES",
        "согласован,утвержд,одобрен,подтвержд,approved",
    ).split(",")
    if v.strip()
)

# Стоп-слова: если статус содержит одно из них — он НЕ считается согласованным,
# даже если внутри есть «согласован» (например «На согласовании», «Не согласовано»,
# «Ожидает согласования», «Отклонено»). Защищает от преждевременной рассылки.
NOT_APPROVED_VALUES = tuple(
    v.strip().lower()
    for v in os.environ.get(
        "VACATIONS_NOT_APPROVED_VALUES",
        "на согласован,на утвержд,не согласован,не утвержд,ожид,отклон,черновик,запрош,рассмотр,в процесс",
    ).split(",")
    if v.strip()
)

# Статусы этапа «отправлено/на согласовании» — по ним шлём отдельное
# уведомление «отпуск подан на согласование» (а не «согласован»).
PENDING_VALUES = tuple(
    v.strip().lower()
    for v in os.environ.get(
        "VACATIONS_PENDING_VALUES",
        "на согласован,на утвержд,ожид,рассмотр,в процесс,отправлен",
    ).split(",")
    if v.strip()
)

# Явные имена колонок (если автоопределение ошибётся — задать точные заголовки).
COL_NAME = os.environ.get("VACATIONS_COL_NAME", "").strip()
COL_BITRIX = os.environ.get("VACATIONS_COL_BITRIX", "").strip()
COL_START = os.environ.get("VACATIONS_COL_START", "").strip()
COL_END = os.environ.get("VACATIONS_COL_END", "").strip()
COL_PERIOD = os.environ.get("VACATIONS_COL_PERIOD", "").strip()
COL_STATUS = os.environ.get("VACATIONS_COL_STATUS", "").strip()
COL_COMMENT = os.environ.get("VACATIONS_COL_COMMENT", "").strip()

# Ключевые слова для автоопределения колонок по заголовку (в порядке приоритета).
# Без одиночных «с»/«по» — они ловят лишние колонки («Даты отпуска» и т.п.).
_KEYWORDS = {
    "name":   ["фио", "ф.и.о", "сотрудник", "работник", "имя", "name"],
    "bitrix": ["id битрикс", "ид битрикс", "битрикс", "bitrix", "b24", "профиль", "id"],
    "start":  ["дата начала", "начало отпуска", "дата с", "отпуск с", "начал", "start"],
    "end":    ["дата окончания", "дата по", "окончание", "отпуск по", "оконч", "конец", "end"],
    # одна колонка с периодом отпуска целиком (например «Даты отпуска»: «27.07-02.08»)
    "period": ["даты отпуска", "период отпуска", "срок отпуска", "даты", "период", "срок"],
    "status": ["статус согласования", "статус", "согласование", "состояние", "status"],
    "comment": ["комментарий", "комментарии", "коммент", "примечание", "comment", "note"],
}

# Где хранить ключи уже отправленных уведомлений (переживает редеплои на Railway).
_RAILWAY_VOL = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
NOTIFIED_PATH = (
    os.environ.get("VACATIONS_NOTIFIED")
    or (os.path.join(_RAILWAY_VOL, "vacations_notified.json") if _RAILWAY_VOL
        else "/data/vacations_notified.json" if os.path.isdir("/data")
        else "vacations_notified.json")
)


def csv_url():
    if SHEET_URL:
        return SHEET_URL
    return (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
        f"/export?format=csv&gid={SHEET_GID}"
    )


# ===================== ПАРСИНГ ТАБЛИЦЫ =====================

def _norm(s):
    return (s or "").strip().lower()


def _match_column(headers, keywords, explicit=""):
    """Возвращает индекс колонки по точному имени (explicit) или ключевым словам."""
    norm = [_norm(h) for h in headers]
    if explicit:
        e = _norm(explicit)
        for i, h in enumerate(norm):
            if h == e:
                return i
        for i, h in enumerate(norm):
            if e in h:
                return i
    # сначала ищем точное совпадение по ключевому слову, потом по подстроке
    for kw in keywords:
        for i, h in enumerate(norm):
            if h == kw:
                return i
    for kw in keywords:
        for i, h in enumerate(norm):
            if kw in h:
                return i
    return None


def _find_header_row(rows):
    """Ищет строку заголовков среди первых строк (та, где больше всего ключевых слов)."""
    best_idx, best_score = 0, -1
    all_kw = [kw for kws in _KEYWORDS.values() for kw in kws]
    for idx, row in enumerate(rows[:8]):
        cells = [_norm(c) for c in row]
        score = sum(1 for c in cells if c and any(kw in c for kw in all_kw))
        if score > best_score:
            best_idx, best_score = idx, score
    return best_idx


def _is_approved(status_value):
    s = _norm(status_value)
    if not s:
        return False
    # стоп-слова (ожидает/отклонён/не согласован) отменяют согласование
    if any(v in s for v in NOT_APPROVED_VALUES):
        return False
    return any(v in s for v in APPROVED_VALUES)


def _is_pending(status_value):
    s = _norm(status_value)
    return bool(s) and any(v in s for v in PENDING_VALUES)


def notify_stage(status_value):
    """Этап для уведомления: 'approved', 'pending' или None (не уведомляем)."""
    if _is_approved(status_value):
        return "approved"
    if _is_pending(status_value):
        return "pending"
    return None


def extract_bitrix_id(value):
    """Достаёт числовой ID пользователя Битрикс24 из значения ячейки.

    В таблице ID хранится ссылкой вида
    https://joto.bitrix24.ru/company/personal/user/123/ — берём число
    после /user/. Поддерживаем и просто число в ячейке.
    """
    v = (value or "").strip()
    if not v:
        return ""
    m = re.search(r"/user/(\d+)", v)
    if m:
        return m.group(1)
    if v.isdigit():
        return v
    # запасной вариант — первое самостоятельное число
    m = re.search(r"\b(\d{1,9})\b", v)
    return m.group(1) if m else ""


def _split_period(period):
    """Разбивает «27.07-02.08» на (начало, конец). Если не вышло — («», «»)."""
    p = (period or "").strip()
    if not p:
        return "", ""
    parts = re.split(r"\s*[-–—]\s*", p)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return "", ""


def _has_real_dates(period, start, end):
    """True, если в датах есть цифры (отсекает «без отпуска», пустые ячейки)."""
    text = f"{period} {start} {end}"
    return bool(re.search(r"\d", text))


def fetch_rows():
    """Скачивает и разбирает таблицу. Возвращает dict:

        {
          "ok": bool, "error": str|None, "fetched_at": str,
          "columns": {"name": "...", "bitrix": "...", ...},  # распознанные заголовки
          "rows": [ {name, bitrix_id, start, end, status, approved, key}, ... ],
        }
    """
    out = {
        "ok": False, "error": None,
        "fetched_at": _now_msk().strftime("%Y-%m-%d %H:%M"),
        "columns": {}, "rows": [],
    }
    try:
        resp = httpx.get(csv_url(), timeout=30, follow_redirects=True)
        if resp.status_code != 200:
            out["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return out
        text = resp.text
    except Exception as e:
        out["error"] = f"Ошибка загрузки таблицы: {e}"
        return out

    try:
        all_rows = list(csv.reader(io.StringIO(text)))
    except Exception as e:
        out["error"] = f"Ошибка разбора CSV: {e}"
        return out

    if not all_rows:
        out["error"] = "Таблица пустая"
        return out

    hidx = _find_header_row(all_rows)
    headers = all_rows[hidx]

    ci = {
        "name": _match_column(headers, _KEYWORDS["name"], COL_NAME),
        "bitrix": _match_column(headers, _KEYWORDS["bitrix"], COL_BITRIX),
        "start": _match_column(headers, _KEYWORDS["start"], COL_START),
        "end": _match_column(headers, _KEYWORDS["end"], COL_END),
        "period": _match_column(headers, _KEYWORDS["period"], COL_PERIOD),
        "status": _match_column(headers, _KEYWORDS["status"], COL_STATUS),
        "comment": _match_column(headers, _KEYWORDS["comment"], COL_COMMENT),
    }
    out["columns"] = {
        k: (headers[i] if i is not None and i < len(headers) else None)
        for k, i in ci.items()
    }

    def cell(row, key):
        i = ci[key]
        if i is None or i >= len(row):
            return ""
        return (row[i] or "").strip()

    rows = []
    for row in all_rows[hidx + 1:]:
        if not any((c or "").strip() for c in row):
            continue  # пустая строка
        name = cell(row, "name")
        bitrix_raw = cell(row, "bitrix")
        bitrix_id = extract_bitrix_id(bitrix_raw)
        start = cell(row, "start")
        end = cell(row, "end")
        period = cell(row, "period")
        status = cell(row, "status")
        comment = cell(row, "comment")
        if not name and not bitrix_raw and not period and not start:
            continue
        # период отпуска для показа и для ключа: либо отдельная колонка,
        # либо собранный из дата-начала/дата-конца
        if not period and (start or end):
            period = " – ".join(x for x in [start, end] if x)
        # если есть только период «27.07-02.08» — попробуем разложить на даты
        if period and not start and not end:
            start, end = _split_period(period)
        stage = notify_stage(status)
        dates = period or f"{start}-{end}"
        rows.append({
            "name": name,
            "bitrix_id": bitrix_id,
            "bitrix_raw": bitrix_raw,
            "start": start,
            "end": end,
            "period": period,
            "status": status,
            "comment": comment,
            "approved": stage == "approved",
            "stage": stage,
            "has_dates": _has_real_dates(period, start, end),
            "key": row_key(bitrix_id, dates, stage or "approved"),
        })

    out["rows"] = rows
    out["ok"] = True
    return out


def row_key(bitrix_id, dates, stage="approved"):
    """Ключ для защиты от дублей. Этап 'approved' сохраняет старый формат
    (без префикса) — чтобы ранее уведомлённые согласования не ушли повторно."""
    base = f"{(bitrix_id or '').strip()}|{(dates or '').strip()}"
    return base if stage == "approved" else f"{stage}:{base}"


# ===================== ХРАНИЛИЩЕ ОТПРАВЛЕННЫХ =====================

def load_notified():
    try:
        with open(NOTIFIED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set(data.get("keys", []))
    except FileNotFoundError:
        return None  # отличаем «первый запуск» от «никого не уведомляли»
    except Exception as e:
        print(f"Отпуска: не прочитал {NOTIFIED_PATH}: {e}")
        return set()


def save_notified(keys):
    try:
        os.makedirs(os.path.dirname(NOTIFIED_PATH) or ".", exist_ok=True)
        with open(NOTIFIED_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(keys), f, ensure_ascii=False, indent=0)
        return True
    except Exception as e:
        print(f"Отпуска: не сохранил {NOTIFIED_PATH}: {e}")
        return False


def employee_message(row):
    """Текст личного уведомления сотруднику. Зависит от этапа (stage)."""
    when = ""
    if row.get("start") and row.get("end"):
        when = f" с *{row['start']}* по *{row['end']}*"
    elif row.get("period") and row.get("has_dates"):
        when = f" *{row['period']}*"

    if row.get("stage") == "pending":
        head = (
            f"📝 Ваш отпуск{when} отправлен *на согласование*.\n"
            "Мы сообщим, как только его согласуют."
        )
    else:  # approved
        head = (
            f"🌴 Ваш отпуск{when} *согласован*!\n"
            "Хорошего отдыха 🙌"
        )

    comment = (row.get("comment") or "").strip()
    comment_block = f"\n\n💬 Комментарий: {comment}" if comment else ""

    return (
        f"{head}{comment_block}\n\n"
        "_Сообщение от JOTO — сформировано автоматически из графика отпусков._"
    )
