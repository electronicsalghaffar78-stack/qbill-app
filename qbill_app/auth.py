"""Email-OTP auth + QBill app password, mirroring the Muse QBill v13
semantics exactly:

- request code -> 6-digit random code, stored HASHED (HMAC), 10-minute
  expiry, ONE active code per email (a resend invalidates the old code).
- The code NEVER appears in any API response, log line, or file. If no
  mail route is configured the code is delivered nowhere (v13 behavior).
- Wrong / used / expired / missing codes are rejected with distinct,
  plain errors.

SESSION / DEVICE MODEL (the simple choice, documented):
- One session row = one signed-in device (bearer token, only its hash is
  stored; 30-day expiry).
- OTP verify creates the session LOCKED (unlocked=0). The app password
  unlocks it: first time via set-password ("Set a password" popup in
  v13), later via verify-password. Business data endpoints require an
  unlocked session.
- "Reopen needs only the password": the client keeps its session token
  and calls /lock when the app closes; reopening is verify-password —
  no new OTP.
- "Logout needs OTP + password again": /logout destroys the session, so
  the next sign-in is a fresh OTP verify followed by the password.
- "Forgot password": a session created by verify-otp is marked
  otp_fresh=1. Only such a session may call /reset-password (the email
  code just proved ownership), which replaces the password, unlocks this
  session, clears otp_fresh, and deletes the account's OTHER sessions.
  /lock, verify-password and set-password also clear otp_fresh — once a
  session has been used normally it can no longer reset the password.
- "Classic login" (/login): the everyday route once a password exists —
  email + password, no email code. The session starts UNLOCKED. With
  remember=true it lasts 30 days; without, 1 day. A wrong email and a
  wrong password get the same generic 401 (no account enumeration);
  an account with no password yet gets a 400 pointing at the
  "Create an account" code route instead.
- "Owner approval" (anti-sharing, owner's order): QBill is SOLD by the
  owner, so nobody may USE it without his permission. A brand-new
  account is created UNAPPROVED (approved=0) unless it is the owner's
  own email; pre-existing accounts keep approved=1 (column default).
  Every data route refuses an unapproved account with 403 code
  "pending" BEFORE the locked check (see require_approved); the client
  shows a "waiting for approval" screen. Signups and logins record the
  client IP (first X-Forwarded-For entry, else the peer address) on
  the account (signup_ip / last_ip) and in the access_log table, so
  the owner sees where every account came from (see admin.py).
"""
import hmac
import logging
import re
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request

from . import db, mailer
from .security import (
    ApiError,
    after_iso,
    check_password,
    code_hash,
    hash_password,
    is_past,
    new_otp_code,
    new_session_token,
    now_iso,
    token_hash,
)

log = logging.getLogger("qbill.auth")

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

# ------------------------------------------------------- email checks ----
# Build .21 (owner's check of .20): the old loose regex accepted any
# typo domain ("gmal.com", "gmail.con") and the sign-in flow simply
# continued. _clean_email below is the ONE validation point used by
# request-otp, verify-otp and login, so all three inherit this:
# strict format + a typo guard for the big providers.
_LOCAL_PART_RE = re.compile(r"^[A-Za-z0-9._%+\-]+$")
_DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9-]+$")
_TLD_RE = re.compile(r"^[A-Za-z]{2,}$")
_KNOWN_PROVIDERS = (
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "icloud.com",
)


