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

# Явные имена колонок (если автоопределение ошибётся — задать точные заголовки).
COL_NAME = os.environ.get("VACATIONS_COL_NAME", "").strip()
COL_BITRIX = os.environ.get("VACATIONS_COL_BITRIX", "").strip()
COL_START = os.environ.get("VACATIONS_COL_START", "").strip()
COL_END = os.environ.get("VACATIONS_COL_END", "").strip()
COL_STATUS = os.environ.get("VACATIONS_COL_STATUS", "").strip()

# Ключевые слова для автоопределения колонок по заголовку (в порядке приоритета).
_KEYWORDS = {
    "name":   ["фио", "ф.и.о", "сотрудник", "работник", "имя", "name"],
    "bitrix": ["битрикс", "bitrix", "b24", "id битрикс", "ид битрикс", "id"],
    "start":  ["дата начала", "начало отпуска", "начал", "отпуск с", "дата с", "start", "с"],
    "end":    ["дата окончания", "окончание", "оконч", "конец", "отпуск по", "дата по", "end", "по"],
    "status": ["статус", "согласован", "состояние", "утвержд", "status"],
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
        "status": _match_column(headers, _KEYWORDS["status"], COL_STATUS),
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
        bitrix_id = cell(row, "bitrix")
        start = cell(row, "start")
        end = cell(row, "end")
        status = cell(row, "status")
        if not name and not bitrix_id and not start:
            continue
        approved = _is_approved(status)
        rows.append({
            "name": name,
            "bitrix_id": bitrix_id,
            "start": start,
            "end": end,
            "status": status,
            "approved": approved,
            "key": row_key(bitrix_id, start, end),
        })

    out["rows"] = rows
    out["ok"] = True
    return out


def row_key(bitrix_id, start, end):
    return f"{(bitrix_id or '').strip()}|{(start or '').strip()}|{(end or '').strip()}"


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
    """Текст личного уведомления сотруднику о согласованном отпуске."""
    period = ""
    if row["start"] and row["end"]:
        period = f" с *{row['start']}* по *{row['end']}*"
    elif row["start"]:
        period = f" с *{row['start']}*"
    return (
        f"🌴 Ваш отпуск{period} *согласован*!\n"
        "Хорошего отдыха 🙌\n\n"
        "_Сообщение сформировано автоматически из графика отпусков._"
    )
