"""QBill Web — Phase 1 foundation test suite.

Plain python, no framework. Boots the app on a throwaway SQLite file with
the dev mail recorder on (QBILL_DEV_MAIL=1) and checks the whole foundation
end to end: health, OTP semantics (v13), owner gate, write-only API keys,
the app-password gate, and paisa-exact invoice math.

Run:  .venv/bin/python tests/run_tests.py
"""
import io
import json
import logging
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TEST_DB = os.path.join(ROOT, "tests", "test.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

# Environment must be set BEFORE the app is created.
os.environ["QBILL_SQLITE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["QBILL_DEV_MAIL"] = "1"
os.environ.pop("DATABASE_URL", None)
for _k in ("RESEND_API_KEY", "BREVO_API_KEY", "MJ_API_KEY", "MJ_API_SECRET"):
    os.environ.pop(_k, None)  # never let a real key leak into the tests

OWNER = "electronicsalghaffar78@gmail.com"  # Config default owner
DUMMY_KEY = "re_DUMMYKEY_do_not_log_123456"

# Capture ALL app log output so we can prove secrets never reach the logs.
log_stream = io.StringIO()
_handler = logging.StreamHandler(log_stream)
_handler.setLevel(logging.DEBUG)
logging.getLogger().addHandler(_handler)
logging.getLogger().setLevel(logging.DEBUG)

from qbill_app import create_app, db, mailer  # noqa: E402

app = create_app()
client = app.test_client()

RESULTS = []
ALL_CODES = set()


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print("{} {}{}".format("PASS" if cond else "FAIL", name,
                           (" — " + detail) if (detail and not cond) else ""))


def H(token):
    return {"Authorization": "Bearer {}".format(token)}


def latest_code(email):
    for msg in reversed(mailer.DEV_OUTBOX):
        if msg["to"] == email:
            m = re.search(r"\b(\d{6})\b", msg["text"])
            if m:
                ALL_CODES.add(m.group(1))
                return m.group(1)
    return None


def request_and_get_code(email):
    r = client.post("/api/auth/request-otp", json={"email": email})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r, latest_code(email)


# ------------------------------------------------------------ 1. health --
r = client.get("/healthz")
check("healthz is ok", r.status_code == 200 and r.get_json().get("ok") is True)

# --------------------------------------- 2. no mail route = safe no-op --
os.environ.pop("QBILL_DEV_MAIL", None)
mailer.DEV_OUTBOX.clear()
r = client.post("/api/auth/request-otp", json={"email": "noroute@example.com"})
body = r.get_json()
check(
    "no-route OTP request still succeeds (v13 no-op delivery)",
    r.status_code == 200 and body.get("ok") is True
    and set(body.keys()) == {"ok", "isNewAccount"}
    and len(mailer.DEV_OUTBOX) == 0,
    "body={} outbox={}".format(body, len(mailer.DEV_OUTBOX)),
)
os.environ["QBILL_DEV_MAIL"] = "1"

# -------------------------------------------- 3. owner OTP via recorder --
r, code1 = request_and_get_code(OWNER)
check(
    "OTP request: code delivered via recorder only, absent from response",
    code1 is not None and code1 not in r.get_data(as_text=True)
    and set(r.get_json().keys()) == {"ok", "isNewAccount"},
)

# ------------------------------------------------------- 4. wrong code --
wrong = "000000" if code1 != "000000" else "111111"
r = client.post("/api/auth/verify-otp", json={"email": OWNER, "code": wrong})
check(
    "wrong code rejected with wrong_code",
    r.status_code == 400 and r.get_json().get("code") == "wrong_code",
    r.get_data(as_text=True),
)

# ------------------------------------------------------- 5. right code --
r = client.post("/api/auth/verify-otp", json={"email": OWNER, "code": code1})
owner_token = r.get_json().get("token") if r.status_code == 200 else None
check("right code creates a session", bool(owner_token), r.get_data(as_text=True))
r = client.get("/api/auth/me", headers=H(owner_token))
me = r.get_json()
check(
    "me: owner, locked until password, no app password yet",
    me.get("email") == OWNER and me.get("isOwner") is True
    and me.get("unlocked") is False and me.get("hasAppPassword") is False,
    json.dumps(me),
)

# -------------------------------------------------------- 6. used code --
r = client.post("/api/auth/verify-otp", json={"email": OWNER, "code": code1})
check(
    "same code twice: second use rejected as used",
    r.status_code == 400 and r.get_json().get("code") == "used",
    r.get_data(as_text=True),
)

# ------------------------------------------- 7. resend invalidates old --
code2 = None
for _ in range(6):
    _, code2 = request_and_get_code(OWNER)
    if code2 != code1:
        break
r = client.post("/api/auth/verify-otp", json={"email": OWNER, "code": code1})
check(
    "resend invalidates the old code",
    r.status_code == 400 and r.get_json().get("code") == "used",
    r.get_data(as_text=True),
)
r = client.post("/api/auth/verify-otp", json={"email": OWNER, "code": code2})
check("the new code works", r.status_code == 200 and bool(r.get_json().get("token")))

# ------------------------------------------------------ 8. expired code --
email3 = "expired@example.com"
_, code3 = request_and_get_code(email3)
db.run(
    "UPDATE otp_codes SET expires_at = ? WHERE email = ?",
    ("2000-01-01T00:00:00+00:00", email3),
)
r = client.post("/api/auth/verify-otp", json={"email": email3, "code": code3})
check(
    "expired code rejected as expired",
    r.status_code == 400 and r.get_json().get("code") == "expired",
    r.get_data(as_text=True),
)

# ------------------------------------------------- 9. non-owner is gated --
email4 = "shopkeeper@example.com"
_, code4 = request_and_get_code(email4)
r = client.post("/api/auth/verify-otp", json={"email": email4, "code": code4})
user_token = r.get_json().get("token")
r = client.get("/api/admin/mail-config", headers=H(user_token))
check("owner gate: non-owner GET mail-config refused", r.status_code == 403)
r = client.post("/api/admin/mail-config", headers=H(user_token),
                json={"resend_api_key": "re_should_not_stick"})
check("owner gate: non-owner POST mail-config refused", r.status_code == 403)
r = client.get("/api/admin/mail-config")
check("owner gate: no session refused", r.status_code == 401)

# ------------------------------------------- 10. app-password gate flow --
r = client.get("/api/parties", headers=H(user_token))
check("locked session cannot use business data", r.status_code == 403)
r = client.post("/api/auth/set-password", headers=H(user_token),
                json={"password": "shop-pass-1"})
check("set-password succeeds and unlocks", r.status_code == 200)
r = client.get("/api/parties", headers=H(user_token))
check("unapproved account is refused business data (403 pending)",
      r.status_code == 403 and r.get_json().get("code") == "pending",
      r.get_data(as_text=True))
r = client.post("/api/admin/accounts/approve", headers=H(owner_token),
                json={"email": email4})
check("owner approves the shopkeeper account", r.status_code == 200)
r = client.get("/api/parties", headers=H(user_token))
check("unlocked session can use business data", r.status_code == 200)
r = client.post("/api/auth/lock", headers=H(user_token))
r = client.get("/api/parties", headers=H(user_token))
check("after lock, business data is closed again", r.status_code == 403)
r = client.post("/api/auth/verify-password", headers=H(user_token),
                json={"password": "not-the-password"})
check("reopen with wrong password refused", r.status_code == 401)
r = client.post("/api/auth/verify-password", headers=H(user_token),
                json={"password": "shop-pass-1"})
check("reopen needs only the password (no new OTP)", r.status_code == 200)
r = client.post("/api/auth/change-password", headers=H(user_token),
                json={"oldPassword": "wrong-old", "newPassword": "shop-pass-2"})
check("change-password with wrong old password refused", r.status_code == 401)
r = client.post("/api/auth/change-password", headers=H(user_token),
                json={"oldPassword": "shop-pass-1", "newPassword": "shop-pass-2"})
check("change-password succeeds", r.status_code == 200)
client.post("/api/auth/lock", headers=H(user_token))
r = client.post("/api/auth/verify-password", headers=H(user_token),
                json={"password": "shop-pass-2"})
check("new password reopens after change", r.status_code == 200)

# ------------------------------------- 11. mail-config key is write-only --
r = client.post("/api/admin/mail-config", headers=H(owner_token),
                json={"resend_api_key": DUMMY_KEY, "from_name": "QBill"})
check(
    "owner saves mail config; key absent from the save response",
    r.status_code == 200 and DUMMY_KEY not in r.get_data(as_text=True),
    r.get_data(as_text=True),
)
r = client.get("/api/admin/mail-config", headers=H(owner_token))
cfg_body = r.get_json()
check(
    "mail-config status shows resend configured, key absent",
    cfg_body.get("routes", {}).get("resend") is True
    and DUMMY_KEY not in r.get_data(as_text=True),
    r.get_data(as_text=True),
)
# The dev recorder short-circuits real routes by design, so switch it off
# for this one call: mail-test must exercise the real Resend route with the
# stored dummy key (mailer reads the env flag at call time).
os.environ.pop("QBILL_DEV_MAIL", None)
r = client.post("/api/admin/mail-test", headers=H(owner_token),
                json={"to": OWNER})
os.environ["QBILL_DEV_MAIL"] = "1"
check(
    "mail-test with a dummy key fails honestly, key absent from response",
    r.status_code == 200 and r.get_json().get("ok") is False
    and DUMMY_KEY not in r.get_data(as_text=True),
    r.get_data(as_text=True),
)

# -------------------------------------------- 12. paisa-exact invoicing --
r = client.post("/api/parties", headers=H(user_token),
                json={"name": "Ahmed Traders", "phone": "0300-1234567"})
party_id = r.get_json()["party"]["id"]
check("party created", r.status_code == 201 and party_id > 0)
r = client.post("/api/items", headers=H(user_token),
                json={"name": "Solar Panel 550W", "sale_price_paisa": 150050,
                      "purchase_price_paisa": 140000, "stock_qty": 100})
item_a = r.get_json()["item"]["id"]
r = client.post("/api/items", headers=H(user_token),
                json={"name": "Battery 12V", "sale_price_paisa": 99999,
                      "stock_qty": 50})
item_b = r.get_json()["item"]["id"]
r = client.post(
    "/api/invoices",
    headers=H(user_token),
    json={
        "party_id": party_id,
        "discount_paisa": 2479,
        "tax_paisa": 0,
        "paid_paisa": 200000,
        "lines": [
            {"item_id": item_a, "qty": 3, "rate_paisa": 150050},
            {"item_id": item_b, "qty": 2, "rate_paisa": 99999},
            {"description": "Installation charges", "qty": 7, "rate_paisa": 333},
        ],
    },
)
inv = r.get_json().get("invoice", {}) if r.status_code == 201 else {}
check(
    "invoice totals exact to the paisa (652479 - 2479 = 650000)",
    inv.get("subtotalPaisa") == 652479 and inv.get("totalPaisa") == 650000
    and inv.get("paidPaisa") == 200000,
    json.dumps(inv),
)
check(
    "payment reduces the balance exactly (650000 - 200000 = 450000)",
    inv.get("balancePaisa") == 450000 and inv.get("invoiceNo") == "S-1",
    json.dumps(inv),
)
r = client.get("/api/invoices/{}".format(inv.get("id")), headers=H(user_token))
check(
    "invoice re-read shows the same exact balance",
    r.get_json()["invoice"]["balancePaisa"] == 450000,
)
r = client.get("/api/payments", headers=H(user_token))
pays = r.get_json().get("payments", [])
check(
    "the payment is recorded once, for the exact amount",
    len(pays) == 1 and pays[0]["amountPaisa"] == 200000
    and pays[0]["invoiceId"] == inv.get("id"),
    json.dumps(pays),
)
r = client.get("/api/items", headers=H(user_token))
stock = {i["id"]: i["stockQty"] for i in r.get_json()["items"]}
check("stock decremented exactly (100->97, 50->48)",
      stock.get(item_a) == 97 and stock.get(item_b) == 48, json.dumps(stock))

# ============================================================ STEP 2 ====
from datetime import datetime as _dt, timedelta as _td, timezone as _tz  # noqa: E402

from qbill_app import heavy  # noqa: E402
from qbill_app.backups import maybe_run_daily_backup  # noqa: E402

USER_EMAIL = "shopkeeper@example.com"

# ------------------------------------- S2-1. business profile round-trip --
r = client.put("/api/business", headers=H(user_token), json={
    "name": "Al-Ghaffar Electronics and Solar",
    "address": "ABC Center, Bunder Road, Larkana",
    "phone": "03273799008",
    "email": "electronicsalghaffar78@gmail.com",
})
r2 = client.get("/api/business", headers=H(user_token))
prof = r2.get_json().get("business", {})
check(
    "profile round-trip: PUT then GET returns the same fields",
    r.status_code == 200 and prof.get("name") == "Al-Ghaffar Electronics and Solar"
    and prof.get("address") == "ABC Center, Bunder Road, Larkana"
    and prof.get("phone") == "03273799008"
    and prof.get("email") == "electronicsalghaffar78@gmail.com",
    json.dumps(prof),
)
biz_id = prof.get("id")

# --------------------------- S2-2. expense, purchase, dashboard and P&L --
r = client.post("/api/expenses", headers=H(user_token),
                json={"title": "Shop rent", "category": "Rent",
                      "amount_paisa": 50000})
check("expense created with category", r.status_code == 201
      and r.get_json()["expense"]["category"] == "Rent")
r = client.post("/api/parties", headers=H(user_token), json={"name": "Supplier Co"})
supplier_id = r.get_json()["party"]["id"]
r = client.post("/api/items", headers=H(user_token),
                json={"name": "Inverter 2000W", "sale_price_paisa": 95000,
                      "purchase_price_paisa": 80000, "stock_qty": 0})
item_c = r.get_json()["item"]["id"]
r = client.post("/api/invoices", headers=H(user_token), json={
    "type": "purchase", "party_id": supplier_id, "paid_paisa": 30000,
    "lines": [{"item_id": item_c, "qty": 5, "rate_paisa": 80000}],
})
pinv = r.get_json().get("invoice", {}) if r.status_code == 201 else {}
check(
    "purchase invoice: P-1 numbering, exact totals",
    pinv.get("invoiceNo") == "P-1" and pinv.get("totalPaisa") == 400000
    and pinv.get("paidPaisa") == 30000 and pinv.get("balancePaisa") == 370000
    and pinv.get("type") == "purchase",
    json.dumps(pinv),
)
r = client.get("/api/items", headers=H(user_token))
stock = {i["id"]: i["stockQty"] for i in r.get_json()["items"]}
check("purchase ADDS stock (0->5)", stock.get(item_c) == 5, json.dumps(stock))
r = client.get("/api/dashboard", headers=H(user_token))
dash = r.get_json().get("dashboard", {})
check(
    "dashboard totals exact (sales 650000, receivable 450000, cash 120000, stock 13980000)",
    dash.get("todaySalesPaisa") == 650000 and dash.get("monthSalesPaisa") == 650000
    and dash.get("totalReceivablePaisa") == 450000
    and dash.get("cashInHandPaisa") == 120000
    and dash.get("stockValuePaisa") == 13980000,
    json.dumps(dash),
)
r = client.get("/api/pnl", headers=H(user_token))
pnl = r.get_json().get("pnl", {})
check(
    "P&L exact (650000 - 400000 - 50000 = 200000)",
    pnl.get("salesPaisa") == 650000 and pnl.get("purchasesPaisa") == 400000
    and pnl.get("expensesPaisa") == 50000 and pnl.get("profitPaisa") == 200000,
    json.dumps(pnl),
)

# ------------------------------------------- S2-3. party ledger entries --
r = client.get("/api/parties/{}/ledger".format(party_id), headers=H(user_token))
led = r.get_json()
check(
    "ledger: sale + its payment, running balance ends at 450000",
    r.status_code == 200 and led.get("balancePaisa") == 450000
    and len(led.get("entries", [])) == 2
    and led["entries"][-1]["balancePaisa"] == 450000,
    json.dumps(led),
)

# --------------------- S2-4. invoice PDF (bytes, shop name, heavy-gated) --
r = client.get("/api/invoices/{}/pdf".format(inv["id"]), headers=H(user_token))
pdf_bytes = r.get_data()
check(
    "PDF: 200, %PDF bytes, business name inside, pdf content-type",
    r.status_code == 200 and pdf_bytes.startswith(b"%PDF")
    and b"Al-Ghaffar Electronics and Solar" in pdf_bytes
    and "application/pdf" in r.headers.get("Content-Type", ""),
    "status={} ctype={}".format(r.status_code, r.headers.get("Content-Type")),
)

# -------------- S2-5. backup run emails the snapshot; restore is exact --
backup_emails_before = len(
    [m for m in mailer.DEV_OUTBOX if m["subject"].startswith("QBill daily backup")]
)
r = client.post("/api/backups/run", headers=H(user_token))
bkp = r.get_json().get("backup", {}) if r.status_code == 201 else {}
snap_totals = bkp.get("totals") or {}
check(
    "backup run: snapshot created, totals match the dashboard exactly",
    r.status_code == 201 and snap_totals.get("cashInHandPaisa") == 120000
    and snap_totals.get("totalReceivablePaisa") == 450000
    and snap_totals.get("stockValuePaisa") == 13980000,
    json.dumps(bkp),
)
backup_msgs = [m for m in mailer.DEV_OUTBOX
               if m["subject"].startswith("QBill daily backup")]
new_msgs = backup_msgs[backup_emails_before:]
att_ok = False
if len(new_msgs) == 1 and new_msgs[0].get("attachments"):
    att = new_msgs[0]["attachments"][0]
    stored = db.query_one(
        "SELECT payload FROM auto_backups WHERE id = ?", (bkp.get("id"),)
    )
    att_ok = (
        new_msgs[0]["to"] == USER_EMAIL
        and att["filename"].endswith(".json")
        and json.loads(att["content"]) == json.loads(stored["payload"])
    )
check(
    "backup emailed exactly once to the login email, attachment == snapshot",
    bkp.get("emailed") is True and len(new_msgs) == 1 and att_ok,
    "emailed={} new={}".format(bkp.get("emailed"), len(new_msgs)),
)
r = client.post("/api/expenses", headers=H(user_token),
                json={"title": "Tea", "amount_paisa": 7777})
client.post("/api/parties", headers=H(user_token), json={"name": "Temp Party"})
r = client.get("/api/dashboard", headers=H(user_token))
check(
    "mutation before restore really changed the totals",
    r.get_json()["dashboard"]["cashInHandPaisa"] == 120000 - 7777,
)
r = client.post("/api/backups/{}/restore".format(bkp["id"]), headers=H(user_token))
r2 = client.get("/api/dashboard", headers=H(user_token))
dash_after = r2.get_json()["dashboard"]
r3 = client.get("/api/parties", headers=H(user_token))
party_names = [p["name"] for p in r3.get_json()["parties"]]
check(
    "restore: totals identical to the snapshot, mutation wiped",
    r.status_code == 200 and dash_after == snap_totals
    and "Temp Party" not in party_names,
    json.dumps(dash_after),
)

# --------------------------------- S2-6. daily helper honours the 20h rule --
check(
    "daily helper: fresh snapshot exists, so it makes none",
    maybe_run_daily_backup(biz_id) is None,
)
old_ts = (_dt.now(_tz.utc) - _td(hours=21)).isoformat()
db.run(
    "UPDATE auto_backups SET created_at = ? WHERE id = "
    "(SELECT MAX(id) FROM auto_backups)",
    (old_ts,),
)
new_daily_id = maybe_run_daily_backup(biz_id)
check(
    "daily helper: last snapshot >20h old, so it makes one",
    isinstance(new_daily_id, int) and new_daily_id > 0,
)

# --------------------------- S2-7. offline upload dedupes by client_ref --
device_payload = {
    "version": 1, "created_at": "2026-11-08T10:00:00+00:00",
    "source": "device",
    "business": {"name": "Al-Ghaffar Electronics and Solar"},
    "parties": [], "items": [], "invoices": [], "payments": [], "expenses": [],
    "totals": {"todaySalesPaisa": 0, "monthSalesPaisa": 0,
               "totalReceivablePaisa": 0, "cashInHandPaisa": 0,
               "stockValuePaisa": 0},
}
r = client.post("/api/backups/upload", headers=H(user_token), json={
    "payload": device_payload, "created_at": "2026-11-08T10:00:00+00:00",
    "client_ref": "device-snap-1",
})
up1 = r.get_json().get("backup", {}) if r.status_code == 201 else {}
r = client.post("/api/backups/upload", headers=H(user_token), json={
    "payload": device_payload, "created_at": "2026-11-08T10:00:00+00:00",
    "client_ref": "device-snap-1",
})
up2 = r.get_json().get("backup", {}) if r.status_code == 200 else {}
r = client.get("/api/backups", headers=H(user_token))
uploaded = [b for b in r.get_json()["backups"]
            if b.get("createdAt") == "2026-11-08T10:00:00+00:00"]
check(
    "offline upload: same client_ref twice -> ONE server snapshot, emailed",
    up1.get("id") is not None and up2.get("id") == up1.get("id")
    and len(uploaded) == 1 and up1.get("emailed") is True,
    "up1={} up2={} count={}".format(up1.get("id"), up2.get("id"), len(uploaded)),
)

# ---------------------- S2-8. mail failure never breaks a backup (b) -----
os.environ.pop("QBILL_DEV_MAIL", None)  # routes now try the dummy key and fail
r = client.post("/api/backups/run", headers=H(user_token))
os.environ["QBILL_DEV_MAIL"] = "1"
fail_bkp = r.get_json().get("backup", {}) if r.status_code == 201 else {}
r = client.get("/api/backups", headers=H(user_token))
listed = {b["id"]: b for b in r.get_json()["backups"]}
check(
    "forced mailer failure: backup still created, recorded emailed=false",
    r.status_code == 200 and fail_bkp.get("id") in listed
    and listed[fail_bkp["id"]]["emailed"] is False,
    json.dumps(fail_bkp),
)

# -------------------------------- S2-9. client_ref idempotency on creates --
r = client.post("/api/parties", headers=H(user_token),
                json={"name": "Dup Party", "client_ref": "test-ref-1"})
dup_id = r.get_json()["party"]["id"]
r = client.post("/api/parties", headers=H(user_token),
                json={"name": "Dup Party", "client_ref": "test-ref-1"})
r2 = client.get("/api/parties", headers=H(user_token))
dup_count = len([p for p in r2.get_json()["parties"] if p["name"] == "Dup Party"])
check(
    "duplicate client_ref party: same record, no duplicate",
    r.status_code == 200 and r.get_json()["party"]["id"] == dup_id
    and dup_count == 1,
)
r = client.post("/api/items", headers=H(user_token),
                json={"name": "Dedup Item", "sale_price_paisa": 1000,
                      "stock_qty": 10})
item_d = r.get_json()["item"]["id"]
inv_payload = {"client_ref": "inv-ref-1",
               "lines": [{"item_id": item_d, "qty": 2, "rate_paisa": 1000}]}
r = client.post("/api/invoices", headers=H(user_token), json=inv_payload)
inv1_id = r.get_json()["invoice"]["id"]
r = client.post("/api/invoices", headers=H(user_token), json=inv_payload)
r2 = client.get("/api/items", headers=H(user_token))
stock_d = {i["id"]: i["stockQty"] for i in r2.get_json()["items"]}.get(item_d)
check(
    "duplicate client_ref invoice: same invoice, stock NOT decremented twice",
    r.status_code == 200 and r.get_json()["invoice"]["id"] == inv1_id
    and stock_d == 8,
    "stock={}".format(stock_d),
)

# -------------------- S2-10. standalone payment settles an invoice -------
r = client.post("/api/payments", headers=H(user_token), json={
    "invoice_id": inv["id"], "direction": "in", "amount_paisa": 50000,
})
pay_ok = r.status_code == 201
r = client.get("/api/invoices/{}".format(inv["id"]), headers=H(user_token))
inv_now = r.get_json()["invoice"]
r2 = client.post("/api/payments", headers=H(user_token), json={
    "invoice_id": inv["id"], "direction": "in", "amount_paisa": 99999999,
})
check(
    "payment settles invoice (paid 250000, balance 400000); overpay refused",
    pay_ok and inv_now["paidPaisa"] == 250000
    and inv_now["balancePaisa"] == 400000 and r2.status_code == 400,
    json.dumps(inv_now),
)

# ---------------- S2-11. PDF heavy gate (direct semaphore hold + HTTP) ---
_wait_before = heavy.WAIT_SECONDS
heavy.WAIT_SECONDS = 1
for _ in range(3):
    heavy._semaphore.acquire()
try:
    r = client.get("/api/invoices/{}/pdf".format(inv["id"]), headers=H(user_token))
    gate_status, gate_retry = r.status_code, r.headers.get("Retry-After")
finally:
    for _ in range(3):
        heavy._semaphore.release()
    heavy.WAIT_SECONDS = _wait_before
check(
    "heavy gate: with all 3 slots held, the PDF call gets an honest 503",
    gate_status == 503 and gate_retry is not None,
    "status={} retry={}".format(gate_status, gate_retry),
)

# ------------------------------------------------ S2-12. frontend shell --
r = client.get("/")
check(
    "frontend: / serves the SPA with the sync pill",
    r.status_code == 200 and b"syncPill" in r.get_data(),
)
r_sw = client.get("/sw.js")
r_mf = client.get("/manifest.webmanifest")
r_js = client.get("/static/app.js")
check(
    "frontend: service worker, manifest and app.js are served",
    r_sw.status_code == 200 and r_mf.status_code == 200
    and r_js.status_code == 200 and len(r_js.get_data()) > 1000,
)
html = r.get_data(as_text=True)
check(
    "frontend: ONE auth popup (email/code/create/welcome/reset), old auth screens gone",
    'id="authModal"' in html and 'id="stepEmail"' in html
    and 'id="stepCode"' in html and 'id="stepCreate"' in html
    and 'id="stepWelcome"' in html and 'id="stepReset"' in html
    and 'id="btnWelcomeReset"' in html
    and "Create a new password for billing software" in html
    and 'id="authView"' not in html and 'id="lockView"' not in html
    and 'id="pwModal"' not in html,
)

# ============ 12b. reported case: first-run password flow (owner) =======
# Reported 2026-11-09 (4 failed phone attempts): after entering the email
# code, setting his password of choice NEVER worked, and the app kept
# landing on a "locked" screen where Unlock could never succeed because
# no password existed. Cause: the old 6-character minimum silently
# rejected his short password every time. Minimum is now 4; the exact
# reported flow is exercised end to end here.
email5 = "newshop@example.com"
_, code5 = request_and_get_code(email5)
r = client.post("/api/auth/verify-otp", json={"email": email5, "code": code5})
new_token = r.get_json().get("token") if r.status_code == 200 else None
me5 = client.get("/api/auth/me", headers=H(new_token)).get_json() if new_token else {}
check(
    "reported case: fresh account starts locked, no password set",
    bool(new_token) and me5.get("hasAppPassword") is False
    and me5.get("unlocked") is False,
    json.dumps(me5),
)
r = client.get("/api/parties", headers=H(new_token))
check(
    "reported case: no-password session cannot use business data yet",
    r.status_code == 403,
)
r = client.post("/api/auth/set-password", headers=H(new_token),
                json={"password": "ab1"})
check(
    "password under 4 chars rejected with the clear error",
    r.status_code == 400
    and r.get_json().get("error") == "Password must be at least 4 characters.",
    r.get_data(as_text=True),
)
r = client.post("/api/auth/set-password", headers=H(new_token),
                json={"password": "ab12"})
check(
    "reported case FIXED: a 4-character password is now accepted",
    r.status_code == 200, r.get_data(as_text=True),
)
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": email5})  # owner approval opens the data gate
me5 = client.get("/api/auth/me", headers=H(new_token)).get_json()
r2 = client.get("/api/parties", headers=H(new_token))
check(
    "setting the password unlocks the app (business data opens)",
    me5.get("hasAppPassword") is True and me5.get("unlocked") is True
    and r2.status_code == 200,
    json.dumps(me5),
)
client.post("/api/auth/lock", headers=H(new_token))
r = client.get("/api/parties", headers=H(new_token))
check("returning device: lock closes business data again",
      r.status_code == 403)
r = client.post("/api/auth/verify-password", headers=H(new_token),
                json={"password": "zz99"})
check("returning device: wrong password refused", r.status_code == 401)
r = client.post("/api/auth/verify-password", headers=H(new_token),
                json={"password": "ab12"})
check("returning device: the same 4-char password reopens (no new OTP)",
      r.status_code == 200)
r = client.post("/api/auth/change-password", headers=H(new_token),
                json={"oldPassword": "ab12", "newPassword": "xy9"})
check(
    "change-password also enforces the 4-char minimum clearly",
    r.status_code == 400
    and r.get_json().get("error") == "New password must be at least 4 characters.",
    r.get_data(as_text=True),
)
r = client.post("/api/auth/change-password", headers=H(new_token),
                json={"oldPassword": "ab12", "newPassword": "zz42"})
check("change-password accepts a 4-character password",
      r.status_code == 200, r.get_data(as_text=True))
client.post("/api/auth/lock", headers=H(new_token))
r = client.post("/api/auth/verify-password", headers=H(new_token),
                json={"password": "zz42"})
check("the changed 4-char password reopens after lock",
      r.status_code == 200)

# ============ 12c. forgot password: code -> NEW password -> same account =
# Owner requirement 2026-11-09: a user who FORGETS the account password
# gets back in with an email code and creates a NEW password; the SAME
# account (all its data) opens. Before this fix, a fresh OTP session
# still demanded the old, forgotten password — another dead end. Only a
# session created by that fresh code verify (otp_fresh) may reset.
email6 = "forgotshop@example.com"
_, code6 = request_and_get_code(email6)
r = client.post("/api/auth/verify-otp", json={"email": email6, "code": code6})
forgot_a = r.get_json().get("token") if r.status_code == 200 else None
me6 = client.get("/api/auth/me", headers=H(forgot_a)).get_json() if forgot_a else {}
check(
    "forgot flow: a fresh OTP session is marked otpFresh",
    bool(forgot_a) and me6.get("otpFresh") is True,
    json.dumps(me6),
)
r = client.post("/api/auth/set-password", headers=H(forgot_a),
                json={"password": "oldpass1"})
me6 = client.get("/api/auth/me", headers=H(forgot_a)).get_json()
check(
    "forgot flow: set-password clears otpFresh (session used normally)",
    r.status_code == 200 and me6.get("otpFresh") is False,
    r.get_data(as_text=True),
)
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": email6})  # owner approval opens the data gate
# Data on the account that must survive the reset:
r = client.post("/api/parties", headers=H(forgot_a),
                json={"name": "Forgot Test Party"})
forgot_party_id = r.get_json()["party"]["id"] if r.status_code == 201 else None
r = client.post("/api/invoices", headers=H(forgot_a), json={
    "party_id": forgot_party_id,
    "lines": [{"description": "Forgot test sale", "qty": 1, "rate_paisa": 12345}],
})
forgot_inv = r.get_json().get("invoice", {}) if r.status_code == 201 else {}
check(
    "forgot flow: account has data (party + invoice) before the reset",
    forgot_party_id is not None and forgot_inv.get("totalPaisa") == 12345,
    json.dumps(forgot_inv),
)
# A second device/session for the same account (the reset must kick it):
_, code6b = request_and_get_code(email6)
r = client.post("/api/auth/verify-otp", json={"email": email6, "code": code6b})
forgot_b = r.get_json().get("token") if r.status_code == 200 else None
check("forgot flow: second session exists before the reset", bool(forgot_b))
# The owner "forgets" the password: log out device A, sign in by code.
client.post("/api/auth/logout", headers=H(forgot_a))
_, code6c = request_and_get_code(email6)
r = client.post("/api/auth/verify-otp", json={"email": email6, "code": code6c})
reset_body = r.get_json() if r.status_code == 200 else {}
forgot_c = reset_body.get("token")
me6 = client.get("/api/auth/me", headers=H(forgot_c)).get_json() if forgot_c else {}
check(
    "forgot flow: fresh code session still has the (forgotten) password, locked",
    bool(forgot_c) and reset_body.get("hasAppPassword") is True
    and me6.get("otpFresh") is True and me6.get("unlocked") is False,
    json.dumps(me6),
)
r = client.post("/api/auth/reset-password", headers=H(forgot_c),
                json={"password": "ab1"})
check(
    "forgot flow: reset enforces the 4-char minimum with the clear error",
    r.status_code == 400
    and r.get_json().get("error") == "Password must be at least 4 characters.",
    r.get_data(as_text=True),
)
r = client.post("/api/auth/reset-password", headers=H(forgot_c),
                json={"password": "newpass2"})
me6 = client.get("/api/auth/me", headers=H(forgot_c)).get_json() if forgot_c else {}
check(
    "forgot flow FIXED: reset with a new password succeeds and opens the app",
    r.status_code == 200 and me6.get("unlocked") is True
    and me6.get("otpFresh") is False,
    r.get_data(as_text=True),
)
r = client.get("/api/parties", headers=H(forgot_c))
party_ids = ([p["id"] for p in r.get_json().get("parties", [])]
             if r.status_code == 200 else [])
r2 = client.get("/api/invoices/{}".format(forgot_inv.get("id")),
                headers=H(forgot_c))
check(
    "forgot flow: SAME account — party and invoice survived the reset",
    forgot_party_id in party_ids
    and r2.status_code == 200
    and r2.get_json()["invoice"]["totalPaisa"] == 12345,
)
r = client.get("/api/auth/me", headers=H(forgot_b))
check(
    "forgot flow: the reset signed out the account's OTHER session",
    r.status_code == 401,
)
r = client.post("/api/auth/reset-password", headers=H(forgot_c),
                json={"password": "third33"})
check(
    "forgot flow: a second reset on the same session is refused (otp_fresh spent)",
    r.status_code == 400
    and "right after signing in with an email code"
        in (r.get_json().get("error") or ""),
    r.get_data(as_text=True),
)
client.post("/api/auth/lock", headers=H(forgot_c))
r = client.post("/api/auth/verify-password", headers=H(forgot_c),
                json={"password": "oldpass1"})
check("forgot flow: the OLD password no longer opens the account",
      r.status_code == 401)
r = client.post("/api/auth/verify-password", headers=H(forgot_c),
                json={"password": "newpass2"})
check("forgot flow: the NEW password opens the account",
      r.status_code == 200)
# (a) a merely-locked session (NOT otp-fresh) may not reset:
client.post("/api/auth/lock", headers=H(forgot_c))
r = client.post("/api/auth/reset-password", headers=H(forgot_c),
                json={"password": "sneaky1"})
check(
    "security: reset refused on a locked (not OTP-fresh) session",
    r.status_code == 400
    and "right after signing in with an email code"
        in (r.get_json().get("error") or ""),
    r.get_data(as_text=True),
)
# (b) an account with NO password uses set-password, never reset:
email7 = "nopwshop@example.com"
_, code7 = request_and_get_code(email7)
r = client.post("/api/auth/verify-otp", json={"email": email7, "code": code7})
nopw_token = r.get_json().get("token") if r.status_code == 200 else None
r = client.post("/api/auth/reset-password", headers=H(nopw_token),
                json={"password": "abcd"})
check(
    "security: reset refused when the account has no password (set-password is the path)",
    r.status_code == 400
    and "no password yet" in (r.get_json().get("error") or ""),
    r.get_data(as_text=True),
)

# ================= 12d. classic login (email + password) ===============
# The everyday route from the owner's reference picture: email +
# password + "Remember me", no email code needed. A wrong email and a
# wrong password must get the IDENTICAL generic 401 (no enumeration);
# an account with no password yet gets guidance, not the 401.
from datetime import datetime as _dt2, timezone as _tz2  # noqa: E402

from qbill_app.security import token_hash as _token_hash  # noqa: E402


def _session_days(tok):
    row = db.query_one(
        "SELECT expires_at FROM sessions WHERE token_hash = ?",
        (_token_hash(tok),),
    )
    exp = _dt2.fromisoformat(row["expires_at"])
    return (exp - _dt2.now(_tz2.utc)).total_seconds() / 86400.0


login_email = "classic@example.com"
_, lcode = request_and_get_code(login_email)
r = client.post("/api/auth/verify-otp", json={"email": login_email, "code": lcode})
login_otp_token = (r.get_json() or {}).get("token")
r = client.post("/api/auth/set-password", headers=H(login_otp_token),
                json={"password": "mypw42"})
check("classic login setup: password set via the code route",
      r.status_code == 200, r.get_data(as_text=True))
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": login_email})  # owner approval opens the data gate
client.post("/api/auth/logout", headers=H(login_otp_token))

r = client.post("/api/auth/login", json={"email": "ghost@example.com",
                                         "password": "mypw42", "remember": True})
unknown_body = r.get_json()
check("login: unknown email gets the generic 401",
      r.status_code == 401
      and unknown_body.get("error") == "Email or password is wrong.",
      r.get_data(as_text=True))
r = client.post("/api/auth/login", json={"email": login_email,
                                         "password": "wrongpw", "remember": True})
check("login: wrong password gets the IDENTICAL generic 401 (no enumeration)",
      r.status_code == 401 and r.get_json() == unknown_body,
      r.get_data(as_text=True))

client.post("/api/auth/request-otp", json={"email": "nopw@example.com"})
r = client.post("/api/auth/login", json={"email": "nopw@example.com",
                                         "password": "mypw42", "remember": True})
check("login: account without a password gets the create-account guidance",
      r.status_code == 400
      and r.get_json().get("error") == "This account does not have a password yet. "
      "Tap “Create an account” and verify your email to set one.",
      r.get_data(as_text=True))

r = client.post("/api/auth/login", json={"email": login_email,
                                         "password": "mypw42", "remember": True})
login_token = (r.get_json() or {}).get("token")
check("login: correct email + password signs in (remember)",
      r.status_code == 200 and bool(login_token)
      and r.get_json().get("hasAppPassword") is True,
      r.get_data(as_text=True))
r = client.get("/api/dashboard", headers=H(login_token))
check("login: remembered session is unlocked immediately (dashboard opens)",
      r.status_code == 200, r.get_data(as_text=True)[:200])
check("login: remembered session lasts ~30 days",
      bool(login_token) and _session_days(login_token) > 20,
      str(_session_days(login_token)) if login_token else "no token")

r = client.post("/api/auth/login", json={"email": login_email,
                                         "password": "mypw42", "remember": False})
short_token = (r.get_json() or {}).get("token")
r = client.get("/api/dashboard", headers=H(short_token))
check("login: correct login without remember also works at once",
      bool(short_token) and r.status_code == 200,
      r.get_data(as_text=True)[:200])
check("login: non-remembered session lasts ~1 day",
      bool(short_token) and 0 < _session_days(short_token) < 2,
      str(_session_days(short_token)) if short_token else "no token")

client.post("/api/auth/logout", headers=H(login_token))
r = client.get("/api/auth/me", headers=H(login_token))
check("login: after logout the login token is dead", r.status_code == 401)

r = client.post("/api/auth/change-password", headers=H(short_token),
                json={"oldPassword": "mypw42", "newPassword": "newpw99"})
check("login setup: password changed on the no-remember session",
      r.status_code == 200, r.get_data(as_text=True))
r = client.post("/api/auth/login", json={"email": login_email,
                                         "password": "mypw42", "remember": True})
check("login: after a password change the old password is refused",
      r.status_code == 401
      and r.get_json().get("error") == "Email or password is wrong.",
      r.get_data(as_text=True))
r = client.post("/api/auth/login", json={"email": login_email,
                                         "password": "newpw99", "remember": True})
check("login: after a password change only the new password works",
      r.status_code == 200 and bool((r.get_json() or {}).get("token")),
      r.get_data(as_text=True))

html = client.get("/").get_data(as_text=True)
js = client.get("/static/app.js").get_data(as_text=True)
check("frontend: classic login panels served (stepLogin/stepSignup/stepForgot + Remember me)",
      'id="stepLogin"' in html and 'id="stepSignup"' in html
      and 'id="stepForgot"' in html and 'id="loginForm"' in html
      and "Remember me" in html)
check("frontend: app.js posts to /api/auth/login", "/api/auth/login" in js)

# "Create an account" is ONE single form (owner's design): email,
# the emailed code, a created password, then "Log in" — stacked in
# that exact order inside the stepOne panel. The signed-out default
# stays the classic login, and an emailed code is never offered as
# a third login route (owner's rule).
one_at = html.find('id="stepOne"')
login_at = html.find('id="stepLogin"')
one_block = html[one_at:login_at] if 0 <= one_at < login_at else ""
check("frontend: ONE-form panel served (stepOne with a Send code button)",
      bool(one_block) and 'id="btnOneSend"' in one_block and "Send code" in one_block)
_i_email = one_block.find('id="oneEmail"')
_i_code = one_block.find('id="oneCode"')
_i_pw = one_block.find('id="onePassword"')
_i_login = one_block.find('id="btnOneLogin"')
check("frontend: stepOne stacks email -> code -> create password -> Log in, in that order",
      0 <= _i_email < _i_code < _i_pw < _i_login)
_sa = js.find("function showAuth()")
check("frontend: app.js makes stepLogin the signed-out default",
      _sa >= 0 and 'showAuthStep("stepLogin")' in js[_sa:_sa + 200])
check("frontend: no 'login code instead' third route on the login panel",
      'id="btnCodeInstead"' not in html and "Email me a login code instead" not in html)

# ============ 12e. owner approval gate (anti-sharing access control) ====
# Owner's order: QBill is SOLD by him, so a stranger must not be able to
# self-register and start billing. A new account is born UNAPPROVED: it
# can sign in and set a password, but every data route answers 403 with
# code "pending" until the owner approves it from his customer list —
# which also shows the signup/last IPs. Blocking un-approves AND kills
# the account's live sessions; the owner's own account can never be
# blocked and is approved from birth.
email8 = "customer@example.com"
r = client.post("/api/auth/request-otp", json={"email": email8},
                headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
code8 = latest_code(email8)
r = client.post("/api/auth/verify-otp", json={"email": email8, "code": code8})
cust_token = (r.get_json() or {}).get("token")
r = client.post("/api/auth/set-password", headers=H(cust_token),
                json={"password": "cust42"})
check("access: new customer signs in and sets a password",
      bool(cust_token) and r.status_code == 200, r.get_data(as_text=True))
me8 = client.get("/api/auth/me", headers=H(cust_token)).get_json()
r = client.get("/api/dashboard", headers=H(cust_token))
check(
    "access: unapproved account gets 403 code 'pending' on data routes",
    me8.get("approved") is False and r.status_code == 403
    and r.get_json().get("code") == "pending"
    and r.get_json().get("error")
    == "Your account is waiting for the owner's approval.",
    r.get_data(as_text=True),
)
r = client.get("/api/admin/accounts", headers=H(cust_token))
check("access: a non-owner cannot read the customer list (403)",
      r.status_code == 403)
r = client.get("/api/admin/accounts", headers=H(owner_token))
accts = {a["email"]: a for a in (r.get_json() or {}).get("accounts", [])}
cust_row = accts.get(email8, {})
owner_row = accts.get(OWNER, {})
check(
    "access: owner list shows the customer, unapproved, signup IP = first XFF entry",
    r.status_code == 200 and cust_row.get("approved") is False
    and cust_row.get("isOwner") is False
    and cust_row.get("signupIp") == "203.0.113.7"
    and cust_row.get("lastIp") == "127.0.0.1"  # verify-otp call had no XFF: last IP tracks the latest login
    and cust_row.get("lastLoginAt") is not None,
    json.dumps(cust_row),
)
me_owner = client.get("/api/auth/me", headers=H(owner_token)).get_json()
check(
    "access: the owner's own account is approved (list + /me agree)",
    owner_row.get("approved") is True and owner_row.get("isOwner") is True
    and me_owner.get("approved") is True,
    json.dumps(owner_row),
)
r = client.post("/api/admin/accounts/approve", headers=H(owner_token),
                json={"email": "nobody@example.com"})
check("access: approving an unknown email is a 404", r.status_code == 404)
r = client.post("/api/admin/accounts/approve", headers=H(owner_token),
                json={"email": email8})
r2 = client.get("/api/dashboard", headers=H(cust_token))
check(
    "access: after the owner approves, the customer's data opens",
    r.status_code == 200 and r2.status_code == 200,
    r2.get_data(as_text=True)[:200],
)
r = client.post("/api/admin/accounts/block", headers=H(owner_token),
                json={"email": OWNER})
check(
    "access: the owner's own account can never be blocked (400)",
    r.status_code == 400
    and r.get_json().get("error") == "The owner's account cannot be blocked.",
    r.get_data(as_text=True),
)
r = client.post("/api/admin/accounts/block", headers=H(owner_token),
                json={"email": "nobody@example.com"})
check("access: blocking an unknown email is a 404", r.status_code == 404)
r = client.post("/api/admin/accounts/block", headers=H(owner_token),
                json={"email": email8})
block_ok = r.status_code == 200
r = client.get("/api/auth/me", headers=H(cust_token))
check(
    "access: blocking kills the customer's live session (me = 401)",
    block_ok and r.status_code == 401, r.get_data(as_text=True)[:120],
)
r = client.post("/api/auth/login", json={"email": email8,
                                         "password": "cust42", "remember": True})
cust_token2 = (r.get_json() or {}).get("token")
r2 = client.get("/api/dashboard", headers=H(cust_token2))
check(
    "access: a blocked customer who logs in again is pending again (403)",
    r.status_code == 200 and bool(cust_token2) and r2.status_code == 403
    and r2.get_json().get("code") == "pending",
    r2.get_data(as_text=True)[:200],
)
html = client.get("/").get_data(as_text=True)
check(
    "frontend: activation (pending) view + owner customer-accounts card served",
    'id="pendingView"' in html and 'id="ownerAccountsCard"' in html
    and 'id="accountList"' in html,
)

# ============ 12f. duplicate-IP auto-reject + activation plans screen ====
# Owner's anti-sharing rule: a second signup from an IP that already
# has an account is auto-rejected (reject_reason 'ip_match') — the
# account is still created so the person stays identifiable, and the
# owner can override by approving (which clears the mark). Anyone not
# activated sees the plans screen with the owner's prices.
def _signup_from_ip(email, ip, password):
    r = client.post("/api/auth/request-otp", json={"email": email},
                    headers={"X-Forwarded-For": ip})
    code = latest_code(email)
    r = client.post("/api/auth/verify-otp", json={"email": email, "code": code})
    tok = (r.get_json() or {}).get("token")
    client.post("/api/auth/set-password", headers=H(tok),
                json={"password": password})
    return tok


ip_a, ip_b, ip_c = ("ipa@example.com", "ipb@example.com", "ipc@example.com")
tok_ip_a = _signup_from_ip(ip_a, "198.51.100.9", "shop-a1")
me_a = client.get("/api/auth/me", headers=H(tok_ip_a)).get_json()
r = client.get("/api/dashboard", headers=H(tok_ip_a))
check(
    "ip: first signup from an IP is a NORMAL pending account (no reject mark)",
    me_a.get("approved") is False and me_a.get("rejectReason") is None
    and r.status_code == 403 and r.get_json().get("code") == "pending",
    json.dumps(me_a),
)
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": ip_a})
r = client.get("/api/dashboard", headers=H(tok_ip_a))
check("ip: the first signup is approvable (dashboard opens)",
      r.status_code == 200)

tok_ip_b = _signup_from_ip(ip_b, "198.51.100.9", "shop-b1")
me_b = client.get("/api/auth/me", headers=H(tok_ip_b)).get_json()
r = client.get("/api/dashboard", headers=H(tok_ip_b))
rej_event = db.query_one(
    "SELECT id FROM access_log WHERE email = ? AND event = 'signup_rejected_ip'",
    (ip_b,),
)
ok_event = db.query_one(
    "SELECT id FROM access_log WHERE email = ? AND event = 'signup'", (ip_a,)
)
check(
    "ip: second signup, different email, SAME IP is auto-rejected (ip_match)",
    me_b.get("approved") is False and me_b.get("rejectReason") == "ip_match"
    and r.status_code == 403 and r.get_json().get("code") == "pending"
    and rej_event is not None and ok_event is not None,
    json.dumps(me_b),
)
r = client.get("/api/admin/accounts", headers=H(owner_token))
ip_rows = {a["email"]: a for a in (r.get_json() or {}).get("accounts", [])}
check(
    "ip: owner list carries the rejectReason for the rejected account",
    ip_rows.get(ip_b, {}).get("rejectReason") == "ip_match"
    and ip_rows.get(ip_a, {}).get("rejectReason") is None,
    json.dumps(ip_rows.get(ip_b)),
)
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": ip_b})
me_b = client.get("/api/auth/me", headers=H(tok_ip_b)).get_json()
r = client.get("/api/dashboard", headers=H(tok_ip_b))
check(
    "ip: owner approval OVERRIDES the auto-reject (mark cleared, data opens)",
    me_b.get("approved") is True and me_b.get("rejectReason") is None
    and r.status_code == 200,
    json.dumps(me_b),
)
tok_ip_c = _signup_from_ip(ip_c, "203.0.113.99", "shop-c1")
me_c = client.get("/api/auth/me", headers=H(tok_ip_c)).get_json()
r = client.get("/api/dashboard", headers=H(tok_ip_c))
check(
    "ip: a signup from a DIFFERENT IP is a normal pending account",
    me_c.get("approved") is False and me_c.get("rejectReason") is None
    and r.status_code == 403,
    json.dumps(me_c),
)
html = client.get("/").get_data(as_text=True)
check(
    "frontend: activation screen shows the owner's plans and heading",
    "Activate your QBill account" in html
    and "Rs 30,000" in html and "Rs 60,000" in html and "Rs 70,000" in html
    and "03273799008" in html,
)

# ============ 12g. Vyapar document engine (types, stock, convert) ======
# Every document type rides the one invoices table. Stock moves ONLY
# for sale / purchase / sale_return / purchase_return; estimates,
# orders and challans are paper only. Conversions copy lines + totals
# into a real sale invoice exactly once.
r = client.post("/api/parties", headers=H(user_token), json={"name": "Doc Customer"})
doc_party = r.get_json()["party"]["id"]
r = client.post("/api/parties", headers=H(user_token), json={"name": "Doc Supplier"})
doc_supplier = r.get_json()["party"]["id"]
r = client.post("/api/items", headers=H(user_token), json={
    "name": "Doc Test Widget", "sale_price_paisa": 60000,
    "purchase_price_paisa": 50000, "stock_qty": 10})
doc_item = r.get_json()["item"]["id"]

def _doc_stock():
    rr = client.get("/api/items", headers=H(user_token))
    return {i["id"]: i["stockQty"] for i in rr.get_json()["items"]}[doc_item]

def _doc_balance(pid):
    rr = client.get("/api/parties", headers=H(user_token))
    return {p["id"]: p["balancePaisa"] for p in rr.get_json()["parties"]}[pid]

def _mkdoc(payload):
    rr = client.post("/api/invoices", headers=H(user_token), json=payload)
    return rr, (rr.get_json() or {}).get("invoice", {})

r, est = _mkdoc({"type": "estimate", "party_id": doc_party,
                 "lines": [{"item_id": doc_item, "qty": 2, "rate_paisa": 60000}]})
check(
    "docs: estimate created with EST- prefix, no due balance",
    r.status_code == 201 and est.get("invoiceNo", "").startswith("EST-")
    and est.get("totalPaisa") == 120000 and est.get("balancePaisa") == 0,
    json.dumps(est),
)
check("docs: estimate does NOT move stock or party balance",
      _doc_stock() == 10 and _doc_balance(doc_party) == 0)
r, _bad = _mkdoc({"type": "estimate", "party_id": doc_party, "paid_paisa": 100,
                  "lines": [{"item_id": doc_item, "qty": 1, "rate_paisa": 60000}]})
check("docs: an estimate cannot take a payment (400)", r.status_code == 400)
r, so = _mkdoc({"type": "sale_order", "party_id": doc_party,
                "lines": [{"item_id": doc_item, "qty": 1, "rate_paisa": 60000}]})
check("docs: sale order SO- prefix, no stock change",
      r.status_code == 201 and so.get("invoiceNo", "").startswith("SO-")
      and _doc_stock() == 10, json.dumps(so))
r, dc = _mkdoc({"type": "challan", "party_id": doc_party,
                "lines": [{"item_id": doc_item, "qty": 1, "rate_paisa": 60000}]})
check("docs: delivery challan DC- prefix, no stock change",
      r.status_code == 201 and dc.get("invoiceNo", "").startswith("DC-")
      and _doc_stock() == 10, json.dumps(dc))
r, po = _mkdoc({"type": "purchase_order", "party_id": doc_supplier,
                "lines": [{"item_id": doc_item, "qty": 3, "rate_paisa": 50000}]})
check("docs: purchase order PO- prefix, no stock change",
      r.status_code == 201 and po.get("invoiceNo", "").startswith("PO-")
      and _doc_stock() == 10, json.dumps(po))

r = client.post("/api/invoices/{}/convert".format(est["id"]),
                headers=H(user_token), json={})
conv = (r.get_json() or {}).get("invoice", {})
check(
    "docs: estimate converts to a sale invoice copying lines + totals",
    r.status_code == 201 and conv.get("type") == "sale"
    and conv.get("invoiceNo", "").startswith("S-")
    and conv.get("totalPaisa") == 120000 and conv.get("balancePaisa") == 120000
    and len(conv.get("lines", [])) == 1 and conv["lines"][0]["qty"] == 2,
    json.dumps(conv),
)
check("docs: the converted sale moves stock (10->8) and the party balance",
      _doc_stock() == 8 and _doc_balance(doc_party) == 120000)
r = client.get("/api/invoices/{}".format(est["id"]), headers=H(user_token))
check("docs: the source estimate is marked converted",
      (r.get_json() or {})["invoice"].get("convertedToId") == conv["id"])
r = client.post("/api/invoices/{}/convert".format(est["id"]),
                headers=H(user_token), json={})
check("docs: converting twice is refused (400)", r.status_code == 400)
r = client.post("/api/invoices/{}/convert".format(conv["id"]),
                headers=H(user_token), json={})
check("docs: a plain sale invoice cannot be converted (400)",
      r.status_code == 400)

r, sret = _mkdoc({"type": "sale_return", "party_id": doc_party,
                  "lines": [{"item_id": doc_item, "qty": 1, "rate_paisa": 60000}]})
check(
    "docs: sale return SR- prefix restores stock (8->9), balance drops",
    r.status_code == 201 and sret.get("invoiceNo", "").startswith("SR-")
    and _doc_stock() == 9 and _doc_balance(doc_party) == 60000,
    json.dumps(sret),
)
r, pur = _mkdoc({"type": "purchase", "party_id": doc_supplier,
                 "lines": [{"item_id": doc_item, "qty": 4, "rate_paisa": 50000}]})
check("docs: purchase adds stock (9->13)",
      r.status_code == 201 and _doc_stock() == 13, json.dumps(pur))
r, pret = _mkdoc({"type": "purchase_return", "party_id": doc_supplier,
                  "lines": [{"item_id": doc_item, "qty": 1, "rate_paisa": 50000}]})
check(
    "docs: purchase return PR- prefix reduces stock (13->12)",
    r.status_code == 201 and pret.get("invoiceNo", "").startswith("PR-")
    and _doc_stock() == 12, json.dumps(pret),
)

# Day Book: today's documents + a payment + an expense all appear.
client.post("/api/payments", headers=H(user_token),
            json={"party_id": doc_party, "direction": "in", "amount_paisa": 1000})
client.post("/api/expenses", headers=H(user_token),
            json={"title": "Doc day expense", "amount_paisa": 500})
today = _dt.now(_tz.utc).date().isoformat()
r = client.get("/api/reports/day-book?date=" + today, headers=H(user_token))
dbk = r.get_json() or {}
kinds = {e["kind"] for e in dbk.get("entries", [])}
check(
    "reports: day book holds documents + a payment + an expense for the date",
    r.status_code == 200 and "sale" in kinds and "payment" in kinds
    and "expense" in kinds and dbk.get("totals", {}).get("expensesPaisa", 0) >= 500,
    json.dumps(dbk.get("totals", {})),
)
r = client.get("/api/reports/stock-summary", headers=H(user_token))
srow = {i["id"]: i for i in (r.get_json() or {}).get("items", [])}.get(doc_item, {})
check(
    "reports: stock summary math (opening 10, in 5, out 3, closing 12)",
    srow.get("openingQty") == 10 and srow.get("inQty") == 5
    and srow.get("outQty") == 3 and srow.get("closingQty") == 12
    and srow.get("valuePaisa") == 12 * 50000,
    json.dumps(srow),
)

# Guards on the new routes: pending account + locked session refused.
r = client.post("/api/invoices", headers=H(tok_ip_c), json={
    "type": "estimate", "lines": [{"qty": 1, "rate_paisa": 100}]})
r2 = client.get("/api/reports/day-book", headers=H(tok_ip_c))
r3 = client.post("/api/invoices/{}/convert".format(dc["id"]),
                 headers=H(tok_ip_c), json={})
check(
    "docs: unapproved account refused on create, day-book and convert (403 pending)",
    r.status_code == 403 and r.get_json().get("code") == "pending"
    and r2.status_code == 403 and r3.status_code == 403,
)
r = client.get("/api/invoices/{}/pdf".format(est["id"]), headers=H(user_token))
check("docs: estimate PDF renders (Estimate title route works)",
      r.status_code == 200 and r.data[:4] == b"%PDF")
client.post("/api/auth/lock", headers=H(user_token), json={})
r = client.get("/api/reports/stock-summary", headers=H(user_token))
r2 = client.post("/api/invoices", headers=H(user_token), json={
    "type": "sale", "lines": [{"qty": 1, "rate_paisa": 100}]})
check("docs: a locked session is refused on the new routes (403)",
      r.status_code == 403 and r2.status_code == 403)
r = client.post("/api/auth/login",
                json={"email": USER_EMAIL, "password": "shop-pass-2", "remember": True})
user_token = (r.get_json() or {}).get("token") or user_token

html = client.get("/").get_data(as_text=True)
check(
    "frontend: Vyapar shell served (Add More menu, shortcuts, search, owner contact)",
    'id="addMoreMenu"' in html and "Shortcut to open this menu" in html
    and "Search Transactions" in html and "abdulqudoos5581@gmail.com" in html
    and "03273799008" in html and 'data-nav="salebook"' in html
    and "Alt + S" in html and 'id="btnAddPurchase"' in html
    and 'id="searchTx"' in html and 'data-setcard="transaction"' in html,
)

# ---------------------------------------------------------- 13. logout --
r = client.post("/api/auth/logout", headers=H(owner_token))
r = client.get("/api/auth/me", headers=H(owner_token))
check("logout destroys the session (OTP needed again)", r.status_code == 401)

# ------------------------------------------- 14. secrets never in logs --
logs = log_stream.getvalue()
leaked = [c for c in ALL_CODES if c and c in logs]
check(
    "no OTP code and no API key in any log output",
    not leaked and DUMMY_KEY not in logs and "re_should_not_stick" not in logs,
    "leaked codes: {}".format(leaked),
)

# -------------------------------------------------------------- summary --
passed = sum(1 for _, ok in RESULTS if ok)
print("SUMMARY: {}/{} passed".format(passed, len(RESULTS)))
sys.exit(0 if passed == len(RESULTS) else 1)