def _edit_distance(a, b):
    """Levenshtein distance (the strings compared here are tiny)."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(
                min(
                    prev[j] + 1,
                    cur[j - 1] + 1,
                    prev[j - 1] + (0 if ca == cb else 1),
                )
            )
        prev = cur
    return prev[-1]


def _validate_email(email):
    """Return an error message for a bad address, or None when valid.

    Strict format: exactly one @; a local part of allowed characters
    (never starting/ending with a dot, no doubled dots); a domain of
    valid labels (no leading/trailing hyphen) ending in a letters-only
    TLD of at least 2 characters. Then the typo guard: a domain within
    edit distance 2 of a big provider (gmail / yahoo / hotmail /
    outlook / icloud) is almost certainly a mistyped address, so it is
    rejected with the corrected address as a suggestion. Any other
    well-formed domain — including unusual business domains — passes;
    there is deliberately no provider whitelist.
    """
    invalid = "Please enter a valid email address."
    if email.count("@") != 1:
        return invalid
    local, domain = email.split("@")
    if (
        not local
        or not _LOCAL_PART_RE.match(local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
    ):
        return invalid
    labels = domain.split(".")
    if len(labels) < 2:
        return invalid
    for label in labels:
        if (
            not label
            or not _DOMAIN_LABEL_RE.match(label)
            or label.startswith("-")
            or label.endswith("-")
        ):
            return invalid
    if not _TLD_RE.match(labels[-1]):
        return invalid
    if domain not in _KNOWN_PROVIDERS:
        for provider in _KNOWN_PROVIDERS:
            if _edit_distance(domain, provider) <= 2:
                return (
                    "This email address looks incorrect — did you "
                    "mean {}@{}?".format(local, provider)
                )
    return None


OTP_EXPIRY_MINUTES = 10
SESSION_DAYS = 30


def _cfg():
    return current_app.config["QBILL_CFG"]


def _body():
    return request.get_json(silent=True) or {}


def _clean_email(raw):
    email = str(raw or "").strip().lower()
    error = _validate_email(email)
    if error:
        raise ApiError(error)
    return email


def client_ip():
    """The client IP as the owner should see it: behind Render's proxy
    the real address is the FIRST entry of X-Forwarded-For; direct
    connections use the peer address."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.remote_addr


def _log_access(tx, account_id, email, ip, event):
    tx.execute(
        "INSERT INTO access_log (account_id, email, ip, event, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (account_id, email, ip, event, now_iso()),
    )


# ------------------------------------------------------------ sessions ----
def load_session():
    """Return the session+account row for the bearer token, or None."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    th = token_hash(header[len("Bearer "):].strip())
    row = db.query_one(
        "SELECT s.id AS session_id, s.account_id, s.unlocked, s.otp_fresh, "
        "s.expires_at, s.active_business_id, a.email, a.is_owner, a.approved, "
        "a.reject_reason, a.pw_salt, a.pw_hash "
        "FROM sessions s JOIN site_accounts a ON a.id = s.account_id "
        "WHERE s.token_hash = ?",
        (th,),
    )
    if not row or is_past(row["expires_at"]):
        return None
    return row


def require_session(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        session = load_session()
        if not session:
            return jsonify(ok=False, error="Sign-in required."), 401
        g.session = session
        return fn(*args, **kwargs)

    return wrapper


def require_approved(fn):
    """Data routes only: the owner's approval gate. An account he has
    not approved (or has blocked) gets a 403 with code "pending" —
    checked BEFORE the locked check, so a waiting customer always sees
    the approval message, never a password prompt."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not g.session["approved"]:
            return (
                jsonify(
                    ok=False,
                    code="pending",
                    error="Your account is waiting for the owner's approval.",
                ),
                403,
            )
        return fn(*args, **kwargs)

    return wrapper


