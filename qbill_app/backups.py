"""Auto-backups: full business snapshots, restore, and email delivery.

Owner's rules (2026-11-09):
- A snapshot is a JSON copy of ALL of one business's data plus its totals.
- Whenever a snapshot is created — by the run endpoint, by the daily
  helper, or by an offline device uploading its local snapshot — the
  server ALSO emails it to the account's login email as an attachment
  ("QBill daily backup — <date>", file qbill-backup-<date>.json), via the
  usual mailer failover, but ONLY when a mail route is configured.
- An email failure NEVER fails or rolls back a backup: the outcome is
  recorded on the snapshot row (emailed 1/0) and shown in the list.
- Offline devices create snapshots locally and upload them later;
  uploads dedupe by client_ref exactly like the other creates.
"""
import json
import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from . import db, mailer
from .auth import require_approved, require_session, require_unlocked
from .business import compute_dashboard
from .crud import _business_id, _client_ref
from .security import ApiError, now_iso

log = logging.getLogger("qbill.backups")

bp = Blueprint("backups", __name__, url_prefix="/api/backups")

DAILY_MAX_AGE_HOURS = 20


# ------------------------------------------------------------ snapshots ----
def build_snapshot(tx, business_id, source="server", created_at=None):
    business = tx.query_one("SELECT * FROM businesses WHERE id = ?", (business_id,))
    parties = tx.query_all(
        "SELECT * FROM parties WHERE business_id = ? ORDER BY id", (business_id,)
    )
    items = tx.query_all(
        "SELECT * FROM items WHERE business_id = ? ORDER BY id", (business_id,)
    )
    invoices = tx.query_all(
        "SELECT * FROM invoices WHERE business_id = ? ORDER BY id", (business_id,)
    )
    for inv in invoices:
        inv["lines"] = tx.query_all(
            "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY id",
            (inv["id"],),
        )
    payments = tx.query_all(
        "SELECT * FROM payments WHERE business_id = ? ORDER BY id", (business_id,)
    )
    expenses = tx.query_all(
        "SELECT * FROM expenses WHERE business_id = ? ORDER BY id", (business_id,)
    )
    return {
        "version": 1,
        "created_at": created_at or now_iso(),
        "source": source,
        "business": {
            "name": business["name"],
            "address": business.get("address"),
            "phone": business.get("phone"),
            "email": business.get("email"),
            "tax_note": business.get("tax_note"),
            "terms": business.get("terms"),
            "business_type": business.get("business_type"),
            "business_category": business.get("business_category"),
            "pincode": business.get("pincode"),
            "state": business.get("state"),
            "signature_text": business.get("signature_text"),
        },
        "parties": parties,
        "items": items,
        "invoices": invoices,
        "payments": payments,
        "expenses": expenses,
        "totals": compute_dashboard(tx, business_id),
    }


def _insert_snapshot(tx, business_id, payload, created_at, client_ref=None):
    return tx.insert(
        "INSERT INTO auto_backups (business_id, payload, emailed, client_ref, created_at) "
        "VALUES (?, ?, 0, ?, ?) RETURNING id",
        (business_id, json.dumps(payload), client_ref, created_at),
    )


def _row_dict(row):
    try:
        totals = json.loads(row["payload"]).get("totals")
    except (ValueError, AttributeError):
        totals = None
    return {
        "id": row["id"],
        "createdAt": row["created_at"],
        "totals": totals,
        "emailed": bool(row["emailed"]),
    }


def _email_snapshot(business_id, backup_id):
    """Best-effort email of one snapshot to the account's login email.

    Runs AFTER the snapshot is committed; any failure leaves the backup
    standing and records emailed=0. Never raises."""
    try:
        row = db.query_one(
            "SELECT b.payload, b.created_at, a.email AS login_email "
            "FROM auto_backups b "
            "JOIN businesses biz ON biz.id = b.business_id "
            "JOIN site_accounts a ON a.id = biz.account_id "
            "WHERE b.id = ? AND b.business_id = ?",
            (backup_id, business_id),
        )
        if row is None:
            return False
        date = (row["created_at"] or "")[:10]
        route, _errors = mailer.send_email(
            row["login_email"],
            "QBill daily backup — {}".format(date),
            "This is your automatic daily backup from QBill, taken on {}.\n\n"
            "The attached file (qbill-backup-{}.json) contains your complete "
            "QBill data — bills, parties, items, payments and expenses.\n\n"
            "Please keep it safe. It can be used to restore your accounts if "
            "anything ever goes wrong.".format(date, date),
            attachments=[
                {
                    "filename": "qbill-backup-{}.json".format(date),
                    "content": row["payload"].encode("utf-8"),
                    "content_type": "application/json",
                }
            ],
        )
        if route is None:
            log.info("backup %s not emailed (no route could deliver)", backup_id)
            return False
        db.run("UPDATE auto_backups SET emailed = 1 WHERE id = ?", (backup_id,))
        return True
    except Exception:  # email must never break a backup — log and move on
        log.exception("backup email failed for backup %s", backup_id)
        return False


