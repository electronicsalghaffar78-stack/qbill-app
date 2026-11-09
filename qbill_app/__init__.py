"""QBill Web application factory.

Anti-freeze rules (QGrab lessons, blueprint design locks):
- /healthz answers instantly and NEVER touches the database or network —
  it is registered here, before and independently of every blueprint.
- Heavy work sits behind the concurrency gate in qbill_app.heavy.
- All data lives in the external Postgres from DATABASE_URL; the local
  SQLite fallback exists for dev/tests only and announces itself.
"""
import logging

from flask import Flask, jsonify, send_from_directory

from . import db
from .config import Config
from .security import ApiError

APP_VERSION = "2026-11-09.12-vyapar"


def create_app():
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO)
    cfg = Config()
    if cfg.secret_key_is_dev:
        print(
            "WARNING: SECRET_KEY is not set — using a development fallback. "
            "Production must set SECRET_KEY.",
            flush=True,
        )
    app = Flask(__name__)
    app.config["QBILL_CFG"] = cfg

    db.init_app(cfg)

    from .admin import bp as admin_bp
    from .auth import bp as auth_bp
    from .backups import bp as backups_bp
    from .business import bp as business_bp
    from .crud import bp as crud_bp
    from .pdf import bp as pdf_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(crud_bp)
    app.register_blueprint(business_bp)
    app.register_blueprint(pdf_bp)
    app.register_blueprint(backups_bp)

    @app.errorhandler(ApiError)
    def _api_error(err):
        payload = {"ok": False, "error": err.message}
        if err.code:
            payload["code"] = err.code
        return jsonify(payload), err.status

    @app.get("/healthz")
    def healthz():
        # DB-free and instant by design — keep-warm and auto-heal depend on it.
        return jsonify(ok=True, app="qbill-web", version=APP_VERSION)

    # ------------------------------------------------------------- PWA ----
    # The single-page app lives in qbill_app/static/. The service worker is
    # served from the root path so its scope covers the whole app; the app
    # shell is cached by it so QBill OPENS offline. API responses are never
    # cached by the worker (see static/sw.js).
    import os

    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

    @app.get("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.get("/sw.js")
    def service_worker():
        resp = send_from_directory(static_dir, "sw.js")
        resp.headers["Content-Type"] = "application/javascript"
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    @app.get("/manifest.webmanifest")
    def manifest():
        resp = send_from_directory(static_dir, "manifest.webmanifest")
        resp.headers["Content-Type"] = "application/manifest+json"
        return resp

    return app
