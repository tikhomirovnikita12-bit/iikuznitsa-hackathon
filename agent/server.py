"""Local integration UI and backend endpoints."""
import secrets
from collections import defaultdict, deque
from threading import Lock
from flask import Flask, abort, jsonify, request, send_file, session
from werkzeug.exceptions import HTTPException
from .backend import Backend
from .dialog import Agent
from .tools.manual import Manuals


def create_app(agent=None):
    app = Flask(__name__, template_folder="web")
    app.secret_key = secrets.token_bytes(32)
    agent = agent or Agent(Backend(), Manuals())
    histories = defaultdict(lambda: deque(maxlen=6))
    drafts = {}
    lock = Lock()

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        if isinstance(exc, (KeyError, ValueError)):
            return jsonify(error=str(exc)), 400
        app.logger.exception("Сбой обработки запроса")
        return jsonify(error="Сервис недоступен. Проверьте журнал бэкенда."), 502

    @app.get("/")
    def index():
        from flask import render_template
        session.setdefault("client", secrets.token_hex(16))
        return render_template("index.html")

    @app.get("/api/fleet")
    def fleet():
        return {v["vehicle_id"]: [s["session_id"] for s in v["sessions"]] for v in agent.backend.fleet.values()}

    @app.get("/manuals/<manual_id>")
    def manual(manual_id):
        if manual_id not in agent.manuals.files:
            abort(404)
        return send_file((agent.backend.materials / agent.manuals.files[manual_id]).resolve())

    def context(body):
        session.setdefault("client", secrets.token_hex(16))
        return session["client"], body["vehicle_id"], body["session_id"]

    @app.post("/api/chat")
    def chat():
        body = request.get_json()
        key = context(body)
        q = body["question"]
        if not isinstance(q, str) or not q.strip() or len(q) > 4000:
            abort(400)
        with lock:
            history = list(histories[key])
        reply = agent.turn(*key[1:], q, history)
        with lock:
            histories[key].append([q, reply["verdict"], reply["clarifying_question"], reply["checks"][:1]])
            if reply["ticket_draft"]:
                token = secrets.token_urlsafe(24)
                drafts[key] = (token, reply["ticket_draft"])
                reply["draft_id"] = token
        return reply

    @app.post("/api/confirm")
    def confirm():
        body = request.get_json()
        key = context(body)
        with lock:
            saved = drafts.get(key)
            if not saved or saved[0] != body["draft_id"]:
                abort(409, "Черновик устарел или относится к другому случаю.")
            # Never retry an ambiguous external write automatically.
            del drafts[key]
        return agent.backend.submit(saved[1])

    return app


if __name__ == "__main__":
    create_app().run(port=8080)
