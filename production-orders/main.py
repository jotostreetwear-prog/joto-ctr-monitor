import os
import time
import base64
from datetime import datetime
from urllib.parse import quote

import httpx
from flask import Flask, request, jsonify, render_template, abort, Response
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # до 25 МБ на файл

# ---- База данных (Postgres на Railway через DATABASE_URL, иначе локальный SQLite) ----
db_url = os.environ.get("DATABASE_URL", "sqlite:///orders.db").strip()
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ---- Конфигурация (переменные окружения на Railway) ----
B24_CLIENT_ID = os.environ.get("B24_CLIENT_ID", "").strip()          # client_id приложения Битрикс
B24_CLIENT_SECRET = os.environ.get("B24_CLIENT_SECRET", "").strip()  # client_secret приложения Битрикс
ALAN_USER_ID = os.environ.get("ALAN_USER_ID", "1").strip()           # кто СОГЛАСОВЫВАЕТ заказы (Алан)
MARINA_USER_ID = os.environ.get("MARINA_USER_ID", "220").strip()     # кому ПАДАЕТ задача (Марина Ванина)
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")  # публичный адрес на Railway
DEAL_CATEGORY_ID = os.environ.get("DEAL_CATEGORY_ID", "").strip()    # ID воронки CRM (опционально)

MENU_TITLE = "Заказы на производство"


# ===================== МОДЕЛИ =====================

class Portal(db.Model):
    """Авторизация портала Битрикс24 (на каждый member_id свои токены)."""
    member_id = db.Column(db.String(64), primary_key=True)
    domain = db.Column(db.String(255))
    access_token = db.Column(db.Text)
    refresh_token = db.Column(db.Text)
    expires = db.Column(db.Integer, default=0)


class Order(db.Model):
    """Заказ на производство."""
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String(64), index=True)
    name = db.Column(db.String(500))          # наименование
    manufacturer = db.Column(db.String(500))  # изготовитель
    sizes = db.Column(db.String(500))         # размеры
    color = db.Column(db.String(255))         # цвет
    quantity = db.Column(db.String(100))      # количество
    cost = db.Column(db.String(100))          # стоимость
    deadline = db.Column(db.String(255))      # сроки
    comment = db.Column(db.Text)              # доп. комментарий
    file_name = db.Column(db.String(500))
    file_data = db.Column(db.LargeBinary)
    status = db.Column(db.String(20), default="pending")  # pending / approved / rejected
    created_by = db.Column(db.String(32))
    created_by_name = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_by = db.Column(db.String(32))
    approved_at = db.Column(db.DateTime)
    reject_reason = db.Column(db.Text)
    task_id = db.Column(db.String(32))
    deal_id = db.Column(db.String(32))


# ===================== БИТРИКС REST =====================

def _save_auth(member_id, domain, access, refresh, expires_in):
    p = db.session.get(Portal, member_id)
    if not p:
        p = Portal(member_id=member_id)
        db.session.add(p)
    if domain:
        p.domain = domain
    if access:
        p.access_token = access
    if refresh:
        p.refresh_token = refresh
    try:
        if expires_in:
            p.expires = int(time.time()) + int(expires_in)
    except (TypeError, ValueError):
        pass
    db.session.commit()
    return p


def _refresh(p):
    """Обновляет access_token по refresh_token. Возвращает True при успехе."""
    if not (B24_CLIENT_ID and B24_CLIENT_SECRET and p.refresh_token):
        return False
    try:
        r = httpx.get("https://oauth.bitrix.info/oauth/token/", params={
            "grant_type": "refresh_token",
            "client_id": B24_CLIENT_ID,
            "client_secret": B24_CLIENT_SECRET,
            "refresh_token": p.refresh_token,
        }, timeout=20)
        data = r.json()
    except Exception as e:
        print("Ошибка refresh:", e)
        return False
    if "access_token" in data:
        p.access_token = data["access_token"]
        p.refresh_token = data.get("refresh_token", p.refresh_token)
        try:
            p.expires = int(time.time()) + int(data.get("expires_in", 3600))
        except (TypeError, ValueError):
            pass
        db.session.commit()
        return True
    print("Ответ refresh без токена:", data)
    return False


