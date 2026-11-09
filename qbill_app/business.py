"""Business profile, expenses, standalone payments, dashboard, P&L, ledger.

All money is integer paisa; every total below is exact integer arithmetic.
Dates: created_at is an ISO-8601 UTC string, so day/month bucketing is a
string prefix match (substr) — "today" and "this month" are UTC, the same
clock the rows were written with.
"""
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request

from . import db
from .auth import require_approved, require_session, require_unlocked
from .crud import _business_id, _client_ref, _int_field, _party_dict, party_balance_map
from .security import ApiError, now_iso

bp = Blueprint("business", __name__, url_prefix="/api")


def _body():
    return request.get_json(silent=True) or {}


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


# ---------------------------------------------------- shared summaries ----
def compute_dashboard(tx, business_id):
    """The five dashboard numbers for one business (exact paisa)."""
    today, month = _today(), _month()
    today_sales = tx.query_one(
        "SELECT COALESCE(SUM(total_paisa), 0) AS s FROM invoices "
        "WHERE business_id = ? AND type = 'sale' AND substr(created_at, 1, 10) = ?",
        (business_id, today),
    )["s"]
    month_sales = tx.query_one(
        "SELECT COALESCE(SUM(total_paisa), 0) AS s FROM invoices "
        "WHERE business_id = ? AND type = 'sale' AND substr(created_at, 1, 7) = ?",
        (business_id, month),
    )["s"]
    balances = party_balance_map(tx, business_id)
    receivable = sum(b for b in balances.values() if b > 0)
    cash_in = tx.query_one(
        "SELECT COALESCE(SUM(amount_paisa), 0) AS s FROM payments "
        "WHERE business_id = ? AND direction = 'in'",
        (business_id,),
    )["s"]
    cash_out = tx.query_one(
        "SELECT COALESCE(SUM(amount_paisa), 0) AS s FROM payments "
        "WHERE business_id = ? AND direction = 'out'",
        (business_id,),
    )["s"]
    expenses = tx.query_one(
        "SELECT COALESCE(SUM(amount_paisa), 0) AS s FROM expenses WHERE business_id = ?",
        (business_id,),
    )["s"]
    stock_value = tx.query_one(
        "SELECT COALESCE(SUM(stock_qty * purchase_price_paisa), 0) AS s "
        "FROM items WHERE business_id = ?",
        (business_id,),
    )["s"]
    return {
        "todaySalesPaisa": today_sales,
        "monthSalesPaisa": month_sales,
        "totalReceivablePaisa": receivable,
        "cashInHandPaisa": cash_in - cash_out - expenses,
        "stockValuePaisa": stock_value,
    }


def compute_pnl(tx, business_id, date_from, date_to):
    sales = tx.query_one(
        "SELECT COALESCE(SUM(total_paisa), 0) AS s FROM invoices "
        "WHERE business_id = ? AND type = 'sale' "
        "AND substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ?",
        (business_id, date_from, date_to),
    )["s"]
    purchases = tx.query_one(
        "SELECT COALESCE(SUM(total_paisa), 0) AS s FROM invoices "
        "WHERE business_id = ? AND type = 'purchase' "
        "AND substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ?",
        (business_id, date_from, date_to),
    )["s"]
    expenses = tx.query_one(
        "SELECT COALESCE(SUM(amount_paisa), 0) AS s FROM expenses "
        "WHERE business_id = ? "
        "AND substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ?",
        (business_id, date_from, date_to),
    )["s"]
    return {
        "from": date_from,
        "to": date_to,
        "salesPaisa": sales,
        "purchasesPaisa": purchases,
        "expensesPaisa": expenses,
        "profitPaisa": sales - purchases - expenses,
    }


# ------------------------------------------------------- business profile --
def _business_row(tx, business_id):
    return tx.query_one("SELECT * FROM businesses WHERE id = ?", (business_id,))


def _profile_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "address": row.get("address") or "",
        "phone": row.get("phone") or "",
        "email": row.get("email") or "",
        "taxNote": row.get("tax_note") or "",
        "terms": row.get("terms") or "",
    }


@bp.get("/business")
@require_session
@require_approved
@require_unlocked
def get_business():
    with db.transaction() as tx:
        row = _business_row(tx, _business_id(tx))
    return jsonify(ok=True, business=_profile_dict(row))


