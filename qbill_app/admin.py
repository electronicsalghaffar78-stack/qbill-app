"""Owner-only admin: email-delivery (mail) configuration + customer
accounts (the approval gate).

Same rule as the Muse QBill build: provider API keys live ONLY server-side
(the mail_config table or environment variables). They are WRITE-ONLY —
accepted on POST, never returned by any endpoint, never logged. Saving with
an empty key field keeps the previously saved key (v13 behavior), so the
owner can update the From name/email without re-entering keys.

Customer accounts: QBill is sold by the owner, so every account is listed
here with its signup/last IPs. New accounts are born UNAPPROVED and cannot
use QBill (auth.require_approved) until he approves them; blocking an
account un-approves it AND kills its live sessions. His own account can
never be blocked.
"""
import logging

from flask import Blueprint, current_app, jsonify, request

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


# ------------------------------------------------------- customer list ----
def _find_account(email):
    return db.query_one(
        "SELECT id, email FROM site_accounts WHERE email = ?", (email,)
    )


def _account_email_from_body():
    data = request.get_json(silent=True) or {}
    return str(data.get("email") or "").strip().lower()


@bp.get("/accounts")
@require_session
@require_owner
def list_accounts():
    rows = db.query_all(
        "SELECT a.id, a.email, a.is_owner, a.approved, a.reject_reason, "
        "a.created_at, a.signup_ip, a.last_ip, "
        "(SELECT MAX(l.created_at) FROM access_log l "
        " WHERE l.account_id = a.id AND l.event = 'login') AS last_login_at "
        "FROM site_accounts a ORDER BY a.id"
    )
    return jsonify(
        ok=True,
        accounts=[
            {
                "email": r["email"],
                "approved": bool(r["approved"]),
                "rejectReason": r["reject_reason"],
                "isOwner": bool(r["is_owner"]),
                "createdAt": r["created_at"],
                "signupIp": r["signup_ip"],
                "lastIp": r["last_ip"],
                "lastLoginAt": r["last_login_at"],
            }
            for r in rows
        ],
    )


@bp.post("/accounts/approve")
@require_session
@require_owner
def approve_account():
    email = _account_email_from_body()
    account = _find_account(email)
    if account is None:
        return jsonify(ok=False, error="No account with that email."), 404
    # Approving is also the owner's override for an auto-rejected
    # (duplicate-IP) signup: the reject mark is cleared.
    db.run(
        "UPDATE site_accounts SET approved = 1, reject_reason = NULL "
        "WHERE id = ?",
        (account["id"],),
    )
    return jsonify(ok=True, message="Account approved: {}".format(email))


@bp.post("/accounts/block")
@require_session
@require_owner
def block_account():
    email = _account_email_from_body()
    if email == current_app.config["QBILL_CFG"].owner_email:
        return (
            jsonify(ok=False, error="The owner's account cannot be blocked."),
            400,
        )
    account = _find_account(email)
    if account is None:
        return jsonify(ok=False, error="No account with that email."), 404
    with db.transaction() as tx:
        tx.execute(
            "UPDATE site_accounts SET approved = 0 WHERE id = ?",
            (account["id"],),
        )
        # Blocking signs the account out everywhere, immediately.
        tx.execute(
            "DELETE FROM sessions WHERE account_id = ?", (account["id"],)
        )
    return jsonify(ok=True, message="Account blocked: {}".format(email))


@bp.post("/heavy/ping")
@require_session
@require_owner
@heavy_endpoint
def heavy_ping():
    """Placeholder proving the anti-freeze heavy-work gate is wired.
    PDF generation and other heavy endpoints will sit behind this gate."""
    return jsonify(ok=True, note="heavy gate OK (placeholder)")