def b24(member_id, method, params=None):
    """Вызов метода REST Битрикс24 с авто-рефрешем токена. Возвращает result."""
    p = db.session.get(Portal, member_id)
    if not p or not p.access_token:
        raise RuntimeError("Портал не авторизован — переустановите приложение")

    if p.expires and p.expires < int(time.time()) + 30:
        _refresh(p)

    def _do():
        url = f"https://{p.domain}/rest/{method}?auth={p.access_token}"
        resp = httpx.post(url, json=params or {}, timeout=30)
        return resp.json()

    data = _do()
    if isinstance(data, dict) and data.get("error") in ("expired_token", "invalid_token", "NO_AUTH_FOUND"):
        if _refresh(p):
            data = _do()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"{data.get('error')}: {data.get('error_description', '')}")
    return data.get("result") if isinstance(data, dict) else data


def upload_file_to_disk(member_id, name, data):
    """Загружает файл в хранилище приложения на Диске. Возвращает 'n<ID>' для UF_TASK_WEBDAV_FILES."""
    storage = b24(member_id, "disk.storage.getforapp")
    storage_id = storage["ID"]
    b64 = base64.b64encode(data).decode()
    res = b24(member_id, "disk.storage.uploadfile", {
        "id": storage_id,
        "data": {"NAME": name},
        "fileContent": [name, b64],
        "generateUniqueName": True,
    })
    return f"n{res['ID']}"


# ===================== ВСПОМОГАТЕЛЬНОЕ =====================

def base_url():
    if APP_BASE_URL:
        return APP_BASE_URL
    proto = request.headers.get("X-Forwarded-Proto", "https")
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}"


def order_description(o):
    lines = [
        f"Наименование: {o.name}",
        f"Изготовитель: {o.manufacturer}",
        f"Размеры: {o.sizes}",
        f"Цвет: {o.color}",
        f"Количество: {o.quantity}",
        f"Стоимость: {o.cost}",
        f"Сроки: {o.deadline}",
    ]
    if o.comment:
        lines.append(f"Комментарий: {o.comment}")
    if o.created_by_name:
        lines.append(f"Заявку создал: {o.created_by_name}")
    return "\n".join(lines)


def _num(s):
    if not s:
        return 0
    cleaned = "".join(ch for ch in str(s) if ch.isdigit() or ch in ".,").replace(",", ".")
    try:
        return float(cleaned) if cleaned else 0
    except ValueError:
        return 0


def serialize(o, domain=None):
    return {
        "id": o.id,
        "name": o.name,
        "manufacturer": o.manufacturer,
        "sizes": o.sizes,
        "color": o.color,
        "quantity": o.quantity,
        "cost": o.cost,
        "deadline": o.deadline,
        "comment": o.comment,
        "status": o.status,
        "has_file": bool(o.file_data),
        "file_name": o.file_name,
        "created_by_name": o.created_by_name,
        "created_at": o.created_at.strftime("%d.%m.%Y %H:%M") if o.created_at else "",
        "reject_reason": o.reject_reason,
        "task_id": o.task_id,
        "deal_id": o.deal_id,
        "domain": domain,
    }


def notify(member_id, user_id, message):
    if not user_id:
        return
    try:
        b24(member_id, "im.notify.system.add", {"USER_ID": user_id, "MESSAGE": message})
    except Exception as e:
        print("Ошибка уведомления:", e)


# ===================== УСТАНОВКА / ПЛЕЙСМЕНТ =====================

