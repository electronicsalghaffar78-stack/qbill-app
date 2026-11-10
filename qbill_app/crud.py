"""Core CRUD smoke surface (JSON API): parties, items, invoices, payments.

Deliberately minimal — enough to prove the data layer end to end. All money
is integer paisa; line amount = qty * rate_paisa computed in integers, so
invoice totals are exact to the paisa. Every endpoint requires a signed-in
session with the app password unlocked (see auth.py's session model).
"""
from flask import Blueprint, g, jsonify, request

from . import db
from .auth import require_approved, require_session, require_unlocked
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
    # Team & Roles: a signed-in team member can switch into the business
    # that shared access with them (sessions.active_business_id, set by
    # /api/team/switch). Honoured only when the account owns that
    # business or is an active member of it.
    active = g.session.get("active_business_id")
    if active:
        own = tx.query_one(
            "SELECT id FROM businesses WHERE id = ? AND account_id = ?",
            (active, g.session["account_id"]),
        )
        if own is not None:
            return own["id"]
        member = tx.query_one(
            "SELECT m.id FROM team_members m "
            "JOIN site_accounts a ON a.email = m.contact "
            "WHERE m.business_id = ? AND a.id = ? AND m.status = 'active'",
            (active, g.session["account_id"]),
        )
        if member is not None:
            return active
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
              - sale returns + purchase returns
              - payments in (party paid us) + payments out (we paid party)
    Invoice-time payments are ordinary payment rows, so an invoice's own
    paid part nets out automatically. Estimates, orders and challans are
    NOT dues and never enter the balance. One aggregate query, exact
    integers.
    """
    rows = tx.query_all(
        "SELECT p.id AS pid, p.opening_balance_paisa AS opening, "
        "COALESCE((SELECT SUM(i.total_paisa) FROM invoices i "
        "          WHERE i.party_id = p.id AND i.type = 'sale' AND i.deleted_at IS NULL), 0) AS sales, "
        "COALESCE((SELECT SUM(i.total_paisa) FROM invoices i "
        "          WHERE i.party_id = p.id AND i.type = 'purchase' AND i.deleted_at IS NULL), 0) AS purchases, "
        "COALESCE((SELECT SUM(i.total_paisa) FROM invoices i "
        "          WHERE i.party_id = p.id AND i.type = 'sale_return' AND i.deleted_at IS NULL), 0) AS sale_returns, "
        "COALESCE((SELECT SUM(i.total_paisa) FROM invoices i "
        "          WHERE i.party_id = p.id AND i.type = 'purchase_return' AND i.deleted_at IS NULL), 0) AS purchase_returns, "
        "COALESCE((SELECT SUM(pay.amount_paisa) FROM payments pay "
        "          WHERE pay.party_id = p.id AND pay.direction = 'in' "
        "          AND (pay.invoice_id IS NULL OR pay.invoice_id IN "
        "               (SELECT id FROM invoices WHERE deleted_at IS NULL))), 0) AS paid_in, "
        "COALESCE((SELECT SUM(pay.amount_paisa) FROM payments pay "
        "          WHERE pay.party_id = p.id AND pay.direction = 'out' "
        "          AND (pay.invoice_id IS NULL OR pay.invoice_id IN "
        "               (SELECT id FROM invoices WHERE deleted_at IS NULL))), 0) AS paid_out "
        "FROM parties p WHERE p.business_id = ?",
        (business_id,),
    )
    return {
        r["pid"]: r["opening"] + r["sales"] - r["purchases"]
        - r["sale_returns"] + r["purchase_returns"]
        - r["paid_in"] + r["paid_out"]
        for r in rows
    }


def _party_dict(row, balance=None):
    import json as _json

    try:
        extras = _json.loads(row.get("extras") or "{}")
    except ValueError:
        extras = {}
    return {
        "id": row["id"],
        "name": row["name"],
        "phone": row["phone"],
        "openingBalancePaisa": row["opening_balance_paisa"],
        "openingDate": row.get("opening_date") or "",
        "creditLimitType": row.get("credit_limit_type") or "none",
        "creditLimitPaisa": row.get("credit_limit_paisa") or 0,
        # A brand-new party's balance is exactly its opening balance.
        "balancePaisa": row["opening_balance_paisa"] if balance is None else balance,
        "group": row.get("group_name") or "",
        "status": row.get("status") or "active",
        "billingAddress": row.get("billing_address") or "",
        "shippingAddress": row.get("shipping_address") or "",
        "email": row.get("email") or "",
        "extras": extras if isinstance(extras, dict) else {},
        "loyaltyPoints": row.get("loyalty_points") or 0,
    }


def _item_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "salePricePaisa": row["sale_price_paisa"],
        "purchasePricePaisa": row["purchase_price_paisa"],
        "stockQty": row["stock_qty"],
        "itemType": row.get("item_type") or "product",
        "itemCode": row.get("item_code") or "",
        "category": row.get("category") or "",
        "unit": row.get("unit") or "",
        "secondaryUnit": row.get("secondary_unit") or "",
        "conversion": row.get("conversion") or 0,
        "wholesalePricePaisa": row.get("wholesale_price_paisa") or 0,
        "minWholesaleQty": row.get("min_wholesale_qty") or 0,
        "mrpPaisa": row.get("mrp_paisa") or 0,
        "taxBp": row.get("tax_bp") or 0,
        "discountBp": row.get("discount_bp") or 0,
        "discountType": row.get("discount_type") or "percentage",
        "discountAmountPaisa": row.get("discount_amount_paisa") or 0,
        "minStock": row.get("min_stock") or 0,
        "location": row.get("location") or "",
        "description": row.get("description") or "",
    }


def _payment_row_dict(r):
    return {
        "id": r["id"],
        "partyId": r["party_id"],
        "invoiceId": r["invoice_id"],
        "direction": r["direction"],
        "amountPaisa": r["amount_paisa"],
        "createdAt": r["created_at"],
        "mode": r.get("mode") or "cash",
        "bankAccountId": r.get("bank_account_id"),
        "chequeNo": r.get("cheque_no") or "",
        "referenceNo": r.get("reference_no") or "",
        "note": r.get("note") or "",
    }


def _invoice_dict(tx, row):
    lines = tx.query_all(
        "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY id",
        (row["id"],),
    )
    pay_rows = tx.query_all(
        "SELECT * FROM payments WHERE invoice_id = ? ORDER BY id",
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
        # Estimates, orders, challans and proformas carry no due balance.
        "balancePaisa": (
            0
            if (row.get("type") or "sale")
            in ("estimate", "sale_order", "purchase_order", "challan", "proforma")
            else row["balance_paisa"]
        ),
        "convertedToId": row.get("converted_to_id"),
        "dueDate": row.get("due_date") or "",
        "paymentTerms": row.get("payment_terms") or "",
        "billingAddress": row.get("billing_address") or "",
        "shippingAddress": row.get("shipping_address") or "",
        "transportName": row.get("transport_name") or "",
        "vehicleNo": row.get("vehicle_no") or "",
        "deliveryDate": row.get("delivery_date") or "",
        "deliveryLocation": row.get("delivery_location") or "",
        "poNumber": row.get("po_number") or "",
        "description": row.get("description") or "",
        "termsId": row.get("terms_id"),
        "repeatFreq": row.get("repeat_freq") or "",
        "attachmentName": row.get("attachment_name") or "",
        "loyaltyPointsUsed": row.get("loyalty_points_used") or 0,
        "loyaltyRedeemedPaisa": row.get("loyalty_redeemed_paisa") or 0,
        "loyaltyPointsEarned": row.get("loyalty_points_earned") or 0,
        "onlineOrderId": row.get("online_order_id"),
        "deletedAt": row.get("deleted_at"),
        "lines": [
            {
                "id": line["id"],
                "itemId": line["item_id"],
                "description": line["description"],
                "qty": line["qty"],
                "ratePaisa": line["rate_paisa"],
                "amountPaisa": line["amount_paisa"],
                "discountPaisa": line.get("discount_paisa") or 0,
                "taxPaisa": line.get("tax_paisa") or 0,
                "discountBp": line.get("discount_bp"),
                "taxBp": line.get("tax_bp"),
                "freeQty": line.get("free_qty") or 0,
                "batchNo": line.get("batch_no") or "",
                "expDate": line.get("exp_date") or "",
                "serialNo": line.get("serial_no") or "",
            }
            for line in lines
        ],
        "payments": [_payment_row_dict(r) for r in pay_rows],
    }


# -------------------------------------------------------------- parties ----
def _credit_limit(data, row=None):
    """Party Credit & Balance tab: (type, paisa). 'none' = No Limit."""
    ctype = str(data.get(
        "credit_limit_type",
        (row or {}).get("credit_limit_type") or "none") or "none").strip().lower()
    if ctype not in ("none", "custom"):
        raise ApiError("Credit limit type must be 'none' or 'custom'.")
    amount = _int_field(
        data, "credit_limit_paisa",
        default=(row or {}).get("credit_limit_paisa") or 0, min_value=0)
    return ctype, (amount if ctype == "custom" else 0)


@bp.post("/parties")
@require_session
@require_approved
@require_unlocked
def create_party():
    data = _body()
    name = str(data.get("name") or "").strip()
    if not name:
        raise ApiError("Party name is required.")
    opening = _int_field(data, "opening_balance_paisa", default=0)
    client_ref = _client_ref(data)
    cl_type, cl_amount = _credit_limit(data)
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
            "INSERT INTO parties (business_id, name, phone, opening_balance_paisa, client_ref, created_at, "
            "group_name, status, billing_address, shipping_address, email, extras, loyalty_points, "
            "opening_date, credit_limit_type, credit_limit_paisa) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (biz, name, str(data.get("phone") or "").strip() or None, opening,
             client_ref, now_iso(),
             str(data.get("group") or "").strip(),
             str(data.get("status") or "active").strip() or "active",
             str(data.get("billing_address") or "").strip(),
             str(data.get("shipping_address") or "").strip(),
             str(data.get("email") or "").strip(),
             _extras_json(data.get("extras")),
             _int_field(data, "loyalty_points", default=0, min_value=0),
             str(data.get("opening_date") or "").strip(),
             cl_type, cl_amount),
        )
        row = tx.query_one("SELECT * FROM parties WHERE id = ?", (pid,))
    return jsonify(ok=True, party=_party_dict(row)), 201


def _extras_json(extras):
    import json as _json

    if isinstance(extras, dict):
        return _json.dumps(extras)
    return "{}"


@bp.put("/parties/<int:party_id>")
@require_session
@require_approved
@require_unlocked
def update_party(party_id):
    data = _body()
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM parties WHERE id = ? AND business_id = ?",
            (party_id, biz),
        )
        if row is None:
            raise ApiError("Party not found.", status=404)
        name = str(data.get("name", row["name"]) or "").strip()
        if not name:
            raise ApiError("Party name is required.")
        cl_type, cl_amount = _credit_limit(data, row)
        tx.execute(
            "UPDATE parties SET name = ?, phone = ?, group_name = ?, status = ?, "
            "billing_address = ?, shipping_address = ?, email = ?, extras = ?, "
            "opening_balance_paisa = ?, opening_date = ?, "
            "credit_limit_type = ?, credit_limit_paisa = ? "
            "WHERE id = ?",
            (
                name,
                str(data.get("phone", row["phone"] or "") or "").strip() or None,
                str(data.get("group", row.get("group_name") or "") or "").strip(),
                str(data.get("status", row.get("status") or "active") or "active"),
                str(data.get("billing_address", row.get("billing_address") or "") or "").strip(),
                str(data.get("shipping_address", row.get("shipping_address") or "") or "").strip(),
                str(data.get("email", row.get("email") or "") or "").strip(),
                _extras_json(data["extras"]) if "extras" in data else (row.get("extras") or "{}"),
                _int_field(data, "opening_balance_paisa",
                           default=row["opening_balance_paisa"]),
                str(data.get("opening_date", row.get("opening_date") or "") or "").strip(),
                cl_type, cl_amount,
                party_id,
            ),
        )
        row = tx.query_one("SELECT * FROM parties WHERE id = ?", (party_id,))
        balances = party_balance_map(tx, biz)
    return jsonify(ok=True, party=_party_dict(row, balances.get(row["id"])))


@bp.get("/parties")
@require_session
@require_approved
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
@require_approved
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
    batches = data.get("batches") or []
    if batches:
        # Batch-wise opening stock (ITEM TRACKING DETAIL): the item's
        # opening stock is the sum of its batches.
        stock = sum(int(b.get("qty") or 0) for b in batches if isinstance(b, dict))
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
            "stock_qty, client_ref, created_at, item_type, item_code, category, unit, "
            "secondary_unit, conversion, wholesale_price_paisa, mrp_paisa, tax_bp, "
            "discount_bp, min_stock, location, description, min_wholesale_qty, "
            "discount_type, discount_amount_paisa) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (biz, name, sale, purchase, stock, client_ref, now_iso(),
             str(data.get("item_type") or "product").strip() or "product",
             str(data.get("item_code") or "").strip(),
             str(data.get("category") or "").strip(),
             str(data.get("unit") or "").strip(),
             str(data.get("secondary_unit") or "").strip(),
             _num_field(data, "conversion", default=0),
             _int_field(data, "wholesale_price_paisa", default=0, min_value=0),
             _int_field(data, "mrp_paisa", default=0, min_value=0),
             _int_field(data, "tax_bp", default=0, min_value=0),
             _int_field(data, "discount_bp", default=0, min_value=0),
             _int_field(data, "min_stock", default=0, min_value=0),
             str(data.get("location") or "").strip(),
             str(data.get("description") or "").strip(),
             _int_field(data, "min_wholesale_qty", default=0, min_value=0),
             _discount_type(data),
             _int_field(data, "discount_amount_paisa", default=0, min_value=0)),
        )
        for b in batches:
            if isinstance(b, dict) and (b.get("batch_no") or b.get("qty")):
                tx.execute(
                    "INSERT INTO item_batches (business_id, item_id, batch_no, "
                    "exp_date, mfg_date, qty, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (biz, iid, str(b.get("batch_no") or "").strip(),
                     str(b.get("exp_date") or "").strip(),
                     str(b.get("mfg_date") or "").strip(),
                     int(b.get("qty") or 0), now_iso()),
                )
        _sync_main_godown(tx, biz, iid)
        row = tx.query_one("SELECT * FROM items WHERE id = ?", (iid,))
    return jsonify(ok=True, item=_item_dict(row)), 201


def _discount_type(data, row=None):
    """Item 'Disc. On Sale Price' basis: a percentage or a flat amount."""
    value = str(data.get(
        "discount_type", (row or {}).get("discount_type") or "percentage"
    ) or "percentage").strip().lower()
    if value not in ("percentage", "amount"):
        raise ApiError("Discount type must be 'percentage' or 'amount'.")
    return value


def _num_field(data, name, default=0):
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError("{} must be a number.".format(name))
    return value


@bp.put("/items/<int:item_id>")
@require_session
@require_approved
@require_unlocked
def update_item(item_id):
    data = _body()
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM items WHERE id = ? AND business_id = ?", (item_id, biz)
        )
        if row is None:
            raise ApiError("Item not found.", status=404)
        name = str(data.get("name", row["name"]) or "").strip()
        if not name:
            raise ApiError("Item name is required.")
        tx.execute(
            "UPDATE items SET name = ?, sale_price_paisa = ?, purchase_price_paisa = ?, "
            "item_type = ?, item_code = ?, category = ?, unit = ?, secondary_unit = ?, "
            "conversion = ?, wholesale_price_paisa = ?, mrp_paisa = ?, tax_bp = ?, "
            "discount_bp = ?, min_stock = ?, location = ?, description = ?, "
            "min_wholesale_qty = ?, discount_type = ?, discount_amount_paisa = ? "
            "WHERE id = ?",
            (
                name,
                _int_field(data, "sale_price_paisa", default=row["sale_price_paisa"], min_value=0),
                _int_field(data, "purchase_price_paisa", default=row["purchase_price_paisa"], min_value=0),
                str(data.get("item_type", row.get("item_type") or "product") or "product"),
                str(data.get("item_code", row.get("item_code") or "") or "").strip(),
                str(data.get("category", row.get("category") or "") or "").strip(),
                str(data.get("unit", row.get("unit") or "") or "").strip(),
                str(data.get("secondary_unit", row.get("secondary_unit") or "") or "").strip(),
                _num_field(data, "conversion", default=row.get("conversion") or 0),
                _int_field(data, "wholesale_price_paisa", default=row.get("wholesale_price_paisa") or 0, min_value=0),
                _int_field(data, "mrp_paisa", default=row.get("mrp_paisa") or 0, min_value=0),
                _int_field(data, "tax_bp", default=row.get("tax_bp") or 0, min_value=0),
                _int_field(data, "discount_bp", default=row.get("discount_bp") or 0, min_value=0),
                _int_field(data, "min_stock", default=row.get("min_stock") or 0, min_value=0),
                str(data.get("location", row.get("location") or "") or "").strip(),
                str(data.get("description", row.get("description") or "") or "").strip(),
                _int_field(data, "min_wholesale_qty", default=row.get("min_wholesale_qty") or 0, min_value=0),
                _discount_type(data, row),
                _int_field(data, "discount_amount_paisa", default=row.get("discount_amount_paisa") or 0, min_value=0),
                item_id,
            ),
        )
        row = tx.query_one("SELECT * FROM items WHERE id = ?", (item_id,))
    return jsonify(ok=True, item=_item_dict(row))


@bp.post("/items/bulk-update")
@require_session
@require_approved
@require_unlocked
def bulk_update_items():
    """Bulk Update Items: one editable grid, many rows saved at once."""
    data = _body()
    rows_in = data.get("rows")
    if not isinstance(rows_in, list) or not rows_in:
        raise ApiError("No rows to update.")
    updated = 0
    with db.transaction() as tx:
        biz = _business_id(tx)
        for raw in rows_in:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            row = tx.query_one(
                "SELECT * FROM items WHERE id = ? AND business_id = ?",
                (raw["id"], biz),
            )
            if row is None:
                continue
            sets, values = [], []
            for field, column in (
                ("sale_price_paisa", "sale_price_paisa"),
                ("purchase_price_paisa", "purchase_price_paisa"),
                ("wholesale_price_paisa", "wholesale_price_paisa"),
                ("mrp_paisa", "mrp_paisa"),
                ("min_stock", "min_stock"),
            ):
                if field in raw:
                    sets.append("{} = ?".format(column))
                    values.append(_int_field(raw, field, default=0, min_value=0))
            if "stock_qty" in raw:
                sets.append("stock_qty = ?")
                values.append(_int_field(raw, "stock_qty", required=True))
            if "name" in raw and str(raw["name"]).strip():
                sets.append("name = ?")
                values.append(str(raw["name"]).strip())
            if sets:
                tx.execute(
                    "UPDATE items SET {} WHERE id = ?".format(", ".join(sets)),
                    tuple(values) + (row["id"],),
                )
                if "stock_qty" in raw:
                    _sync_main_godown(tx, biz, row["id"])
                updated += 1
    return jsonify(ok=True, updated=updated)


@bp.get("/items")
@require_session
@require_approved
@require_unlocked
def list_items():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM items WHERE business_id = ? ORDER BY id", (biz,)
        )
    return jsonify(ok=True, items=[_item_dict(r) for r in rows])


# ------------------------------------------------------------- invoices ----
# One document engine for every Vyapar-style document. The type drives
# the number prefix, the stock effect, and whether a payment can be
# taken on the document:
#   sale            S-   stock out,  payment 'in'  (customer pays us)
#   purchase        P-   stock in,   payment 'out' (we pay the supplier)
#   sale_return     SR-  stock in,   payment 'out' (we refund the customer)
#   purchase_return PR-  stock out,  payment 'in'  (supplier refunds us)
#   estimate        EST- no stock effect, no payments (not a due)
#   sale_order      SO-  no stock effect, no payments (not a due)
#   purchase_order  PO-  no stock effect, no payments (not a due)
#   challan         DC-  no stock effect, no payments (not a due)
#   proforma        PI-  no stock effect, no payments (not a due)
DOC_TYPES = {
    "sale": {"prefix": "S", "stock": -1, "pay_direction": "in"},
    "purchase": {"prefix": "P", "stock": 1, "pay_direction": "out"},
    "sale_return": {"prefix": "SR", "stock": 1, "pay_direction": "out"},
    "purchase_return": {"prefix": "PR", "stock": -1, "pay_direction": "in"},
    "estimate": {"prefix": "EST", "stock": 0, "pay_direction": None},
    "sale_order": {"prefix": "SO", "stock": 0, "pay_direction": None},
    "purchase_order": {"prefix": "PO", "stock": 0, "pay_direction": None},
    "challan": {"prefix": "DC", "stock": 0, "pay_direction": None},
    "proforma": {"prefix": "PI", "stock": 0, "pay_direction": None},
}
CONVERTIBLE_TYPES = ("estimate", "sale_order", "challan", "proforma")
NO_BALANCE_TYPES = ("estimate", "sale_order", "purchase_order", "challan", "proforma")

# Settings > TRANSACTION "More Transactions" gates, per document type.
_TYPE_SETTING_GATES = {
    "estimate": "more_tx_estimate",
    "proforma": "more_tx_proforma",
    "sale_order": "more_tx_sale_order",
    "purchase_order": "more_tx_purchase_order",
    "challan": "more_tx_challan",
}


def _settings(tx, biz):
    from .settings_api import get_settings

    return get_settings(tx, biz)


def _audit(tx, biz, settings, invoice_id, action, detail):
    if settings.get("audit_trail", True):
        tx.execute(
            "INSERT INTO audit_log (business_id, invoice_id, action, detail, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (biz, invoice_id, action, detail, now_iso()),
        )


def ensure_main_godown(tx, biz):
    row = tx.query_one(
        "SELECT id FROM godowns WHERE business_id = ? AND is_main = 1 "
        "ORDER BY id LIMIT 1",
        (biz,),
    )
    if row is not None:
        return row["id"]
    return tx.insert(
        "INSERT INTO godowns (business_id, name, gtype, is_main, created_at) "
        "VALUES (?, 'Main Godown', 'Godown', 1, ?) RETURNING id",
        (biz, now_iso()),
    )


def _sync_main_godown(tx, biz, item_id):
    """Keep the Main Godown's share of an item in step with its total:
    main = total stock - stock held in every other godown."""
    main_id = ensure_main_godown(tx, biz)
    item = tx.query_one("SELECT stock_qty FROM items WHERE id = ?", (item_id,))
    others = tx.query_one(
        "SELECT COALESCE(SUM(qty), 0) AS q FROM godown_stock "
        "WHERE item_id = ? AND godown_id != ?",
        (item_id, main_id),
    )["q"]
    main_qty = (item["stock_qty"] if item else 0) - others
    existing = tx.query_one(
        "SELECT id FROM godown_stock WHERE godown_id = ? AND item_id = ?",
        (main_id, item_id),
    )
    if existing is None:
        tx.execute(
            "INSERT INTO godown_stock (business_id, godown_id, item_id, qty) "
            "VALUES (?, ?, ?, ?)",
            (biz, main_id, item_id, main_qty),
        )
    else:
        tx.execute(
            "UPDATE godown_stock SET qty = ? WHERE id = ?",
            (main_qty, existing["id"]),
        )


def _apply_stock(tx, biz, item_id, delta):
    """Move an item's total stock by delta; the movement lands in the
    Main Godown (documents always trade out of the main godown)."""
    if not delta:
        return
    tx.execute(
        "UPDATE items SET stock_qty = stock_qty + ? WHERE id = ?",
        (delta, item_id),
    )
    main_id = ensure_main_godown(tx, biz)
    existing = tx.query_one(
        "SELECT id FROM godown_stock WHERE godown_id = ? AND item_id = ?",
        (main_id, item_id),
    )
    if existing is not None:
        tx.execute(
            "UPDATE godown_stock SET qty = qty + ? WHERE id = ?",
            (delta, existing["id"]),
        )
    else:
        _sync_main_godown(tx, biz, item_id)


def _apply_batch(tx, biz, item_id, batch_no, exp_date, delta):
    """Move a batch's quantity by delta (creating the batch row when a
    stock-in document names a new batch)."""
    if not batch_no:
        return
    row = tx.query_one(
        "SELECT * FROM item_batches WHERE business_id = ? AND item_id = ? "
        "AND batch_no = ? ORDER BY id LIMIT 1",
        (biz, item_id, batch_no),
    )
    if row is None:
        tx.execute(
            "INSERT INTO item_batches (business_id, item_id, batch_no, exp_date, "
            "mfg_date, qty, created_at) VALUES (?, ?, ?, ?, '', ?, ?)",
            (biz, item_id, batch_no, exp_date or "", delta, now_iso()),
        )
    else:
        tx.execute(
            "UPDATE item_batches SET qty = qty + ? WHERE id = ?", (delta, row["id"])
        )


def _pct_of(amount, bp):
    """bp is percent x 100 (e.g. 500 = 5%). Integer paisa, rounded."""
    if not bp:
        return 0
    return int(round(amount * bp / 10000.0))


def _item_default_discount(item, qty, gross):
    """An item's own 'Disc. On Sale Price' as a paisa amount for a line:
    the flat amount per unit, or the percentage of the line gross."""
    if item is None:
        return 0
    if (item.get("discount_type") or "percentage") == "amount":
        return (item.get("discount_amount_paisa") or 0) * qty
    return _pct_of(gross, item.get("discount_bp") or 0)


def _line_money(raw, qty, rate, settings, item=None):
    """Per-line Discount %/Amount and Tax %/Amount (Vyapar columns).

    Accepts discount_paisa / tax_paisa directly, or discount_bp /
    tax_bp (percent x 100). Line amount stays qty * rate (gross); the
    invoice totals fold the line discount/tax in.

    Settings > TRANSACTION gates: with "Transaction wise Discount" OFF
    the caller's per-line discount is ignored and the ITEM's default
    sale discount applies instead; with "Transaction wise Tax" OFF the
    line tax is always 0."""
    gross = qty * rate
    discount_bp = raw.get("discount_bp")
    tax_bp = raw.get("tax_bp")
    if settings.get("txn_wise_discount", True):
        discount = _int_field(raw, "discount_paisa", default=0, min_value=0)
        if discount_bp is not None and settings.get("item_wise_discount", True):
            discount = _pct_of(
                gross, _int_field(raw, "discount_bp", default=0, min_value=0))
    else:
        discount = _item_default_discount(item, qty, gross)
    if settings.get("txn_wise_tax", True):
        tax = _int_field(raw, "tax_paisa", default=0, min_value=0)
        if tax_bp is not None and settings.get("item_wise_tax", True):
            tax = _pct_of(
                gross - discount, _int_field(raw, "tax_bp", default=0, min_value=0))
    else:
        tax = 0
    if not settings.get("tax_enabled", True):
        tax = 0
    return discount, tax, discount_bp, tax_bp


def _prepare_lines(tx, biz, lines_in, settings):
    """Validate raw line input; returns (lines, subtotal_paisa,
    line_discount_paisa, line_tax_paisa)."""
    if not isinstance(lines_in, list) or not lines_in:
        raise ApiError("An invoice needs at least one line.")
    lines = []
    subtotal = 0
    line_discount = 0
    line_tax = 0
    for raw in lines_in:
        if not isinstance(raw, dict):
            raise ApiError("Each invoice line must be an object.")
        qty = _int_field(raw, "qty", required=True, min_value=1)
        rate = _int_field(raw, "rate_paisa", required=True, min_value=0)
        amount = qty * rate  # integer math — exact to the paisa
        subtotal += amount
        item = None
        if raw.get("item_id") is not None:
            item = tx.query_one(
                "SELECT * FROM items WHERE id = ? AND business_id = ?",
                (raw["item_id"], biz),
            )
            if item is None:
                raise ApiError("Item not found.")
        discount, tax, discount_bp, tax_bp = _line_money(
            raw, qty, rate, settings, item=item)
        line_discount += discount
        line_tax += tax
        lines.append(
            {
                "item_id": raw.get("item_id"),
                "description": str(raw.get("description") or "").strip(),
                "qty": qty,
                "rate_paisa": rate,
                "amount_paisa": amount,
                "discount_paisa": discount,
                "tax_paisa": tax,
                "discount_bp": discount_bp,
                "tax_bp": tax_bp,
                "free_qty": _int_field(raw, "free_qty", default=0, min_value=0),
                "batch_no": str(raw.get("batch_no") or "").strip(),
                "exp_date": str(raw.get("exp_date") or "").strip(),
                "serial_no": str(raw.get("serial_no") or "").strip(),
            }
        )
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
    return lines, subtotal, line_discount, line_tax


def _check_party(tx, biz, party_id):
    if party_id is None:
        return
    party = tx.query_one(
        "SELECT id FROM parties WHERE id = ? AND business_id = ?",
        (party_id, biz),
    )
    if party is None:
        raise ApiError("Party not found.")


def _prefix_for(settings, inv_type):
    prefixes = settings.get("prefixes") or {}
    prefix = str(prefixes.get(inv_type) or "").strip()
    return prefix or DOC_TYPES[inv_type]["prefix"]


def _low_stock_warnings(tx, biz, lines):
    """Items now below their Min Stock To Maintain (Vyapar low-stock
    rule). Items without a min stock warn when they run out."""
    warnings = []
    seen = set()
    for line in lines:
        if line["item_id"] is None or line["item_id"] in seen:
            continue
        seen.add(line["item_id"])
        item = tx.query_one(
            "SELECT name, stock_qty, min_stock FROM items WHERE id = ?",
            (line["item_id"],),
        )
        if item is None:
            continue
        min_stock = item["min_stock"] or 0
        low = item["stock_qty"] < min_stock if min_stock > 0 else item["stock_qty"] <= 0
        if low:
            warnings.append(
                {
                    "itemId": line["item_id"],
                    "name": item["name"],
                    "stockQty": item["stock_qty"],
                    "minStock": min_stock,
                }
            )
    return warnings


PAYMENT_MODES = ("cash", "cheque", "bank", "jazzcash", "easypaisa",
                 "card", "online")


def _prepare_payments(tx, biz, data, meta):
    """Split payments (Vyapar doc-form payment rows): the optional
    `payments` array on an invoice body. Returns None when the body
    carries no usable array — the caller then keeps the classic single
    paid_paisa behaviour exactly. Otherwise returns normalized rows;
    their amounts sum to the document's paid total."""
    raw_list = data.get("payments")
    if not isinstance(raw_list, list) or not raw_list:
        return None
    if meta["pay_direction"] is None:
        raise ApiError("Payments are not taken on this document type.")
    rows = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            raise ApiError("Each payment row must be an object.")
        amount = _int_field(raw, "amount_paisa", default=0, min_value=0)
        if amount == 0:
            continue
        mode = str(raw.get("mode") or "cash").strip().lower() or "cash"
        if mode not in PAYMENT_MODES:
            raise ApiError(
                "Payment type must be one of: {}.".format(
                    ", ".join(PAYMENT_MODES)))
        bank_account_id = raw.get("bank_account_id")
        if bank_account_id is not None:
            account = tx.query_one(
                "SELECT id FROM bank_accounts WHERE id = ? AND business_id = ?",
                (bank_account_id, biz),
            )
            if account is None:
                raise ApiError("Bank account not found.")
        if mode == "bank" and bank_account_id is None:
            raise ApiError("Choose a bank account for a bank payment row.")
        rows.append({
            "mode": mode,
            "amount_paisa": amount,
            "bank_account_id": bank_account_id,
            "cheque_no": str(raw.get("cheque_no") or "").strip(),
            "reference_no": str(raw.get("reference_no") or "").strip(),
        })
    return rows or None