def create_backup_now(business_id, source="server", created_at=None,
                      payload=None, client_ref=None):
    """Create one snapshot (insert + commit), then try to email it.
    Returns the snapshot row dict."""
    with db.transaction() as tx:
        if payload is None:
            payload = build_snapshot(tx, business_id, source=source,
                                     created_at=created_at)
        created_at = created_at or payload.get("created_at") or now_iso()
        backup_id = _insert_snapshot(tx, business_id, payload, created_at, client_ref)
    emailed = _email_snapshot(business_id, backup_id)
    row = db.query_one("SELECT * FROM auto_backups WHERE id = ?", (backup_id,))
    result = _row_dict(row)
    result["emailed"] = emailed
    return result


def maybe_run_daily_backup(business_id):
    """Daily check: snapshot only if the newest one is >20h old (or none).
    Returns the new backup id, or None when a fresh one already exists."""
    row = db.query_one(
        "SELECT created_at FROM auto_backups WHERE business_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (business_id,),
    )
    if row is not None:
        try:
            last = datetime.fromisoformat(row["created_at"])
            age_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
            if age_hours <= DAILY_MAX_AGE_HOURS:
                return None
        except ValueError:
            pass
    result = create_backup_now(business_id, source="daily")
    return result["id"]


# -------------------------------------------------------------- endpoints --
@bp.post("/run")
@require_session
@require_approved
@require_unlocked
def run_backup():
    with db.transaction() as tx:
        biz = _business_id(tx)
    result = create_backup_now(biz, source="manual")
    return jsonify(ok=True, backup=result), 201


@bp.post("/upload")
@require_session
@require_approved
@require_unlocked
def upload_backup():
    """A device that made its daily snapshot OFFLINE uploads it here with
    its original created_at and a client_ref; a repeat upload of the same
    snapshot returns the original row instead of duplicating it. Once
    stored, the server emails it like any other snapshot."""
    data = request.get_json(silent=True) or {}
    payload = data.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            raise ApiError("Snapshot payload is not valid JSON.")
    if not isinstance(payload, dict) or "parties" not in payload:
        raise ApiError("Snapshot payload is missing its data.")
    created_at = str(data.get("created_at") or payload.get("created_at") or "")
    try:
        datetime.fromisoformat(created_at)
    except ValueError:
        created_at = now_iso()
    client_ref = _client_ref(data)
    with db.transaction() as tx:
        biz = _business_id(tx)
        if client_ref:
            existing = tx.query_one(
                "SELECT * FROM auto_backups WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:
                return jsonify(ok=True, backup=_row_dict(existing)), 200
        backup_id = _insert_snapshot(tx, biz, payload, created_at, client_ref)
    _email_snapshot(biz, backup_id)
    row = db.query_one("SELECT * FROM auto_backups WHERE id = ?", (backup_id,))
    return jsonify(ok=True, backup=_row_dict(row)), 201


@bp.get("")
@require_session
@require_approved
@require_unlocked
def list_backups():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM auto_backups WHERE business_id = ? ORDER BY id DESC",
            (biz,),
        )
    return jsonify(ok=True, backups=[_row_dict(r) for r in rows])


# --------------------------------------------------------------- restore ---
_PARTY_COLS = ["id", "business_id", "name", "phone", "opening_balance_paisa",
               "client_ref", "created_at", "opening_date",
               "credit_limit_type", "credit_limit_paisa"]
_ITEM_COLS = ["id", "business_id", "name", "sale_price_paisa",
              "purchase_price_paisa", "stock_qty", "client_ref", "created_at",
              "min_wholesale_qty", "discount_type", "discount_amount_paisa"]