@app.route("/install", methods=["GET", "POST"])
def install():
    """Обработчик установки приложения: сохраняет токены и вешает пункт в левое меню."""
    member_id = request.values.get("member_id")
    domain = request.values.get("DOMAIN") or request.values.get("domain")
    auth = request.values.get("AUTH_ID")
    refresh = request.values.get("REFRESH_ID")
    expires = request.values.get("AUTH_EXPIRES")
    ok = False
    if member_id and auth:
        _save_auth(member_id, domain, auth, refresh, expires)
        try:
            b24(member_id, "placement.bind", {
                "PLACEMENT": "LEFT_MENU",
                "HANDLER": f"{base_url()}/app",
                "TITLE": MENU_TITLE,
            })
            ok = True
        except Exception as e:
            # «уже привязан» — это тоже успех
            if "ERROR_PLACEMENT_HANDLER_ALREADY_BINDED" in str(e):
                ok = True
            else:
                print("Ошибка placement.bind:", e)
    return render_template("install.html", ok=ok)


@app.route("/app", methods=["GET", "POST"])
def app_page():
    """Страница приложения внутри Битрикс24 (открывается из левого меню)."""
    member_id = request.values.get("member_id")
    domain = request.values.get("DOMAIN") or request.values.get("domain")
    auth = request.values.get("AUTH_ID")
    refresh = request.values.get("REFRESH_ID")
    expires = request.values.get("AUTH_EXPIRES")
    if member_id and auth:
        _save_auth(member_id, domain, auth, refresh, expires)

    current = {}
    if member_id:
        try:
            current = b24(member_id, "user.current") or {}
        except Exception as e:
            print("Ошибка user.current:", e)
    current_id = str(current.get("ID", ""))
    current_name = f"{current.get('NAME', '')} {current.get('LAST_NAME', '')}".strip()
    # согласовывать может только Алан; если ID не задан — даём всем (на этапе настройки)
    is_approver = (current_id == ALAN_USER_ID) if ALAN_USER_ID else True

    return render_template(
        "app.html",
        member_id=member_id or "",
        current_id=current_id,
        current_name=current_name,
        is_approver=is_approver,
    )


# ===================== API =====================

@app.route("/api/list")
def api_list():
    member_id = request.args.get("member_id")
    if not member_id:
        return jsonify([])
    p = db.session.get(Portal, member_id)
    domain = p.domain if p else None
    orders = Order.query.filter_by(member_id=member_id).order_by(Order.id.desc()).all()
    return jsonify([serialize(o, domain) for o in orders])


@app.route("/api/create", methods=["POST"])
def api_create():
    member_id = request.form.get("member_id")
    if not member_id:
        abort(400)
    name = request.form.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Укажите наименование"}), 400

    file_name = file_data = None
    f = request.files.get("file")
    if f and f.filename:
        file_name = f.filename
        file_data = f.read()

    o = Order(
        member_id=member_id,
        name=name,
        manufacturer=request.form.get("manufacturer", "").strip(),
        sizes=request.form.get("sizes", "").strip(),
        color=request.form.get("color", "").strip(),
        quantity=request.form.get("quantity", "").strip(),
        cost=request.form.get("cost", "").strip(),
        deadline=request.form.get("deadline", "").strip(),
        comment=request.form.get("comment", "").strip(),
        file_name=file_name,
        file_data=file_data,
        created_by=request.form.get("current_id", ""),
        created_by_name=request.form.get("current_name", ""),
        status="pending",
    )
    db.session.add(o)
    db.session.commit()

    notify(member_id, ALAN_USER_ID,
           f"Новый заказ на производство на согласование: «{o.name}». Откройте «{MENU_TITLE}».")
    return jsonify({"ok": True, "id": o.id})


