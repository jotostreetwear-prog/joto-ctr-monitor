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
from flask import Flask, request, jsonify, render_template, redirect

import tiktok

app = Flask(__name__)


@app.route("/", methods=["GET"])
def index():
    return redirect("/tiktok")


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
