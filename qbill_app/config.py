"""Environment-driven configuration for QBill Web.

Design locks (blueprint, verified 2026-11-09):
- Production data lives in an EXTERNAL Postgres given by DATABASE_URL
  (never Render's free Postgres, which expires in 30 days).
- With DATABASE_URL unset we fall back to a local SQLite file — that is a
  DEV/TEST convenience only and is announced loudly at startup.
"""
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_OWNER_EMAIL = "electronicsalghaffar78@gmail.com"
DEFAULT_FROM_EMAIL = "onboarding@resend.dev"
DEFAULT_FROM_NAME = "QRLedger"


class Config:
    def __init__(self):
        self.secret_key = os.environ.get("SECRET_KEY", "")
        self.secret_key_is_dev = not self.secret_key
        if self.secret_key_is_dev:
            # Deterministic dev fallback so local runs work; production MUST
            # set SECRET_KEY (it keys the OTP code hashes).
            self.secret_key = "qbill-dev-secret-not-for-production"
        self.database_url = os.environ.get("DATABASE_URL", "").strip()
        self.sqlite_path = os.environ.get(
            "QBILL_SQLITE_PATH", os.path.join(_PROJECT_ROOT, "dev.db")
        )
        self.owner_email = (
            os.environ.get("OWNER_EMAIL", DEFAULT_OWNER_EMAIL).strip().lower()
        )

    @property
    def using_sqlite_fallback(self):
        return not self.database_url
