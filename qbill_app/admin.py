"""Owner-only admin: email-delivery (mail) configuration.

Same rule as the Muse QBill build: provider API keys live ONLY server-side
(the mail_config table or environment variables). They are WRITE-ONLY —
accepted on POST, never returned by any endpoint, never logged. Saving with
an empty key field keeps the previously saved key (v13 behavior), so the
owner can update the From name/email without re-entering keys.
"""
import logging

from flask import Blueprint, jsonify, request

from . import db, mailer
from .auth import require_owner, require_session
from .heavy import heavy_endpoint
from .security import ApiError, now_iso

log = logging.getLogger("qbill.admin")

bp = Blueprint("admin", __name__, url_prefix="/api/admin")

_KEY_FIELDS = ("resend_api_key", "brevo_api_key", "mj_api_key", "mj_api_secret")
_PLAIN_FIELDS = ("from_email", "from_name")


def _status_payload():
    from_email, from_name = mailer.from_identity()
    routes = mailer.route_status()
    return {
        "ok": True,
        "routes": routes,
        "anyConfigured": any(routes.values()),
        "fromEmail": from_email,
        "fromName": from_name,
    }


@bp.get("/mail-config")
@require_session
@require_owner
def get_mail_config():
    # Status booleans + From identity only. Key values are never returned.
    return jsonify(_status_payload())


@bp.post("/mail-config")
@require_session
@require_owner
def save_mail_config():
    data = request.get_json(silent=True) or {}
    with db.transaction() as tx:
        for field in _KEY_FIELDS:
            if field in data:
                value = str(data[field] or "").strip()
                if value:  # empty field = keep the saved key (v13 rule)
                    tx.execute(
                        "INSERT INTO mail_config (key, value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                        "updated_at = excluded.updated_at",
                        (field, value, now_iso()),
                    )
        for field in _PLAIN_FIELDS:
            if field in data:
                value = str(data[field] or "").strip()
                if value:
                    tx.execute(
                        "INSERT INTO mail_config (key, value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                        "updated_at = excluded.updated_at",
                        (field, value, now_iso()),
                    )
    # Audit line only — never the values (mirrors the Muse build's log).
    log.info("Email delivery settings saved")
    return jsonify(_status_payload())


@bp.post("/mail-test")
@require_session
@require_owner
def mail_test():
    data = request.get_json(silent=True) or {}
    to = str(data.get("to") or "").strip()
    if not to or "@" not in to:
        raise ApiError("Please enter the email address to send the test to.")
    route, errors = mailer.send_email(
        to,
        "QBill — test email",
        "This is a test email from QBill. Email delivery is working.",
    )
    if route is None:
        return jsonify(ok=False, error="; ".join(errors))
    return jsonify(ok=True, route=route)


@bp.post("/heavy/ping")
@require_session
@require_owner
@heavy_endpoint
def heavy_ping():
    """Placeholder proving the anti-freeze heavy-work gate is wired.
    PDF generation and other heavy endpoints will sit behind this gate."""
    return jsonify(ok=True, note="heavy gate OK (placeholder)")