@app.route("/api/approve", methods=["POST"])
def api_approve():
    member_id = request.form.get("member_id")
    actor = request.form.get("current_id", "")
    o = Order.query.filter_by(member_id=member_id, id=request.form.get("id")).first()
    if not o:
        abort(404)
    if ALAN_USER_ID and actor != ALAN_USER_ID:
        return jsonify({"ok": False, "error": "Согласовывать заказы может только Алан"}), 403
    if o.status != "pending":
        return jsonify({"ok": False, "error": "Заказ уже обработан"}), 400

    # файл -> Диск (для прикрепления к задаче)
    file_ref = None
    if o.file_data:
        try:
            file_ref = upload_file_to_disk(member_id, o.file_name or f"order_{o.id}", o.file_data)
        except Exception as e:
            print("Ошибка загрузки файла на Диск:", e)

    desc = order_description(o)

    # 1) Задача Марине Ваниной
    task_fields = {
        "TITLE": f"Заказ на производство: {o.name}",
        "RESPONSIBLE_ID": MARINA_USER_ID,
        "DESCRIPTION": desc,
    }
    if file_ref:
        task_fields["UF_TASK_WEBDAV_FILES"] = [file_ref]
    try:
        task_res = b24(member_id, "tasks.task.add", {"fields": task_fields})
        if isinstance(task_res, dict):
            o.task_id = str(task_res.get("task", {}).get("id", ""))
    except Exception as e:
        return jsonify({"ok": False, "error": f"Не удалось создать задачу: {e}"}), 500

    # 2) Сделка в CRM
    deal_fields = {
        "TITLE": f"Производство: {o.name}",
        "OPPORTUNITY": _num(o.cost),
        "CURRENCY_ID": "RUB",
        "COMMENTS": desc,
    }
    if DEAL_CATEGORY_ID:
        deal_fields["CATEGORY_ID"] = DEAL_CATEGORY_ID
    try:
        deal_res = b24(member_id, "crm.deal.add", {"fields": deal_fields})
        o.deal_id = str(deal_res)
    except Exception as e:
        print("Ошибка создания сделки:", e)

    o.status = "approved"
    o.approved_by = actor
    o.approved_at = datetime.utcnow()
    db.session.commit()

    notify(member_id, MARINA_USER_ID,
           f"Согласован заказ на производство «{o.name}» — на вас поставлена задача.")
    return jsonify({"ok": True, "task_id": o.task_id, "deal_id": o.deal_id})


@app.route("/api/reject", methods=["POST"])
def api_reject():
    member_id = request.form.get("member_id")
    actor = request.form.get("current_id", "")
    o = Order.query.filter_by(member_id=member_id, id=request.form.get("id")).first()
    if not o:
        abort(404)
    if ALAN_USER_ID and actor != ALAN_USER_ID:
        return jsonify({"ok": False, "error": "Отклонять заказы может только Алан"}), 403
    if o.status != "pending":
        return jsonify({"ok": False, "error": "Заказ уже обработан"}), 400
    o.status = "rejected"
    o.approved_by = actor
    o.approved_at = datetime.utcnow()
    o.reject_reason = request.form.get("reason", "").strip()
    db.session.commit()
    if o.created_by:
        notify(member_id, o.created_by,
               f"Заказ на производство «{o.name}» отклонён." +
               (f" Причина: {o.reject_reason}" if o.reject_reason else ""))
    return jsonify({"ok": True})


@app.route("/api/users")
def api_users():
    """Список сотрудников (id + ФИО) — чтобы найти ID Алана и Марины для настроек."""
    member_id = request.args.get("member_id")
    if not member_id:
        return jsonify([])
    try:
        res = b24(member_id, "user.get", {"FILTER": {"ACTIVE": True}}) or []
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    out = [{
        "id": u.get("ID"),
        "name": f"{u.get('LAST_NAME', '')} {u.get('NAME', '')}".strip() or u.get("ID"),
    } for u in res]
    out.sort(key=lambda x: x["name"])
    return jsonify(out)


@app.route("/file/<int:oid>")
def file_download(oid):
    member_id = request.args.get("member_id")
    o = Order.query.filter_by(member_id=member_id, id=oid).first()
    if not o or not o.file_data:
        abort(404)
    fname = quote(o.file_name or f"order_{oid}")
    return Response(o.file_data, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{fname}",
        "Content-Type": "application/octet-stream",
    })


@app.route("/")
def index():
    return "JOTO «Заказы на производство» — приложение Битрикс24 работает ✓"


# ===================== ИНИЦИАЛИЗАЦИЯ БД =====================

with app.app_context():
    db.create_all()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
