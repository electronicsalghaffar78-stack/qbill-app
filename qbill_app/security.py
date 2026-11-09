"""Hashing, token and time helpers. Money never passes through here —
all money in QBill is integer paisa end to end (no floats, ever)."""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone


class ApiError(Exception):
    """A plain, user-safe API error (message is shown to the client)."""

    def __init__(self, message, status=400, code=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


# ---------------------------------------------------------------- time ----
def now_iso():
    return datetime.now(timezone.utc).isoformat()


def after_iso(minutes=0, days=0):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes, days=days)).isoformat()


def is_past(iso_text):
    return datetime.fromisoformat(iso_text) < datetime.now(timezone.utc)


# ----------------------------------------------------------------- OTP ----
def new_otp_code():
    return "{:06d}".format(secrets.randbelow(1_000_000))


def code_hash(secret_key, email, code):
    """HMAC of the code — the plaintext code is never stored anywhere."""
    return hmac.new(
        secret_key.encode(), "{}:{}".format(email, code).encode(), hashlib.sha256
    ).hexdigest()


# ------------------------------------------------------------- sessions ----
def new_session_token():
    return secrets.token_urlsafe(32)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------- app password ----
_PBKDF2_ROUNDS = 200_000


def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS
    )
    return salt, dk.hex()


def check_password(password, salt, expected_hex):
    _, candidate = hash_password(password, salt)
    return hmac.compare_digest(candidate, expected_hex)
