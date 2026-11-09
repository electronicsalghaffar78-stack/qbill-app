"""Core CRUD smoke surface (JSON API): parties, items, invoices, payments.

Deliberately minimal — enough to prove the data layer end to end. All money
is integer paisa; line amount = qty * rate_paisa computed in integers, so
invoice totals are exact to the paisa. Every endpoint requires a signed-in
session with the app password unlocked (see auth.py's session model).
"""
from flask import Blueprint, g, jsonify, request

from . import db
from .auth import require_session, require_unlocked
from .security import ApiError, now_iso

bp = Blueprint("crud", __name__, url_prefix="/api")


def _body():
    return request.get_json(silent=True) or {}


def _int_field(data, name, default=None, min_value=None, required=False):
    value = data.get(name, default)
    if value is None:
        if required:
            raise ApiError("{} is required.".format(name))
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError("{} must be a whole number.".format(name))
    if min_value is not None and value < min_value:
        raise ApiError("{} must be at least {}.".format(name, min_value))
    return value


def _business_id(tx):
    row = tx.query_one(
        "SELECT id FROM businesses WHERE account_id = ? ORDER BY id LIMIT 1",
        (g.session["account_id"],),
    )
    if row is None:  # defensive: verify-otp normally creates it
        return tx.insert(
            "INSERT INTO businesses (account_id, name, created_at) "
            "VALUES (?, ?, ?) RETURNING id",
            (g.session["account_id"], "My Business", now_iso()),
        )
    return row["id"]


def _client_ref(data):
    ref = str(data.get("client_ref") or "").strip()
    return ref or None


def party_balance_map(tx, business_id):
    """Current balance per party, in paisa (positive = the party owes us).

    balance = opening + sale totals - purchase totals
              - payments in (party paid us) + payments out (we paid party)
    Invoice-time payments are ordinary payment rows, so an invoice's own
    paid part nets out automatically. One aggregate query, exact integers.
    """
    rows = tx.query_all(
        "SELECT p.id AS pid, p.opening_balance_paisa AS opening, "
        "COALESCE((SELECT SUM(i.total_paisa) FROM invoices i "
        "          WHERE i.party_id = p.id AND i.type = 'sale'), 0) AS sales, "
        "COALESCE((SELECT SUM(i.total_paisa) FROM invoices i "
        "          WHERE i.party_id = p.id AND i.type = 'purchase'), 0) AS purchases, "
        "COALESCE((SELECT SUM(pay.amount_paisa) FROM payments pay "
        "          WHERE pay.party_id = p.id AND pay.direction = 'in'), 0) AS paid_in, "
        "COALESCE((SELECT SUM(pay.amount_paisa) FROM payments pay "
        "          WHERE pay.party_id = p.id AND pay.direction = 'out'), 0) AS paid_out "
        "FROM parties p WHERE p.business_id = ?",
        (business_id,),
    )
    return {
        r["pid"]: r["opening"] + r["sales"] - r["purchases"] - r["paid_in"] + r["paid_out"]
        for r in rows
    }


def _party_dict(row, balance=None):
    return {
        "id": row["id"],
        "name": row["name"],
        "phone": row["phone"],
        "openingBalancePaisa": row["opening_balance_paisa"],
        # A brand-new party's balance is exactly its opening balance.
        "balancePaisa": row["opening_balance_paisa"] if balance is None else balance,
    }


def _item_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "salePricePaisa": row["sale_price_paisa"],
        "purchasePricePaisa": row["purchase_price_paisa"],
        "stockQty": row["stock_qty"],
    }


def _invoice_dict(tx, row):
    lines = tx.query_all(
        "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY id",
        (row["id"],),
    )
    return {
        "id": row["id"],
        "invoiceNo": row["invoice_no"],
        "type": row.get("type") or "sale",
        "createdAt": row["created_at"],
        "partyId": row["party_id"],
        "subtotalPaisa": row["subtotal_paisa"],
        "discountPaisa": row["discount_paisa"],
        "taxPaisa": row["tax_paisa"],
        "totalPaisa": row["total_paisa"],
        "paidPaisa": row["paid_paisa"],
        "balancePaisa": row["balance_paisa"],
        "lines": [
            {
                "id": line["id"],
                "itemId": line["item_id"],
                "description": line["description"],
                "qty": line["qty"],
                "ratePaisa": line["rate_paisa"],
                "amountPaisa": line["amount_paisa"],
            }
            for line in lines
        ],
    }