# Snapshots taken before these columns existed restore with these
# defaults instead of NULLs.
_COL_DEFAULTS = {
    "opening_date": "",
    "credit_limit_type": "none",
    "credit_limit_paisa": 0,
    "min_wholesale_qty": 0,
    "discount_type": "percentage",
    "discount_amount_paisa": 0,
    "mode": "cash",
    "cheque_no": "",
    "reference_no": "",
    "note": "",
}
_INVOICE_COLS = ["id", "business_id", "party_id", "invoice_no", "type",
                 "subtotal_paisa", "discount_paisa", "tax_paisa", "total_paisa",
                 "paid_paisa", "balance_paisa", "client_ref", "created_at"]
_LINE_COLS = ["id", "invoice_id", "item_id", "description", "qty",
              "rate_paisa", "amount_paisa"]
_PAYMENT_COLS = ["id", "business_id", "party_id", "invoice_id", "direction",
                 "amount_paisa", "client_ref", "created_at", "mode",
                 "cheque_no", "reference_no", "note"]
_EXPENSE_COLS = ["id", "business_id", "title", "category", "amount_paisa",
                 "client_ref", "created_at"]


def _insert_row(tx, table, cols, row, business_id):
    values = []
    for col in cols:
        value = business_id if col == "business_id" else row.get(col)
        if value is None and col in _COL_DEFAULTS:
            value = _COL_DEFAULTS[col]
        values.append(value)
    tx.execute(
        "INSERT INTO {} ({}) VALUES ({})".format(
            table, ", ".join(cols), ", ".join("?" for _ in cols)
        ),
        values,
    )


def _reset_sequences(tx):
    if db.dialect() != "postgres":
        return  # SQLite advances AUTOINCREMENT past explicit ids by itself
    for table in ("parties", "items", "invoices", "invoice_lines",
                  "payments", "expenses", "auto_backups"):
        tx.execute(
            "SELECT setval(pg_get_serial_sequence('{0}', 'id'), "
            "COALESCE((SELECT MAX(id) FROM {0}), 1))".format(table)
        )


@bp.post("/<int:backup_id>/restore")
@require_session
@require_approved
@require_unlocked
def restore_backup(backup_id):
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM auto_backups WHERE id = ? AND business_id = ?",
            (backup_id, biz),
        )
        if row is None:
            raise ApiError("Backup not found.", status=404)
        try:
            snap = json.loads(row["payload"])
        except ValueError:
            raise ApiError("Backup data is damaged and cannot be restored.")
        # Wipe the business's current data, children first.
        tx.execute(
            "DELETE FROM invoice_lines WHERE invoice_id IN "
            "(SELECT id FROM invoices WHERE business_id = ?)",
            (biz,),
        )
        for table in ("invoices", "payments", "expenses", "parties", "items"):
            tx.execute("DELETE FROM {} WHERE business_id = ?".format(table), (biz,))
        profile = snap.get("business") or {}
        tx.execute(
            "UPDATE businesses SET name = ?, address = ?, phone = ?, email = ?, "
            "tax_note = ?, terms = ?, business_type = ?, business_category = ?, "
            "pincode = ?, state = ?, signature_text = ? WHERE id = ?",
            (profile.get("name") or "My Business", profile.get("address"),
             profile.get("phone"), profile.get("email"),
             profile.get("tax_note"), profile.get("terms"),
             profile.get("business_type") or "",
             profile.get("business_category") or "",
             profile.get("pincode") or "", profile.get("state") or "",
             profile.get("signature_text") or "", biz),
        )
        for party in snap.get("parties", []):
            _insert_row(tx, "parties", _PARTY_COLS, party, biz)
        for item in snap.get("items", []):
            _insert_row(tx, "items", _ITEM_COLS, item, biz)
        for invoice in snap.get("invoices", []):
            _insert_row(tx, "invoices", _INVOICE_COLS, invoice, biz)
            for line in invoice.get("lines", []):
                _insert_row(tx, "invoice_lines", _LINE_COLS, line, biz)
        for payment in snap.get("payments", []):
            _insert_row(tx, "payments", _PAYMENT_COLS, payment, biz)
        for expense in snap.get("expenses", []):
            _insert_row(tx, "expenses", _EXPENSE_COLS, expense, biz)
        _reset_sequences(tx)
        totals = compute_dashboard(tx, biz)
    return jsonify(ok=True, totals=totals)
