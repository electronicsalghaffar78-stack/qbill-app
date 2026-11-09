"""QBill Web — standalone billing website (QGrab pattern).

Entry point kept thin on purpose: the whole application lives in the
qbill_app package; gunicorn serves `app:app` exactly as before
(see deploy/Dockerfile — anti-freeze worker settings unchanged).

Design locks (blueprint, verified 2026-11-09):
- /healthz is instant and DB-free; heavy work is concurrency-gated.
- Data lives ONLY in an external Postgres (DATABASE_URL) — never on the
  ephemeral filesystem, never in Render's expiring free Postgres. The
  SQLite fallback is dev/test only and warns loudly at startup.
- OTP email goes through HTTPS APIs only (Resend -> Brevo -> Mailjet,
  automatic failover): Render's free tier blocks SMTP ports.
"""
import os

from qbill_app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
