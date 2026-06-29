"""Отдельный бот автопубликации в TikTok (деплоится отдельным сервисом на Railway).

Запуск: python tiktok_bot.py
Слушает свой порт ($PORT), отдаёт страницу /tiktok и сам по расписанию публикует
ролики из очереди. От монитора WB (main.py) не зависит — общий только модуль
tiktok.py с логикой Content Posting API.

Переменные окружения (Railway → этот сервис):
  TIKTOK_CLIENT_KEY      — Client key приложения TikTok
  TIKTOK_CLIENT_SECRET   — Client secret приложения TikTok
  PUBLIC_BASE_URL        — публичный адрес ЭТОГО сервиса, напр. https://joto-tiktok.up.railway.app
  TIKTOK_PRIVACY         — SELF_ONLY (черновик, по умолчанию) или PUBLIC_TO_EVERYONE (после аудита)
"""

import os
import time
import threading

import schedule
from flask import Flask, request, jsonify, render_template, redirect, Response, abort

import tiktok

app = Flask(__name__)


_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — JOTO Autoposter</title>
<style>body{{font-family:system-ui,Arial,sans-serif;max-width:760px;margin:40px auto;
padding:0 16px;line-height:1.6;color:#1a1a1a}}h1{{font-size:24px}}h2{{font-size:18px;margin-top:28px}}
a{{color:#0a58ca}}.muted{{color:#666;font-size:14px}}</style></head><body>{body}
<p class="muted" style="margin-top:32px"><a href="/">Главная</a> ·
<a href="/terms">Условия</a> · <a href="/privacy">Конфиденциальность</a></p></body></html>"""


@app.route("/", methods=["GET"])
def index():
    body = """
    <h1>JOTO Autoposter</h1>
    <p>Сервис для планирования и автоматической публикации собственных коротких
    видео бренда в аккаунт владельца. Владелец загружает свои видеофайлы в облачное
    хранилище, а сервис по расписанию публикует их в его собственный профиль.</p>
    <p>A scheduling tool that lets a brand owner publish their own short videos to
    their own social account. The owner uploads their own video files to cloud
    storage and the service publishes them to the owner's profile on a schedule.</p>
    <h2>Возможности</h2>
    <ul>
      <li>Импорт собственных видео из облачной папки владельца</li>
      <li>Планирование публикаций по расписанию</li>
      <li>Публикация в собственный аккаунт владельца</li>
    </ul>
    <p class="muted">Сервис работает только с аккаунтом самого владельца, который
    дал явное разрешение через авторизацию.</p>
    """
    return _PAGE.format(title="Главная", body=body)


@app.route("/terms", methods=["GET"])
def terms():
    body = """
    <h1>Условия использования / Terms of Service</h1>
    <p class="muted">Обновлено: 2026</p>
    <p>Используя этот сервис, вы соглашаетесь с приведёнными условиями. Сервис
    предназначен для планирования и публикации собственных видеоматериалов
    владельца в его собственный аккаунт.</p>
    <h2>Использование</h2>
    <p>Сервисом пользуется владелец аккаунта самостоятельно. Запрещено публиковать
    чужой контент без прав, а также материалы, нарушающие правила площадок и
    законодательство.</p>
    <h2>Ответственность</h2>
    <p>Владелец несёт ответственность за публикуемый контент. Сервис предоставляется
    «как есть», без гарантий бесперебойной работы.</p>
    <h2>Контакты</h2>
    <p>По вопросам: joto.streetwear@gmail.com</p>
    <hr>
    <p>By using this service you agree to these terms. The service is intended for
    scheduling and publishing the owner's own video content to the owner's own
    account. The owner is responsible for the content. The service is provided
    "as is". Contact: joto.streetwear@gmail.com</p>
    """
    return _PAGE.format(title="Условия", body=body)


@app.route("/privacy", methods=["GET"])
def privacy():
    body = """
    <h1>Политика конфиденциальности / Privacy Policy</h1>
    <p class="muted">Обновлено: 2026</p>
    <p>Сервис обрабатывает минимум данных, необходимых только для публикации видео
    владельца в его собственный аккаунт.</p>
    <h2>Какие данные используются</h2>
    <ul>
      <li>Токен авторизации аккаунта (после явного разрешения владельца) — чтобы
      публиковать видео от его имени.</li>
      <li>Базовые данные профиля (имя пользователя) — чтобы показать, какой аккаунт
      подключён.</li>
      <li>Видеофайлы из облачной папки владельца — только для загрузки и публикации.</li>
    </ul>
    <h2>Что мы НЕ делаем</h2>
    <p>Мы не продаём и не передаём данные третьим лицам и не используем их для
    рекламы. Данные применяются исключительно для работы сервиса.</p>
    <h2>Удаление</h2>
    <p>Владелец может в любой момент отозвать доступ; после этого токен удаляется и
    публикации прекращаются.</p>
    <h2>Контакты</h2>
    <p>По вопросам: joto.streetwear@gmail.com</p>
    <hr>
    <p>The service processes the minimum data needed to publish the owner's videos to
    the owner's own account: the account authorization token (granted by the owner),
    basic profile info (username), and the owner's video files (only for upload). We
    do not sell or share data with third parties and do not use it for advertising.
    The owner can revoke access at any time. Contact: joto.streetwear@gmail.com</p>
    """
    return _PAGE.format(title="Конфиденциальность", body=body)


@app.route("/<fname>")
def tiktok_site_verification(fname):
    """Отдаёт файл-подпись для верификации домена в TikTok (URL prefix).

    TikTok запрашивает файл вида /tiktok<код>.txt с одной строкой
    'tiktok-developers-site-verification=<код>'. Содержимое выводится из имени
    файла, поэтому повторная верификация с новым кодом тоже сработает без правок.
    """
    if fname.startswith("tiktok") and fname.endswith(".txt"):
        code = fname[len("tiktok"):-len(".txt")]
        return Response(f"tiktok-developers-site-verification={code}\n",
                        mimetype="text/plain")
    abort(404)


@app.route("/tiktok", methods=["GET", "POST"])
def tiktok_page():
    return render_template("tiktok.html")


@app.route("/tiktok/login", methods=["GET"])
def tiktok_login():
    if not tiktok.enabled():
        return jsonify({
            "error": "Не заданы TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / PUBLIC_BASE_URL"
        }), 400
    return redirect(tiktok.auth_url())


@app.route("/tiktok/callback", methods=["GET"])
def tiktok_callback():
    err = request.args.get("error")
    if err:
        return f"TikTok вернул ошибку: {err} — {request.args.get('error_description','')}", 400
    code = request.args.get("code")
    if not code:
        return "Нет параметра code в ответе TikTok", 400
    ok, data = tiktok.exchange_code(code)
    if not ok:
        return jsonify({"ok": False, "tiktok_response": data}), 400
    return redirect("/tiktok")


@app.route("/tiktok/status", methods=["GET"])
def tiktok_status():
    return jsonify({
        "enabled": tiktok.enabled(),
        "connected": tiktok.is_connected(),
        "privacy": tiktok.PRIVACY,
        "account": tiktok.account_info() if tiktok.is_connected() else {"connected": False},
        "queue": tiktok.get_queue(),
    })


@app.route("/tiktok/queue", methods=["POST"])
def tiktok_queue_add():
    body = request.get_json(silent=True) or {}
    video_url = (body.get("video_url") or "").strip()
    publish_at = (body.get("publish_at") or "").strip()
    if not video_url or not publish_at:
        return jsonify({"ok": False, "error": "Нужны video_url и publish_at"}), 400
    tiktok.add_to_queue(video_url, body.get("caption", ""), publish_at)
    return jsonify({"ok": True})


@app.route("/tiktok/queue/delete", methods=["POST"])
def tiktok_queue_delete():
    body = request.get_json(silent=True) or {}
    tiktok.remove_from_queue(body.get("id"))
    return jsonify({"ok": True})


@app.route("/tiktok/queue/clear", methods=["POST"])
def tiktok_queue_clear():
    tiktok.clear_queue()
    return jsonify({"ok": True})


@app.route("/tiktok/yandex/import", methods=["POST"])
def tiktok_yandex_import():
    """Импортирует видео из публичной папки Яндекс.Диска в очередь по расписанию."""
    body = request.get_json(silent=True) or {}
    folder_url = (body.get("folder_url") or "").strip()
    start_at = (body.get("start_at") or "").strip()
    if not folder_url or not start_at:
        return jsonify({"ok": False, "error": "Нужны folder_url и start_at"}), 400
    try:
        added, total = tiktok.import_yandex_folder(
            folder_url, start_at,
            body.get("interval_hours", 24),
            body.get("caption", ""),
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "added": added, "total": total})


@app.route("/tiktok/publish-now", methods=["POST"])
def tiktok_publish_now():
    """Сразу публикует «созревшие» записи, не дожидаясь планировщика."""
    threading.Thread(target=tiktok.process_due, daemon=True).start()
    return jsonify({"ok": True, "message": "Публикация запущена"})


def run_scheduler():
    # Раз в минуту проверяем очередь и публикуем всё, у чего наступило время
    schedule.every(1).minutes.do(tiktok.process_due)
    print("TikTok-бот: планировщик запущен — проверка очереди каждую минуту")
    while True:
        schedule.run_pending()
        time.sleep(20)


if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    port = int(os.environ.get("PORT", 8081))
    app.run(host="0.0.0.0", port=port)