def require_unlocked(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not g.session["unlocked"]:
            return (
                jsonify(
                    ok=False,
                    code="locked",
                    error="QRLedger is locked. Enter your QRLedger password to continue.",
                ),
                403,
            )
        return fn(*args, **kwargs)

    return wrapper


def require_owner(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not g.session["is_owner"]:
            return jsonify(ok=False, error="Owner sign-in required."), 403
        return fn(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------- OTP ----
@bp.post("/request-otp")
def request_otp():
    email = _clean_email(_body().get("email"))
    cfg = _cfg()
    code = new_otp_code()
    digest = code_hash(cfg.secret_key, email, code)
    with db.transaction() as tx:
        account = tx.query_one(
            "SELECT id FROM site_accounts WHERE email = ?", (email,)
        )
        is_new = account is None
        if is_new:
            # The account row is born here (request-otp is the signup
            # door). New accounts start UNAPPROVED — they may not use
            # QBill until the owner approves them — except the owner's
            # own email, which is approved from birth. The signup IP
            # is recorded for the owner's customer list.
            ip = client_ip()
            is_owner_email = email == cfg.owner_email
            # AUTO-REJECT (owner's anti-sharing rule): a signup whose
            # IP already belongs to a DIFFERENT account is a shared /
            # resold copy starting up — mark it ip_match so the client
            # shows "Account not activated" and the owner sees why.
            # The account is still created (the person stays
            # identifiable, and the owner CAN approve it by hand).
            # The owner's own signups are exempt, and with no IP
            # there is nothing to match.
            reject_reason = None
            if not is_owner_email and ip:
                clash = tx.query_one(
                    "SELECT id FROM site_accounts "
                    "WHERE signup_ip = ? AND email != ? LIMIT 1",
                    (ip, email),
                )
                if clash is not None:
                    reject_reason = "ip_match"
            account_id = tx.insert(
                "INSERT INTO site_accounts (email, is_owner, approved, "
                "signup_ip, last_ip, reject_reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (email, 1 if is_owner_email else 0,
                 1 if is_owner_email else 0, ip, ip, reject_reason, now_iso()),
            )
            _log_access(
                tx, account_id, email, ip,
                "signup_rejected_ip" if reject_reason else "signup",
            )
        # One active code per email: a fresh request kills every older code.
        tx.execute(
            "UPDATE otp_codes SET used = 1 WHERE email = ? AND used = 0", (email,)
        )
        tx.execute(
            "INSERT INTO otp_codes (email, code_hash, expires_at, used, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (email, digest, after_iso(minutes=OTP_EXPIRY_MINUTES), now_iso()),
        )
    # Delivery happens after the code is safely stored; the response below
    # is identical whether delivery worked, failed, or no route exists.
    route, _errors = mailer.send_email(
        email,
        "Your QRLedger login code",
        "Your QRLedger login code is {}.\n\n"
        "It is valid for {} minutes. Do not share it with anyone.\n\n"
        "If you did not request this code, you can ignore this email.".format(
            code, OTP_EXPIRY_MINUTES
        ),
    )
    log.info("otp requested for %s (route: %s)", email, route or "none")
    return jsonify(ok=True, isNewAccount=is_new)


@bp.post("/verify-otp")
def verify_otp():
    data = _body()
    email = _clean_email(data.get("email"))
    code = str(data.get("code") or "").strip()
    if not code:
        raise ApiError("Please enter the code from your email.", code="no_code")
    cfg = _cfg()
    digest = code_hash(cfg.secret_key, email, code)
    rows = db.query_all(
        "SELECT * FROM otp_codes WHERE email = ? ORDER BY id DESC", (email,)
    )
    for row in rows:
        if row["used"] and hmac.compare_digest(row["code_hash"], digest):
            raise ApiError(
                "This code was already used. Please request a new code.",
                code="used",
            )
    active = next((r for r in rows if not r["used"]), None)
    if active is None:
        raise ApiError(
            "No login code is waiting for this email. Please request a code first.",
            code="no_code",
        )
    if is_past(active["expires_at"]):
        raise ApiError(
            "This code has expired. Please request a new code.", code="expired"
        )
    if not hmac.compare_digest(active["code_hash"], digest):
        raise ApiError(
            "Wrong code. Please check the code and try again.", code="wrong_code"
        )

    token = new_session_token()
    with db.transaction() as tx:
        tx.execute("UPDATE otp_codes SET used = 1 WHERE id = ?", (active["id"],))
        account = tx.query_one(
            "SELECT * FROM site_accounts WHERE email = ?", (email,)
        )
        ip = client_ip()
        tx.execute(
            "UPDATE site_accounts SET last_ip = ? WHERE id = ?",
            (ip, account["id"]),
        )
        _log_access(tx, account["id"], email, ip, "login")
        tx.execute(
            "INSERT INTO sessions (token_hash, account_id, unlocked, otp_fresh, expires_at, created_at) "
            "VALUES (?, ?, 0, 1, ?, ?)",
            (token_hash(token), account["id"], after_iso(days=SESSION_DAYS), now_iso()),
        )
        business = tx.query_one(
            "SELECT id FROM businesses WHERE account_id = ? ORDER BY id LIMIT 1",
            (account["id"],),
        )
        if business is None:
            tx.insert(
                "INSERT INTO businesses (account_id, name, created_at) "
                "VALUES (?, ?, ?) RETURNING id",
                (account["id"], "My Business", now_iso()),
            )
    return jsonify(
        ok=True,
        token=token,
        isOwner=bool(account["is_owner"]),
        hasAppPassword=account["pw_hash"] is not None,
    )


# ------------------------------------------------------- classic login ----
@bp.post("/login")
def login():
    """Everyday sign-in: email + password, no email code needed.

    A wrong email and a wrong password return the SAME generic 401, so
    this endpoint never reveals whether an account exists. An account
    that has no password yet cannot use this route — its owner must
    first prove the email with a code ("Create an account") and set one.
    remember -> 30-day session; otherwise the session lasts 1 day.
    """
    data = _body()
    email = _clean_email(data.get("email"))
    password = str(data.get("password") or "")
    remember = bool(data.get("remember"))
    account = db.query_one(
        "SELECT * FROM site_accounts WHERE email = ?", (email,)
    )
    if account is None:
        return jsonify(ok=False, error="Email or password is wrong."), 401
    if account["pw_hash"] is None:
        raise ApiError(
            "This account does not have a password yet. Tap "
            "“Create an account” and verify your email to set one."
        )
    if not check_password(password, account["pw_salt"], account["pw_hash"]):
        return jsonify(ok=False, error="Email or password is wrong."), 401
    token = new_session_token()
    days = SESSION_DAYS if remember else 1
    with db.transaction() as tx:
        ip = client_ip()
        tx.execute(
            "UPDATE site_accounts SET last_ip = ? WHERE id = ?",
            (ip, account["id"]),
        )
        _log_access(tx, account["id"], email, ip, "login")
        tx.execute(
            "INSERT INTO sessions (token_hash, account_id, unlocked, otp_fresh, expires_at, created_at) "
            "VALUES (?, ?, 1, 0, ?, ?)",
            (token_hash(token), account["id"], after_iso(days=days), now_iso()),
        )
        business = tx.query_one(
            "SELECT id FROM businesses WHERE account_id = ? ORDER BY id LIMIT 1",
            (account["id"],),
        )
        if business is None:
            tx.insert(
                "INSERT INTO businesses (account_id, name, created_at) "
                "VALUES (?, ?, ?) RETURNING id",
                (account["id"], "My Business", now_iso()),
            )
    return jsonify(
        ok=True,
        token=token,
        isOwner=bool(account["is_owner"]),
        hasAppPassword=True,
    )


# --------------------------------------------------------- app password ----
@bp.post("/set-password")
@require_session
def set_password():
    password = str(_body().get("password") or "")
    if len(password) < 4:
        raise ApiError("Password must be at least 4 characters.")
    if g.session["pw_hash"] is not None:
        raise ApiError(
            "A QRLedger password is already set. Use change-password instead."
        )
    salt, digest = hash_password(password)
    with db.transaction() as tx:
        tx.execute(
            "UPDATE site_accounts SET pw_salt = ?, pw_hash = ? WHERE id = ?",
            (salt, digest, g.session["account_id"]),
        )
        tx.execute(
            "UPDATE sessions SET unlocked = 1, otp_fresh = 0 WHERE id = ?",
            (g.session["session_id"],),
        )
    return jsonify(ok=True)


@bp.post("/verify-password")
@require_session
def verify_password():
    if g.session["pw_hash"] is None:
        raise ApiError("No QRLedger password is set yet. Set a password first.")
    password = str(_body().get("password") or "")
    if not check_password(password, g.session["pw_salt"], g.session["pw_hash"]):
        return jsonify(ok=False, error="Wrong password."), 401
    db.run(
        "UPDATE sessions SET unlocked = 1, otp_fresh = 0 WHERE id = ?",
        (g.session["session_id"],),
    )
    return jsonify(ok=True)


@bp.post("/change-password")
@require_session
@require_unlocked
def change_password():
    data = _body()
    old = str(data.get("oldPassword") or "")
    new = str(data.get("newPassword") or "")
    if not check_password(old, g.session["pw_salt"], g.session["pw_hash"]):
        return jsonify(ok=False, error="Current password is wrong."), 401
    if len(new) < 4:
        raise ApiError("New password must be at least 4 characters.")
    salt, digest = hash_password(new)
    db.run(
        "UPDATE site_accounts SET pw_salt = ?, pw_hash = ? WHERE id = ?",
        (salt, digest, g.session["account_id"]),
    )
    return jsonify(ok=True)


@bp.post("/reset-password")
@require_session
def reset_password():
    """Forgot password: replace it right after an email-code sign-in.

    Allowed ONLY on a session created moments ago by verify-otp
    (otp_fresh=1) — the code just proved this email belongs to the user —
    and only when the account already HAS a password (a first password
    goes through set-password instead). On success this session is
    unlocked and every OTHER session of the account is deleted, so a
    reset also signs out any other device.
    """
    if g.session["pw_hash"] is None:
        raise ApiError(
            "This account has no password yet, so there is nothing to "
            "replace. Please create your first password."
        )
    if not g.session["otp_fresh"]:
        raise ApiError(
            "For security, a forgotten password can only be replaced "
            "right after signing in with an email code. Please sign in "
            "with a fresh code first."
        )
    password = str(_body().get("password") or "")
    if len(password) < 4:
        raise ApiError("Password must be at least 4 characters.")
    salt, digest = hash_password(password)
    with db.transaction() as tx:
        tx.execute(
            "UPDATE site_accounts SET pw_salt = ?, pw_hash = ? WHERE id = ?",
            (salt, digest, g.session["account_id"]),
        )
        tx.execute(
            "UPDATE sessions SET unlocked = 1, otp_fresh = 0 WHERE id = ?",
            (g.session["session_id"],),
        )
        tx.execute(
            "DELETE FROM sessions WHERE account_id = ? AND id != ?",
            (g.session["account_id"], g.session["session_id"]),
        )
    return jsonify(ok=True)


@bp.post("/lock")
@require_session
def lock():
    """App closed: this device now needs only the password to reopen."""
    db.run(
        "UPDATE sessions SET unlocked = 0, otp_fresh = 0 WHERE id = ?",
        (g.session["session_id"],),
    )
    return jsonify(ok=True)


@bp.post("/logout")
@require_session
def logout():
    """Full sign-out: the next sign-in needs OTP + password again."""
    db.run("DELETE FROM sessions WHERE id = ?", (g.session["session_id"],))
    return jsonify(ok=True)


@bp.get("/me")
@require_session
def me():
    from .team import memberships_for

    memberships = memberships_for(g.session["account_id"])
    active_id = g.session.get("active_business_id")
    active_role = None
    if active_id:
        for membership in memberships:
            if membership["businessId"] == active_id:
                active_role = membership["role"]
    return jsonify(
        ok=True,
        email=g.session["email"],
        isOwner=bool(g.session["is_owner"]),
        approved=bool(g.session["approved"]),
        rejectReason=g.session["reject_reason"],
        hasAppPassword=g.session["pw_hash"] is not None,
        unlocked=bool(g.session["unlocked"]),
        otpFresh=bool(g.session["otp_fresh"]),
        memberOf=memberships,
        activeBusinessId=active_id,
        activeRole=active_role,
    )
