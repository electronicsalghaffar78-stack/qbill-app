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