@bp.put("/business")
@require_session
@require_approved
@require_unlocked
def update_business():
    data = _body()
    name = str(data.get("name") or "").strip()
    if "name" in data and not name:
        raise ApiError("Business name cannot be empty.")
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = _business_row(tx, biz)
        tx.execute(
            "UPDATE businesses SET name = ?, address = ?, phone = ?, email = ?, "
            "tax_note = ?, terms = ? WHERE id = ?",
            (
                name if "name" in data else row["name"],
                str(data.get("address", row.get("address") or "") or "").strip() or None,
                str(data.get("phone", row.get("phone") or "") or "").strip() or None,
                str(data.get("email", row.get("email") or "") or "").strip() or None,
                str(data.get("tax_note", row.get("tax_note") or "") or "").strip() or None,
                str(data.get("terms", row.get("terms") or "") or "").strip() or None,
                biz,
            ),
        )
        row = _business_row(tx, biz)
    return jsonify(ok=True, business=_profile_dict(row))


# --------------------------------------------------------------- expenses --
def _expense_dict(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "category": row["category"],
        "amountPaisa": row["amount_paisa"],
        "createdAt": row["created_at"],
    }


@bp.post("/expenses")
@require_session
@require_approved
@require_unlocked
def create_expense():
    data = _body()
    title = str(data.get("title") or "").strip()
    if not title:
        raise ApiError("Expense title is required.")
    amount = _int_field(data, "amount_paisa", required=True, min_value=1)
    category = str(data.get("category") or "").strip()
    client_ref = _client_ref(data)
    with db.transaction() as tx:
        biz = _business_id(tx)
        if client_ref:
            existing = tx.query_one(
                "SELECT * FROM expenses WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:
                return jsonify(ok=True, expense=_expense_dict(existing)), 200
        eid = tx.insert(
            "INSERT INTO expenses (business_id, title, category, amount_paisa, client_ref, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (biz, title, category, amount, client_ref, now_iso()),
        )
        row = tx.query_one("SELECT * FROM expenses WHERE id = ?", (eid,))
    return jsonify(ok=True, expense=_expense_dict(row)), 201


@bp.get("/expenses")
@require_session
@require_approved
@require_unlocked
def list_expenses():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM expenses WHERE business_id = ? ORDER BY id", (biz,)
        )
    return jsonify(ok=True, expenses=[_expense_dict(r) for r in rows])