# -------------------------------------------------------------- parties ----
@bp.post("/parties")
@require_session
@require_unlocked
def create_party():
    data = _body()
    name = str(data.get("name") or "").strip()
    if not name:
        raise ApiError("Party name is required.")
    opening = _int_field(data, "opening_balance_paisa", default=0)
    client_ref = _client_ref(data)
    with db.transaction() as tx:
        biz = _business_id(tx)
        if client_ref:
            existing = tx.query_one(
                "SELECT * FROM parties WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:  # offline replay of an already-saved create
                return jsonify(ok=True, party=_party_dict(existing)), 200
        pid = tx.insert(
            "INSERT INTO parties (business_id, name, phone, opening_balance_paisa, client_ref, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (biz, name, str(data.get("phone") or "").strip() or None, opening,
             client_ref, now_iso()),
        )
        row = tx.query_one("SELECT * FROM parties WHERE id = ?", (pid,))
    return jsonify(ok=True, party=_party_dict(row)), 201


@bp.get("/parties")
@require_session
@require_unlocked
def list_parties():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM parties WHERE business_id = ? ORDER BY id", (biz,)
        )
        balances = party_balance_map(tx, biz)
    return jsonify(
        ok=True,
        parties=[_party_dict(r, balances.get(r["id"])) for r in rows],
    )


# ---------------------------------------------------------------- items ----
@bp.post("/items")
@require_session
@require_unlocked
def create_item():
    data = _body()
    name = str(data.get("name") or "").strip()
    if not name:
        raise ApiError("Item name is required.")
    sale = _int_field(data, "sale_price_paisa", default=0, min_value=0)
    purchase = _int_field(data, "purchase_price_paisa", default=0, min_value=0)
    stock = _int_field(data, "stock_qty", default=0)
    client_ref = _client_ref(data)
    with db.transaction() as tx:
        biz = _business_id(tx)
        if client_ref:
            existing = tx.query_one(
                "SELECT * FROM items WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:
                return jsonify(ok=True, item=_item_dict(existing)), 200
        iid = tx.insert(
            "INSERT INTO items (business_id, name, sale_price_paisa, purchase_price_paisa, "
            "stock_qty, client_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (biz, name, sale, purchase, stock, client_ref, now_iso()),
        )
        row = tx.query_one("SELECT * FROM items WHERE id = ?", (iid,))
    return jsonify(ok=True, item=_item_dict(row)), 201


@bp.get("/items")
@require_session
@require_unlocked
def list_items():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM items WHERE business_id = ? ORDER BY id", (biz,)
        )
    return jsonify(ok=True, items=[_item_dict(r) for r in rows])


