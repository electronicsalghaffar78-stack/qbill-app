"""Multi-route email delivery with AUTOMATIC FAILOVER (owner's order:
the OTP must never get stuck — if one route fails, the next tries itself).

Routes, tried in order, first success wins:
  1. Resend API   (RESEND_API_KEY   env, else mail_config 'resend_api_key')
  2. Brevo API    (BREVO_API_KEY    env, else mail_config 'brevo_api_key')
  3. Mailjet API  (MJ_API_KEY + MJ_API_SECRET env, else mail_config)

All routes are HTTPS APIs — Render's free tier blocks SMTP ports, so there
is deliberately no SMTP route (blueprint design lock).

If NO route is configured, delivery is a no-op: send_email returns
(None, [...]) and the caller still succeeds — the OTP code exists hashed
server-side but is delivered nowhere, exactly the Muse QBill v13 safe
behavior.

DEV RECORDER: when the environment variable QBILL_DEV_MAIL=1 (read from the
process environment only — never from any request input), the recorder
short-circuits all real routes and appends the message to DEV_OUTBOX in
memory. It exists for the test suite; it exposes no HTTP endpoint.

SECRECY: this module never logs a message body or a code, and never logs a
key. Log lines carry only the route name and the recipient address.
"""
import base64
import logging
import os

import requests

from . import db
from .config import DEFAULT_FROM_EMAIL, DEFAULT_FROM_NAME

log = logging.getLogger("qbill.mailer")

TIMEOUT_SECONDS = 10

# In-memory only, dev/test only (QBILL_DEV_MAIL=1). Never served over HTTP.
DEV_OUTBOX = []


def _mail_config(key):
    row = db.query_one("SELECT value FROM mail_config WHERE key = ?", (key,))
    return row["value"] if row and row["value"] else None


def _key(env_name, config_key):
    return os.environ.get(env_name) or _mail_config(config_key)


def from_identity():
    email = (
        os.environ.get("MAIL_FROM_EMAIL")
        or _mail_config("from_email")
        or DEFAULT_FROM_EMAIL
    )
    name = os.environ.get("MAIL_FROM_NAME") or _mail_config("from_name") or DEFAULT_FROM_NAME
    return email, name


def route_status():
    """Booleans only — key values are never returned anywhere."""
    return {
        "dev_recorder": os.environ.get("QBILL_DEV_MAIL") == "1",
        "resend": bool(_key("RESEND_API_KEY", "resend_api_key")),
        "brevo": bool(_key("BREVO_API_KEY", "brevo_api_key")),
        "mailjet": bool(
            _key("MJ_API_KEY", "mj_api_key") and _key("MJ_API_SECRET", "mj_api_secret")
        ),
    }


def _try(label, fn, errors):
    try:
        status, detail = fn()
    except requests.RequestException as exc:
        errors.append("{}: {}".format(label, type(exc).__name__))
        return False
    if status < 300:
        return True
    errors.append("{}: HTTP {} {}".format(label, status, detail[:200]))
    return False


def send_email(to, subject, text, attachments=None):
    """Deliver one email. Returns (route_name, errors); route_name is None
    when nothing could deliver. Never raises for delivery problems.

    attachments: optional list of {"filename", "content" (bytes),
    "content_type"} — carried by every route that supports them (all
    three HTTPS APIs do). Attachment contents are never logged."""
    attachments = attachments or []
    if os.environ.get("QBILL_DEV_MAIL") == "1":
        DEV_OUTBOX.append(
            {
                "to": to,
                "subject": subject,
                "text": text,
                "attachments": [
                    {
                        "filename": a["filename"],
                        "content": bytes(a["content"]),
                        "content_type": a.get("content_type", "application/octet-stream"),
                    }
                    for a in attachments
                ],
            }
        )
        log.info("mail recorded by dev recorder for %s", to)
        return "dev-recorder", []

    from_email, from_name = from_identity()
    errors = []

    resend_key = _key("RESEND_API_KEY", "resend_api_key")
    if resend_key:
        def _resend():
            r = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": "Bearer {}".format(resend_key),
                    "Content-Type": "application/json",
                },
                json={
                    "from": "{} <{}>".format(from_name, from_email),
                    "to": [to],
                    "subject": subject,
                    "text": text,
                    "attachments": [
                        {
                            "filename": a["filename"],
                            "content": base64.b64encode(a["content"]).decode(),
                        }
                        for a in attachments
                    ],
                },
                timeout=TIMEOUT_SECONDS,
            )
            return r.status_code, r.text

        if _try("resend", _resend, errors):
            log.info("mail sent via resend to %s", to)
            return "resend", errors

    brevo_key = _key("BREVO_API_KEY", "brevo_api_key")
    if brevo_key:
        def _brevo():
            r = requests.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={"api-key": brevo_key, "Content-Type": "application/json"},
                json={
                    "sender": {"name": from_name, "email": from_email},
                    "to": [{"email": to}],
                    "subject": subject,
                    "textContent": text,
                    "attachment": [
                        {
                            "name": a["filename"],
                            "content": base64.b64encode(a["content"]).decode(),
                        }
                        for a in attachments
                    ],
                },
                timeout=TIMEOUT_SECONDS,
            )
            return r.status_code, r.text

        if _try("brevo", _brevo, errors):
            log.info("mail sent via brevo to %s", to)
            return "brevo", errors

    mj_key = _key("MJ_API_KEY", "mj_api_key")
    mj_secret = _key("MJ_API_SECRET", "mj_api_secret")
    if mj_key and mj_secret:
        def _mailjet():
            r = requests.post(
                "https://api.mailjet.com/v3.1/send",
                auth=(mj_key, mj_secret),
                json={
                    "Messages": [
                        {
                            "From": {"Email": from_email, "Name": from_name},
                            "To": [{"Email": to}],
                            "Subject": subject,
                            "TextPart": text,
                            "Attachments": [
                                {
                                    "ContentType": a.get(
                                        "content_type", "application/octet-stream"
                                    ),
                                    "Filename": a["filename"],
                                    "Base64Content": base64.b64encode(
                                        a["content"]
                                    ).decode(),
                                }
                                for a in attachments
                            ],
                        }
                    ]
                },
                timeout=TIMEOUT_SECONDS,
            )
            return r.status_code, r.text

        if _try("mailjet", _mailjet, errors):
            log.info("mail sent via mailjet to %s", to)
            return "mailjet", errors

    if not errors:
        errors.append("no mail route configured")
    log.warning("mail to %s not delivered: %s", to, "; ".join(errors))
    return None, errors