# --------------------------------------------------------------- payments --
@bp.post("/payments")
@require_session
@require_approved
@require_unlocked
def create_payment():
    data = _body()
    direction = str(data.get("direction") or "").strip().lower()
    if direction not in ("in", "out"):
        raise ApiError("Payment direction must be 'in' or 'out'.")
    amount = _int_field(data, "amount_paisa", required=True, min_value=1)
    party_id = data.get("party_id")
    invoice_id = data.get("invoice_id")
    client_ref = _client_ref(data)
    with db.transaction() as tx:
        biz = _business_id(tx)
        if client_ref:
            existing = tx.query_one(
                "SELECT * FROM payments WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:
                return jsonify(ok=True, payment=_payment_dict(existing)), 200
        if party_id is not None:
            party = tx.query_one(
                "SELECT id FROM parties WHERE id = ? AND business_id = ?",
                (party_id, biz),
            )
            if party is None:
                raise ApiError("Party not found.")
        invoice = None
        if invoice_id is not None:
            invoice = tx.query_one(
                "SELECT * FROM invoices WHERE id = ? AND business_id = ?",
                (invoice_id, biz),
            )
            if invoice is None:
                raise ApiError("Invoice not found.")
            if invoice["type"] in ("estimate", "sale_order", "purchase_order", "challan"):
                raise ApiError(
                    "Payments are recorded against invoices and bills, not against "
                    "estimates, orders or challans."
                )
            if amount > invoice["balance_paisa"]:
                raise ApiError("Payment is more than the invoice balance.")
            if party_id is None:
                party_id = invoice["party_id"]
        pid = tx.insert(
            "INSERT INTO payments (business_id, party_id, invoice_id, direction, "
            "amount_paisa, client_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (biz, party_id, invoice_id, direction, amount, client_ref, now_iso()),
        )
        if invoice is not None:
            # Settle against the invoice: 'in' pays a sale down, 'out' pays
            # a purchase down; either way the open balance shrinks.
            new_paid = invoice["paid_paisa"] + amount
            tx.execute(
                "UPDATE invoices SET paid_paisa = ?, balance_paisa = ? WHERE id = ?",
                (new_paid, invoice["total_paisa"] - new_paid, invoice_id),
            )
        row = tx.query_one("SELECT * FROM payments WHERE id = ?", (pid,))
    return jsonify(ok=True, payment=_payment_dict(row)), 201


def _payment_dict(row):
    return {
        "id": row["id"],
        "partyId": row["party_id"],
        "invoiceId": row["invoice_id"],
        "direction": row["direction"],
        "amountPaisa": row["amount_paisa"],
        "createdAt": row["created_at"],
    }


# -------------------------------------------------------------- reporting --
@bp.get("/dashboard")
@require_session
@require_approved
@require_unlocked
def dashboard():
    with db.transaction() as tx:
        summary = compute_dashboard(tx, _business_id(tx))
    return jsonify(ok=True, dashboard=summary)


@bp.get("/pnl")
@require_session
@require_approved
@require_unlocked
def pnl():
    today = _today()
    date_from = request.args.get("from") or (today[:7] + "-01")
    date_to = request.args.get("to") or today
    for value in (date_from, date_to):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ApiError("Dates must be YYYY-MM-DD.")
    if date_from > date_to:
        raise ApiError("'from' date is after 'to' date.")
    with db.transaction() as tx:
        result = compute_pnl(tx, _business_id(tx), date_from, date_to)
    return jsonify(ok=True, pnl=result)


# ----------------------------------------------------------- party ledger --
@bp.get("/parties/<int:party_id>/ledger")
@require_session
@require_approved
@require_unlocked
def party_ledger(party_id):
    with db.transaction() as tx:
        biz = _business_id(tx)
        party = tx.query_one(
            "SELECT * FROM parties WHERE id = ? AND business_id = ?",
            (party_id, biz),
        )
        if party is None:
            raise ApiError("Party not found.", status=404)
        invoices = tx.query_all(
            "SELECT * FROM invoices WHERE business_id = ? AND party_id = ? ORDER BY id",
            (biz, party_id),
        )
        payments = tx.query_all(
            "SELECT * FROM payments WHERE business_id = ? AND party_id = ? ORDER BY id",
            (biz, party_id),
        )
    # Only balance-affecting documents enter the ledger: estimates,
    # orders and challans are not dues and are skipped. Returns move
    # the balance back the other way.
    ledger_docs = {
        "sale": (True, "Sale invoice {}"),
        "purchase": (False, "Purchase invoice {}"),
        "sale_return": (False, "Sale return / Credit note {}"),
        "purchase_return": (True, "Purchase return / Debit note {}"),
    }
    entries = []
    for inv in invoices:
        rule = ledger_docs.get(inv["type"])
        if rule is None:
            continue
        is_debit, desc_fmt = rule
        debit, credit = (inv["total_paisa"], 0) if is_debit else (0, inv["total_paisa"])
        desc = desc_fmt.format(inv["invoice_no"])
        entries.append(
            {
                "date": inv["created_at"],
                "kind": inv["type"],
                "ref": inv["invoice_no"],
                "description": desc,
                "debitPaisa": debit,
                "creditPaisa": credit,
                "_sort": (inv["created_at"], 0, inv["id"]),
            }
        )
    for pay in payments:
        if pay["direction"] == "in":
            debit, credit = 0, pay["amount_paisa"]
            desc = "Payment received"
        else:
            debit, credit = pay["amount_paisa"], 0
            desc = "Payment made"
        entries.append(
            {
                "date": pay["created_at"],
                "kind": "payment",
                "ref": "",
                "description": desc,
                "debitPaisa": debit,
                "creditPaisa": credit,
                "_sort": (pay["created_at"], 1, pay["id"]),
            }
        )
    entries.sort(key=lambda e: e["_sort"])
    balance = party["opening_balance_paisa"]
    for entry in entries:
        balance += entry["debitPaisa"] - entry["creditPaisa"]
        entry["balancePaisa"] = balance
        del entry["_sort"]
    return jsonify(
        ok=True,
        party=_party_dict(party, balance),
        openingBalancePaisa=party["opening_balance_paisa"],
        entries=entries,
        balancePaisa=balance,
    )


# ------------------------------------------------------------ day book ----
DOC_LABELS = {
    "sale": "Sale Invoice",
    "purchase": "Purchase Bill",
    "sale_return": "Sale Return / Credit Note",
    "purchase_return": "Purchase Return / Debit Note",
    "estimate": "Estimate/Quotation",
    "sale_order": "Sale Order",
    "purchase_order": "Purchase Order",
    "challan": "Delivery Challan",
}


@bp.get("/reports/day-book")
@require_session
@require_approved
@require_unlocked
def day_book():
    """Every transaction of one date: documents, payments, expenses."""
    date = request.args.get("date") or _today()
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise ApiError("Date must be YYYY-MM-DD.")
    with db.transaction() as tx:
        biz = _business_id(tx)
        parties = {
            r["id"]: r["name"]
            for r in tx.query_all(
                "SELECT id, name FROM parties WHERE business_id = ?", (biz,)
            )
        }
        docs = tx.query_all(
            "SELECT * FROM invoices WHERE business_id = ? "
            "AND substr(created_at, 1, 10) = ? ORDER BY id",
            (biz, date),
        )
        pays = tx.query_all(
            "SELECT * FROM payments WHERE business_id = ? "
            "AND substr(created_at, 1, 10) = ? ORDER BY id",
            (biz, date),
        )
        exps = tx.query_all(
            "SELECT * FROM expenses WHERE business_id = ? "
            "AND substr(created_at, 1, 10) = ? ORDER BY id",
            (biz, date),
        )
    entries = []
    for d in docs:
        entries.append(
            {
                "kind": d["type"],
                "ref": d["invoice_no"],
                "description": "{} {}".format(
                    DOC_LABELS.get(d["type"], "Document"), d["invoice_no"]
                ),
                "party": parties.get(d["party_id"]) or "",
                "amountPaisa": d["total_paisa"],
                "createdAt": d["created_at"],
                "_sort": (d["created_at"], 0, d["id"]),
            }
        )
    for p in pays:
        entries.append(
            {
                "kind": "payment",
                "ref": "",
                "description": "Payment In" if p["direction"] == "in" else "Payment Out",
                "party": parties.get(p["party_id"]) or "",
                "amountPaisa": p["amount_paisa"],
                "createdAt": p["created_at"],
                "_sort": (p["created_at"], 1, p["id"]),
            }
        )
    for e in exps:
        entries.append(
            {
                "kind": "expense",
                "ref": "",
                "description": "Expense — {}".format(e["title"]),
                "party": "",
                "amountPaisa": e["amount_paisa"],
                "createdAt": e["created_at"],
                "_sort": (e["created_at"], 2, e["id"]),
            }
        )
    entries.sort(key=lambda x: x["_sort"])
    for entry in entries:
        del entry["_sort"]
    totals = {
        "documentsPaisa": sum(d["total_paisa"] for d in docs),
        "paymentsInPaisa": sum(
            p["amount_paisa"] for p in pays if p["direction"] == "in"
        ),
        "paymentsOutPaisa": sum(
            p["amount_paisa"] for p in pays if p["direction"] == "out"
        ),
        "expensesPaisa": sum(e["amount_paisa"] for e in exps),
    }
    return jsonify(ok=True, date=date, entries=entries, totals=totals)


# -------------------------------------------------------- stock summary ----
@bp.get("/reports/stock-summary")
@require_session
@require_approved
@require_unlocked
def stock_summary():
    """Per item: opening, in, out, closing quantities + closing value.

    Movements come from document lines of the stock-affecting types
    (in = purchase + sale return; out = sale + purchase return).
    Closing is the item's live stock; opening is derived from it, so
    opening + in - out always equals closing exactly.
    """
    with db.transaction() as tx:
        biz = _business_id(tx)
        items = tx.query_all(
            "SELECT * FROM items WHERE business_id = ? ORDER BY id", (biz,)
        )
        in_rows = tx.query_all(
            "SELECT l.item_id AS iid, COALESCE(SUM(l.qty), 0) AS q "
            "FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id "
            "WHERE i.business_id = ? AND l.item_id IS NOT NULL "
            "AND i.type IN ('purchase', 'sale_return') GROUP BY l.item_id",
            (biz,),
        )
        out_rows = tx.query_all(
            "SELECT l.item_id AS iid, COALESCE(SUM(l.qty), 0) AS q "
            "FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id "
            "WHERE i.business_id = ? AND l.item_id IS NOT NULL "
            "AND i.type IN ('sale', 'purchase_return') GROUP BY l.item_id",
            (biz,),
        )
    in_map = {r["iid"]: r["q"] for r in in_rows}
    out_map = {r["iid"]: r["q"] for r in out_rows}
    result = []
    for item in items:
        qty_in = in_map.get(item["id"], 0)
        qty_out = out_map.get(item["id"], 0)
        closing = item["stock_qty"]
        result.append(
            {
                "id": item["id"],
                "name": item["name"],
                "openingQty": closing - (qty_in - qty_out),
                "inQty": qty_in,
                "outQty": qty_out,
                "closingQty": closing,
                "valuePaisa": closing * item["purchase_price_paisa"],
            }
        )
    return jsonify(ok=True, items=result)
