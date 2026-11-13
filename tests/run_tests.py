"""QRLedger Web — Phase 1 foundation test suite.

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
    and dash.get("stockValuePaisa") == 13980000
    and dash.get("monthProfitPaisa") == 180000
    and dash.get("monthExpensesPaisa") == 50000,
    json.dumps(dash),
)
r = client.get("/api/pnl", headers=H(user_token))
pnl = r.get_json().get("pnl", {})
check(
    "P&L exact (margin: sales 650000 - COGS 420000 - expenses 50000 = 180000; "
    "purchases 400000 reported, not subtracted)",
    pnl.get("salesPaisa") == 650000 and pnl.get("purchasesPaisa") == 400000
    and pnl.get("cogsPaisa") == 420000
    and pnl.get("expensesPaisa") == 50000 and pnl.get("profitPaisa") == 180000,
    json.dumps(pnl),
)

# ------------------------------------------- S2-3. party ledger entries --
r = client.get("/api/parties/{}/ledger".format(party_id), headers=H(user_token))
led = r.get_json()
check(
    "ledger: sale + its payment, running balance ends at -450000 "
    "(build .20 sign convention: negative = the party owes us)",
    r.status_code == 200 and led.get("balancePaisa") == -450000
    and len(led.get("entries", [])) == 2
    and led["entries"][-1]["balancePaisa"] == -450000,
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
    [m for m in mailer.DEV_OUTBOX if m["subject"].startswith("QRLedger daily backup")]
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
               if m["subject"].startswith("QRLedger daily backup")]
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
    "Activate your QRLedger account" in html
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
      _doc_stock() == 8 and _doc_balance(doc_party) == -120000)
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
    and _doc_stock() == 9 and _doc_balance(doc_party) == -60000,
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

# ============ 12h. vyapar2 build (settings, stock, money, store…) =====
# The consolidated Vyapar apply-build: settings document + terms
# library, per-line discount/tax, proforma, invoice edit/delete +
# recycle bin, godowns/batches, bank accounts, allocate, expenses
# rework, other income, reports, loyalty, online store, team roles,
# accounting. All against the shopkeeper session (user_token).
UH = H(user_token)

def _v2_item(name, **kw):
    payload = {"name": name, "sale_price_paisa": kw.get("sale", 10000),
               "purchase_price_paisa": kw.get("purchase", 8000),
               "stock_qty": kw.get("stock", 10)}
    for key in ("min_stock", "batches", "item_code", "category"):
        if key in kw:
            payload[key] = kw[key]
    rr = client.post("/api/items", headers=UH, json=payload)
    return rr, (rr.get_json() or {}).get("item", {})

def _v2_sale(party_id, lines, **kw):
    payload = {"type": "sale", "party_id": party_id, "lines": lines}
    payload.update(kw)
    rr = client.post("/api/invoices", headers=UH, json=payload)
    return rr, (rr.get_json() or {}).get("invoice", {})

# -- settings document round-trip + terms library ----------------------
r = client.put("/api/settings", headers=UH, json={"settings": {"decimal_places": 0}})
r2 = client.get("/api/settings", headers=UH)
check("v2 settings: PUT then GET round-trips a value",
      r.status_code == 200
      and (r2.get_json() or {}).get("settings", {}).get("decimal_places") == 0)
client.put("/api/settings", headers=UH, json={"settings": {"decimal_places": 2}})
r = client.get("/api/terms", headers=UH)
terms = (r.get_json() or {}).get("terms", [])
check("v2 terms: library is seeded with named terms that carry 'applies'",
      len(terms) >= 6 and all(t.get("applies") for t in terms),
      "count={}".format(len(terms)))
r = client.post("/api/terms", headers=UH, json={
    "header": "V2 Test Terms", "description": "Test only.", "applies": ["sale"]})
new_term = (r.get_json() or {}).get("term", {})
r = client.delete("/api/terms/{}".format(new_term.get("id")), headers=UH)
check("v2 terms: add then delete a term",
      bool(new_term.get("id")) and r.status_code == 200)

# -- proforma + prefix override ----------------------------------------
r = client.post("/api/parties", headers=UH, json={"name": "V2 Customer"})
v2_party = r.get_json()["party"]["id"]
_, v2_item = _v2_item("V2 Widget", stock=10)
r, prof = _mkdoc({"type": "proforma", "party_id": v2_party,
                  "lines": [{"item_id": v2_item["id"], "qty": 2, "rate_paisa": 10000}]})
stock_now = {i["id"]: i["stockQty"] for i in
             client.get("/api/items", headers=UH).get_json()["items"]}[v2_item["id"]]
check("v2 proforma: PI- prefix, no balance, no stock move",
      r.status_code == 201 and prof.get("invoiceNo", "").startswith("PI-")
      and prof.get("balancePaisa") == 0 and stock_now == 10,
      json.dumps(prof))
client.put("/api/settings", headers=UH, json={"settings": {"prefixes": {"sale": "INV-"}}})
r, inv_pref = _v2_sale(v2_party, [{"item_id": v2_item["id"], "qty": 1, "rate_paisa": 10000}])
client.put("/api/settings", headers=UH, json={"settings": {"prefixes": {"sale": "S-"}}})
check("v2 prefixes: a settings prefix override drives the invoice number",
      inv_pref.get("invoiceNo", "").startswith("INV-"), inv_pref.get("invoiceNo", ""))

# -- per-line discount / tax --------------------------------------------
client.put("/api/settings", headers=UH, json={"settings": {
    "tax_enabled": True, "item_wise_tax": True, "item_wise_discount": True}})
r, inv_lt = _v2_sale(v2_party, [{
    "item_id": v2_item["id"], "qty": 2, "rate_paisa": 10000,
    "discount_paisa": 1000, "tax_paisa": 500}])
client.put("/api/settings", headers=UH, json={"settings": {"tax_enabled": False}})
check("v2 lines: per-line discount and tax land in the total",
      inv_lt.get("totalPaisa") == 19500 and inv_lt.get("discountPaisa") == 1000
      and inv_lt.get("taxPaisa") == 500, json.dumps(inv_lt))

# -- stop sale on negative stock ----------------------------------------
_, small_item = _v2_item("V2 Scarce", stock=2)
client.put("/api/settings", headers=UH, json={"settings": {"stop_sale_negative_stock": True}})
r, _ = _v2_sale(v2_party, [{"item_id": small_item["id"], "qty": 5, "rate_paisa": 10000}])
client.put("/api/settings", headers=UH, json={"settings": {"stop_sale_negative_stock": False}})
check("v2 stock: Stop Sale on Negative Stock refuses an oversell (400)",
      r.status_code == 400)

# -- invoice edit + history ----------------------------------------------
r, inv_e = _v2_sale(v2_party, [{"item_id": v2_item["id"], "qty": 1, "rate_paisa": 10000}])
stock_mid = {i["id"]: i["stockQty"] for i in
             client.get("/api/items", headers=UH).get_json()["items"]}[v2_item["id"]]
r = client.put("/api/invoices/{}".format(inv_e["id"]), headers=UH, json={
    "type": "sale", "party_id": v2_party, "paid_paisa": 0,
    "lines": [{"item_id": v2_item["id"], "qty": 3, "rate_paisa": 10000}]})
inv_e2 = (r.get_json() or {}).get("invoice", {})
stock_after = {i["id"]: i["stockQty"] for i in
               client.get("/api/items", headers=UH).get_json()["items"]}[v2_item["id"]]
hist = client.get("/api/invoices/{}/history".format(inv_e["id"]),
                  headers=UH).get_json().get("history", [])
check("v2 edit: PUT re-prices the bill, moves the extra stock, writes history",
      inv_e2.get("totalPaisa") == 30000 and stock_after == stock_mid - 2
      and any(h.get("action") == "edited" for h in hist),
      json.dumps({"total": inv_e2.get("totalPaisa"), "stock": stock_after}))

# -- delete -> recycle bin -> restore -------------------------------------
stock_before_del = stock_after
r = client.delete("/api/invoices/{}".format(inv_e["id"]), headers=UH)
bin_rows = client.get("/api/recycle-bin", headers=UH).get_json().get("invoices", [])
stock_deleted = {i["id"]: i["stockQty"] for i in
                 client.get("/api/items", headers=UH).get_json()["items"]}[v2_item["id"]]
r2 = client.post("/api/recycle-bin/{}/restore".format(inv_e["id"]),
                 headers=UH, json={})
stock_restored = {i["id"]: i["stockQty"] for i in
                  client.get("/api/items", headers=UH).get_json()["items"]}[v2_item["id"]]
check("v2 recycle: delete reverses stock, bin lists it, restore brings both back",
      r.status_code == 200
      and any(b["id"] == inv_e["id"] for b in bin_rows)
      and stock_deleted == stock_before_del + 3
      and r2.status_code == 200 and stock_restored == stock_before_del)

# -- godowns + stock transfer + batches ------------------------------------
godowns = client.get("/api/godowns", headers=UH).get_json().get("godowns", [])
main_g = [g for g in godowns if g.get("isMain")]
r = client.post("/api/godowns", headers=UH, json={"name": "V2 Branch", "type": "Retail Store"})
branch_g = (r.get_json() or {}).get("godown", {})
_, batch_item = _v2_item("V2 Batched", stock=0, batches=[
    {"batch_no": "B1", "qty": 6, "exp_date": "03/27", "mfg_date": "01/26"}])
r = client.post("/api/stock-transfers", headers=UH, json={
    "item_id": batch_item["id"], "qty": 2,
    "from_godown_id": main_g[0]["id"], "to_godown_id": branch_g.get("id")})
branch_stock = client.get("/api/godowns/{}/stock".format(branch_g.get("id")),
                          headers=UH).get_json().get("items", [])
batch_rows = client.get("/api/reports/item-batch", headers=UH).get_json().get("rows", [])
low_rows = client.get("/api/reports/low-stock", headers=UH).get_json().get("rows", [])
check("v2 stock: main godown exists; transfer lands; batch + low-stock reports read",
      bool(main_g) and r.status_code in (200, 201)
      and any(s["id"] == batch_item["id"] and s["qty"] == 2 for s in branch_stock)
      and any(b["batchNo"] == "B1" for b in batch_rows)
      and isinstance(low_rows, list))
_, low_item = _v2_item("V2 Low", stock=3, min_stock=5)
low_rows = client.get("/api/reports/low-stock", headers=UH).get_json().get("rows", [])
dash = client.get("/api/dashboard", headers=UH).get_json().get("dashboard", {})
check("v2 stock: low stock means below Min Stock To Maintain (not zero)",
      any(x["item"] == "V2 Low" for x in low_rows)
      and (dash.get("lowStockCount") or 0) >= 1
      and isinstance(dash.get("salesSeries"), list)
      and "salesVsLastMonthPct" in dash)

# -- bank accounts, payment via bank, Pay To on the PDF ---------------------
r = client.post("/api/bank-accounts", headers=UH, json={
    "display_name": "V2 Bank", "opening_balance_paisa": 100000,
    "account_number": "PK00V2", "iban": "PK36V2BANK", "qr_handle": "03270000000",
    "bank_name": "Test Bank", "holder_name": "V2 Holder",
    "print_details": True, "print_qr": True})
bank = (r.get_json() or {}).get("bankAccount", {})
r = client.post("/api/bank-accounts/{}/transact".format(bank.get("id")),
                headers=UH, json={"kind": "deposit", "amount_paisa": 50000})
bal = (r.get_json() or {}).get("balancePaisa")
r, inv_b = _v2_sale(v2_party, [{"item_id": v2_item["id"], "qty": 1, "rate_paisa": 10000}])
r = client.post("/api/payments", headers=UH, json={
    "party_id": v2_party, "direction": "in", "amount_paisa": 10000,
    "invoice_id": inv_b["id"], "mode": "bank", "bank_account_id": bank.get("id")})
txs = client.get("/api/bank-accounts/{}/transactions".format(bank.get("id")),
                 headers=UH).get_json().get("transactions", [])
pdf = client.get("/api/invoices/{}/pdf".format(inv_b["id"]), headers=UH)
check("v2 money: bank balance moves; a bank payment lands as a deposit row; Pay To prints",
      bal == 150000
      and any(t.get("kind") == "deposit" and t.get("amountPaisa") == 10000 for t in txs)
      and pdf.status_code == 200 and b"Pay To" in pdf.get_data(),
      "balance={} kinds={}".format(bal, [t.get("kind") for t in txs]))

# -- link payments to invoices (allocate) -----------------------------------
r = client.post("/api/parties", headers=UH, json={"name": "V2 Alloc"})
alloc_party = r.get_json()["party"]["id"]
_, a1 = _v2_sale(alloc_party, [{"item_id": v2_item["id"], "qty": 1, "rate_paisa": 10000}])
_, a2 = _v2_sale(alloc_party, [{"item_id": v2_item["id"], "qty": 1, "rate_paisa": 10000}])
r = client.post("/api/payments/allocate", headers=UH, json={
    "party_id": alloc_party, "amount_paisa": 15000, "direction": "in"})
alloc = r.get_json() or {}
check("v2 money: allocate spreads one payment over the oldest bills first",
      len(alloc.get("allocations", [])) == 2
      and alloc.get("allocations", [{}])[0].get("amountPaisa") == 10000
      and alloc.get("unallocatedPaisa") == 0, json.dumps(alloc))

# -- expenses rework + other income ------------------------------------------
r = client.post("/api/expenses", headers=UH, json={
    "title": "V2 Generator Diesel", "category": "Fuel", "category_type": "Direct Expense",
    "expense_item": "Diesel", "amount_paisa": 20000, "paid_paisa": 5000})
exp = (r.get_json() or {}).get("expense", {})
cats = client.get("/api/expense-categories", headers=UH).get_json().get("categories", [])
r = client.post("/api/other-income", headers=UH, json={
    "title": "V2 Scrap Sale", "category": "Scrap", "amount_paisa": 7000})
pnl = client.get("/api/pnl", headers=UH).get_json().get("pnl", {})
check("v2 money: expense keeps paid/balance + a typed category; other income reaches P&L",
      exp.get("balancePaisa") == 15000
      and any(c.get("name") == "Fuel" and c.get("type") == "Direct Expense" for c in cats)
      and (pnl.get("otherIncomePaisa") or 0) >= 7000, json.dumps(exp))

# -- reports smoke ------------------------------------------------------------
ok_reports = True
for path, key in (("/api/reports/all-transactions", "entries"),
                  ("/api/reports/cash-flow", "rows"),
                  ("/api/reports/tax-rate-report", "rows"),
                  ("/api/reports/sales-graph?period=monthly", "points"),
                  ("/api/reports/bill-wise-profit", "rows")):
    rr = client.get(path, headers=UH)
    if rr.status_code != 200 or not isinstance((rr.get_json() or {}).get(key), list):
        ok_reports = False
check("v2 reports: all-transactions / cash-flow / tax-rate / sales-graph / bill-profit respond",
      ok_reports)

# -- loyalty -------------------------------------------------------------------
client.put("/api/settings", headers=UH, json={"settings": {"loyalty": {
    "enabled": True, "spend_per_point_paisa": 10000, "redeem_value_paisa": 100,
    "max_redeem_pct": 50, "min_invoice_redeem_paisa": 0,
    "min_invoice_earn_paisa": 0, "expiry_days": 365}}})
r = client.post("/api/parties", headers=UH, json={"name": "V2 Loyal"})
loyal_party = r.get_json()["party"]["id"]
_, lv = _v2_sale(loyal_party, [{"item_id": v2_item["id"], "qty": 1, "rate_paisa": 100000}],
                 paid_paisa=100000)
pts = {p["id"]: p["loyaltyPoints"] for p in
       client.get("/api/parties", headers=UH).get_json()["parties"]}[loyal_party]
_, lv2 = _v2_sale(loyal_party, [{"item_id": v2_item["id"], "qty": 1, "rate_paisa": 10000}],
                  paid_paisa=9500, loyalty_points_used=5)
pts2 = {p["id"]: p["loyaltyPoints"] for p in
        client.get("/api/parties", headers=UH).get_json()["parties"]}[loyal_party]
client.put("/api/settings", headers=UH, json={"settings": {"loyalty": {"enabled": False}}})
check("v2 loyalty: a paid bill earns points; redeeming spends them at the set value",
      pts == 10 and lv2.get("loyaltyRedeemedPaisa") == 500 and pts2 == 6,
      "pts={} redeemed={} pts2={}".format(pts, lv2.get("loyaltyRedeemedPaisa"), pts2))

# -- online store ----------------------------------------------------------------
r = client.get("/api/store/settings", headers=UH)
biz_id = (r.get_json() or {}).get("businessId")
client.put("/api/store/settings", headers=UH, json={"store": {
    "enabled": True, "delivery_charges_paisa": 10000, "accept_orders": True}})
client.put("/api/store/items/{}".format(v2_item["id"]), headers=UH, json={
    "listed": True, "store_price_paisa": 12000, "in_stock": True})
pub = client.get("/api/public/store/{}".format(biz_id))
pub_items = (pub.get_json() or {}).get("items", [])
r = client.post("/api/public/store/{}/order".format(biz_id), json={
    "customer_name": "Web Buyer", "phone": "03001112222",
    "address": "Larkana", "lines": [{"item_id": v2_item["id"], "qty": 1}]})
order_id = (r.get_json() or {}).get("orderId")
r2 = client.post("/api/store/orders/{}/convert".format(order_id), headers=UH, json={})
conv = r2.get_json() or {}
orders = client.get("/api/store/orders", headers=UH).get_json().get("orders", [])
client.put("/api/store/settings", headers=UH, json={"store": {"enabled": False}})
check("v2 store: public catalogue + COD order + convert-to-sale (delivery rides along)",
      pub.status_code == 200 and any(i["itemId"] == v2_item["id"] for i in pub_items)
      and r.status_code == 201
      and conv.get("invoiceId") and conv.get("order", {}).get("totalPaisa") == 22000
      and any(o["id"] == order_id and o["status"] == "converted" for o in orders),
      json.dumps({"order": order_id, "conv": conv.get("order", {}).get("totalPaisa")}))

# -- team & roles ------------------------------------------------------------------
tok_member = _signup_from_ip("member2@example.com", "198.51.100.77", "member-pass-1")
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": "member2@example.com"})
r = client.post("/api/team", headers=UH, json={
    "name": "Member Two", "contact": "member2@example.com", "role": "Biller"})
r = client.post("/api/team/switch", headers=H(tok_member), json={"business_id": biz_id})
me_m = client.get("/api/auth/me", headers=H(tok_member)).get_json()
r = client.post("/api/expenses", headers=H(tok_member), json={
    "title": "Nope", "amount_paisa": 100})
denied = r.status_code == 403 and (r.get_json() or {}).get("code") == "forbidden_role"
client.post("/api/team/switch", headers=H(tok_member), json={"business_id": None})
check("v2 team: member switches into the shop as Biller; Expenses create is refused by role",
      me_m.get("activeBusinessId") == biz_id and me_m.get("activeRole") == "Biller"
      and denied, json.dumps(me_m))

# -- accounting ----------------------------------------------------------------------
client.put("/api/settings", headers=UH, json={"settings": {"accounting_enabled": True}})
accts = client.get("/api/accounts", headers=UH).get_json().get("accounts", [])
ids = [a["id"] for a in accts[:2]]
r = client.post("/api/journal", headers=UH, json={
    "narration": "V2 unbalanced", "lines": [
        {"account_id": ids[0], "debit_paisa": 1000, "credit_paisa": 0},
        {"account_id": ids[1], "debit_paisa": 0, "credit_paisa": 500}]})
unbalanced_refused = r.status_code == 400
r = client.post("/api/journal", headers=UH, json={
    "narration": "V2 balanced", "lines": [
        {"account_id": ids[0], "debit_paisa": 1000, "credit_paisa": 0},
        {"account_id": ids[1], "debit_paisa": 0, "credit_paisa": 1000}]})
tb = client.get("/api/reports/trial-balance", headers=UH).get_json()
bs = client.get("/api/reports/balance-sheet", headers=UH).get_json()
client.put("/api/settings", headers=UH, json={"settings": {"accounting_enabled": False}})
check("v2 accounting: seeded chart; Debit=Credit enforced; trial balance balances",
      len(accts) >= 11 and unbalanced_refused and r.status_code in (200, 201)
      and tb.get("totalDebitPaisa") == tb.get("totalCreditPaisa")
      and "totalAssetsPaisa" in bs)

# -- utilities: import + verify -------------------------------------------------------
r = client.post("/api/utilities/import/validate", headers=UH, json={
    "kind": "items", "rows": [
        {"name": "V2 Imported", "sale_price": "10.00", "stock_qty": "4"},
        {"sale_price": "5.00"}]})
val = r.get_json() or {}
r2 = client.post("/api/utilities/import/confirm", headers=UH, json={
    "kind": "items", "batch": "v2batch1", "rows": val.get("valid", [])})
verify = client.get("/api/utilities/verify", headers=UH).get_json().get("checks", [])
check("v2 utilities: import validates (1 valid / 1 error), imports, data verifies clean",
      len(val.get("valid", [])) == 1 and len(val.get("errors", [])) == 1
      and (r2.get_json() or {}).get("imported") == 1
      and verify and all(c.get("ok") for c in verify),
      json.dumps({"valid": len(val.get("valid", [])), "checks": verify}))

# -- frontend: new screens are served --------------------------------------------------
html = client.get("/").get_data(as_text=True)
check("v2 frontend: graph, widgets, store, preview and print settings markup served",
      'id="saleGraph"' in html and "data-widget" in html
      and 'id="screen-store"' in html and 'id="screen-preview"' in html
      and 'data-setcard="print"' in html and 'id="screen-accounting"' in html
      and 'id="screen-utilities"' in html and 'id="screen-grow"' in html)

# ------------------------------------- 12i. delta build #2 (Vyapar v3) --
# Party credit limit + opening date round-trip.
r = client.post("/api/parties", headers=UH, json={
    "name": "Delta Credit Party", "opening_balance_paisa": 1000,
    "opening_date": "2026-11-01", "credit_limit_type": "custom",
    "credit_limit_paisa": 250000})
dp = (r.get_json() or {}).get("party") or {}
check("delta: party stores credit limit + opening date",
      r.status_code == 201 and dp.get("creditLimitType") == "custom"
      and dp.get("creditLimitPaisa") == 250000
      and dp.get("openingDate") == "2026-11-01", json.dumps(dp))
r = client.put("/api/parties/{}".format(dp["id"]), headers=UH, json={
    "credit_limit_type": "none", "credit_limit_paisa": 999})
dp2 = (r.get_json() or {}).get("party") or {}
check("delta: switching credit limit to No Limit zeroes the amount",
      dp2.get("creditLimitType") == "none" and dp2.get("creditLimitPaisa") == 0,
      json.dumps(dp2))

# Business profile: new fields + signature text reaches the PDF.
r = client.put("/api/business", headers=UH, json={
    "business_type": "Retail", "business_category": "Electronics",
    "pincode": "77150", "state": "Sindh",
    "signature_text": "For Delta Signature Co"})
prof = (client.get("/api/business", headers=UH).get_json() or {}).get("business") or {}
check("delta: business profile stores type/category/pincode/state/signature",
      prof.get("businessType") == "Retail"
      and prof.get("businessCategory") == "Electronics"
      and prof.get("pincode") == "77150" and prof.get("state") == "Sindh"
      and prof.get("signatureText") == "For Delta Signature Co", json.dumps(prof))

# Item: amount-type sale discount + minimum wholesale qty round-trip.
r = client.post("/api/items", headers=UH, json={
    "name": "Delta Widget", "sale_price_paisa": 10000, "stock_qty": 100,
    "discount_type": "amount", "discount_amount_paisa": 250,
    "min_wholesale_qty": 12})
ditem = (r.get_json() or {}).get("item") or {}
check("delta: item stores amount discount + min wholesale qty",
      r.status_code == 201 and ditem.get("discountType") == "amount"
      and ditem.get("discountAmountPaisa") == 250
      and ditem.get("minWholesaleQty") == 12, json.dumps(ditem))

# Split payments: cash + bank rows become real linked payments; the
# bank balance moves; editing replaces them and reverses the bank row.
r = client.post("/api/bank-accounts", headers=UH, json={
    "display_name": "Delta Bank", "opening_balance_paisa": 0})
dbank = (r.get_json() or {}).get("bankAccount") or {}
r = client.post("/api/invoices", headers=UH, json={
    "type": "sale", "party_id": dp["id"],
    "lines": [{"item_id": ditem["id"], "qty": 2, "rate_paisa": 10000}],
    "payments": [{"mode": "cash", "amount_paisa": 5000},
                 {"mode": "bank", "bank_account_id": dbank["id"],
                  "amount_paisa": 15000, "reference_no": "TRX-D1"}]})
sinv = (r.get_json() or {}).get("invoice") or {}
bank_now = (client.get("/api/bank-accounts", headers=UH).get_json() or {}).get("bankAccounts") or []
bank_bal = [a["balancePaisa"] for a in bank_now if a["id"] == dbank["id"]]
check("delta: split payments save as linked rows and move the bank balance",
      r.status_code == 201 and sinv.get("paidPaisa") == 20000
      and sinv.get("balancePaisa") == 0 and len(sinv.get("payments") or []) == 2
      and bank_bal == [15000], json.dumps({"inv": sinv, "bank": bank_bal}))
pdf_bytes = client.get("/api/invoices/{}/pdf".format(sinv["id"]), headers=UH).get_data()
check("delta: profile signature text prints on the invoice PDF",
      b"For Delta Signature Co" in pdf_bytes)
r = client.put("/api/invoices/{}".format(sinv["id"]), headers=UH, json={
    "type": "sale", "party_id": dp["id"],
    "lines": [{"item_id": ditem["id"], "qty": 2, "rate_paisa": 10000}],
    "payments": [{"mode": "cheque", "amount_paisa": 20000, "cheque_no": "CHQ-D1"}]})
sinv2 = (r.get_json() or {}).get("invoice") or {}
bank_now = (client.get("/api/bank-accounts", headers=UH).get_json() or {}).get("bankAccounts") or []
bank_bal = [a["balancePaisa"] for a in bank_now if a["id"] == dbank["id"]]
chq = client.get("/api/cheque-payments", headers=UH).get_json() or {}
dash = (client.get("/api/dashboard", headers=UH).get_json() or {}).get("dashboard") or {}
check("delta: editing split payments replaces rows, reverses bank, feeds cheques page + Home card",
      r.status_code == 200 and len(sinv2.get("payments") or []) == 1
      and (sinv2["payments"][0]).get("mode") == "cheque"
      and bank_bal == [0]
      and any(c["refNo"] == "CHQ-D1" and c["amountPaisa"] == 20000
              for c in chq.get("received", []))
      and (dash.get("openCheques") or {}).get("receivedCount", 0) >= 1,
      json.dumps({"inv": sinv2, "bank": bank_bal, "chq": chq}))

# Cash adjustment: moves Cash in Hand and lands in the Day Book.
before = (client.get("/api/dashboard", headers=UH).get_json() or {}).get("dashboard", {}).get("cashInHandPaisa", 0)
r = client.post("/api/cash-adjustments", headers=UH, json={
    "direction": "in", "amount_paisa": 7000, "description": "Owner top-up",
    "client_ref": "test-adj-1"})
r2 = client.post("/api/cash-adjustments", headers=UH, json={
    "direction": "in", "amount_paisa": 7000, "description": "Owner top-up",
    "client_ref": "test-adj-1"})
after = (client.get("/api/dashboard", headers=UH).get_json() or {}).get("dashboard", {}).get("cashInHandPaisa", 0)
daybook = client.get("/api/reports/day-book", headers=UH).get_json() or {}
check("delta: cash adjustment moves Cash in Hand once (idempotent) and shows in Day Book",
      r.status_code == 201 and r2.status_code == 200 and after == before + 7000
      and any("Cash Adjustment" in (e.get("description") or "")
              for e in daybook.get("entries", [])),
      json.dumps({"before": before, "after": after}))

# Bank transfers: all four actions move real balances.
r = client.post("/api/bank-accounts", headers=UH, json={
    "display_name": "Delta Bank 2", "opening_balance_paisa": 0})
dbank2 = (r.get_json() or {}).get("bankAccount") or {}
def _bal(aid):
    rows = (client.get("/api/bank-accounts", headers=UH).get_json() or {}).get("bankAccounts") or []
    return [a["balancePaisa"] for a in rows if a["id"] == aid][0]
client.post("/api/bank-accounts/{}/transfer".format(dbank["id"]), headers=UH,
            json={"kind": "cash_to_bank", "amount_paisa": 40000, "client_ref": "t-ctb"})
client.post("/api/bank-accounts/{}/transfer".format(dbank["id"]), headers=UH,
            json={"kind": "bank_to_bank", "amount_paisa": 15000,
                  "to_account_id": dbank2["id"], "client_ref": "t-btb"})
cash_before = (client.get("/api/dashboard", headers=UH).get_json() or {}).get("dashboard", {}).get("cashInHandPaisa", 0)
client.post("/api/bank-accounts/{}/transfer".format(dbank["id"]), headers=UH,
            json={"kind": "bank_to_cash", "amount_paisa": 5000, "client_ref": "t-btc"})
cash_after = (client.get("/api/dashboard", headers=UH).get_json() or {}).get("dashboard", {}).get("cashInHandPaisa", 0)
client.post("/api/bank-accounts/{}/transfer".format(dbank2["id"]), headers=UH,
            json={"kind": "adjust", "adjust_to_paisa": 21000, "client_ref": "t-adj"})
check("delta: bank transfers (cash->bank, bank->bank, bank->cash, adjust) move real balances",
      _bal(dbank["id"]) == 20000 and _bal(dbank2["id"]) == 21000
      and cash_after == cash_before + 5000,
      json.dumps({"b1": _bal(dbank["id"]), "b2": _bal(dbank2["id"])}))

# Home: purchase card data + expenses series.
r = client.post("/api/parties", headers=UH, json={"name": "Delta Big Supplier"})
dsup = (r.get_json() or {}).get("party") or {}
client.post("/api/invoices", headers=UH, json={
    "type": "purchase", "party_id": dsup["id"],
    "lines": [{"item_id": ditem["id"], "qty": 1, "rate_paisa": 99999900}]})
dash = (client.get("/api/dashboard", headers=UH).get_json() or {}).get("dashboard") or {}
check("delta: dashboard carries month purchases, top purchase party, 30-day expense series",
      dash.get("monthPurchasesPaisa", 0) >= 99999900
      and (dash.get("topPurchaseParty") or {}).get("name") == "Delta Big Supplier"
      and len(dash.get("expensesSeries") or []) == 30, json.dumps(dash)[:300])

# Transaction-wise gates: with both OFF, line tax is forced to 0 and
# the line discount falls back to the item default.
cur = (client.get("/api/settings", headers=UH).get_json() or {}).get("settings") or {}
client.put("/api/settings", headers=UH, json={"settings": {
    "txn_wise_tax": False, "txn_wise_discount": False}})
r = client.post("/api/items", headers=UH, json={
    "name": "Delta Gate Item", "sale_price_paisa": 10000, "stock_qty": 10,
    "discount_bp": 1000})
gitem = (r.get_json() or {}).get("item") or {}
r = client.post("/api/invoices", headers=UH, json={
    "type": "sale", "party_id": dp["id"],
    "lines": [{"item_id": gitem["id"], "qty": 1, "rate_paisa": 10000,
               "discount_paisa": 9999, "tax_paisa": 5000}]})
ginv = (r.get_json() or {}).get("invoice") or {}
client.put("/api/settings", headers=UH, json={"settings": {
    "txn_wise_tax": True, "txn_wise_discount": True}})
check("delta: txn-wise OFF forces tax 0 and the item-default discount",
      ginv.get("discountPaisa") == 1000 and ginv.get("taxPaisa") == 0
      and ginv.get("totalPaisa") == 9000, json.dumps(ginv))

# Payment In prefix + Add Time on Transactions in the PDF.
prefixes = dict(cur.get("prefixes") or {})
prefixes["payment_in"] = "RCPT"
client.put("/api/settings", headers=UH, json={"settings": {
    "prefixes": prefixes, "txn_add_time": True}})
r = client.post("/api/payments", headers=UH, json={
    "party_id": dp["id"], "direction": "in", "amount_paisa": 100})
pays = (client.get("/api/payments", headers=UH).get_json() or {}).get("payments") or []
newest = [p for p in pays if p["id"] == (r.get_json() or {}).get("payment", {}).get("id")]
pdf_bytes = client.get("/api/invoices/{}/pdf".format(sinv["id"]), headers=UH).get_data()
prefixes["payment_in"] = "PIN"
client.put("/api/settings", headers=UH, json={"settings": {
    "prefixes": prefixes, "txn_add_time": False}})
check("delta: Payment In prefix used on payment refs; bill time prints when enabled",
      bool(newest) and (newest[0].get("ref") or "").startswith("RCPT-")
      and re.search(rb"Date: \d{4}-\d{2}-\d{2} \d{2}:\d{2}", pdf_bytes) is not None,
      json.dumps(newest))

# Reports: item serial, sale orders, sale order items, trial balance options.
client.post("/api/invoices", headers=UH, json={
    "type": "sale", "party_id": dp["id"],
    "lines": [{"item_id": ditem["id"], "qty": 1, "rate_paisa": 10000,
               "serial_no": "IMEI-D1"}]})
r = client.post("/api/invoices", headers=UH, json={
    "type": "sale_order", "party_id": dp["id"],
    "lines": [{"item_id": ditem["id"], "qty": 3, "rate_paisa": 10000}]})
so_no = ((r.get_json() or {}).get("invoice") or {}).get("invoiceNo")
serial = client.get("/api/reports/item-serial", headers=UH).get_json() or {}
orders = client.get("/api/reports/sale-orders", headers=UH).get_json() or {}
oitems = client.get("/api/reports/sale-order-items", headers=UH).get_json() or {}
tb_all = client.get("/api/reports/trial-balance", headers=UH).get_json() or {}
tb_nozero = client.get("/api/reports/trial-balance?zero=0", headers=UH).get_json() or {}
tb_work = client.get("/api/reports/trial-balance?working=1", headers=UH).get_json() or {}
check("delta: item serial + sale order reports return rows",
      any(x.get("serialNo") == "IMEI-D1" for x in serial.get("rows", []))
      and any(x.get("invoiceNo") == so_no and x.get("status") == "Open"
              for x in orders.get("rows", []))
      and any(x.get("invoiceNo") == so_no and x.get("qty") == 3
              for x in oitems.get("rows", [])), json.dumps(orders)[:200])
check("delta: trial balance zero/working options behave and totals stay balanced",
      any(x.get("debitPaisa") == 0 and x.get("creditPaisa") == 0 for x in tb_all.get("rows", []))
      and all(not (x.get("debitPaisa") == 0 and x.get("creditPaisa") == 0)
              for x in tb_nozero.get("rows", []))
      and tb_nozero.get("totalDebitPaisa") == tb_nozero.get("totalCreditPaisa")
      and bool(tb_work.get("rows")) and "journalDebitPaisa" in tb_work["rows"][0],
      json.dumps({"all": len(tb_all.get("rows", [])), "nozero": len(tb_nozero.get("rows", []))}))

# Frontend: POS screen, settings entries and party tabs are served.
html = client.get("/").get_data(as_text=True)
js = client.get("/static/app.js").get_data(as_text=True)
check("delta: POS screen, Adjust Cash, USER MANAGEMENT and Credit & Balance markup served",
      'id="screen-quick"' in html and "Adjust Cash" in html
      and "USER MANAGEMENT" in html and "Credit &amp; Balance" in html
      and "Save &amp; Print Bill" in html and "payment_in" in js
      and "Show working trial balance" in html)

# ============ 12j. delta build #3 (list-first + Vyapar sale form + profile)
# Owner complaints 2026-11-10: (1) every Add form sat permanently open —
# screens are now LIST-FIRST, the form lives in a hidden panel opened by
# Add/Edit; (2) the Sale/document form is rebuilt to his Vyapar photo;
# (3) Business Profile is complete + findable and strictly per-account:
# a new account starts EMPTY ("My Business") and never inherits the
# owner's shop details; logos are per-account and print on PDFs.
html = client.get("/").get_data(as_text=True)
js = client.get("/static/app.js").get_data(as_text=True)
_panels = ["partyPanel", "itemPanel", "paymentPanel", "expensePanel",
           "incomePanel", "bankPanel", "chequePanel", "loanPanel",
           "godownPanel", "transferPanel", "teamPanel", "termAddPanel",
           "catAddPanel", "unitAddPanel", "coaPanel", "journalPanel",
           "loyaltyPanel", "batchAddPanel"]
check(
    "delta3: every add/edit form lives in a hidden list-first panel",
    all('id="%s" class="hidden"' % p in html for p in _panels)
    and all(b in html for b in [
        'id="btnAddParty"', 'id="btnAddItem"', 'id="btnAddPayment"',
        'id="btnAddExpense"', 'id="btnAddIncome"', 'id="btnAddBank"',
        'id="btnAddCheque"', 'id="btnAddLoan"', 'id="btnAddGodown"',
        'id="btnAddTransfer"', 'id="btnAddTeamUser"', 'id="btnTermNew"',
        'id="btnAddCategory"', 'id="btnAddUnit"', 'id="btnAddCoa"',
        'id="btnAddJournal"', 'id="btnAddBatch"'])
    and "function showPanel" in js and 'showPanel("partyPanel")' in js
    and 'hidePanel("itemPanel")' in js,
)
_head = html[html.find('class="doc-lines-head"'):html.find('id="saleLines"')]
_pos = [_head.find(w) for w in
        ("ITEM", "QTY", "UNIT", "PRICE/UNIT", "DISCOUNT", "TAX")]
check(
    "delta3: doc grid columns in the Vyapar order with Without Tax caption",
    all(p >= 0 for p in _pos) and _pos == sorted(_pos)
    and "Without Tax" in _head and _head.find(">AMOUNT<", _pos[-1]) > _pos[-1],
    _head[:300],
)
check(
    "delta3: doc form = party picker + balances, date input, totals row, "
    "description/image/document buttons, Share + Save",
    all(s in html for s in [
        'id="docPartyBtn"', 'id="docPartyMenu"', "Party Balance",
        'id="docDateInput"', 'id="docTotalQty"', 'id="docTotalDisc"',
        'id="docTotalTax"', 'id="docTotalAmt"', "ADD ROW",
        "ADD DESCRIPTION", "ADD IMAGE", "ADD DOCUMENT",
        'id="btnDocShare"', 'id="docShareMenu"', 'id="docSaveNewBtn"',
        'id="btnAddPayRow"'])
    and "renderDocPartyMenu" in js and "sl-disc-amt" in js
    and "sl-unit-sel" in js,
)
check(
    "delta3: Business Profile has logo upload + owner/city fields and the "
    "version/cache moved to vyapar17/v26 (build .28)",
    'id="profLogoFile"' in html and 'id="profOwner"' in html
    and 'id="profCity"' in html
    and client.get("/healthz").get_json().get("version")
    == "2026-11-13.28-vyapar17"
    and "qbill-shell-v26" in client.get("/sw.js").get_data(as_text=True),
)
# Build .24 markup: the profile form's new home + what stayed behind.
_set0 = html.index('id="screen-settings"')
_settings_html = html[_set0:html.index('</section>', _set0)]
_firm_html = html[html.index('id="editFirmModal"'):html.index('id="dlgModal"')]
_paytax_html = html[html.index('id="payTaxForm"'):html.index('id="taxTermsForm"')]
_t0 = html.index('id="taxTermsForm"')
_terms_html = html[_t0:html.index('data-setcard="transaction"', _t0)]
check(
    "build .24: the Business Profile form left Settings for the Edit "
    "Firm popup (logo + More options + description + signature image); "
    "GENERAL keeps General settings + the Coming Soon payment/tax "
    "card + the Tax Note & Terms card (no banner on the last one)",
    'class="settings-main"' in _settings_html
    and 'id="profileForm"' not in _settings_html
    and 'id="editFirmModal"' in html and 'id="profileForm"' in _firm_html
    and 'id="btnEfMore"' in _firm_html and 'id="efMoreBody"' in _firm_html
    and 'id="profDescription"' in _firm_html
    and 'id="profSigFile"' in _firm_html
    and all(s not in _firm_html for s in (
        'id="profNtn"', 'id="profStrn"', 'id="profRaastIban"',
        'id="profJazzcash"', 'id="profEasypaisa"',
        'id="profPaymentDetails"', 'id="profTaxNote"', 'id="profTerms"'))
    and 'class="coming-soon-banner"' in _paytax_html
    and all(s in _paytax_html for s in (
        'id="profNtn"', 'id="profStrn"', 'id="profRaastIban"',
        'id="profJazzcash"', 'id="profEasypaisa"',
        'id="profPaymentDetails"'))
    and 'coming-soon-banner' not in _terms_html
    and 'id="profTaxNote"' in _terms_html and 'id="profTerms"' in _terms_html
    and '"editFirmModal"' in js,
)
# Build .25 markup: ONE logo tile per brand area (owner's "do do logo"
# report) — the tile shows the business logo when one is set, otherwise
# the QRLedger monogram; app.js swaps the single tile's src per state.
_side_seg = html[html.index('id="sideBrand"'):html.index('id="sideBizName"')]
_head_seg = html[html.index("<header"):html.index('id="headBizName"')]
check(
    "build .25: sidebar card + header brand each contain exactly ONE "
    "logo image (app monogram by default); the duplicate business-logo "
    "<img> elements are gone in both states (no logo / logo set) and "
    "refreshLogoImages swaps the single tile's src",
    _side_seg.count("<img") == 1 and 'id="sideMark"' in _side_seg
    and "/static/icons/icon-192.png" in _side_seg
    and _head_seg.count("<img") == 1 and 'id="headMark"' in _head_seg
    and "/static/icons/icon-192.png" in _head_seg
    and 'id="sideBizLogo"' not in html and 'id="headBizLogo"' not in html
    and "sideBizLogo" not in js and "headBizLogo" not in js
    and "sideMark" in js and "headMark" in js and "APP_MARK_SRC" in js
    and "refreshLogoImages" in js,
)
# Build .28: the main menu is AUTO-CLOSE at every width (owner) —
# phone: ☰ or a left-edge rightward swipe opens the drawer, a
# leftward swipe on drawer/dim closes it; desktop: hidden by default,
# the left-edge hot-zone + hover (fine pointers) opens it as an
# overlay, pointer-leave closes it after the grace delay. Build .26's
# localStorage-persisted desktop collapse is REMOVED.
_css28 = client.get("/static/styles.css").get_data(as_text=True)
check(
    "build .28: auto-close menu wiring — ☰ toggle + scrim + no-scroll, "
    "phone edge swipe (touchstart/touchmove, SWIPE_EDGE_PX/SWIPE_MIN_DX), "
    "desktop hot-zone + hover open with grace-delay close "
    "(scheduleNavClose/cancelNavClose, NAV_CLOSE_DELAY_MS, hover/pointer "
    "media gate), and build .26's persisted collapse fully removed",
    'id="btnMenu"' in html and 'aria-expanded="false"' in html
    and 'id="navHotZone"' in html
    and "openSidebarMobile" in js and "isNarrowShell" in js
    and "navHoverCapable" in js and "NAV_HOVER_MQ" in js
    and "(hover: hover) and (pointer: fine)" in js
    and "scheduleNavClose" in js and "cancelNavClose" in js
    and "NAV_CLOSE_DELAY_MS = 2500" in js
    and "navHotZone" in js and "mouseover" in js
    and 'closest(".modal")' in js
    and 'addEventListener("touchstart"' in js
    and 'addEventListener("touchmove"' in js
    and "SWIPE_EDGE_PX = 24" in js and "SWIPE_MIN_DX = 50" in js
    and "no-scroll" in js
    and "setSidebarCollapsed" not in js and "applySidebarPref" not in js
    and "SIDEBAR_COLLAPSED_KEY" not in js and "nav-collapsed" not in js
    and 'setItem("qrledger_sidebar_collapsed"' not in js
    and 'localStorage.removeItem("qrledger_sidebar_collapsed")' in js
    and "@media (min-width: 901px)" in _css28
    and "body.no-scroll" in _css28
    and ".side-scrim { top: 57px; }" in _css28,
)
check(
    "build .28: menu is a hidden-by-default overlay on desktop "
    "(off-canvas translateX(-105%), .open slides in, pointer-events "
    "gated), the 16px nav hot-zone is served and phone/touch-hidden, "
    "and the menu navy is MORE transparent (~0.85 alpha) with the "
    "frosted blur kept",
    ".nav-hotzone" in _css28 and "width: 16px" in _css28
    and "translateX(-105%)" in _css28 and ".sidebar.open" in _css28
    and "pointer-events: none" in _css28
    and "@media (hover: none)" in _css28
    and "body.nav-collapsed .sidebar" not in _css28
    and "rgba(13, 47, 79, .84)" in _css28
    and "rgba(10, 37, 64, .86)" in _css28
    and "rgba(13, 47, 79, .9)" not in _css28
    and "backdrop-filter: blur(9px)" in _css28,
)
_LOGO_A = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAf"
           "FcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
_LOGO_B = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAf"
           "FcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
tok_a3 = _signup_from_ip("shopa3@example.com", "198.51.100.201", "shop-a3")
tok_b3 = _signup_from_ip("shopb3@example.com", "198.51.100.202", "shop-b3")
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": "shopa3@example.com"})
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": "shopb3@example.com"})
HA3, HB3 = H(tok_a3), H(tok_b3)
prof_b = client.get("/api/business", headers=HB3).get_json()["business"]
check(
    "delta3: a NEW account's profile is EMPTY (neutral placeholder only)",
    prof_b.get("name") == "My Business" and prof_b.get("hasLogo") is False
    and prof_b.get("ownerName") == "" and prof_b.get("phone") == ""
    and prof_b.get("email") == ""
    and not any(s in json.dumps(prof_b) for s in
                ("AL-GHAFFAR", "03273799008", "electronicsalghaffar78",
                 "abdulqudoos5581")),
    json.dumps(prof_b),
)
r = client.get("/api/business/logo", headers=HB3)
check("delta3: no logo yet -> logo endpoint 404", r.status_code == 404)
client.put("/api/business", headers=HA3, json={
    "name": "Alpha Traders", "owner_name": "Alpha Owner", "city": "Karachi",
    "address": "Shop 1, Alpha Market", "phone": "03001234567",
    "email": "alpha@example.com"})
r = client.put("/api/business/logo", headers=HA3, json={"logo_data": _LOGO_A})
prof_a = client.get("/api/business", headers=HA3).get_json()["business"]
logo_a = client.get("/api/business/logo", headers=HA3)
check(
    "delta3: account A profile + logo round-trip (own details only)",
    r.status_code == 200 and prof_a.get("name") == "Alpha Traders"
    and prof_a.get("ownerName") == "Alpha Owner"
    and prof_a.get("city") == "Karachi" and prof_a.get("hasLogo") is True
    and logo_a.status_code == 200
    and logo_a.get_data().startswith(b"\x89PNG"),
    json.dumps(prof_a),
)
client.put("/api/business", headers=HB3, json={
    "name": "Beta Mart", "owner_name": "Beta Owner", "city": "Hyderabad",
    "address": "Beta Plaza", "phone": "03339998877",
    "email": "beta@example.com"})


def _mk_inv3(tok):
    p = client.post("/api/parties", headers=H(tok),
                    json={"name": "Walk-in"}).get_json()["party"]["id"]
    i = client.post("/api/items", headers=H(tok), json={
        "name": "Thing", "sale_price_paisa": 10000,
        "stock_qty": 5}).get_json()["item"]["id"]
    v = client.post("/api/invoices", headers=H(tok), json={
        "type": "sale", "party_id": p,
        "lines": [{"item_id": i, "qty": 1, "rate_paisa": 10000}]})
    return v.get_json()["invoice"]["id"]


pdf_b = client.get("/api/invoices/{}/pdf".format(_mk_inv3(tok_b3)),
                   headers=HB3).get_data()
check(
    "delta3: B's PDF carries ONLY B's details (no logo image, no A data)",
    b"Beta Mart" in pdf_b and b"Alpha Traders" not in pdf_b
    and b"AL-GHAFFAR" not in pdf_b and b"/Subtype /Image" not in pdf_b,
)
pdf_a = client.get("/api/invoices/{}/pdf".format(_mk_inv3(tok_a3)),
                   headers=HA3).get_data()
check(
    "delta3: A's PDF carries A's name AND its embedded logo image",
    b"Alpha Traders" in pdf_a and b"/Subtype /Image" in pdf_a
    and b"Beta Mart" not in pdf_a and b"AL-GHAFFAR" not in pdf_a,
)
client.put("/api/business/logo", headers=HB3, json={"logo_data": _LOGO_B})
logo_b = client.get("/api/business/logo", headers=HB3).get_data()
logo_a2 = client.get("/api/business/logo", headers=HA3).get_data()
check(
    "delta3: B's different logo never touches A's logo",
    logo_b.startswith(b"\x89PNG") and logo_b != logo_a2
    and logo_a2 == logo_a.get_data(),
)
r = client.put("/api/business/logo", headers=HB3, json={
    "logo_data": "data:text/html;base64,"
                 "PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="})
check("delta3: a non-image logo upload is refused (400)",
      r.status_code == 400)
client.put("/api/business/logo", headers=HB3, json={"logo_data": None})
prof_b = client.get("/api/business", headers=HB3).get_json()["business"]
r = client.get("/api/business/logo", headers=HB3)
check("delta3: logo removal clears hasLogo and the endpoint 404s",
      prof_b.get("hasLogo") is False and r.status_code == 404)
store_b = client.get("/api/store/settings", headers=HB3).get_json()
biz_b = store_b["businessId"]
client.put("/api/store/settings", headers=HB3,
           json={"store": {"enabled": True, "accept_orders": True}})
items_b = client.get("/api/items", headers=HB3).get_json()["items"]
client.put("/api/store/items/{}".format(items_b[0]["id"]), headers=HB3,
           json={"listed": True, "store_price_paisa": 10000,
                 "in_stock": True})
pub_b = client.get("/api/public/store/{}".format(biz_b)).get_json()
check(
    "delta3: B's public store shows B's own business name, never A's",
    pub_b["store"]["name"] == "Beta Mart"
    and "Alpha" not in json.dumps(pub_b),
    json.dumps(pub_b.get("store")),
)
client.put("/api/store/settings", headers=HB3,
           json={"store": {"enabled": False}})

# ============ 12k. delta build #3 Fix 4 — delete everywhere ============
# Every creatable record gets a Delete. Parties / items / expenses /
# income soft-delete into the Recycle Bin (totals stay exact because
# every query filters deleted_at); documents already did. Payments,
# bank accounts, loans and godowns delete only when it cannot corrupt
# the books — otherwise the API refuses with the reason.
HU = H(user_token)
dp = client.post("/api/parties", headers=HU,
                 json={"name": "Delete Me Traders"}).get_json()["party"]
di = client.post("/api/items", headers=HU, json={
    "name": "Delete Me Widget", "sale_price_paisa": 20000,
    "purchase_price_paisa": 12000, "stock_qty": 10}).get_json()["item"]
dv = client.post("/api/invoices", headers=HU, json={
    "type": "sale", "party_id": dp["id"],
    "lines": [{"item_id": di["id"], "qty": 2, "rate_paisa": 20000}]})
dinv = dv.get_json()["invoice"]


def _party_bal(pid):
    rows = client.get("/api/parties", headers=HU).get_json()["parties"]
    return {p["id"]: p["balancePaisa"] for p in rows}.get(pid)


def _item_stock(iid):
    rows = client.get("/api/items", headers=HU).get_json()["items"]
    return {i["id"]: i["stockQty"] for i in rows}.get(iid)


check("fix4: setup — balance -40000 (party owes us, build .20 sign), stock 8 after the sale",
      _party_bal(dp["id"]) == -40000 and _item_stock(di["id"]) == 8)
r = client.delete("/api/parties/{}".format(dp["id"]), headers=HU)
led = client.get("/api/parties/{}/ledger".format(dp["id"]), headers=HU)
bin_r = client.get("/api/recycle-bin", headers=HU).get_json()
bin_p = [p for p in bin_r.get("parties", []) if p["id"] == dp["id"]]
check(
    "fix4: party delete -> bin (balance kept), list + ledger closed",
    r.status_code == 200 and _party_bal(dp["id"]) is None
    and led.status_code == 404 and len(bin_p) == 1
    and bin_p[0]["balancePaisa"] == -40000,
    json.dumps(bin_r.get("parties"))[:200],
)
r = client.post("/api/invoices", headers=HU, json={
    "type": "sale", "party_id": dp["id"],
    "lines": [{"item_id": di["id"], "qty": 1, "rate_paisa": 20000}]})
check("fix4: a deleted party cannot take a new bill (400)",
      r.status_code == 400)
r = client.delete("/api/recycle-bin/parties/{}".format(dp["id"]),
                  headers=HU)
check("fix4: permanent delete of a party WITH bills is refused (400)",
      r.status_code == 400 and "bills or payments" in
      (r.get_json() or {}).get("error", ""))
r = client.post("/api/recycle-bin/parties/{}/restore".format(dp["id"]),
                headers=HU, json={})
check("fix4: party restore brings the exact balance back",
      r.status_code == 200 and _party_bal(dp["id"]) == -40000)
r = client.delete("/api/items/{}".format(di["id"]), headers=HU)
dash = client.get("/api/dashboard", headers=HU).get_json()
bin_i = [i for i in client.get("/api/recycle-bin", headers=HU)
         .get_json().get("items", []) if i["id"] == di["id"]]
check(
    "fix4: item delete -> bin with its stock figure kept (8)",
    r.status_code == 200 and _item_stock(di["id"]) is None
    and len(bin_i) == 1 and bin_i[0]["stockQty"] == 8,
)
r = client.post("/api/invoices", headers=HU, json={
    "type": "sale", "party_id": dp["id"],
    "lines": [{"item_id": di["id"], "qty": 1, "rate_paisa": 20000}]})
check("fix4: a deleted item cannot be billed (400)", r.status_code == 400)
client.post("/api/recycle-bin/items/{}/restore".format(di["id"]),
            headers=HU, json={})
check("fix4: item restore returns it with stock still 8",
      _item_stock(di["id"]) == 8)
ni = client.post("/api/items", headers=HU, json={
    "name": "Never Billed", "sale_price_paisa": 100,
    "stock_qty": 1}).get_json()["item"]
client.delete("/api/items/{}".format(ni["id"]), headers=HU)
r = client.delete("/api/recycle-bin/items/{}".format(ni["id"]),
                  headers=HU)
bin_ids = [i["id"] for i in client.get("/api/recycle-bin", headers=HU)
           .get_json().get("items", [])]
check("fix4: an unreferenced item deletes forever from the bin",
      r.status_code == 200 and ni["id"] not in bin_ids)
# invoice delete from the list endpoint: stock reversal both ways
si = client.post("/api/items", headers=HU, json={
    "name": "Stock Flow", "sale_price_paisa": 5000,
    "purchase_price_paisa": 3000, "stock_qty": 10}).get_json()["item"]
sv = client.post("/api/invoices", headers=HU, json={
    "type": "sale", "party_id": dp["id"],
    "lines": [{"item_id": si["id"], "qty": 3, "rate_paisa": 5000}]})
sid = sv.get_json()["invoice"]["id"]
client.delete("/api/invoices/{}".format(sid), headers=HU)
after_del = _item_stock(si["id"])
client.post("/api/recycle-bin/{}/restore".format(sid), headers=HU,
            json={})
check("fix4: list delete reverses stock (7->10), restore re-applies (7)",
      after_del == 10 and _item_stock(si["id"]) == 7)
# expenses + income: soft delete, totals move, restore
pnl0 = client.get("/api/pnl", headers=HU).get_json()["pnl"]
ex = client.post("/api/expenses", headers=HU, json={
    "title": "Delete Me Expense", "amount_paisa": 50000,
    "paid_paisa": 50000}).get_json()["expense"]
client.delete("/api/expenses/{}".format(ex["id"]), headers=HU)
pnl1 = client.get("/api/pnl", headers=HU).get_json()["pnl"]
bin_e = [e for e in client.get("/api/recycle-bin", headers=HU)
         .get_json().get("expenses", []) if e["id"] == ex["id"]]
client.post("/api/recycle-bin/expenses/{}/restore".format(ex["id"]),
            headers=HU, json={})
pnl2 = client.get("/api/pnl", headers=HU).get_json()["pnl"]
check(
    "fix4: expense delete drops it from P&L, bin holds it, restore exact",
    pnl1["expensesPaisa"] == pnl0["expensesPaisa"]
    and len(bin_e) == 1
    and pnl2["expensesPaisa"] == pnl0["expensesPaisa"] + 50000,
    "{} {} {}".format(pnl0["expensesPaisa"], pnl1["expensesPaisa"],
                      pnl2["expensesPaisa"]),
)
inc = client.post("/api/other-income", headers=HU, json={
    "title": "Delete Me Income", "amount_paisa": 70000}
    ).get_json()["income"]
client.delete("/api/other-income/{}".format(inc["id"]), headers=HU)
bin_n = [e for e in client.get("/api/recycle-bin", headers=HU)
         .get_json().get("incomes", []) if e["id"] == inc["id"]]
client.post("/api/recycle-bin/incomes/{}/restore".format(inc["id"]),
            headers=HU, json={})
inc_list = client.get("/api/other-income", headers=HU).get_json()
check(
    "fix4: income delete -> bin -> restore returns it to the list",
    len(bin_n) == 1 and any(i["id"] == inc["id"]
                             for i in inc_list["incomes"]),
)
# payments: standalone cash deletes; bill-linked and bank refuse
pay = client.post("/api/payments", headers=HU, json={
    "direction": "in", "party_id": dp["id"], "amount_paisa": 15000,
    "mode": "cash"}).get_json()["payment"]
bal_before = _party_bal(dp["id"])
r = client.delete("/api/payments/{}".format(pay["id"]), headers=HU)
check("fix4: standalone cash payment deletes, balance reverses exactly",
      r.status_code == 200 and _party_bal(dp["id"]) == bal_before - 15000)
pv = client.post("/api/invoices", headers=HU, json={
    "type": "sale", "party_id": dp["id"], "paid_paisa": 5000,
    "lines": [{"item_id": si["id"], "qty": 1, "rate_paisa": 5000}]})
pinv = pv.get_json()["invoice"]
pays = client.get("/api/payments", headers=HU).get_json()["payments"]
linked = [p for p in pays if p.get("invoiceId") == pinv["id"]]
r = client.delete("/api/payments/{}".format(linked[0]["id"]), headers=HU) \
    if linked else None
check("fix4: a bill's own payment refuses standalone delete (400+reason)",
      bool(linked) and r.status_code == 400
      and "recorded against bill" in (r.get_json() or {}).get("error", ""))
bank = client.post("/api/bank-accounts", headers=HU, json={
    "display_name": "Delete Test Bank"}).get_json()["bankAccount"]
bpay = client.post("/api/payments", headers=HU, json={
    "direction": "in", "party_id": dp["id"], "amount_paisa": 5000,
    "mode": "bank", "bank_account_id": bank["id"]}).get_json()["payment"]
r = client.delete("/api/payments/{}".format(bpay["id"]), headers=HU)
r2 = client.delete("/api/bank-accounts/{}".format(bank["id"]), headers=HU)
check("fix4: bank payment + used bank account both refuse delete (400)",
      r.status_code == 400 and r2.status_code == 400)
bank2 = client.post("/api/bank-accounts", headers=HU, json={
    "display_name": "Empty Bank"}).get_json()["bankAccount"]
r = client.delete("/api/bank-accounts/{}".format(bank2["id"]), headers=HU)
banks = client.get("/api/bank-accounts", headers=HU).get_json()
check("fix4: an unused bank account deletes cleanly",
      r.status_code == 200 and not any(
          a["id"] == bank2["id"] for a in banks["bankAccounts"]))
chq = client.post("/api/cheques", headers=HU, json={
    "cheque_no": "DEL-1", "bank_name": "Test Bank",
    "amount_paisa": 9000}).get_json()["cheque"]
r = client.delete("/api/cheques/{}".format(chq["id"]), headers=HU)
chqs = client.get("/api/cheques", headers=HU).get_json()["cheques"]
check("fix4: a cheque-book entry deletes",
      r.status_code == 200
      and not any(c["id"] == chq["id"] for c in chqs))
loan = client.post("/api/loans", headers=HU, json={
    "name": "Delete Loan", "principal_paisa": 100000}).get_json()["loan"]
r = client.delete("/api/loans/{}".format(loan["id"]), headers=HU)
loan2 = client.post("/api/loans", headers=HU, json={
    "name": "Paid Loan", "principal_paisa": 100000}).get_json()["loan"]
client.post("/api/loans/{}/pay".format(loan2["id"]), headers=HU,
            json={"amount_paisa": 1000})
r2 = client.delete("/api/loans/{}".format(loan2["id"]), headers=HU)
check("fix4: untouched loan deletes; a loan with repayments refuses",
      r.status_code == 200 and r2.status_code == 400)
gd = client.post("/api/godowns", headers=HU, json={
    "name": "Delete Godown", "type": "Godown"}).get_json()["godown"]
r = client.delete("/api/godowns/{}".format(gd["id"]), headers=HU)
gds = client.get("/api/godowns", headers=HU).get_json()["godowns"]
main_g = [g for g in gds if g["isMain"]][0]
r2 = client.delete("/api/godowns/{}".format(main_g["id"]), headers=HU)
check("fix4: empty godown deletes; the Main Godown refuses",
      r.status_code == 200 and r2.status_code == 400
      and not any(g["id"] == gd["id"] for g in gds))
tm = client.post("/api/team", headers=HU, json={
    "name": "Delete Member", "contact": "del@example.com",
    "role": "Biller"}).get_json()["member"]
r = client.delete("/api/team/{}".format(tm["id"]), headers=HU)
members = client.get("/api/team", headers=HU).get_json()["members"]
gone = [m for m in members if m["id"] == tm["id"]]
check("fix4: team Remove now works (member marked removed)",
      r.status_code == 200 and (not gone or gone[0]["status"] == "removed"))

# ============ 12l. delta build #3 Fix 5 — import wizard ================
# "Bring your old data": Vyapar-shaped exports auto-map; generic files
# map by hand; duplicates (phone+name / code+name) default to SKIP;
# error rows never block the valid ones.
tok_w = _signup_from_ip("wizard@example.com", "198.51.100.210", "wiz-1234")
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": "wizard@example.com"})
HW = H(tok_w)
PARTY_CSV = (
    "Party Name,Phone Number,Address,Opening Balance,Email\n"
    "Wizard Customer,0300-1112223,Main Bazaar,5000,wc@example.com\n"
    "Wizard Supplier,0321-4445556,Old City,-2500,ws@example.com\n"
    "Broken Row,0300-0000000,Nowhere,notanumber,bad@example.com\n"
    ",0300-9999999,No Name,100,noname@example.com\n"
)
r = client.post("/api/utilities/import/parse", headers=HW,
                data={"kind": "parties",
                      "file": (io.BytesIO(PARTY_CSV.encode()),
                               "parties.csv")},
                content_type="multipart/form-data")
parsed = r.get_json() or {}
check(
    "fix5: Vyapar party headers auto-map (name/phone/address/balance)",
    r.status_code == 200 and parsed.get("mapping", {}).get("name")
    == "Party Name" and parsed["mapping"].get("phone") == "Phone Number"
    and parsed["mapping"].get("opening_balance") == "Opening Balance"
    and len(parsed.get("rows", [])) == 4,
    json.dumps(parsed.get("mapping")),
)
r = client.post("/api/utilities/import/wizard-validate", headers=HW,
                json={"kind": "parties", "mapping": parsed["mapping"],
                      "rows": parsed["rows"]})
val = r.get_json()
check("fix5: validate splits rows into new (2) and errors (2)",
      val["counts"] == {"new": 2, "duplicate": 0, "error": 2},
      json.dumps(val["counts"]))
rows_ok = [{"data": e["data"], "action": "create"}
           for e in val["rows"] if e["status"] == "new"]
r = client.post("/api/utilities/import/wizard-confirm", headers=HW,
                json={"kind": "parties", "batch": "wizbatch1",
                      "opening_date": "2026-01-15", "rows": rows_ok})
plist = client.get("/api/parties", headers=HW).get_json()["parties"]
by_name = {p["name"]: p for p in plist}
check(
    "fix5: parties land with opening balances + the chosen as-of date "
    "(build .20 sign: customer owes us = negative, supplier owed = positive)",
    r.status_code == 201 and r.get_json()["created"] == 2
    and by_name["Wizard Customer"]["balancePaisa"] == -500000
    and by_name["Wizard Supplier"]["balancePaisa"] == 250000
    and by_name["Wizard Customer"]["openingDate"] == "2026-01-15",
    json.dumps(r.get_json()),
)
r = client.post("/api/utilities/import/wizard-validate", headers=HW,
                json={"kind": "parties", "mapping": parsed["mapping"],
                      "rows": parsed["rows"][:2]})
check("fix5: re-import detects duplicates by phone+name",
      r.get_json()["counts"]["duplicate"] == 2)
dup_rows = r.get_json()["rows"]
r = client.post("/api/utilities/import/wizard-confirm", headers=HW,
                json={"kind": "parties", "batch": "wizbatch2",
                      "rows": [{"data": e["data"], "action": "create"}
                               for e in dup_rows]})
n_after = len(client.get("/api/parties", headers=HW).get_json()
              ["parties"])
check("fix5: duplicates default to SKIP even if 'create' is sent",
      r.get_json()["created"] == 0 and r.get_json()["skipped"] == 2
      and n_after == 2)
upd = [{"data": dict(e["data"], address="New Address Road"),
        "action": "update"} for e in dup_rows[:1]]
r = client.post("/api/utilities/import/wizard-confirm", headers=HW,
                json={"kind": "parties", "batch": "wizbatch3",
                      "rows": upd})
plist = client.get("/api/parties", headers=HW).get_json()["parties"]
wc = {p["name"]: p for p in plist}["Wizard Customer"]
check("fix5: choosing Update overwrites the matched party's fields",
      r.get_json()["updated"] == 1
      and wc["billingAddress"] == "New Address Road")
ITEM_CSV = (
    "Item Name,Item Code,Sale Price,Purchase Price,MRP,Unit,Stock,Tax %\n"
    "Wizard Panel,WP-550,1500.50,1400,1800,Crate,25,5\n"
    "Wizard Cable,,300,250,,Pcs,100,\n"
)
r = client.post("/api/utilities/import/parse", headers=HW,
                data={"kind": "items",
                      "file": (io.BytesIO(ITEM_CSV.encode()),
                               "items.csv")},
                content_type="multipart/form-data")
iparsed = r.get_json() or {}
r = client.post("/api/utilities/import/wizard-validate", headers=HW,
                json={"kind": "items", "mapping": iparsed["mapping"],
                      "rows": iparsed["rows"]})
ival = r.get_json()
r = client.post("/api/utilities/import/wizard-confirm", headers=HW,
                json={"kind": "items", "batch": "wizbatch4",
                      "rows": [{"data": e["data"], "action": "create"}
                               for e in ival["rows"]]})
ilist = client.get("/api/items", headers=HW).get_json()["items"]
panel = {i["name"]: i for i in ilist}["Wizard Panel"]
check(
    "fix5: items land with prices, MRP, stock, tax (5% -> 500bp), unit",
    r.get_json()["created"] == 2
    and panel["salePricePaisa"] == 150050
    and panel["purchasePricePaisa"] == 140000
    and panel["mrpPaisa"] == 180000 and panel["stockQty"] == 25
    and panel["taxBp"] == 500 and panel["unit"] == "Crate"
    and "Crate" in ival["units"],
    json.dumps(panel),
)
GEN_CSV = "Title,Cost,Rates\nGeneric Thing,40,55.5\n"
r = client.post("/api/utilities/import/parse", headers=HW,
                data={"kind": "items",
                      "file": (io.BytesIO(GEN_CSV.encode()), "gen.csv")},
                content_type="multipart/form-data")
grows = r.get_json()["rows"]
r = client.post("/api/utilities/import/wizard-validate", headers=HW,
                json={"kind": "items",
                      "mapping": {"name": "Title",
                                  "purchase_price": "Cost",
                                  "sale_price": "Rates"},
                      "rows": grows})
gval = r.get_json()
r = client.post("/api/utilities/import/wizard-confirm", headers=HW,
                json={"kind": "items", "batch": "wizbatch5",
                      "rows": [{"data": e["data"], "action": "create"}
                               for e in gval["rows"]]})
ilist = client.get("/api/items", headers=HW).get_json()["items"]
gen = {i["name"]: i for i in ilist}.get("Generic Thing", {})
check("fix5: a generic CSV imports through a hand-made mapping",
      r.get_json()["created"] == 1
      and gen.get("salePricePaisa") == 5550
      and gen.get("purchasePricePaisa") == 4000)
import zipfile as _zip

_buf = io.BytesIO()
with _zip.ZipFile(_buf, "w") as z:
    z.writestr("xl/sharedStrings.xml",
               '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<sst xmlns="http://schemas.openxmlformats.org/'
               'spreadsheetml/2006/main" count="4" uniqueCount="4">'
               "<si><t>Party Name</t></si><si><t>Phone Number</t></si>"
               "<si><t>Xlsx Party</t></si><si><t>0300-1212121</t></si>"
               "</sst>")
    z.writestr("xl/worksheets/sheet1.xml",
               '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<worksheet xmlns="http://schemas.openxmlformats.org/'
               'spreadsheetml/2006/main"><sheetData>'
               '<row r="1"><c r="A1" t="s"><v>0</v></c>'
               '<c r="B1" t="s"><v>1</v></c></row>'
               '<row r="2"><c r="A2" t="s"><v>2</v></c>'
               '<c r="B2" t="s"><v>3</v></c></row>'
               "</sheetData></worksheet>")
_buf.seek(0)
r = client.post("/api/utilities/import/parse", headers=HW,
                data={"kind": "parties",
                      "file": (_buf, "book.xlsx")},
                content_type="multipart/form-data")
xparsed = r.get_json() or {}
check("fix5: a real .xlsx file parses (headers + rows, stdlib reader)",
      r.status_code == 200
      and xparsed.get("headers") == ["Party Name", "Phone Number"]
      and xparsed["rows"][0]["Party Name"] == "Xlsx Party",
      json.dumps(xparsed)[:200])
html = client.get("/").get_data(as_text=True)
js = client.get("/static/app.js").get_data(as_text=True)
check("fix5: wizard UI is served (Utilities card + Items/Parties entries)",
      'id="importWizard"' in html and 'id="btnWizParties"' in html
      and 'id="btnWizItems"' in html and "wizard-validate" in js
      and "Import from Vyapar / another software" in html)

# --------------------------------- 12m. full-site audit fixes (2026-11-10) --
# Research-driven parity repairs: Party Status round-trip, the Home
# You'll Pay top party, and the audited shell (wording + new screens).
r = client.post("/api/parties", headers=HW,
                json={"name": "Audit Status Party", "status": "inactive"})
audit_pid = (r.get_json() or {}).get("party", {}).get("id")
r = client.get("/api/parties", headers=HW)
audit_row = {p["id"]: p for p in r.get_json()["parties"]}.get(audit_pid, {})
r = client.put("/api/parties/{}".format(audit_pid), headers=HW,
               json={"status": "active"})
audit_put = (r.get_json() or {}).get("party", {})
check("audit: party status stores inactive, a status-only PUT reactivates",
      audit_row.get("status") == "inactive"
      and audit_put.get("status") == "active",
      json.dumps({"row": audit_row, "put": audit_put})[:200])
r = client.post("/api/parties", headers=HW, json={"name": "Audit Supplier"})
audit_sup = (r.get_json() or {}).get("party", {}).get("id")
r = client.post("/api/invoices", headers=HW,
                json={"type": "purchase", "party_id": audit_sup,
                      "lines": [{"qty": 1, "rate_paisa": 99999900,
                                 "description": "Audit purchase"}]})
r = client.get("/api/dashboard", headers=HW)
audit_top = (r.get_json() or {}).get("dashboard", {}).get("topPayableParty") or {}
check("audit: dashboard names the top You'll Pay party with its amount",
      audit_top.get("name") == "Audit Supplier"
      and audit_top.get("amountPaisa") == 99999900,
      json.dumps(audit_top))
audit_html = client.get("/").get_data(as_text=True)
audit_js = client.get("/static/app.js").get_data(as_text=True)
check("audit: shell carries the research fixes (billing box, overdue card, "
      "Most Used Reports, import entries + template box, cash/expenses "
      "tables, party status UI)",
      'id="docBillingWrap"' in audit_html and 'id="bookOverdue"' in audit_html
      and "Sale Report" in audit_html and "Daybook Report" in audit_html
      and "Party Statement" in audit_html and "View All" in audit_html
      and 'id="btnUtilImportItems"' in audit_html
      and 'id="btnUtilImportParties"' in audit_html
      and "Download .xls/.xlsx (excel sheet) template file to enter Data"
      in audit_html
      and 'id="cashBody"' in audit_html and 'id="expenseBody"' in audit_html
      and 'id="expenseCats"' in audit_html
      and 'id="partyStatusChips"' in audit_html
      and 'id="ledgerStatusToggle"' in audit_html
      and 'id="dTopPayParty"' in audit_html)
check("audit: app.js carries Vyapar's verified wording + the new flows",
      "Do you really want to delete this item?" in audit_js
      and "Do you really want to delete this transaction?" in audit_js
      and "Your stock for following item is not sufficient to fulfil "
          "the sale." in audit_js
      and "SALE PRICE" in audit_js and "Last purchase prices:" in audit_js
      and "topPayableParty" in audit_js
      and "Valid " in audit_js and "with Errors : " in audit_js)

# ----------------------- 12n. sale-form cleanup (Qudoos phone check) --
# His marked screenshot of .16 ordered: (1) the per-line Description /
# Batch No. / Serial No. / Exp inputs under every bill line are GONE
# from the doc form, (2) "More Details" is ONE collapsed option, (3)
# "ADD TERMS AND CONDITIONS" is its own collapsed option. The fields
# inside keep saving exactly as before.
sf_html = client.get("/").get_data(as_text=True)
sf_js = client.get("/static/app.js").get_data(as_text=True)
check("cleanup: doc form has NO per-line Description/Batch/Serial/Exp "
      "inputs (form-level ADD DESCRIPTION stays)",
      "sl-desc" not in sf_js and "sl-batch" not in sf_js
      and "sl-serial" not in sf_js and "sl-exp" not in sf_js
      and 'id="btnDocDesc"' in sf_html and 'id="btnDocImage"' in sf_html
      and 'id="btnDocDoc"' in sf_html)
check("cleanup: More Details + Terms are collapsed-by-default toggles "
      "(hidden bodies, expand-on-tap wiring in app.js)",
      'id="btnDocMore"' in sf_html and 'id="btnDocTerms"' in sf_html
      and 'id="docMoreBody" class="hidden"' in sf_html
      and 'id="docTermsBody" class="hidden"' in sf_html
      and sf_html.count('aria-expanded="false"') >= 2
      and "docSectionSet" in sf_js and "docMoreHasContent" in sf_js
      and "moreOpen" in sf_js and "termsOpen" in sf_js)
r = client.post("/api/terms", headers=HW,
                json={"header": "Cleanup Terms", "description": "Net due.",
                      "applies": ["sale"]})
sf_term_id = (r.get_json() or {}).get("term", {}).get("id")
r = client.post("/api/invoices", headers=HW,
                json={"type": "sale",
                      "lines": [{"qty": 1, "rate_paisa": 50000,
                                 "description": "Cleanup line"}],
                      "due_date": "2026-12-01", "payment_terms": "Net 15",
                      "shipping_address": "Warehouse 9, Lahore",
                      "po_number": "PO-77", "transport_name": "Bilal Goods",
                      "vehicle_no": "LEA-1234", "delivery_date": "2026-11-20",
                      "delivery_location": "Karachi",
                      "repeat_freq": "monthly", "terms_id": sf_term_id})
sf_inv_id = (r.get_json() or {}).get("invoice", {}).get("id")
r = client.get("/api/invoices/{}".format(sf_inv_id), headers=HW)
sf_inv = (r.get_json() or {}).get("invoice", {})
check("cleanup: values inside More Details + the Terms selection still "
      "save and read back exact",
      sf_inv.get("dueDate") == "2026-12-01"
      and sf_inv.get("paymentTerms") == "Net 15"
      and sf_inv.get("shippingAddress") == "Warehouse 9, Lahore"
      and sf_inv.get("poNumber") == "PO-77"
      and sf_inv.get("transportName") == "Bilal Goods"
      and sf_inv.get("vehicleNo") == "LEA-1234"
      and sf_inv.get("deliveryDate") == "2026-11-20"
      and sf_inv.get("deliveryLocation") == "Karachi"
      and sf_inv.get("repeatFreq") == "monthly"
      and sf_inv.get("termsId") == sf_term_id,
      json.dumps(sf_inv)[:240])

# ----------------- 12o. type-to-search + sidebar + tax dropdown -------
# Qudoos phone check of .17 (2026-11-10 ~22:03), against his Vyapar
# reference photo: (1) the ITEM cell is a TEXT INPUT — he types the
# item name himself, matching items drop down with the picker columns
# (ITEM / SALE PRICE / PURCHASE PRICE / STOCK / LOCATION + an
# "+ Add Item" header); picking nothing leaves a free-text line whose
# typed name is saved as the line description — there is NO
# "— Custom line —" resting state; (2) sidebar groups start
# MINIMIZED, opening only on click, with a has-active header hint;
# (3) the TAX cell is a dropdown resting on "Select".
tt_html = client.get("/").get_data(as_text=True)
tt_js = client.get("/static/app.js").get_data(as_text=True)
tt_css = client.get("/static/styles.css").get_data(as_text=True)
check("typetosearch: item cell = text input + hidden id, suggestion "
      "menu keeps picker columns + Add Item, no Custom-line label, "
      "free text saved as the line description",
      'class="sl-item-name" type="text"' in tt_js
      and 'class="sl-item" type="hidden"' in tt_js
      and "Custom line" not in tt_js
      and "showItemMenu" in tt_js and "buildItemMenu" in tt_js
      and "SALE PRICE" in tt_js and "PURCHASE PRICE" in tt_js
      and "sl-add-item" in tt_js
      and "line.description = typedName" in tt_js
      and ".sl-item-name" in tt_css)
check("sidebar: SALE/PURCHASE/STOCK/REPORTS groups served closed "
      "(head + body) with toggle + has-active hint wiring",
      tt_html.count("nav-group-head closed") == 4
      and tt_html.count("nav-group-body closed") == 4
      and "has-active" in tt_js
      and ".nav-group-head.has-active" in tt_css)
check("tax dropdown: TAX cell is a <select> resting on 'Select' "
      "(None + the account's rates), choice sets the line tax %",
      '<select class="sl-tax"></select>' in tt_js
      and 'textContent = "Select"' in tt_js
      and 'textContent = "None"' in tt_js
      and "buildLineTax" in tt_js and "setLineTaxPct" in tt_js
      and "lineTaxRates" in tt_js)
r = client.post("/api/invoices", headers=HW,
                json={"type": "sale",
                      "lines": [{"qty": 2, "rate_paisa": 10000,
                                 "tax_paisa": 2000,
                                 "description": "Taxed line"}]})
tt_inv_id = (r.get_json() or {}).get("invoice", {}).get("id")
r = client.get("/api/invoices/{}".format(tt_inv_id), headers=HW)
tt_inv = (r.get_json() or {}).get("invoice", {})
tt_line = (tt_inv.get("lines") or [{}])[0]
check("tax dropdown: a chosen 10% rate saves exact (line tax 2000, "
      "invoice tax 2000, total 22000)",
      tt_line.get("taxPaisa") == 2000 and tt_inv.get("taxPaisa") == 2000
      and tt_inv.get("totalPaisa") == 22000,
      json.dumps(tt_inv)[:240])
r = client.post("/api/invoices", headers=HW,
                json={"type": "sale",
                      "lines": [{"qty": 3, "rate_paisa": 15000,
                                 "description": "Hand Typed Gadget"}]})
ft_id = (r.get_json() or {}).get("invoice", {}).get("id")
r = client.get("/api/invoices/{}".format(ft_id), headers=HW)
ft_inv = (r.get_json() or {}).get("invoice", {})
ft_line = (ft_inv.get("lines") or [{}])[0]
check("typetosearch: a free-text line (no item id) saves and reloads "
      "with its typed name",
      ft_line.get("description") == "Hand Typed Gadget"
      and ft_line.get("itemId") is None and ft_line.get("qty") == 3,
      json.dumps(ft_inv)[:240])

# ---------------- 12p. round-3: Home numbers agree + PDF item table -----
# Qudoos's phone check of .18 (2026-11-10): Home showed PROFIT (THIS
# MONTH) Rs 800 beside TODAY'S SALES Rs 0.00, and his printed bill S-1
# carried no item name, price or amount — only the qty. Fresh account,
# controlled data: item purchase price Rs 40 with opening stock 10;
# one cash sale TODAY, 2 x Rs 50 -> T = 10000, COGS = 8000, M = 2000.
tok_r3 = _signup_from_ip("round3@example.com", "198.51.100.77", "r3pass")
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": "round3@example.com"})
HR3 = H(tok_r3)
r = client.post("/api/items", headers=HR3, json={
    "name": "Round3 Widget", "sale_price_paisa": 5000,
    "purchase_price_paisa": 4000, "stock_qty": 10})
r3_item = r.get_json()["item"]["id"]
r = client.post("/api/invoices", headers=HR3, json={
    "type": "sale", "paid_paisa": 10000,
    "lines": [{"item_id": r3_item, "qty": 2, "rate_paisa": 5000}]})
r3_inv = r.get_json().get("invoice", {})
dash3 = client.get("/api/dashboard", headers=HR3).get_json()["dashboard"]
series3 = {p["date"]: p["amountPaisa"] for p in dash3["salesSeries"]}
today3 = _dt.now(_tz.utc).strftime("%Y-%m-%d")
check(
    "round3 A: one sale today counts the same everywhere on Home "
    "(today = month = graph point = 10000)",
    dash3["todaySalesPaisa"] == 10000 and dash3["monthSalesPaisa"] == 10000
    and series3.get(today3) == 10000,
    json.dumps(dash3)[:300],
)
pnl3 = client.get("/api/pnl", headers=HR3).get_json()["pnl"]
check(
    "round3 A: Home PROFIT is the sale's margin and equals P&L net "
    "(profit 2000, cogs 8000; figures ride in the dashboard payload)",
    dash3["monthProfitPaisa"] == 2000 and dash3["monthExpensesPaisa"] == 0
    and pnl3["profitPaisa"] == 2000 and pnl3["cogsPaisa"] == 8000
    and pnl3["salesPaisa"] == 10000,
    "dash_profit={} pnl={}".format(dash3.get("monthProfitPaisa"),
                                   json.dumps(pnl3)),
)
bwp3 = client.get("/api/reports/bill-wise-profit", headers=HR3).get_json()
check(
    "round3 A: Bill Wise Profit tells the same story (total 2000)",
    bwp3.get("totalProfitPaisa") == 2000,
    json.dumps(bwp3)[:200],
)

# Defect B: the printed bill always carries the fixed customer columns.
r = client.post("/api/invoices", headers=HR3, json={
    "type": "sale", "paid_paisa": 1500,
    "lines": [{"description": "Freight and handling charges", "qty": 1,
               "rate_paisa": 1500}]})
r3_inv2 = r.get_json().get("invoice", {})
pdf3 = client.get("/api/invoices/{}/pdf".format(r3_inv["id"]),
                  headers=HR3).get_data()
txt3 = pdf3.decode("latin-1")
check(
    "round3 B: sale PDF prints item name, Price/Unit, rate and amount; "
    "Batch/Serial/Exp columns are gone",
    "Round3 Widget" in txt3 and "Price/Unit" in txt3
    and "Rs 50.00" in txt3 and "Rs 100.00" in txt3
    and "Batch No" not in txt3 and "Serial No" not in txt3
    and "Exp Date" not in txt3,
    txt3[:400],
)
pdf3b = client.get("/api/invoices/{}/pdf".format(r3_inv2["id"]),
                   headers=HR3).get_data()
txt3b = pdf3b.decode("latin-1")
check(
    "round3 B: a free-text line prints its typed name and amount",
    "Freight and handling charges" in txt3b and "Rs 15.00" in txt3b,
    txt3b[:300],
)
r = client.post("/api/items", headers=HR3, json={
    "name": "Super Deluxe Refrigerator Double Door Frost Free 260 Litre",
    "sale_price_paisa": 700, "purchase_price_paisa": 500, "stock_qty": 3})
r3_long = r.get_json()["item"]["id"]
r = client.post("/api/invoices", headers=HR3, json={
    "type": "sale", "paid_paisa": 700,
    "lines": [{"item_id": r3_long, "qty": 1, "rate_paisa": 700}]})
r3_inv3 = r.get_json().get("invoice", {})
pdf3c = client.get("/api/invoices/{}/pdf".format(r3_inv3["id"]),
                   headers=HR3).get_data()
txt3c = pdf3c.decode("latin-1")
check(
    "round3 B: a long item name wraps and prints in FULL (tail visible)",
    "Super Deluxe Refrigerator" in txt3c and "260 Litre" in txt3c,
    txt3c[:300],
)

# ---------------- 12q. build .20: the owner's recording defects -------
# Qudoos's own Vyapar recordings + his spoken defect list (2026-11-11).
# D1 today's sales are their own figure, consistent everywhere; D2
# profit separate and equal to P&L; D3 money out (unpaid purchases +
# standalone payments-out) visible in You'll Pay; D4 balances go
# NEGATIVE for advances and are never clamped; D5 line numbers on the
# thermal bill too; D6 Payment-In / Payment-Out are separate popups.
tok_q = _signup_from_ip("build20@example.com", "198.51.100.120", "b20pass")
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": "build20@example.com"})
HQ = H(tok_q)
r = client.post("/api/items", headers=HQ, json={
    "name": "B20 Widget", "sale_price_paisa": 200000,
    "purchase_price_paisa": 120000, "stock_qty": 10})
q_item = r.get_json()["item"]["id"]

# D4: an advance Payment-Out to a ZERO-balance party reads MINUS.
r = client.post("/api/parties", headers=HQ, json={"name": "B20 Zero"})
q_zero = r.get_json()["party"]["id"]
client.post("/api/payments", headers=HQ, json={
    "direction": "out", "party_id": q_zero, "amount_paisa": 50000,
    "mode": "cash"})
q_parties = {p["id"]: p for p in
             client.get("/api/parties", headers=HQ).get_json()["parties"]}
q_led = client.get("/api/parties/{}/ledger".format(q_zero),
                   headers=HQ).get_json()
q_dash = client.get("/api/dashboard", headers=HQ).get_json()["dashboard"]
check(
    "build20 D4: advance Payment-Out to a zero-balance party = MINUS "
    "50000 in the party list AND the ledger, counted in You'll Receive",
    q_parties[q_zero]["balancePaisa"] == -50000
    and q_led["balancePaisa"] == -50000
    and q_dash["totalReceivablePaisa"] == 50000
    and (q_dash.get("topReceivableParty") or {}).get("amountPaisa") == 50000,
    json.dumps({"party": q_parties.get(q_zero),
                "totalPayablePaisa": q_dash.get("totalPayablePaisa")})[:300],
)

# D3: unpaid purchase + standalone payment-out both land in You'll Pay.
r = client.post("/api/parties", headers=HQ, json={"name": "B20 Supplier"})
q_sup = r.get_json()["party"]["id"]
client.post("/api/invoices", headers=HQ, json={
    "type": "purchase", "party_id": q_sup,
    "lines": [{"item_id": q_item, "qty": 1, "rate_paisa": 100000}]})
client.post("/api/payments", headers=HQ, json={
    "direction": "out", "party_id": q_sup, "amount_paisa": 30000,
    "mode": "cash"})
q_parties = {p["id"]: p for p in
             client.get("/api/parties", headers=HQ).get_json()["parties"]}
q_dash = client.get("/api/dashboard", headers=HQ).get_json()["dashboard"]
check(
    "build20 D3: unpaid purchase 100000 - payment-out 30000 = You'll "
    "Pay 70000, inside the dashboard payload (totalPayablePaisa)",
    q_parties[q_sup]["balancePaisa"] == 70000
    and q_dash["totalPayablePaisa"] == 70000
    and (q_dash.get("topPayableParty") or {}).get("name") == "B20 Supplier"
    and (q_dash.get("topPayableParty") or {}).get("amountPaisa") == 70000,
    json.dumps(q_dash)[:300],
)

# D4 other side: an overpaid customer is an advance WE hold (+), also
# never clamped to zero.
r = client.post("/api/parties", headers=HQ, json={"name": "B20 Customer"})
q_cus = r.get_json()["party"]["id"]
client.post("/api/invoices", headers=HQ, json={
    "type": "sale", "party_id": q_cus, "paid_paisa": 10000,
    "lines": [{"item_id": q_item, "qty": 1, "rate_paisa": 10000}]})
client.post("/api/payments", headers=HQ, json={
    "direction": "in", "party_id": q_cus, "amount_paisa": 5000,
    "mode": "cash"})
q_parties = {p["id"]: p for p in
             client.get("/api/parties", headers=HQ).get_json()["parties"]}
check(
    "build20 D4: an overpaid customer's advance reads +5000 (we hold "
    "their money), not clamped to zero",
    q_parties[q_cus]["balancePaisa"] == 5000,
    json.dumps(q_parties.get(q_cus))[:200],
)

# D1 + D2 on a second fresh account, exact figures: one unpaid sale
# today, price 200000, cost 120000 -> today = month = graph = book =
# 200000; Home profit = P&L profit = 80000.
tok_q2 = _signup_from_ip("build20b@example.com", "198.51.100.121", "b20pass")
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": "build20b@example.com"})
HQ2 = H(tok_q2)
r = client.post("/api/items", headers=HQ2, json={
    "name": "B20 Gadget", "sale_price_paisa": 200000,
    "purchase_price_paisa": 120000, "stock_qty": 5})
q2_item = r.get_json()["item"]["id"]
r = client.post("/api/invoices", headers=HQ2, json={
    "type": "sale",
    "lines": [{"item_id": q2_item, "qty": 1, "rate_paisa": 200000},
              {"description": "B20 Cable", "qty": 1, "rate_paisa": 0}]})
q2_inv = r.get_json().get("invoice", {})
dash_q2 = client.get("/api/dashboard", headers=HQ2).get_json()["dashboard"]
series_q2 = {p["date"]: p["amountPaisa"] for p in dash_q2["salesSeries"]}
today_q2 = _dt.now(_tz.utc).strftime("%Y-%m-%d")
book_q2 = sum(i["totalPaisa"] for i in
              client.get("/api/invoices", headers=HQ2)
              .get_json()["invoices"] if i["type"] == "sale")
pnl_q2 = client.get("/api/pnl", headers=HQ2).get_json()["pnl"]
check(
    "build20 D1/D2: today's sale is one consistent figure (today = "
    "month = graph point = Sale book = 200000) and Home profit = P&L "
    "profit = 80000 (COGS-based)",
    dash_q2["todaySalesPaisa"] == 200000
    and dash_q2["monthSalesPaisa"] == 200000
    and series_q2.get(today_q2) == 200000 and book_q2 == 200000
    and dash_q2["monthProfitPaisa"] == 80000
    and pnl_q2["profitPaisa"] == 80000 and pnl_q2["cogsPaisa"] == 120000,
    json.dumps({"dash": dash_q2, "pnl": pnl_q2})[:300],
)

# D5: the thermal bill numbers its lines like the A4 bill does.
th_q2 = client.get("/api/invoices/{}/pdf?format=thermal".format(q2_inv["id"]),
                   headers=HQ2).get_data().decode("latin-1")
check(
    "build20 D5: thermal PDF lines are numbered 1..N",
    "1. B20 Gadget" in th_q2 and "2. B20 Cable" in th_q2,
    th_q2[:300],
)

# D6: two separate popups; the merged Direction form is gone; the
# payments screen keeps its panel + Add button (settled markup).
q_html = client.get("/").get_data(as_text=True)
q_js = client.get("/static/app.js").get_data(as_text=True)
check(
    "build20 D6: Payment-In and Payment-Out are separate dialogs "
    "(own ids/titles, LINK PAYMENT, BAL: line); no shared Direction "
    "form remains",
    'id="payInModal"' in q_html and 'id="payOutModal"' in q_html
    and ">Payment-In</h2>" in q_html and ">Payment-Out</h2>" in q_html
    and "LINK PAYMENT" in q_html and 'id="paymentPanel"' in q_html
    and 'id="btnAddPayment"' in q_html
    and 'id="payDirection"' not in q_html
    and 'id="paymentForm"' not in q_html
    and "openPayModal" in q_js and "payModalSettle" in q_js
    and "BAL: " in q_js,
)

# Party additional field with Show In Print ON actually prints.
client.put("/api/settings", headers=HQ, json={"settings": {
    "party_extra_fields": [
        {"label": "B20 Field", "enabled": True, "show_in_print": True},
        {"label": "B20 Hidden", "enabled": True, "show_in_print": False}]}})
r = client.post("/api/parties", headers=HQ, json={
    "name": "B20 Print Party",
    "extras": {"B20 Field": "B20-VALUE", "B20 Hidden": "B20-SECRET"}})
q_pp = r.get_json()["party"]["id"]
r = client.post("/api/invoices", headers=HQ, json={
    "type": "sale", "party_id": q_pp,
    "lines": [{"item_id": q_item, "qty": 1, "rate_paisa": 10000}]})
q_pp_inv = r.get_json().get("invoice", {})
q_pp_txt = client.get("/api/invoices/{}/pdf".format(q_pp_inv["id"]),
                      headers=HQ).get_data().decode("latin-1")
check(
    "build20 parity: a party field with Show In Print ON prints on "
    "the bill; one with it OFF does not",
    "B20 Field: B20-VALUE" in q_pp_txt and "B20-SECRET" not in q_pp_txt,
    q_pp_txt[:300],
)

# Stock Transfer honours its Transfer Date.
r = client.post("/api/godowns", headers=HQ2,
                json={"name": "B20 Store B", "type": "Godown"})
q2_godown = (r.get_json() or {}).get("godown", {}).get("id")
main_q2 = [g for g in client.get("/api/godowns", headers=HQ2)
           .get_json()["godowns"] if g["id"] != q2_godown][0]
r = client.post("/api/stock-transfers", headers=HQ2, json={
    "item_id": q2_item, "qty": 1, "from_godown_id": main_q2["id"],
    "to_godown_id": q2_godown, "date": "2026-11-05"})
q2_tr = (r.get_json() or {}).get("transfer", {})
check(
    "build20 parity: a stock transfer is dated on its Transfer Date",
    r.status_code == 201
    and (q2_tr.get("createdAt") or "").startswith("2026-11-05"),
    json.dumps(q2_tr)[:200],
)

# ---------------- 12r. build .20 D7: one account per shop --------------
# The owner's model (2026-11-11 21:00): each SHOP gets its OWN
# account holding only that shop's complete data; the owner account
# sits above them all (approval gate). Prove the isolation
# end-to-end, prove a new account starts pending, and prove the
# in-app entry point exists.
tok_a = _signup_from_ip("shopa@example.com", "198.51.100.130", "shopapass")
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": "shopa@example.com"})
HA = H(tok_a)
tok_b = _signup_from_ip("shopb@example.com", "198.51.100.131", "shopbpass")
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": "shopb@example.com"})
HB = H(tok_b)
r = client.post("/api/items", headers=HA, json={
    "name": "Iso A Item", "sale_price_paisa": 11000,
    "purchase_price_paisa": 7000, "stock_qty": 4})
ia_item = r.get_json()["item"]["id"]
r = client.post("/api/parties", headers=HA, json={"name": "Iso A Party"})
ia_party = r.get_json()["party"]["id"]
client.post("/api/invoices", headers=HA, json={
    "type": "sale", "party_id": ia_party,
    "lines": [{"item_id": ia_item, "qty": 1, "rate_paisa": 11000}]})
r = client.post("/api/items", headers=HB, json={
    "name": "Iso B Item", "sale_price_paisa": 22000,
    "purchase_price_paisa": 9000, "stock_qty": 4})
ib_item = r.get_json()["item"]["id"]
r = client.post("/api/parties", headers=HB, json={"name": "Iso B Party"})
ib_party = r.get_json()["party"]["id"]
client.post("/api/invoices", headers=HB, json={
    "type": "sale", "party_id": ib_party,
    "lines": [{"item_id": ib_item, "qty": 1, "rate_paisa": 22000}]})
a_parties = client.get("/api/parties", headers=HA).get_json()["parties"]
b_parties = client.get("/api/parties", headers=HB).get_json()["parties"]
a_items = client.get("/api/items", headers=HA).get_json()["items"]
b_items = client.get("/api/items", headers=HB).get_json()["items"]
a_invs = client.get("/api/invoices", headers=HA).get_json()["invoices"]
b_invs = client.get("/api/invoices", headers=HB).get_json()["invoices"]
a_dash = client.get("/api/dashboard", headers=HA).get_json()["dashboard"]
b_dash = client.get("/api/dashboard", headers=HB).get_json()["dashboard"]
check(
    "build20 D7: two shop accounts are fully isolated — each sees "
    "only its own parties, items, invoices and dashboard figures",
    [p["name"] for p in a_parties] == ["Iso A Party"]
    and [p["name"] for p in b_parties] == ["Iso B Party"]
    and [i["name"] for i in a_items] == ["Iso A Item"]
    and [i["name"] for i in b_items] == ["Iso B Item"]
    and len(a_invs) == 1 and a_invs[0]["totalPaisa"] == 11000
    and len(b_invs) == 1 and b_invs[0]["totalPaisa"] == 22000
    and a_dash["monthSalesPaisa"] == 11000
    and b_dash["monthSalesPaisa"] == 22000
    and a_dash["totalReceivablePaisa"] == 11000
    and b_dash["totalReceivablePaisa"] == 22000,
    json.dumps({"a": a_dash, "b": b_dash})[:240],
)
tok_c = _signup_from_ip("shopc@example.com", "198.51.100.132", "shopcpass")
HC = H(tok_c)
r = client.get("/api/dashboard", headers=HC)
pend_403 = r.status_code == 403
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": "shopc@example.com"})
r = client.get("/api/dashboard", headers=HC)
check(
    "build20 D7: a newly created shop account starts PENDING (data "
    "routes refuse 403) until the owner approves it, then it opens",
    pend_403 and r.status_code == 200,
    "before={} after={}".format(pend_403, r.status_code),
)
d7_html = client.get("/").get_data(as_text=True)
d7_js = client.get("/static/app.js").get_data(as_text=True)
check(
    "build20 D7: the owner's Customer accounts card carries the "
    "'Add a shop account' entry point + the per-shop explainer; the "
    "auth Create panel carries the same explainer; the button is "
    "wired to the existing create flow",
    'id="btnAddShopAccount"' in d7_html
    and "Add a shop account" in d7_html
    and "Each shop gets its own separate account" in d7_html
    and "One account per shop" in d7_html
    and "btnAddShopAccount" in d7_js
    and 'showAuthStep("stepOne")' in d7_js,
)

# ------------------------------------------- 12s. build .21 fix round --
# Fix round .21 (owner's laptop check of .20, 2026-11-13):
# (A) popups sit on a dark DIM (the frosted blur is gone) in a compact
#     bordered box with a corner X close;
# (B) strict email validation + provider typo guard on the auth forms;
# (C) click feedback + a working indicator while requests run.
b21_css = client.get("/static/styles.css").get_data(as_text=True)
b21_js = client.get("/static/app.js").get_data(as_text=True)
b21_html = client.get("/").get_data(as_text=True)
# The no-blur rule guards the MODAL overlay only. Build .26 put a
# frosted backdrop blur on the SIDEBAR drawer (owner's direct order,
# a different element) — so this assertion slices the .modal block
# instead of the whole stylesheet; the popup rule itself is unchanged.
_b21_modal = b21_css[b21_css.index(".modal {"):
                     b21_css.index("}", b21_css.index(".modal {"))]
check(
    "build21 A: modal overlay is a dark dim with NO backdrop blur; the "
    "dialog box is compact with the light-blue border; corner X "
    "buttons exist on the Payment-In / Payment-Out / search popups",
    "backdrop-filter" not in _b21_modal
    and "rgba(0, 0, 0, .5)" in _b21_modal
    and "rgba(0, 0, 0, .5)" in b21_css
    and "border: 2px solid #7fb3d5" in b21_css
    and "max-width: 520px" in b21_css
    and 'id="piCloseX"' in b21_html and 'id="poCloseX"' in b21_html
    and 'id="searchCloseX"' in b21_html
    and "CloseX" in b21_js,
)
check(
    "build21 C: click feedback (:active + .click-flash) and the "
    "working indicator (#netBar + body.net-busy fetch wrapper) ship "
    "in the shell; the strict auth email check is mirrored in app.js",
    ".click-flash" in b21_css and "#netBar" in b21_css
    and "click-flash" in b21_js and "net-busy" in b21_js
    and "authEmailError" in b21_js and "did you mean" in b21_js,
)
B21_BAD = [
    "shop21@gmal.com", "shop21@gmial.com", "shop21@gmail.con",
    "shop21@gmail.cm", "shop21@hotmai.com", "shop21@yahooo.com",
    "shop21gmail.com", "shop21@@gmail.com", "shop21 @gmail.com",
    "shop21@gmail", "shop21@gmail.c",
]
for bad in B21_BAD:
    r = client.post("/api/auth/request-otp", json={"email": bad})
    check(
        "build21 B: request-otp rejects {!r}".format(bad),
        r.status_code == 400,
        r.get_data(as_text=True)[:120],
    )
r = client.post("/api/auth/request-otp", json={"email": "typo21@gmal.com"})
check(
    "build21 B: a gmail typo is rejected with the corrected suggestion",
    r.status_code == 400
    and "did you mean typo21@gmail.com?" in (r.get_json() or {}).get("error", ""),
    r.get_data(as_text=True)[:160],
)
r = client.post(
    "/api/auth/login", json={"email": "typo21@gmial.com", "password": "x123"}
)
check(
    "build21 B: login stops a typo email before any password check",
    r.status_code == 400
    and "did you mean typo21@gmail.com?" in (r.get_json() or {}).get("error", ""),
    r.get_data(as_text=True)[:160],
)
for good in ["valid21@gmail.com", "valid21@yahoo.com", "info@alghaffar.pk"]:
    r = client.post("/api/auth/request-otp", json={"email": good})
    check(
        "build21 B: request-otp accepts " + good,
        r.status_code == 200 and (r.get_json() or {}).get("ok") is True,
        r.get_data(as_text=True)[:120],
    )

# ---------------- 12t. build .22: QRLedger rebrand + research P0 ------
# (A) The product is QRLedger now: shell, manifest and the OTP email
#     carry the new name; the version/cache moved to vyapar11/v20
#     (and to vyapar12/v21 in build .23).
# (B1) Every SALE bill prints the khata balance block:
#     Previous Balance + This Bill - Paid = Total Payable (paisa-exact,
#     in "party owes us" space — the negation of party_balance_map).
# (B2) Business Profile payment rails (Raast / JazzCash / Easypaisa +
#     free-text) print as a "Pay To:" block on PDF + thermal bills.
# (B3) A thermal print can name its roll width (58mm / 80mm).
# (B4) Backup status endpoint: last backup time + emailed flag.
# (B5) NTN / STRN print on bills when filled; the tax-inclusive
#     toggle keeps invoice math paisa-exact in BOTH modes.
tok_22 = _signup_from_ip("shop22@example.com", "198.51.100.222", "shop-22")
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": "shop22@example.com"})
H22 = H(tok_22)

shell22 = client.get("/").get_data(as_text=True)
mani22 = client.get("/manifest.webmanifest").get_json()
check(
    "build22 A: the shell is QRLedger (title, log-in, activation) and "
    "the PWA manifest carries the new name",
    "<title>QRLedger — Billing for Shopkeepers</title>" in shell22
    and "Log in to QRLedger" in shell22
    and "Activate your QRLedger account" in shell22
    and "QBill" not in shell22
    and mani22.get("name") == "QRLedger — Billing for Shopkeepers"
    and mani22.get("short_name") == "QRLedger",
)
r = client.post("/api/auth/request-otp", json={"email": "otp22@example.com"})
subj22 = [m["subject"] for m in mailer.DEV_OUTBOX
          if m["to"] == "otp22@example.com"]
check(
    "build22 A: the login-code email is branded QRLedger",
    subj22 and subj22[-1] == "Your QRLedger login code",
    json.dumps(subj22),
)
r = client.get("/static/icons/icon-512.png")
r2 = client.get("/static/icons/logo-full.png")
check(
    "build22 A: the new brand assets serve (icon + full logo)",
    r.status_code == 200 and r.get_data()[:8] == b"\x89PNG\r\n\x1a\n"
    and r2.status_code == 200 and r2.get_data()[:8] == b"\x89PNG\r\n\x1a\n",
)

# Profile: a fresh account's new fields are EMPTY (per-account
# isolation — never seeded with anyone's real details).
prof22 = client.get("/api/business", headers=H22).get_json()["business"]
check(
    "build22 B2/B5: a new account's NTN/STRN and payment fields start empty",
    prof22.get("ntn") == "" and prof22.get("strn") == ""
    and prof22.get("raastIban") == "" and prof22.get("jazzcashNumber") == ""
    and prof22.get("easypaisaNumber") == ""
    and prof22.get("paymentDetails") == "",
    json.dumps(prof22),
)
r = client.get("/api/settings", headers=H22)
check(
    "build22 B5: tax_inclusive defaults OFF on a new account",
    (r.get_json().get("settings") or {}).get("tax_inclusive") is False,
)
r = client.put("/api/business", headers=H22, json={
    "name": "B22 Test Shop",
    "ntn": "1234567-8", "strn": "01-2345678-001-90",
    "raast_iban": "PK36TEST0000000000000001",
    "jazzcash_number": "03001234567", "easypaisa_number": "03451234567",
    "payment_details": "Account Title: B22 Test\nBranch: Main Bazaar",
})
prof22 = (r.get_json() or {}).get("business", {})
check(
    "build22 B2/B5: profile saves NTN/STRN + payment rails (round-trip)",
    r.status_code == 200 and prof22.get("ntn") == "1234567-8"
    and prof22.get("strn") == "01-2345678-001-90"
    and prof22.get("raastIban") == "PK36TEST0000000000000001"
    and prof22.get("jazzcashNumber") == "03001234567"
    and prof22.get("easypaisaNumber") == "03451234567"
    and prof22.get("paymentDetails", "").startswith("Account Title: B22 Test"),
    json.dumps(prof22),
)


def _b22_sale(party_id, lines, **kw):
    payload = {"type": "sale", "lines": lines}
    if party_id is not None:
        payload["party_id"] = party_id
    payload.update(kw)
    rr = client.post("/api/invoices", headers=H22, json=payload)
    assert rr.status_code == 201, rr.get_data(as_text=True)
    return rr.get_json()["invoice"]


def _b22_pdf(inv_id, query=""):
    return client.get(
        "/api/invoices/{}/pdf{}".format(inv_id, query), headers=H22
    ).get_data().decode("latin-1")


r = client.post("/api/parties", headers=H22, json={"name": "B22 Customer"})
p22a = r.get_json()["party"]["id"]
inv22a = _b22_sale(p22a, [{"description": "B22 Widget", "qty": 2,
                           "rate_paisa": 50000}], paid_paisa=40000)
txt22a = _b22_pdf(inv22a["id"])
check(
    "build22 B1: first bill's block — Previous Rs 0.00 + This Bill "
    "Rs 1,000.00 - Paid Rs 400.00 = Total Payable Rs 600.00",
    "Balance Summary" in txt22a and "Previous Balance" in txt22a
    and "This Bill" in txt22a and "Total Payable" in txt22a
    and "Rs 1,000.00" in txt22a and "Rs 600.00" in txt22a,
    txt22a[:300],
)
inv22b = _b22_sale(p22a, [{"description": "B22 Cable", "qty": 1,
                           "rate_paisa": 30000}])
txt22b = _b22_pdf(inv22b["id"])
check(
    "build22 B1: second bill carries the first bill's due forward "
    "(Total Payable Rs 900.00)",
    "Total Payable" in txt22b and "Rs 900.00" in txt22b
    and "Rs 300.00" in txt22b,
    txt22b[:300],
)
# Advance case: money OUT to the party first (they owe us for it),
# then a fully-paid bill — the block still balances to the paisa.
r = client.post("/api/parties", headers=H22, json={"name": "B22 Advance"})
p22b = r.get_json()["party"]["id"]
client.post("/api/payments", headers=H22, json={
    "party_id": p22b, "direction": "out", "amount_paisa": 20000})
inv22c = _b22_sale(p22b, [{"description": "B22 Lamp", "qty": 1,
                           "rate_paisa": 15000}], paid_paisa=15000)
txt22c = _b22_pdf(inv22c["id"])
party22b = [p for p in client.get("/api/parties", headers=H22)
            .get_json()["parties"] if p["id"] == p22b][0]
check(
    "build22 B1: advance-out case — block total = the advance still "
    "owed (Rs 200.00) and the party balance stays MINUS 20000",
    "Total Payable" in txt22c and "Rs 200.00" in txt22c
    and party22b["balancePaisa"] == -20000,
    txt22c[:300],
)
# Customer-advance case: a big Payment-In first — the total payable
# goes NEGATIVE (we hold their money) and prints signed.
r = client.post("/api/parties", headers=H22, json={"name": "B22 Prepaid"})
p22c = r.get_json()["party"]["id"]
client.post("/api/payments", headers=H22, json={
    "party_id": p22c, "direction": "in", "amount_paisa": 50000})
inv22d = _b22_sale(p22c, [{"description": "B22 Fan", "qty": 1,
                           "rate_paisa": 10000}])
txt22d = _b22_pdf(inv22d["id"])
check(
    "build22 B1: prepaid case — Total Payable prints signed "
    "(Rs -400.00: we hold the party's advance)",
    "Total Payable" in txt22d and "Rs -400.00" in txt22d,
    txt22d[:300],
)
inv22e = _b22_sale(None, [{"description": "B22 Cash", "qty": 1,
                           "rate_paisa": 5000}])
check(
    "build22 B1: a cash sale (no party) prints NO balance block",
    "Balance Summary" not in _b22_pdf(inv22e["id"]),
)
r = client.post("/api/invoices", headers=H22, json={
    "type": "purchase", "party_id": p22a,
    "lines": [{"description": "B22 Stock", "qty": 1, "rate_paisa": 7000}]})
inv22f = (r.get_json() or {}).get("invoice", {})
check(
    "build22 B1: a purchase bill prints NO sale balance block",
    r.status_code == 201 and "Balance Summary" not in _b22_pdf(inv22f["id"]),
    r.get_data(as_text=True)[:200],
)
check(
    "build22 B2/B5: Pay To block + NTN/STRN print on the A4 bill",
    "Pay To:" in txt22a and "Raast IBAN: PK36TEST0000000000000001" in txt22a
    and "JazzCash: 03001234567" in txt22a
    and "Easypaisa: 03451234567" in txt22a
    and "Account Title: B22 Test" in txt22a
    and "NTN: 1234567-8" in txt22a
    and "STRN: 01-2345678-001-90" in txt22a,
    txt22a[:300],
)
th22 = _b22_pdf(inv22a["id"], "?format=thermal")
check(
    "build22 B1/B2/B5: the thermal bill carries the block, Pay To "
    "and NTN too",
    "Balance Summary" in th22 and "Total Payable" in th22
    and "Pay To:" in th22 and "JazzCash: 03001234567" in th22
    and "NTN: 1234567-8" in th22,
    th22[:300],
)


def _b22_mediabox(query):
    raw = client.get("/api/invoices/{}/pdf{}".format(inv22a["id"], query),
                     headers=H22).get_data()
    m = re.search(rb"/MediaBox \[ 0 0 (\d+)", raw)
    return int(m.group(1)) if m else None


check(
    "build22 B3: thermal width choice — 58mm renders a 164pt roll, "
    "80mm a 226pt roll, a bogus size falls back to the setting",
    _b22_mediabox("?format=thermal&size=58mm") == 164
    and _b22_mediabox("?format=thermal&size=80mm") == 226
    and _b22_mediabox("?format=thermal") == 226
    and _b22_mediabox("?format=thermal&size=42mm") == 226,
)
client.put("/api/settings", headers=H22,
           json={"settings": {"thermal_size": "58mm"}})
check(
    "build22 B3: the saved PRINT thermal_size rules when no size "
    "is chosen at print time",
    _b22_mediabox("?format=thermal") == 164,
)
client.put("/api/settings", headers=H22,
           json={"settings": {"thermal_size": "80mm"}})

# Backup status: nothing yet, then a manual run flips it.
tok_22b = _signup_from_ip("shop22b@example.com", "198.51.100.223", "shop-22b")
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": "shop22b@example.com"})
H22B = H(tok_22b)
st22 = client.get("/api/backups/status", headers=H22B).get_json()
r = client.post("/api/backups/run", headers=H22B)
st22b = client.get("/api/backups/status", headers=H22B).get_json()
check(
    "build22 B4: backup status starts empty, then reports the run "
    "(time + emailed + count)",
    st22.get("lastBackupAt") is None and st22.get("backupCount") == 0
    and r.status_code == 201
    and st22b.get("backupCount") == 1
    and bool(st22b.get("lastBackupAt"))
    and st22b.get("lastEmailed") is True,
    json.dumps({"before": st22, "after": st22b}),
)
r = client.post("/api/invoices", headers=H22B, json={
    "type": "sale",
    "lines": [{"description": "B22 Plain", "qty": 1, "rate_paisa": 5000}]})
# (party-less on the empty-profile account keeps the PDF clean either way)
inv22h = (r.get_json() or {}).get("invoice", {})
txt22h = client.get("/api/invoices/{}/pdf".format(inv22h["id"]),
                    headers=H22B).get_data().decode("latin-1")
check(
    "build22 B2/B5: an empty profile prints NO Pay To block and "
    "NO NTN/STRN on the bill",
    r.status_code == 201 and "Pay To:" not in txt22h
    and "NTN:" not in txt22h and "STRN:" not in txt22h,
    txt22h[:300],
)

# Tax math — exclusive baseline first (the default mode).
inv22x = _b22_sale(p22a, [{"description": "B22 Taxed", "qty": 1,
                           "rate_paisa": 10000, "tax_bp": 1800}])
check(
    "build22 B5: EXCLUSIVE 18% — Rs 100.00 line carries Rs 18.00 tax "
    "added on top (total Rs 118.00)",
    inv22x["taxPaisa"] == 1800 and inv22x["totalPaisa"] == 11800,
    json.dumps(inv22x)[:200],
)
client.put("/api/settings", headers=H22,
           json={"settings": {"tax_inclusive": True}})
inv22i = _b22_sale(p22a, [{"description": "B22 Taxed", "qty": 1,
                           "rate_paisa": 11800, "tax_bp": 1800}])
check(
    "build22 B5: INCLUSIVE 18% — a Rs 118.00 rate CONTAINS Rs 18.00 "
    "tax; the total stays Rs 118.00 (never added twice)",
    inv22i["taxPaisa"] == 1800 and inv22i["totalPaisa"] == 11800
    and inv22i["lines"][0]["taxPaisa"] == 1800,
    json.dumps(inv22i)[:240],
)
inv22j = _b22_sale(p22a, [{"description": "B22 Taxed", "qty": 2,
                           "rate_paisa": 11800, "discount_bp": 1000,
                           "tax_bp": 1800}])
check(
    "build22 B5: INCLUSIVE with 10% discount — net Rs 212.40 "
    "contains exactly Rs 32.40 tax; total = the net itself",
    inv22j["discountPaisa"] == 2360 and inv22j["taxPaisa"] == 3240
    and inv22j["totalPaisa"] == 21240,
    json.dumps(inv22j)[:240],
)
inv22k = _b22_sale(p22a, [{"description": "B22 Taxed", "qty": 1,
                           "rate_paisa": 11800, "tax_paisa": 1800}])
r = client.post("/api/invoices", headers=H22, json={
    "type": "sale", "party_id": p22a,
    "lines": [{"description": "B22 Taxed", "qty": 1, "rate_paisa": 11800,
               "tax_bp": 1800}],
    "paid_paisa": 11801})
check(
    "build22 B5: INCLUSIVE — a directly-given tax amount is a "
    "portion, not an addition (total Rs 118.00); paying MORE than "
    "the inclusive total is refused",
    inv22k["taxPaisa"] == 1800 and inv22k["totalPaisa"] == 11800
    and r.status_code == 400,
    r.get_data(as_text=True)[:160],
)
txt22i = _b22_pdf(inv22i["id"])
check(
    "build22 B5: the inclusive bill labels its tax 'Tax (included)' "
    "and the line amount is the rate itself",
    # PDF literal strings escape parentheses: the bytes carry
    # "Tax \(included\)" for the printed text Tax (included).
    "Tax \\(included\\)" in txt22i and "Rs 118.00" in txt22i,
    txt22i[:300],
)
r = client.put("/api/invoices/{}".format(inv22i["id"]), headers=H22, json={
    "party_id": p22a,
    "lines": [{"description": "B22 Taxed", "qty": 1, "rate_paisa": 23600,
               "tax_bp": 1800}]})
inv22i2 = (r.get_json() or {}).get("invoice", {})
check(
    "build22 B5: INCLUSIVE edit path — Rs 236.00 at 18% contains "
    "Rs 36.00 tax, total stays Rs 236.00",
    r.status_code == 200 and inv22i2.get("taxPaisa") == 3600
    and inv22i2.get("totalPaisa") == 23600,
    r.get_data(as_text=True)[:200],
)
client.put("/api/settings", headers=H22,
           json={"settings": {"tax_inclusive": False}})
inv22y = _b22_sale(p22a, [{"description": "B22 Taxed", "qty": 1,
                           "rate_paisa": 10000, "tax_bp": 1800}])
check(
    "build22 B5: toggling back to EXCLUSIVE restores add-on-top "
    "math exactly",
    inv22y["taxPaisa"] == 1800 and inv22y["totalPaisa"] == 11800,
    json.dumps(inv22y)[:200],
)

# ---------------- 12u. build .24: Edit Firm popup ----------------
# The Business Profile moved out of Settings into the Edit Firm popup
# (owner's Vyapar reference, 2026-11-13): the popup gained a Business
# Description and a signature IMAGE; the tax IDs + payment rails
# (Coming Soon card) and the Tax Note + Terms (own card) stayed in
# Settings > GENERAL, each with its own save. The profile endpoint's
# partial-save contract makes the split saves safe: a save changes
# only the fields it sends.
tok_24 = _signup_from_ip("shop24@example.com", "198.51.100.224", "shop-24")
tok_24b = _signup_from_ip("shop24b@example.com", "198.51.100.225", "shop-24b")
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": "shop24@example.com"})
client.post("/api/admin/accounts/approve", headers=H(owner_token),
            json={"email": "shop24b@example.com"})
H24, H24B = H(tok_24), H(tok_24b)

prof24 = client.get("/api/business", headers=H24).get_json()["business"]
check(
    "build24: a new account's Business Description + signature start empty",
    prof24.get("businessDescription") == ""
    and prof24.get("hasSignature") is False
    and prof24.get("signatureUrl") == "",
    json.dumps(prof24),
)
r = client.get("/api/business/signature", headers=H24)
check("build24: no signature yet -> signature endpoint 404",
      r.status_code == 404)

# The Edit Firm save: the popup's own fields, incl. the description.
r = client.put("/api/business", headers=H24, json={
    "name": "Firm24 Electronics", "owner_name": "Firm Owner",
    "business_type": "Wholesale",
    "business_category": "Electrical & Electronics Equipment",
    "address": "ABC Center, Bunder Road", "city": "Larkana",
    "pincode": "77150", "state": "Sindh", "phone": "03001112223",
    "email": "firm24@example.com", "signature_text": "For Firm24",
    "business_description": "We sell all types of electronics."})
prof24 = (r.get_json() or {}).get("business", {})
check(
    "build24: Edit Firm save round-trips incl. Business Description",
    r.status_code == 200
    and prof24.get("businessDescription") == "We sell all types of electronics."
    and prof24.get("signatureText") == "For Firm24",
    json.dumps(prof24),
)


def _mk_inv24(tok):
    p = client.post("/api/parties", headers=H(tok),
                    json={"name": "Walk-in 24"}).get_json()["party"]["id"]
    i = client.post("/api/items", headers=H(tok), json={
        "name": "Thing 24", "sale_price_paisa": 10000,
        "stock_qty": 5}).get_json()["item"]["id"]
    v = client.post("/api/invoices", headers=H(tok), json={
        "type": "sale", "party_id": p,
        "lines": [{"item_id": i, "qty": 1, "rate_paisa": 10000}]})
    return v.get_json()["invoice"]["id"]


inv24 = _mk_inv24(tok_24)
pdf24_before = client.get("/api/invoices/{}/pdf".format(inv24),
                          headers=H24).get_data()
check(
    "build24: before any signature, the A4 bill carries NO image "
    "(layout exactly as before)",
    b"/Subtype /Image" not in pdf24_before
    and b"Authorised Signatory" in pdf24_before,
)

# Signature image: the logo's rules (bad type / oversize refused).
r = client.put("/api/business/signature", headers=H24, json={
    "signature_data": "data:text/html;base64,"
                      "PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="})
check("build24: a non-image signature upload is refused (400)",
      r.status_code == 400)
import base64 as _b64_24
_BIG24 = "data:image/png;base64," + _b64_24.b64encode(
    b"\x89PNG" + b"\x00" * (401 * 1024)).decode()
r = client.put("/api/business/signature", headers=H24,
               json={"signature_data": _BIG24})
check("build24: an oversize signature upload is refused (400)",
      r.status_code == 400)
r = client.put("/api/business/signature", headers=H24,
               json={"signature_data": _LOGO_B})
prof24 = (r.get_json() or {}).get("business", {})
sig24 = client.get("/api/business/signature", headers=H24)
check(
    "build24: signature round-trip (flag set + own bytes back)",
    r.status_code == 200 and prof24.get("hasSignature") is True
    and sig24.status_code == 200
    and sig24.get_data().startswith(b"\x89PNG"),
)
pdf24_after = client.get("/api/invoices/{}/pdf".format(inv24),
                         headers=H24).get_data()
check(
    "build24: the signature image prints in the A4 bill's signature area",
    b"/Subtype /Image" in pdf24_after
    and b"Authorised Signatory" in pdf24_after
    and b"Firm24 Electronics" in pdf24_after,
)
th24 = client.get("/api/invoices/{}/pdf?format=thermal".format(inv24),
                  headers=H24).get_data()
check(
    "build24: the thermal bill has NO signature image (the thermal "
    "layout has no signature area)",
    b"/Subtype /Image" not in th24 and th24.startswith(b"%PDF"),
)
prof24b = client.get("/api/business", headers=H24B).get_json()["business"]
r = client.get("/api/business/signature", headers=H24B)
check(
    "build24: account B sees NONE of A's signature or description",
    prof24b.get("hasSignature") is False
    and prof24b.get("businessDescription") == ""
    and r.status_code == 404,
)
client.put("/api/business/signature", headers=H24,
           json={"signature_data": None})
prof24 = client.get("/api/business", headers=H24).get_json()["business"]
pdf24_gone = client.get("/api/invoices/{}/pdf".format(inv24),
                        headers=H24).get_data()
check(
    "build24: removing the signature restores the image-free bill",
    prof24.get("hasSignature") is False
    and b"/Subtype /Image" not in pdf24_gone,
)

# The Coming Soon card's save: ONLY the payment/tax fields — the
# firm's own fields must survive untouched (partial-save contract).
r = client.put("/api/business", headers=H24, json={
    "ntn": "7654321-0", "strn": "02-7654321-001-90",
    "raast_iban": "PK36FIRM0000000000000001",
    "jazzcash_number": "03001112223", "easypaisa_number": "03451112223",
    "payment_details": "Account Title: Firm24"})
prof24 = (r.get_json() or {}).get("business", {})
check(
    "build24: payment/tax card save round-trips and leaves the firm's "
    "own fields untouched",
    r.status_code == 200 and prof24.get("ntn") == "7654321-0"
    and prof24.get("raastIban") == "PK36FIRM0000000000000001"
    and prof24.get("name") == "Firm24 Electronics"
    and prof24.get("businessDescription") == "We sell all types of electronics.",
    json.dumps(prof24),
)
# The Tax Note & Terms card's save behaves the same way.
r = client.put("/api/business", headers=H24, json={
    "tax_note": "Tax is charged as shown on the bill.",
    "terms": "1. Goods once sold will not be taken back."})
prof24 = (r.get_json() or {}).get("business", {})
check(
    "build24: Tax Note & Terms save round-trips, payment fields untouched",
    r.status_code == 200
    and prof24.get("taxNote") == "Tax is charged as shown on the bill."
    and prof24.get("terms", "").startswith("1. Goods once sold")
    and prof24.get("ntn") == "7654321-0",
    json.dumps(prof24),
)

# ============ 12z. duplicate accounts for one email (build .27) =======
# Owner's live-site report: in CUSTOMER ACCOUNTS the same Gmail showed
# THREE times. The stored rows are case variants of one address —
# lookups used to compare byte-for-byte, so every variant looked like
# a stranger and collected its own row, its own Approve button and its
# own login fate. One email must now show + count ONCE; signup must
# never spawn a twin; OTP verify and classic login must land on the
# PRIMARY row (the one the owner sees); approve/block act on the
# email. The variants are seeded directly — today's API normalizes
# input, so they can only arrive as legacy rows, exactly like the
# owner's production data.
from qbill_app.security import (  # noqa: E402
    after_iso as _after_iso,
    hash_password as _hash_pw,
    now_iso as _now_iso,
    token_hash as _token_hash,
)


def _seed_account(email, approved, password=None, reject=None,
                  created="2026-11-12T00:00:00+00:00"):
    salt = digest = None
    if password:
        salt, digest = _hash_pw(password)
    return db.insert(
        "INSERT INTO site_accounts (email, is_owner, approved, pw_salt, "
        "pw_hash, signup_ip, last_ip, reject_reason, created_at) "
        "VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (email, approved, salt, digest, "198.51.100.99", "198.51.100.99",
         reject, created),
    )


def _rows_of(email):
    return db.query_all(
        "SELECT * FROM site_accounts WHERE lower(trim(email)) = ? "
        "ORDER BY id", (email,))


DUP = "dupshop@example.com"
dup_a = _seed_account("DupShop@Example.com", 1, password="dup42",
                      created="2026-11-10T09:00:00+00:00")
dup_b = _seed_account("dupshop@example.com", 0,
                      created="2026-11-12T18:30:00+00:00")
dup_c = _seed_account("dupshop@Example.com", 0, reject="ip_match",
                      created="2026-11-12T18:31:00+00:00")
r = client.get("/api/admin/accounts", headers=H(owner_token))
payload27 = r.get_json() or {}
accts27 = payload27.get("accounts", [])
dup_entries = [a for a in accts27
               if (a["email"] or "").strip().lower() == DUP]
check(
    "dup: owner list shows the triple-stored email ONCE, as its "
    "primary (the approved, password-holding row)",
    len(dup_entries) == 1
    and dup_entries[0]["email"] == "DupShop@Example.com"
    and dup_entries[0]["approved"] is True,
    json.dumps(dup_entries),
)
check(
    "dup: list totals count DISTINCT emails (total = entries shown; "
    "pending = unapproved entries shown)",
    payload27.get("total") == len(accts27)
    and payload27.get("pending")
    == sum(1 for a in accts27 if not a["approved"]),
    json.dumps({k: payload27.get(k) for k in ("total", "pending")}),
)
r = client.post("/api/auth/login", json={
    "email": "DUPSHOP@example.com", "password": "dup42", "remember": True})
dup_token = (r.get_json() or {}).get("token")
me_dup = (client.get("/api/auth/me", headers=H(dup_token)).get_json()
          if dup_token else {})
check(
    "dup: email+password login (any case) lands on the PRIMARY row — "
    "the approved account the owner sees in his list",
    r.status_code == 200 and bool(dup_token)
    and me_dup.get("email") == "DupShop@Example.com"
    and me_dup.get("approved") is True,
    json.dumps(me_dup),
)
before27 = len(_rows_of(DUP))
r = client.post("/api/auth/request-otp",
                json={"email": "DupShop@EXAMPLE.com"})
code_dup = latest_code(DUP)
check(
    "dup: 'create account' for an existing email spawns NO new row "
    "(group still 3) and is not flagged as a new account",
    r.status_code == 200 and r.get_json().get("isNewAccount") is False
    and before27 == 3 and len(_rows_of(DUP)) == 3,
    r.get_data(as_text=True)[:200],
)
r = client.post("/api/auth/verify-otp",
                json={"email": DUP, "code": code_dup})
dup_otp_token = (r.get_json() or {}).get("token")
me_dup2 = (client.get("/api/auth/me", headers=H(dup_otp_token)).get_json()
           if dup_otp_token else {})
check(
    "dup: OTP verify attaches the session to the SAME primary row",
    bool(dup_otp_token)
    and (r.get_json() or {}).get("hasAppPassword") is True
    and me_dup2.get("email") == "DupShop@Example.com"
    and me_dup2.get("approved") is True,
    json.dumps(me_dup2),
)
PEND = "pendshop@example.com"
pend_a = _seed_account("PendShop@Example.com", 0,
                       created="2026-11-11T09:00:00+00:00")
pend_b = _seed_account("pendshop@example.com", 0, password="pend42",
                       created="2026-11-12T09:00:00+00:00")
r = client.post("/api/admin/accounts/approve", headers=H(owner_token),
                json={"email": "PENDSHOP@EXAMPLE.COM"})
rows_pend = {row["id"]: row for row in _rows_of(PEND)}
r2 = client.get("/api/admin/accounts", headers=H(owner_token))
pend_entries = [a for a in (r2.get_json() or {}).get("accounts", [])
                if (a["email"] or "").strip().lower() == PEND]
check(
    "dup: approving an email approves its PRIMARY (password) row; the "
    "list still shows the email once, now Active — no pending twin",
    r.status_code == 200
    and rows_pend[pend_b]["approved"] == 1
    and len(pend_entries) == 1 and pend_entries[0]["approved"] is True
    and pend_entries[0]["email"] == "pendshop@example.com",
    json.dumps(pend_entries),
)
db.run(
    "INSERT INTO sessions (token_hash, account_id, unlocked, otp_fresh, "
    "expires_at, created_at) VALUES (?, ?, 1, 0, ?, ?)",
    (_token_hash("dup-stray-session"), dup_b, _after_iso(days=1),
     _now_iso()),
)
r = client.post("/api/admin/accounts/block", headers=H(owner_token),
                json={"email": "DupShop@Example.com"})
rows_dup = _rows_of(DUP)
sess_left = db.query_all(
    "SELECT id FROM sessions WHERE account_id IN (?, ?, ?)",
    (dup_a, dup_b, dup_c))
me_stray = client.get("/api/auth/me", headers=H("dup-stray-session"))
me_primary = client.get("/api/auth/me", headers=H(dup_otp_token))
check(
    "dup: blocking an email un-approves EVERY row of it and kills "
    "every session (primary + stray variants alike)",
    r.status_code == 200
    and all(row["approved"] == 0 for row in rows_dup)
    and len(sess_left) == 0
    and me_stray.status_code == 401 and me_primary.status_code == 401,
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