def _insert_split_payments(tx, biz, party_id, inv_id, direction, rows,
                           ref_base, created_at):
    """One real payment row per split row, note 'at_document' like the
    classic single payment. A row tied to a bank account also lands in
    that account's transactions, exactly like a standalone bank
    payment, and carries the same client_ref so an edit can reverse
    both sides together."""
    for index, row in enumerate(rows):
        ref = "{}:pay{}".format(ref_base, index) if ref_base else None
        tx.execute(
            "INSERT INTO payments (business_id, party_id, invoice_id, "
            "direction, amount_paisa, client_ref, created_at, note, mode, "
            "bank_account_id, cheque_no, reference_no) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'at_document', ?, ?, ?, ?)",
            (biz, party_id, inv_id, direction, row["amount_paisa"], ref,
             created_at, row["mode"], row["bank_account_id"],
             row["cheque_no"], row["reference_no"]),
        )
        if row["bank_account_id"] is not None:
            tx.execute(
                "INSERT INTO bank_transactions (business_id, "
                "bank_account_id, kind, amount_paisa, note, created_at, "
                "client_ref) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (biz, row["bank_account_id"],
                 "deposit" if direction == "in" else "withdraw",
                 row["amount_paisa"],
                 "Payment {} — {}".format(
                     "In" if direction == "in" else "Out", row["mode"]),
                 created_at, ref),
            )