# ------------------------------------------------------------- invoices ----
@bp.post("/invoices")
@require_session
@require_unlocked
def create_invoice():
    data = _body()
    inv_type = str(data.get("type") or "sale").strip().lower()
    if inv_type not in ("sale", "purchase"):
        raise ApiError("Invoice type must be 'sale' or 'purchase'.")
    client_ref = _client_ref(data)
    lines_in = data.get("lines")
    if not isinstance(lines_in, list) or not lines_in:
        raise ApiError("An invoice needs at least one line.")
    discount = _int_field(data, "discount_paisa", default=0, min_value=0)
    tax = _int_field(data, "tax_paisa", default=0, min_value=0)
    paid = _int_field(data, "paid_paisa", default=0, min_value=0)
    party_id = data.get("party_id")

    lines = []
    subtotal = 0
    for raw in lines_in:
        if not isinstance(raw, dict):
            raise ApiError("Each invoice line must be an object.")
        qty = _int_field(raw, "qty", required=True, min_value=1)
        rate = _int_field(raw, "rate_paisa", required=True, min_value=0)
        amount = qty * rate  # integer math — exact to the paisa
        subtotal += amount
        lines.append(
            {
                "item_id": raw.get("item_id"),
                "description": str(raw.get("description") or "").strip(),
                "qty": qty,
                "rate_paisa": rate,
                "amount_paisa": amount,
            }
        )
    if discount > subtotal:
        raise ApiError("Discount cannot be more than the subtotal.")
    total = subtotal - discount + tax
    if paid > total:
        raise ApiError("Paid amount cannot be more than the invoice total.")
    balance = total - paid

    with db.transaction() as tx:
        biz = _business_id(tx)
        if client_ref:
            existing = tx.query_one(
                "SELECT * FROM invoices WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:  # offline replay: never double the stock
                return jsonify(ok=True, invoice=_invoice_dict(tx, existing)), 200
        if party_id is not None:
            party = tx.query_one(
                "SELECT id FROM parties WHERE id = ? AND business_id = ?",
                (party_id, biz),
            )
            if party is None:
                raise ApiError("Party not found.")
        for line in lines:
            if line["item_id"] is not None:
                item = tx.query_one(
                    "SELECT id, name FROM items WHERE id = ? AND business_id = ?",
                    (line["item_id"], biz),
                )
                if item is None:
                    raise ApiError("Item not found.")
                if not line["description"]:
                    line["description"] = item["name"]
            if not line["description"]:
                raise ApiError("Each invoice line needs a description or an item.")
        count = tx.query_one(
            "SELECT COUNT(*) AS c FROM invoices WHERE business_id = ? AND type = ?",
            (biz, inv_type),
        )["c"]
        # Server-assigned per type (S-1.., P-1..); devices can't collide.
        invoice_no = "{}-{}".format("S" if inv_type == "sale" else "P", count + 1)
        inv_id = tx.insert(
            "INSERT INTO invoices (business_id, party_id, invoice_no, type, subtotal_paisa, "
            "discount_paisa, tax_paisa, total_paisa, paid_paisa, balance_paisa, client_ref, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (biz, party_id, invoice_no, inv_type, subtotal, discount, tax, total,
             paid, balance, client_ref, now_iso()),
        )
        stock_sign = -1 if inv_type == "sale" else 1  # purchases add stock
        for line in lines:
            tx.execute(
                "INSERT INTO invoice_lines (invoice_id, item_id, description, qty, "
                "rate_paisa, amount_paisa) VALUES (?, ?, ?, ?, ?, ?)",
                (inv_id, line["item_id"], line["description"], line["qty"],
                 line["rate_paisa"], line["amount_paisa"]),
            )
            if line["item_id"] is not None:
                tx.execute(
                    "UPDATE items SET stock_qty = stock_qty + ? WHERE id = ?",
                    (stock_sign * line["qty"], line["item_id"]),
                )
        if paid > 0:
            tx.execute(
                "INSERT INTO payments (business_id, party_id, invoice_id, direction, "
                "amount_paisa, client_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (biz, party_id, inv_id, "in" if inv_type == "sale" else "out",
                 paid, "{}:pay".format(client_ref) if client_ref else None, now_iso()),
            )
        row = tx.query_one("SELECT * FROM invoices WHERE id = ?", (inv_id,))
        result = _invoice_dict(tx, row)
    return jsonify(ok=True, invoice=result), 201


@bp.get("/invoices")
@require_session
@require_unlocked
def list_invoices():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM invoices WHERE business_id = ? ORDER BY id", (biz,)
        )
        result = [_invoice_dict(tx, r) for r in rows]
    return jsonify(ok=True, invoices=result)


@bp.get("/invoices/<int:invoice_id>")
@require_session
@require_unlocked
def get_invoice(invoice_id):
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM invoices WHERE id = ? AND business_id = ?",
            (invoice_id, biz),
        )
        if row is None:
            raise ApiError("Invoice not found.", status=404)
        result = _invoice_dict(tx, row)
    return jsonify(ok=True, invoice=result)


# ------------------------------------------------------------- payments ----
@bp.get("/payments")
@require_session
@require_unlocked
def list_payments():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM payments WHERE business_id = ? ORDER BY id", (biz,)
        )
    return jsonify(
        ok=True,
        payments=[
            {
                "id": r["id"],
                "partyId": r["party_id"],
                "invoiceId": r["invoice_id"],
                "direction": r["direction"],
                "amountPaisa": r["amount_paisa"],
                "createdAt": r["created_at"],
            }
            for r in rows
        ],
    )