def _delete_atdoc_payments(tx, biz, invoice_id):
    """Remove an invoice's at-document payments (edit/delete path),
    reversing any bank-account transactions they created."""
    rows = tx.query_all(
        "SELECT client_ref FROM payments WHERE invoice_id = ? "
        "AND note = 'at_document'",
        (invoice_id,),
    )
    refs = [r["client_ref"] for r in rows if r["client_ref"]]
    if refs:
        tx.execute(
            "DELETE FROM bank_transactions WHERE business_id = ? "
            "AND client_ref IN ({})".format(", ".join("?" for _ in refs)),
            tuple([biz] + refs),
        )
    tx.execute(
        "DELETE FROM payments WHERE invoice_id = ? AND note = 'at_document'",
        (invoice_id,),
    )


def _insert_document(tx, biz, inv_type, party_id, lines, subtotal,
                     discount, tax, paid, client_ref, settings=None,
                     extra=None, loyalty_redeemed=0, payments=None):
    """Insert one document + its lines, apply its stock effect and its
    at-document payment (if any). Returns the new invoice row id."""
    settings = settings or {}
    extra = extra or {}
    meta = DOC_TYPES[inv_type]
    total = subtotal - discount + tax - loyalty_redeemed
    balance = total - paid
    count = tx.query_one(
        "SELECT COUNT(*) AS c FROM invoices WHERE business_id = ? AND type = ?",
        (biz, inv_type),
    )["c"]
    # Server-assigned per type (S-1.., EST-1..); devices can't collide.
    invoice_no = "{}-{}".format(_prefix_for(settings, inv_type), count + 1)
    inv_id = tx.insert(
        "INSERT INTO invoices (business_id, party_id, invoice_no, type, subtotal_paisa, "
        "discount_paisa, tax_paisa, total_paisa, paid_paisa, balance_paisa, client_ref, created_at, "
        "due_date, payment_terms, billing_address, shipping_address, transport_name, "
        "vehicle_no, delivery_date, delivery_location, po_number, description, terms_id, "
        "repeat_freq, attachment_name, loyalty_points_used, loyalty_redeemed_paisa, "
        "loyalty_points_earned, online_order_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "RETURNING id",
        (biz, party_id, invoice_no, inv_type, subtotal, discount, tax, total,
         paid, balance, client_ref, now_iso(),
         extra.get("due_date", ""), extra.get("payment_terms", ""),
         extra.get("billing_address", ""), extra.get("shipping_address", ""),
         extra.get("transport_name", ""), extra.get("vehicle_no", ""),
         extra.get("delivery_date", ""), extra.get("delivery_location", ""),
         extra.get("po_number", ""), extra.get("description", ""),
         extra.get("terms_id"), extra.get("repeat_freq", ""),
         extra.get("attachment_name", ""),
         extra.get("loyalty_points_used", 0), loyalty_redeemed,
         extra.get("loyalty_points_earned", 0), extra.get("online_order_id")),
    )
    stock_on = settings.get("stock_maintenance", True)
    for line in lines:
        tx.execute(
            "INSERT INTO invoice_lines (invoice_id, item_id, description, qty, "
            "rate_paisa, amount_paisa, discount_paisa, tax_paisa, discount_bp, "
            "tax_bp, free_qty, batch_no, exp_date, serial_no) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (inv_id, line["item_id"], line["description"], line["qty"],
             line["rate_paisa"], line["amount_paisa"], line["discount_paisa"],
             line["tax_paisa"], line["discount_bp"], line["tax_bp"],
             line["free_qty"], line["batch_no"], line["exp_date"],
             line["serial_no"]),
        )
        if line["item_id"] is not None and meta["stock"] and stock_on:
            units = line["qty"] + line["free_qty"]
            delta = meta["stock"] * units
            _apply_stock(tx, biz, line["item_id"], delta)
            if line["batch_no"]:
                _apply_batch(
                    tx, biz, line["item_id"], line["batch_no"],
                    line["exp_date"], delta,
                )
            if settings.get("update_sale_price_from_txn"):
                if inv_type == "sale":
                    tx.execute(
                        "UPDATE items SET sale_price_paisa = ? WHERE id = ?",
                        (line["rate_paisa"], line["item_id"]),
                    )
                elif inv_type == "purchase":
                    tx.execute(
                        "UPDATE items SET purchase_price_paisa = ? WHERE id = ?",
                        (line["rate_paisa"], line["item_id"]),
                    )
    if meta["pay_direction"] and payments:
        _insert_split_payments(
            tx, biz, party_id, inv_id, meta["pay_direction"], payments,
            client_ref or "inv{}".format(inv_id), now_iso())
    elif paid > 0 and meta["pay_direction"]:
        tx.execute(
            "INSERT INTO payments (business_id, party_id, invoice_id, direction, "
            "amount_paisa, client_ref, created_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, 'at_document')",
            (biz, party_id, inv_id, meta["pay_direction"],
             paid, "{}:pay".format(client_ref) if client_ref else None, now_iso()),
        )
    _audit(tx, biz, settings, inv_id, "created",
           "{} created (total {})".format(invoice_no, total))
    return inv_id


def _totals_check(subtotal, discount, tax, paid):
    if discount > subtotal:
        raise ApiError("Discount cannot be more than the subtotal.")
    total = subtotal - discount + tax
    if paid > total:
        raise ApiError("Paid amount cannot be more than the invoice total.")
    return total


def _invoice_extra(data):
    return {
        "due_date": str(data.get("due_date") or "").strip(),
        "payment_terms": str(data.get("payment_terms") or "").strip(),
        "billing_address": str(data.get("billing_address") or "").strip(),
        "shipping_address": str(data.get("shipping_address") or "").strip(),
        "transport_name": str(data.get("transport_name") or "").strip(),
        "vehicle_no": str(data.get("vehicle_no") or "").strip(),
        "delivery_date": str(data.get("delivery_date") or "").strip(),
        "delivery_location": str(data.get("delivery_location") or "").strip(),
        "po_number": str(data.get("po_number") or "").strip(),
        "description": str(data.get("description") or "").strip(),
        "terms_id": data.get("terms_id"),
        "repeat_freq": str(data.get("repeat_freq") or "").strip(),
        "attachment_name": str(data.get("attachment_name") or "").strip(),
    }


def _loyalty_for(tx, biz, settings, inv_type, party_id, total_after, paid,
                 points_used_in):
    """Loyalty Points on a sale: redemption reduces the amount due;
    points are earned on the amount actually received (paid + redeemed).
    Returns (redeemed_paisa, points_used, points_earned)."""
    loyalty = settings.get("loyalty") or {}
    if not loyalty.get("enabled") or inv_type != "sale" or party_id is None:
        return 0, 0, 0
    party = tx.query_one(
        "SELECT loyalty_points FROM parties WHERE id = ?", (party_id,)
    )
    available = (party or {}).get("loyalty_points") or 0
    used = 0
    redeemed = 0
    if points_used_in and int(points_used_in) > 0:
        value = int(loyalty.get("redeem_value_paisa") or 0)
        if value > 0 and total_after >= int(loyalty.get("min_invoice_redeem_paisa") or 0):
            max_redeem = total_after * int(loyalty.get("max_redeem_pct") or 0) // 100
            used = min(int(points_used_in), available, max_redeem // value)
            redeemed = used * value
    received = paid + redeemed
    earned = 0
    spend = int(loyalty.get("spend_per_point_paisa") or 0)
    if spend > 0 and received >= int(loyalty.get("min_invoice_earn_paisa") or 0):
        earned = received // spend
    if used or earned:
        tx.execute(
            "UPDATE parties SET loyalty_points = loyalty_points - ? + ? WHERE id = ?",
            (used, earned, party_id),
        )
    return redeemed, used, earned


@bp.post("/invoices")
@require_session
@require_approved
@require_unlocked
def create_invoice():
    data = _body()
    inv_type = str(data.get("type") or "sale").strip().lower()
    if inv_type not in DOC_TYPES:
        raise ApiError(
            "Document type must be one of: {}.".format(", ".join(sorted(DOC_TYPES)))
        )
    client_ref = _client_ref(data)
    discount_in = _int_field(data, "discount_paisa", default=0, min_value=0)
    tax_in = _int_field(data, "tax_paisa", default=0, min_value=0)
    paid = _int_field(data, "paid_paisa", default=0, min_value=0)
    party_id = data.get("party_id")
    if DOC_TYPES[inv_type]["pay_direction"] is None and paid > 0:
        raise ApiError("Payments are not taken on this document type.")

    with db.transaction() as tx:
        biz = _business_id(tx)
        settings = _settings(tx, biz)
        gate = _TYPE_SETTING_GATES.get(inv_type)
        if gate and not settings.get(gate, True):
            raise ApiError(
                "This transaction type is turned off in Settings > GENERAL "
                "(More Transactions)."
            )
        if client_ref:
            existing = tx.query_one(
                "SELECT * FROM invoices WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:  # offline replay: never double the stock
                return jsonify(ok=True, invoice=_invoice_dict(tx, existing)), 200
        _check_party(tx, biz, party_id)
        lines, subtotal, line_discount, line_tax = _prepare_lines(
            tx, biz, data.get("lines"), settings
        )
        pay_rows = _prepare_payments(tx, biz, data, DOC_TYPES[inv_type])
        if pay_rows is not None:
            paid = sum(r["amount_paisa"] for r in pay_rows)
        discount = discount_in + line_discount
        tax = tax_in + line_tax
        if not settings.get("tax_enabled", True):
            tax = 0
        _totals_check(subtotal, discount, tax, paid)
        if settings.get("stop_sale_negative_stock") and DOC_TYPES[inv_type]["stock"] < 0:
            for line in lines:
                if line["item_id"] is not None:
                    item = tx.query_one(
                        "SELECT name, stock_qty FROM items WHERE id = ?",
                        (line["item_id"],),
                    )
                    if item and item["stock_qty"] < line["qty"] + line["free_qty"]:
                        raise ApiError(
                            "Not enough stock for {} ({} available). Sale is "
                            "stopped on negative stock in Settings.".format(
                                item["name"], item["stock_qty"])
                        )
        total_after = subtotal - discount + tax
        redeemed, points_used, points_earned = _loyalty_for(
            tx, biz, settings, inv_type, party_id, total_after, paid,
            data.get("loyalty_points_used"),
        )
        if paid > total_after - redeemed:
            raise ApiError("Paid amount cannot be more than the invoice total.")
        extra = _invoice_extra(data)
        extra["loyalty_points_used"] = points_used
        extra["loyalty_points_earned"] = points_earned
        inv_id = _insert_document(
            tx, biz, inv_type, party_id, lines, subtotal,
            discount, tax, paid, client_ref, settings=settings, extra=extra,
            loyalty_redeemed=redeemed, payments=pay_rows,
        )
        row = tx.query_one("SELECT * FROM invoices WHERE id = ?", (inv_id,))
        result = _invoice_dict(tx, row)
        warnings = (
            _low_stock_warnings(tx, biz, lines)
            if DOC_TYPES[inv_type]["stock"] < 0 else []
        )
    return jsonify(ok=True, invoice=result, lowStock=warnings), 201


@bp.put("/invoices/<int:invoice_id>")
@require_session
@require_approved
@require_unlocked
def update_invoice(invoice_id):
    """Edit a document: old stock effects are reversed, the new lines
    are applied, and the change lands in the Edit History (audit
    trail). The invoice number and date never change. The body's
    paid_paisa is the at-document payment; standalone payments recorded
    against the invoice later are kept and still count."""
    data = _body()
    discount_in = _int_field(data, "discount_paisa", default=0, min_value=0)
    tax_in = _int_field(data, "tax_paisa", default=0, min_value=0)
    with db.transaction() as tx:
        biz = _business_id(tx)
        settings = _settings(tx, biz)
        row = tx.query_one(
            "SELECT * FROM invoices WHERE id = ? AND business_id = ? "
            "AND deleted_at IS NULL",
            (invoice_id, biz),
        )
        if row is None:
            raise ApiError("Invoice not found.", status=404)
        inv_type = row["type"]
        meta = DOC_TYPES[inv_type]
        old_lines = tx.query_all(
            "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY id",
            (invoice_id,),
        )
        if meta["stock"] and settings.get("stock_maintenance", True):
            for line in old_lines:
                if line["item_id"] is not None:
                    units = line["qty"] + (line.get("free_qty") or 0)
                    _apply_stock(tx, biz, line["item_id"], -meta["stock"] * units)
                    if line.get("batch_no"):
                        _apply_batch(tx, biz, line["item_id"], line["batch_no"],
                                     line.get("exp_date") or "",
                                     -meta["stock"] * units)
        _delete_atdoc_payments(tx, biz, invoice_id)
        standalone_paid = tx.query_one(
            "SELECT COALESCE(SUM(amount_paisa), 0) AS s FROM payments "
            "WHERE invoice_id = ?",
            (invoice_id,),
        )["s"]
        old_at_doc_paid = row["paid_paisa"] - standalone_paid
        tx.execute("DELETE FROM invoice_lines WHERE invoice_id = ?", (invoice_id,))
        party_id = data.get("party_id", row["party_id"])
        _check_party(tx, biz, party_id)
        lines, subtotal, line_discount, line_tax = _prepare_lines(
            tx, biz, data.get("lines"), settings
        )
        discount = discount_in + line_discount
        tax = tax_in + line_tax
        if not settings.get("tax_enabled", True):
            tax = 0
        pay_rows = _prepare_payments(tx, biz, data, meta)
        if pay_rows is not None:
            at_doc_paid = sum(r["amount_paisa"] for r in pay_rows)
        elif "paid_paisa" in data:
            at_doc_paid = _int_field(data, "paid_paisa", default=0, min_value=0)
        else:
            at_doc_paid = max(old_at_doc_paid, 0)
        redeemed = row.get("loyalty_redeemed_paisa") or 0
        total = subtotal - discount + tax - redeemed
        paid_total = standalone_paid + at_doc_paid
        if discount > subtotal:
            raise ApiError("Discount cannot be more than the subtotal.")
        if paid_total > total:
            raise ApiError("Paid amount cannot be more than the invoice total.")
        for line in lines:
            tx.execute(
                "INSERT INTO invoice_lines (invoice_id, item_id, description, qty, "
                "rate_paisa, amount_paisa, discount_paisa, tax_paisa, discount_bp, "
                "tax_bp, free_qty, batch_no, exp_date, serial_no) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (invoice_id, line["item_id"], line["description"], line["qty"],
                 line["rate_paisa"], line["amount_paisa"], line["discount_paisa"],
                 line["tax_paisa"], line["discount_bp"], line["tax_bp"],
                 line["free_qty"], line["batch_no"], line["exp_date"],
                 line["serial_no"]),
            )
            if line["item_id"] is not None and meta["stock"] \
                    and settings.get("stock_maintenance", True):
                units = line["qty"] + line["free_qty"]
                delta = meta["stock"] * units
                _apply_stock(tx, biz, line["item_id"], delta)
                if line["batch_no"]:
                    _apply_batch(tx, biz, line["item_id"], line["batch_no"],
                                 line["exp_date"], delta)
        extra = _invoice_extra(data)
        tx.execute(
            "UPDATE invoices SET party_id = ?, subtotal_paisa = ?, "
            "discount_paisa = ?, tax_paisa = ?, total_paisa = ?, paid_paisa = ?, "
            "balance_paisa = ?, due_date = ?, payment_terms = ?, "
            "billing_address = ?, shipping_address = ?, transport_name = ?, "
            "vehicle_no = ?, delivery_date = ?, delivery_location = ?, "
            "po_number = ?, description = ?, terms_id = ?, repeat_freq = ?, "
            "attachment_name = ? WHERE id = ?",
            (party_id, subtotal, discount, tax, total, paid_total,
             total - paid_total, extra["due_date"], extra["payment_terms"],
             extra["billing_address"], extra["shipping_address"],
             extra["transport_name"], extra["vehicle_no"],
             extra["delivery_date"], extra["delivery_location"],
             extra["po_number"], extra["description"], extra["terms_id"],
             extra["repeat_freq"], extra["attachment_name"], invoice_id),
        )
        if pay_rows and meta["pay_direction"]:
            _insert_split_payments(
                tx, biz, party_id, invoice_id, meta["pay_direction"],
                pay_rows, "inv{}".format(invoice_id), row["created_at"])
        elif at_doc_paid > 0 and meta["pay_direction"]:
            tx.execute(
                "INSERT INTO payments (business_id, party_id, invoice_id, direction, "
                "amount_paisa, client_ref, created_at, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'at_document')",
                (biz, party_id, invoice_id, meta["pay_direction"], at_doc_paid,
                 None, row["created_at"]),
            )
        _audit(tx, biz, settings, invoice_id, "edited",
               "{} edited (total {})".format(row["invoice_no"], total))
        row = tx.query_one("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
        result = _invoice_dict(tx, row)
    return jsonify(ok=True, invoice=result)


@bp.delete("/invoices/<int:invoice_id>")
@require_session
@require_approved
@require_unlocked
def delete_invoice(invoice_id):
    """Delete moves a document to the Recycle Bin (Utilities): its stock
    effect is reversed and it stops counting in balances and reports,
    but it can be restored or permanently deleted from there."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        settings = _settings(tx, biz)
        row = tx.query_one(
            "SELECT * FROM invoices WHERE id = ? AND business_id = ? "
            "AND deleted_at IS NULL",
            (invoice_id, biz),
        )
        if row is None:
            raise ApiError("Invoice not found.", status=404)
        meta = DOC_TYPES[row["type"]]
        if meta["stock"] and settings.get("stock_maintenance", True):
            lines = tx.query_all(
                "SELECT * FROM invoice_lines WHERE invoice_id = ?", (invoice_id,)
            )
            for line in lines:
                if line["item_id"] is not None:
                    units = line["qty"] + (line.get("free_qty") or 0)
                    _apply_stock(tx, biz, line["item_id"], -meta["stock"] * units)
                    if line.get("batch_no"):
                        _apply_batch(tx, biz, line["item_id"], line["batch_no"],
                                     line.get("exp_date") or "",
                                     -meta["stock"] * units)
        tx.execute(
            "UPDATE invoices SET deleted_at = ? WHERE id = ?",
            (now_iso(), invoice_id),
        )
        _audit(tx, biz, settings, invoice_id, "deleted",
               "{} moved to Recycle Bin".format(row["invoice_no"]))
    return jsonify(ok=True)


@bp.get("/invoices/<int:invoice_id>/history")
@require_session
@require_approved
@require_unlocked
def invoice_history(invoice_id):
    """Edit History: the audit trail of one transaction."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM audit_log WHERE business_id = ? AND invoice_id = ? "
            "ORDER BY id",
            (biz, invoice_id),
        )
    return jsonify(
        ok=True,
        history=[
            {"action": r["action"], "detail": r["detail"],
             "createdAt": r["created_at"]}
            for r in rows
        ],
    )


@bp.get("/parties/<int:party_id>/item-rate/<int:item_id>")
@require_session
@require_approved
@require_unlocked
def party_item_rate(party_id, item_id):
    """Party Wise Item Rate: the rate this party was last charged for
    this item on a sale invoice."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT l.rate_paisa FROM invoice_lines l "
            "JOIN invoices i ON i.id = l.invoice_id "
            "WHERE i.business_id = ? AND i.party_id = ? AND l.item_id = ? "
            "AND i.type = 'sale' AND i.deleted_at IS NULL "
            "ORDER BY l.id DESC LIMIT 1",
            (biz, party_id, item_id),
        )
    return jsonify(ok=True, ratePaisa=(row or {}).get("rate_paisa"))


@bp.post("/invoices/<int:invoice_id>/convert")
@require_session
@require_approved
@require_unlocked
def convert_invoice(invoice_id):
    """Estimate / Sale Order / Delivery Challan / Proforma -> Sale Invoice.

    Copies party, lines, discount and tax into a brand-new sale invoice
    (which takes the sale stock effect), and marks the source document
    as converted so it cannot be converted twice.
    """
    data = _body()
    client_ref = _client_ref(data)
    with db.transaction() as tx:
        biz = _business_id(tx)
        settings = _settings(tx, biz)
        if client_ref:
            existing = tx.query_one(
                "SELECT * FROM invoices WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:  # replay of an already-done conversion
                return jsonify(ok=True, invoice=_invoice_dict(tx, existing)), 200
        src = tx.query_one(
            "SELECT * FROM invoices WHERE id = ? AND business_id = ? "
            "AND deleted_at IS NULL",
            (invoice_id, biz),
        )
        if src is None:
            raise ApiError("Document not found.", status=404)
        if (src.get("type") or "sale") not in CONVERTIBLE_TYPES:
            raise ApiError(
                "Only an Estimate, Sale Order, Delivery Challan or Proforma "
                "Invoice can be converted to a Sale Invoice."
            )
        if src.get("converted_to_id"):
            raise ApiError("This document was already converted to a Sale Invoice.")
        src_lines = tx.query_all(
            "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY id",
            (src["id"],),
        )
        lines = [
            {
                "item_id": l["item_id"],
                "description": l["description"],
                "qty": l["qty"],
                "rate_paisa": l["rate_paisa"],
                "amount_paisa": l["amount_paisa"],
                # Stored per-line copies move to the new document's
                # invoice-level discount/tax (the source totals already
                # include them), so zero them here to avoid double
                # counting; the printed lines keep the source amounts
                # via the invoice-level totals.
                "discount_paisa": 0,
                "tax_paisa": 0,
                "discount_bp": None,
                "tax_bp": None,
                "free_qty": l.get("free_qty") or 0,
                "batch_no": l.get("batch_no") or "",
                "exp_date": l.get("exp_date") or "",
                "serial_no": l.get("serial_no") or "",
            }
            for l in src_lines
        ]
        subtotal = sum(l["amount_paisa"] for l in lines)
        extra = {
            "due_date": src.get("due_date") or "",
            "payment_terms": src.get("payment_terms") or "",
            "billing_address": src.get("billing_address") or "",
            "shipping_address": src.get("shipping_address") or "",
            "transport_name": src.get("transport_name") or "",
            "vehicle_no": src.get("vehicle_no") or "",
            "delivery_date": src.get("delivery_date") or "",
            "delivery_location": src.get("delivery_location") or "",
            "po_number": src.get("po_number") or "",
            "description": src.get("description") or "",
        }
        new_id = _insert_document(
            tx, biz, "sale", src["party_id"], lines, subtotal,
            src["discount_paisa"], src["tax_paisa"], 0, client_ref,
            settings=settings, extra=extra,
        )
        tx.execute(
            "UPDATE invoices SET converted_to_id = ? WHERE id = ?",
            (new_id, src["id"]),
        )
        _audit(tx, biz, settings, src["id"], "converted",
               "{} converted to Sale Invoice".format(src["invoice_no"]))
        row = tx.query_one("SELECT * FROM invoices WHERE id = ?", (new_id,))
        result = _invoice_dict(tx, row)
    return jsonify(ok=True, invoice=result), 201


@bp.get("/invoices")
@require_session
@require_approved
@require_unlocked
def list_invoices():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM invoices WHERE business_id = ? AND deleted_at IS NULL "
            "ORDER BY id",
            (biz,),
        )
        result = [_invoice_dict(tx, r) for r in rows]
    return jsonify(ok=True, invoices=result)


@bp.get("/invoices/<int:invoice_id>")
@require_session
@require_approved
@require_unlocked
def get_invoice(invoice_id):
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM invoices WHERE id = ? AND business_id = ? "
            "AND deleted_at IS NULL",
            (invoice_id, biz),
        )
        if row is None:
            raise ApiError("Invoice not found.", status=404)
        result = _invoice_dict(tx, row)
    return jsonify(ok=True, invoice=result)


# ------------------------------------------------------------- payments ----
def payment_ref(settings, payment_id):
    """Display reference for a payment, e.g. PIN-12 (Settings >
    TRANSACTION > Transaction Prefixes > Payment In)."""
    prefix = str((settings.get("prefixes") or {}).get("payment_in") or "").strip()
    return "{}-{}".format(prefix or "PIN", payment_id)


@bp.get("/payments")
@require_session
@require_approved
@require_unlocked
def list_payments():
    with db.transaction() as tx:
        biz = _business_id(tx)
        settings = _settings(tx, biz)
        rows = tx.query_all(
            "SELECT * FROM payments WHERE business_id = ? ORDER BY id", (biz,)
        )
    result = []
    for r in rows:
        entry = _payment_row_dict(r)
        entry["ref"] = payment_ref(settings, r["id"])
        result.append(entry)
    return jsonify(ok=True, payments=result)
