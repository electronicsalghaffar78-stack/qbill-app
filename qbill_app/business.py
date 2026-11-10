"""Business profile, expenses, standalone payments, dashboard, P&L, ledger.

All money is integer paisa; every total below is exact integer arithmetic.
Dates: created_at is an ISO-8601 UTC string, so day/month bucketing is a
string prefix match (substr) — "today" and "this month" are UTC, the same
clock the rows were written with.
"""
import base64
import binascii
from datetime import datetime, timezone

from flask import Blueprint, Response, g, jsonify, request

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
    """The dashboard numbers for one business (exact paisa), plus the
    Home extras: the Total Sale graph series, the top You'll Receive
    party, expiring stock and the bank balance."""
    today, month = _today(), _month()
    today_sales = tx.query_one(
        "SELECT COALESCE(SUM(total_paisa), 0) AS s FROM invoices "
        "WHERE business_id = ? AND type = 'sale' AND deleted_at IS NULL "
        "AND substr(created_at, 1, 10) = ?",
        (business_id, today),
    )["s"]
    month_sales = tx.query_one(
        "SELECT COALESCE(SUM(total_paisa), 0) AS s FROM invoices "
        "WHERE business_id = ? AND type = 'sale' AND deleted_at IS NULL "
        "AND substr(created_at, 1, 7) = ?",
        (business_id, month),
    )["s"]
    balances = party_balance_map(tx, business_id)
    receivable = sum(b for b in balances.values() if b > 0)
    cash_in = tx.query_one(
        "SELECT COALESCE(SUM(amount_paisa), 0) AS s FROM payments "
        "WHERE business_id = ? AND direction = 'in' "
        "AND (invoice_id IS NULL OR invoice_id IN "
        "     (SELECT id FROM invoices WHERE deleted_at IS NULL))",
        (business_id,),
    )["s"]
    cash_out = tx.query_one(
        "SELECT COALESCE(SUM(amount_paisa), 0) AS s FROM payments "
        "WHERE business_id = ? AND direction = 'out' "
        "AND (invoice_id IS NULL OR invoice_id IN "
        "     (SELECT id FROM invoices WHERE deleted_at IS NULL))",
        (business_id,),
    )["s"]
    expenses = tx.query_one(
        "SELECT COALESCE(SUM(amount_paisa), 0) AS s FROM expenses WHERE business_id = ? AND deleted_at IS NULL",
        (business_id,),
    )["s"]
    stock_value = tx.query_one(
        "SELECT COALESCE(SUM(stock_qty * purchase_price_paisa), 0) AS s "
        "FROM items WHERE business_id = ? AND deleted_at IS NULL",
        (business_id,),
    )["s"]
    # Total Sale graph: daily sale totals for the last 30 days, plus
    # this month vs last month for the "% vs last month" badge.
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    series = []
    for back in range(29, -1, -1):
        day = (now - timedelta(days=back)).strftime("%Y-%m-%d")
        amount = tx.query_one(
            "SELECT COALESCE(SUM(total_paisa), 0) AS s FROM invoices "
            "WHERE business_id = ? AND type = 'sale' AND deleted_at IS NULL "
            "AND substr(created_at, 1, 10) = ?",
            (business_id, day),
        )["s"]
        series.append({"date": day, "amountPaisa": amount})
    if now.month == 1:
        last_month = "{}-12".format(now.year - 1)
    else:
        last_month = "{}-{:02d}".format(now.year, now.month - 1)
    last_month_sales = tx.query_one(
        "SELECT COALESCE(SUM(total_paisa), 0) AS s FROM invoices "
        "WHERE business_id = ? AND type = 'sale' AND deleted_at IS NULL "
        "AND substr(created_at, 1, 7) = ?",
        (business_id, last_month),
    )["s"]
    if last_month_sales:
        pct = round((month_sales - last_month_sales) * 100.0 / last_month_sales, 1)
    else:
        pct = 100.0 if month_sales else 0.0
    # You'll Receive: the party who owes the most.
    top_party = None
    if balances:
        top_id = max(balances, key=lambda pid: balances[pid])
        if balances[top_id] > 0:
            prow = tx.query_one(
                "SELECT name FROM parties WHERE id = ?", (top_id,)
            )
            top_party = {
                "name": (prow or {}).get("name") or "",
                "amountPaisa": balances[top_id],
            }
    # You'll Pay: the party we owe the most (audit 2026-11-10 — the Home
    # card names the top party, like You'll Receive does).
    top_payable = None
    if balances:
        low_id = min(balances, key=lambda pid: balances[pid])
        if balances[low_id] < 0:
            prow = tx.query_one(
                "SELECT name FROM parties WHERE id = ?", (low_id,)
            )
            top_payable = {
                "name": (prow or {}).get("name") or "",
                "amountPaisa": -balances[low_id],
            }
    # Expiring stock: batches expiring within 30 days that still hold stock.
    horizon = (now + timedelta(days=30)).strftime("%Y-%m")
    expiring = tx.query_all(
        "SELECT b.batch_no, b.exp_date, b.qty, i.name AS item_name "
        "FROM item_batches b JOIN items i ON i.id = b.item_id "
        "WHERE b.business_id = ? AND b.qty > 0 AND b.exp_date != '' "
        "AND substr(b.exp_date, 1, 7) <= ? ORDER BY b.exp_date LIMIT 20",
        (business_id, horizon),
    )
    low_stock = tx.query_one(
        "SELECT COUNT(*) AS c FROM items WHERE business_id = ? AND deleted_at IS NULL AND "
        "((min_stock > 0 AND stock_qty < min_stock) OR "
        " (min_stock = 0 AND stock_qty <= 0))",
        (business_id,),
    )["c"]
    bank_balance = 0
    for account in tx.query_all(
        "SELECT * FROM bank_accounts WHERE business_id = ?", (business_id,)
    ):
        bank_balance += account["opening_balance_paisa"] + tx.query_one(
            "SELECT COALESCE(SUM(CASE WHEN kind = 'withdraw' THEN -amount_paisa "
            "ELSE amount_paisa END), 0) AS s FROM bank_transactions "
            "WHERE bank_account_id = ?",
            (account["id"],),
        )["s"]
    # Home Purchase card: this month's purchase total + the supplier
    # with the largest purchase total this month.
    month_purchases = tx.query_one(
        "SELECT COALESCE(SUM(total_paisa), 0) AS s FROM invoices "
        "WHERE business_id = ? AND type = 'purchase' AND deleted_at IS NULL "
        "AND substr(created_at, 1, 7) = ?",
        (business_id, month),
    )["s"]
    top_purchase = tx.query_one(
        "SELECT p.name AS name, SUM(i.total_paisa) AS s FROM invoices i "
        "JOIN parties p ON p.id = i.party_id "
        "WHERE i.business_id = ? AND i.type = 'purchase' "
        "AND i.deleted_at IS NULL AND substr(i.created_at, 1, 7) = ? "
        "GROUP BY i.party_id ORDER BY s DESC LIMIT 1",
        (business_id, month),
    )
    top_purchase_party = (
        {"name": top_purchase["name"], "amountPaisa": top_purchase["s"]}
        if top_purchase else None
    )
    # Expenses mini chart on Home: daily expense totals, last 30 days.
    expenses_series = []
    for back in range(29, -1, -1):
        day = (now - timedelta(days=back)).strftime("%Y-%m-%d")
        amount = tx.query_one(
            "SELECT COALESCE(SUM(amount_paisa), 0) AS s FROM expenses "
            "WHERE business_id = ? AND deleted_at IS NULL AND substr(created_at, 1, 10) = ?",
            (business_id, day),
        )["s"]
        expenses_series.append({"date": day, "amountPaisa": amount})
    # Home "Open Cheques" card: cheque-mode payments, received vs paid.
    cheque_rows = tx.query_all(
        "SELECT direction, COUNT(*) AS c, COALESCE(SUM(amount_paisa), 0) AS s "
        "FROM payments WHERE business_id = ? AND mode = 'cheque' "
        "AND (invoice_id IS NULL OR invoice_id IN "
        "     (SELECT id FROM invoices WHERE deleted_at IS NULL)) "
        "GROUP BY direction",
        (business_id,),
    )
    open_cheques = {
        "receivedCount": 0, "receivedPaisa": 0, "paidCount": 0, "paidPaisa": 0,
    }
    for r in cheque_rows:
        if r["direction"] == "in":
            open_cheques["receivedCount"] = r["c"]
            open_cheques["receivedPaisa"] = r["s"]
        else:
            open_cheques["paidCount"] = r["c"]
            open_cheques["paidPaisa"] = r["s"]
    # Home's PROFIT / EXPENSES (THIS MONTH) cards ride in THIS payload
    # (round-3 fix): they used to come from a second /api/pnl fetch, so
    # on a slow connection a stale cached dashboard (all zeros from the
    # empty account) rendered beside live P&L numbers and the screen
    # disagreed with itself. One fetch = one consistent snapshot; the
    # figures come from compute_pnl over the same month window.
    month_pnl = compute_pnl(tx, business_id, month + "-01", today)
    return {
        "todaySalesPaisa": today_sales,
        "monthSalesPaisa": month_sales,
        "monthExpensesPaisa": month_pnl["expensesPaisa"],
        "monthProfitPaisa": month_pnl["profitPaisa"],
        "totalReceivablePaisa": receivable,
        "cashInHandPaisa": cash_in - cash_out - expenses,
        "stockValuePaisa": stock_value,
        "salesSeries": series,
        "salesVsLastMonthPct": pct,
        "lastMonthSalesPaisa": last_month_sales,
        "topReceivableParty": top_party,
        "topPayableParty": top_payable,
        "expiringItems": [
            {
                "itemName": r["item_name"],
                "batchNo": r["batch_no"],
                "expDate": r["exp_date"],
                "qty": r["qty"],
            }
            for r in expiring
        ],
        "lowStockCount": low_stock,
        "bankBalancePaisa": bank_balance,
        "monthPurchasesPaisa": month_purchases,
        "topPurchaseParty": top_purchase_party,
        "expensesSeries": expenses_series,
        "openCheques": open_cheques,
    }


def _sales_cogs(tx, business_id, date_from, date_to):
    """Cost of the goods actually sold in the window: for every line of
    every sale invoice, (qty + free qty) x the item's purchase price —
    the same cost rule as the Bill Wise Profit report, so every profit
    figure in the app tells the same story. Free-text lines and lines
    whose item is gone cost 0."""
    return tx.query_one(
        "SELECT COALESCE(SUM((l.qty + COALESCE(l.free_qty, 0)) * "
        "COALESCE(i.purchase_price_paisa, 0)), 0) AS s "
        "FROM invoice_lines l "
        "JOIN invoices inv ON inv.id = l.invoice_id "
        "LEFT JOIN items i ON i.id = l.item_id "
        "WHERE inv.business_id = ? AND inv.type = 'sale' "
        "AND inv.deleted_at IS NULL "
        "AND substr(inv.created_at, 1, 10) >= ? "
        "AND substr(inv.created_at, 1, 10) <= ?",
        (business_id, date_from, date_to),
    )["s"]


def compute_pnl(tx, business_id, date_from, date_to):
    sales = tx.query_one(
        "SELECT COALESCE(SUM(total_paisa), 0) AS s FROM invoices "
        "WHERE business_id = ? AND type = 'sale' AND deleted_at IS NULL "
        "AND substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ?",
        (business_id, date_from, date_to),
    )["s"]
    cogs = _sales_cogs(tx, business_id, date_from, date_to)
    purchases = tx.query_one(
        "SELECT COALESCE(SUM(total_paisa), 0) AS s FROM invoices "
        "WHERE business_id = ? AND type = 'purchase' AND deleted_at IS NULL "
        "AND substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ?",
        (business_id, date_from, date_to),
    )["s"]
    expenses = tx.query_one(
        "SELECT COALESCE(SUM(amount_paisa), 0) AS s FROM expenses "
        "WHERE business_id = ? AND deleted_at IS NULL "
        "AND substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ?",
        (business_id, date_from, date_to),
    )["s"]
    other_income = tx.query_one(
        "SELECT COALESCE(SUM(amount_paisa), 0) AS s FROM other_incomes "
        "WHERE business_id = ? AND deleted_at IS NULL "
        "AND substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ?",
        (business_id, date_from, date_to),
    )["s"]
    return {
        "from": date_from,
        "to": date_to,
        "salesPaisa": sales,
        "purchasesPaisa": purchases,
        "cogsPaisa": cogs,
        "expensesPaisa": expenses,
        "otherIncomePaisa": other_income,
        # Profit is the MARGIN story (round-3 fix, 2026-11-10): what the
        # sales earned above the cost of the goods sold, minus expenses,
        # plus other income. Purchases of stock still on the shelf are
        # NOT an expense here — they enter profit only when sold, via
        # COGS. (The old formula subtracted ALL purchases in the window,
        # so Home's PROFIT never matched the sales beside it.)
        "profitPaisa": sales - cogs - expenses + other_income,
    }


# ------------------------------------------------------- business profile --
def _business_row(tx, business_id):
    return tx.query_one("SELECT * FROM businesses WHERE id = ?", (business_id,))


def _profile_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "ownerName": row.get("owner_name") or "",
        "address": row.get("address") or "",
        "city": row.get("city") or "",
        "phone": row.get("phone") or "",
        "email": row.get("email") or "",
        "taxNote": row.get("tax_note") or "",
        "terms": row.get("terms") or "",
        "businessType": row.get("business_type") or "",
        "businessCategory": row.get("business_category") or "",
        "pincode": row.get("pincode") or "",
        "state": row.get("state") or "",
        "signatureText": row.get("signature_text") or "",
        # The logo image itself is served from /api/business/logo; the
        # profile only carries the flag + URL so the (possibly large)
        # base64 never rides along with every profile fetch.
        "hasLogo": bool(row.get("logo_data")),
        "logoUrl": "/api/business/logo" if row.get("logo_data") else "",
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
            "UPDATE businesses SET name = ?, owner_name = ?, address = ?, "
            "city = ?, phone = ?, email = ?, "
            "tax_note = ?, terms = ?, business_type = ?, business_category = ?, "
            "pincode = ?, state = ?, signature_text = ? WHERE id = ?",
            (
                name if "name" in data else row["name"],
                str(data.get("owner_name", row.get("owner_name") or "") or "").strip(),
                str(data.get("address", row.get("address") or "") or "").strip() or None,
                str(data.get("city", row.get("city") or "") or "").strip(),
                str(data.get("phone", row.get("phone") or "") or "").strip() or None,
                str(data.get("email", row.get("email") or "") or "").strip() or None,
                str(data.get("tax_note", row.get("tax_note") or "") or "").strip() or None,
                str(data.get("terms", row.get("terms") or "") or "").strip() or None,
                str(data.get("business_type", row.get("business_type") or "") or "").strip(),
                str(data.get("business_category", row.get("business_category") or "") or "").strip(),
                str(data.get("pincode", row.get("pincode") or "") or "").strip(),
                str(data.get("state", row.get("state") or "") or "").strip(),
                str(data.get("signature_text", row.get("signature_text") or "") or "").strip(),
                biz,
            ),
        )
        row = _business_row(tx, biz)
    return jsonify(ok=True, business=_profile_dict(row))


# ------------------------------------------------------- business logo --
LOGO_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
LOGO_MAX_BYTES = 400 * 1024


def _logo_response(row):
    if not row or not row.get("logo_data"):
        raise ApiError("No logo is set for this business.", status=404)
    try:
        raw = base64.b64decode(row["logo_data"], validate=True)
    except (binascii.Error, ValueError):
        raise ApiError("The stored logo is damaged.", status=500)
    resp = Response(raw, mimetype=row.get("logo_mime") or "image/png")
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@bp.get("/business/logo")
@require_session
@require_approved
@require_unlocked
def get_business_logo():
    with db.transaction() as tx:
        row = _business_row(tx, _business_id(tx))
    return _logo_response(row)


@bp.put("/business/logo")
@require_session
@require_approved
@require_unlocked
def set_business_logo():
    """Upload (or remove) THIS account's business logo. The image is
    sent as a data URL in JSON so it rides the normal API; it is
    stored on the business row itself — per account, never shared."""
    data = _body()
    logo = data.get("logo_data")
    with db.transaction() as tx:
        biz = _business_id(tx)
        if logo is None or logo == "":
            tx.execute(
                "UPDATE businesses SET logo_mime = '', logo_data = NULL WHERE id = ?",
                (biz,),
            )
        else:
            if not isinstance(logo, str) or not logo.startswith("data:"):
                raise ApiError("Logo must be an image file (PNG, JPG, WebP or GIF).")
            header, _, payload = logo.partition(",")
            mime = header[5:].split(";")[0].strip().lower()
            if mime not in LOGO_MIMES or ";base64" not in header:
                raise ApiError("Logo must be a PNG, JPG, WebP or GIF image.")
            try:
                raw = base64.b64decode(payload, validate=True)
            except (binascii.Error, ValueError):
                raise ApiError("The logo image could not be read.")
            if not raw:
                raise ApiError("The logo image is empty.")
            if len(raw) > LOGO_MAX_BYTES:
                raise ApiError("Logo is too large — please use an image under 400 KB.")
            tx.execute(
                "UPDATE businesses SET logo_mime = ?, logo_data = ? WHERE id = ?",
                (mime, payload, biz),
            )
        row = _business_row(tx, biz)
    return jsonify(ok=True, business=_profile_dict(row))


# --------------------------------------------------------------- expenses --
def _expense_dict(row):
    amount = row["amount_paisa"]
    paid = row.get("paid_paisa")
    if paid is None:
        paid = amount
    return {
        "id": row["id"],
        "title": row["title"],
        "category": row["category"],
        "amountPaisa": amount,
        "createdAt": row["created_at"],
        "partyId": row.get("party_id"),
        "paidPaisa": paid,
        "balancePaisa": amount - paid,
        "expenseItem": row.get("expense_item") or "",
        "categoryType": row.get("category_type") or "",
        "taxPaisa": row.get("tax_paisa") or 0,
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
        party_id = data.get("party_id")
        if party_id is not None:
            party = tx.query_one(
                "SELECT id FROM parties WHERE id = ? AND business_id = ?",
                (party_id, biz),
            )
            if party is None:
                raise ApiError("Party not found.")
        paid = data.get("paid_paisa")
        if paid is not None:
            paid = _int_field(data, "paid_paisa", min_value=0)
            if paid > amount:
                raise ApiError("Paid amount cannot be more than the expense amount.")
        category_type = str(data.get("category_type") or "").strip()
        if category:
            # Remember the category and its Direct/Indirect type so the
            # Expenses reports can group by it.
            tx.execute(
                "INSERT INTO expense_categories (business_id, name, ctype) "
                "VALUES (?, ?, ?) ON CONFLICT(business_id, name) DO NOTHING",
                (biz, category, category_type or "Indirect Expense"),
            )
            if not category_type:
                crow = tx.query_one(
                    "SELECT ctype FROM expense_categories "
                    "WHERE business_id = ? AND name = ?",
                    (biz, category),
                )
                category_type = (crow or {}).get("ctype") or ""
        expense_item = str(data.get("expense_item") or "").strip()
        if expense_item:
            tx.execute(
                "INSERT INTO expense_items (business_id, name) VALUES (?, ?) "
                "ON CONFLICT(business_id, name) DO NOTHING",
                (biz, expense_item),
            )
        eid = tx.insert(
            "INSERT INTO expenses (business_id, title, category, amount_paisa, client_ref, created_at, "
            "party_id, paid_paisa, expense_item, category_type, tax_paisa) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (biz, title, category, amount, client_ref, now_iso(), party_id,
             paid, expense_item, category_type,
             _int_field(data, "tax_paisa", default=0, min_value=0)),
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
            "SELECT * FROM expenses WHERE business_id = ? AND deleted_at IS NULL ORDER BY id", (biz,)
        )
    return jsonify(ok=True, expenses=[_expense_dict(r) for r in rows])


@bp.delete("/expenses/<int:expense_id>")
@require_session
@require_approved
@require_unlocked
def delete_expense(expense_id):
    """Delete moves an expense to the Recycle Bin (Utilities): it stops
    counting in the dashboard, P&L and Day Book until restored."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM expenses WHERE id = ? AND business_id = ? "
            "AND deleted_at IS NULL",
            (expense_id, biz),
        )
        if row is None:
            raise ApiError("Expense not found.", status=404)
        tx.execute(
            "UPDATE expenses SET deleted_at = ? WHERE id = ?",
            (now_iso(), expense_id),
        )
    return jsonify(ok=True)


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
    discount = _int_field(data, "discount_paisa", default=0, min_value=0)
    party_id = data.get("party_id")
    invoice_id = data.get("invoice_id")
    client_ref = _client_ref(data)
    mode = str(data.get("mode") or "cash").strip() or "cash"
    bank_account_id = data.get("bank_account_id")
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
        if bank_account_id is not None:
            account = tx.query_one(
                "SELECT id FROM bank_accounts WHERE id = ? AND business_id = ?",
                (bank_account_id, biz),
            )
            if account is None:
                raise ApiError("Bank account not found.")
        invoice = None
        if invoice_id is not None:
            invoice = tx.query_one(
                "SELECT * FROM invoices WHERE id = ? AND business_id = ? "
                "AND deleted_at IS NULL",
                (invoice_id, biz),
            )
            if invoice is None:
                raise ApiError("Invoice not found.")
            if invoice["type"] in ("estimate", "sale_order", "purchase_order", "challan", "proforma"):
                raise ApiError(
                    "Payments are recorded against invoices and bills, not against "
                    "estimates, orders or challans."
                )
            if amount + discount > invoice["balance_paisa"]:
                raise ApiError("Payment is more than the invoice balance.")
            if party_id is None:
                party_id = invoice["party_id"]
        elif discount > 0:
            raise ApiError("A payment discount needs an invoice to settle.")
        pid = tx.insert(
            "INSERT INTO payments (business_id, party_id, invoice_id, direction, "
            "amount_paisa, client_ref, created_at, mode, bank_account_id, "
            "cheque_no, reference_no, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (biz, party_id, invoice_id, direction, amount, client_ref, now_iso(),
             mode, bank_account_id,
             str(data.get("cheque_no") or "").strip(),
             str(data.get("reference_no") or "").strip(),
             str(data.get("note") or "").strip()),
        )
        if discount > 0 and invoice is not None:
            # Discount During Payments (Settings > TRANSACTION): the
            # waived part is its own payment row so party balances and
            # the invoice both settle exactly.
            tx.execute(
                "INSERT INTO payments (business_id, party_id, invoice_id, "
                "direction, amount_paisa, client_ref, created_at, mode, note) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 'Payment discount')",
                (biz, party_id, invoice_id, direction, discount, now_iso(), mode),
            )
        if invoice is not None:
            # Settle against the invoice: 'in' pays a sale down, 'out' pays
            # a purchase down; either way the open balance shrinks.
            new_paid = invoice["paid_paisa"] + amount + discount
            tx.execute(
                "UPDATE invoices SET paid_paisa = ?, balance_paisa = ? WHERE id = ?",
                (new_paid, invoice["total_paisa"] - new_paid, invoice_id),
            )
        if bank_account_id is not None:
            tx.execute(
                "INSERT INTO bank_transactions (business_id, bank_account_id, "
                "kind, amount_paisa, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (biz, bank_account_id,
                 "deposit" if direction == "in" else "withdraw", amount,
                 "Payment {} — {}".format(
                     "In" if direction == "in" else "Out", mode), now_iso()),
            )
        row = tx.query_one("SELECT * FROM payments WHERE id = ?", (pid,))
    return jsonify(ok=True, payment=_payment_dict(row)), 201


@bp.delete("/payments/<int:payment_id>")
@require_session
@require_approved
@require_unlocked
def delete_payment(payment_id):
    """Delete a standalone payment. Two kinds are refused with a clear
    reason instead of corrupting the books: a payment recorded against
    a bill (it settled that bill — remove it by editing or deleting
    the bill, which reverses its payments exactly), and a payment made
    through a bank account (its bank entry has no separate reversal
    link, so deleting here would leave the bank balance wrong). Plain
    cash payments and cash adjustments delete cleanly: every balance
    is computed from the payments table, so totals stay exact."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM payments WHERE id = ? AND business_id = ?",
            (payment_id, biz),
        )
        if row is None:
            raise ApiError("Payment not found.", status=404)
        if row.get("invoice_id") is not None:
            inv = tx.query_one(
                "SELECT invoice_no FROM invoices WHERE id = ?",
                (row["invoice_id"],),
            )
            raise ApiError(
                "This payment is recorded against bill {} — delete or "
                "edit that bill to remove it, so the bill's balance "
                "stays correct.".format(
                    (inv or {}).get("invoice_no") or ""))
        if row.get("bank_account_id") is not None:
            raise ApiError(
                "This payment went through a bank account, so it cannot "
                "be deleted here without breaking the bank balance.")
        tx.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
    return jsonify(ok=True)


@bp.post("/payments/allocate")
@require_session
@require_approved
@require_unlocked
def allocate_payment():
    """Link Payments to Invoices: one payment from a party is spread
    over their open bills, oldest first, until it runs out."""
    data = _body()
    direction = str(data.get("direction") or "in").strip().lower()
    if direction not in ("in", "out"):
        raise ApiError("Payment direction must be 'in' or 'out'.")
    amount = _int_field(data, "amount_paisa", required=True, min_value=1)
    party_id = data.get("party_id")
    if party_id is None:
        raise ApiError("Party is required.")
    types = ("sale", "sale_return") if direction == "in" else ("purchase", "purchase_return")
    client_ref = _client_ref(data)
    allocations = []
    with db.transaction() as tx:
        biz = _business_id(tx)
        party = tx.query_one(
            "SELECT id FROM parties WHERE id = ? AND business_id = ?",
            (party_id, biz),
        )
        if party is None:
            raise ApiError("Party not found.")
        open_invoices = tx.query_all(
            "SELECT * FROM invoices WHERE business_id = ? AND party_id = ? "
            "AND deleted_at IS NULL AND balance_paisa > 0 "
            "AND type IN ({}) ORDER BY id".format(
                ", ".join("'{}'".format(t) for t in types)
            ),
            (biz, party_id),
        )
        remaining = amount
        for index, inv in enumerate(open_invoices):
            if remaining <= 0:
                break
            part = min(remaining, inv["balance_paisa"])
            ref = "{}:part{}".format(client_ref, index) if client_ref else None
            if ref:
                already = tx.query_one(
                    "SELECT id FROM payments WHERE business_id = ? AND client_ref = ?",
                    (biz, ref),
                )
                if already is not None:
                    remaining -= part
                    allocations.append(
                        {"invoiceId": inv["id"], "invoiceNo": inv["invoice_no"],
                         "amountPaisa": part}
                    )
                    continue
            tx.execute(
                "INSERT INTO payments (business_id, party_id, invoice_id, "
                "direction, amount_paisa, client_ref, created_at, mode, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Allocated payment')",
                (biz, party_id, inv["id"], direction, part, ref, now_iso(),
                 str(data.get("mode") or "cash")),
            )
            new_paid = inv["paid_paisa"] + part
            tx.execute(
                "UPDATE invoices SET paid_paisa = ?, balance_paisa = ? WHERE id = ?",
                (new_paid, inv["total_paisa"] - new_paid, inv["id"]),
            )
            remaining -= part
            allocations.append(
                {"invoiceId": inv["id"], "invoiceNo": inv["invoice_no"],
                 "amountPaisa": part}
            )
    return jsonify(
        ok=True, allocations=allocations, unallocatedPaisa=remaining
    ), 201


def _payment_dict(row):
    return {
        "id": row["id"],
        "partyId": row["party_id"],
        "invoiceId": row["invoice_id"],
        "direction": row["direction"],
        "amountPaisa": row["amount_paisa"],
        "createdAt": row["created_at"],
        "mode": row.get("mode") or "cash",
        "bankAccountId": row.get("bank_account_id"),
        "chequeNo": row.get("cheque_no") or "",
        "referenceNo": row.get("reference_no") or "",
        "note": row.get("note") or "",
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
            "SELECT * FROM parties WHERE id = ? AND business_id = ? "
            "AND deleted_at IS NULL",
            (party_id, biz),
        )
        if party is None:
            raise ApiError("Party not found.", status=404)
        invoices = tx.query_all(
            "SELECT * FROM invoices WHERE business_id = ? AND party_id = ? "
            "AND deleted_at IS NULL ORDER BY id",
            (biz, party_id),
        )
        payments = tx.query_all(
            "SELECT * FROM payments WHERE business_id = ? AND party_id = ? "
            "AND (invoice_id IS NULL OR invoice_id IN "
            "     (SELECT id FROM invoices WHERE deleted_at IS NULL)) ORDER BY id",
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
    "proforma": "Proforma Invoice",
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
            "SELECT * FROM invoices WHERE business_id = ? AND deleted_at IS NULL "
            "AND substr(created_at, 1, 10) = ? ORDER BY id",
            (biz, date),
        )
        pays = tx.query_all(
            "SELECT * FROM payments WHERE business_id = ? "
            "AND (invoice_id IS NULL OR invoice_id IN "
            "     (SELECT id FROM invoices WHERE deleted_at IS NULL)) "
            "AND substr(created_at, 1, 10) = ? ORDER BY id",
            (biz, date),
        )
        exps = tx.query_all(
            "SELECT * FROM expenses WHERE business_id = ? "
            "AND deleted_at IS NULL AND substr(created_at, 1, 10) = ? ORDER BY id",
            (biz, date),
        )
        incs = tx.query_all(
            "SELECT * FROM other_incomes WHERE business_id = ? "
            "AND deleted_at IS NULL AND substr(created_at, 1, 10) = ? ORDER BY id",
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
        note = p.get("note") or ""
        entries.append(
            {
                "kind": "payment",
                "ref": "",
                "description": note if note.startswith("Cash Adjustment")
                else ("Payment In" if p["direction"] == "in" else "Payment Out"),
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
    for inc in incs:
        entries.append(
            {
                "kind": "other_income",
                "ref": "",
                "description": "Other Income — {}".format(inc["title"]),
                "party": "",
                "amountPaisa": inc["amount_paisa"],
                "createdAt": inc["created_at"],
                "_sort": (inc["created_at"], 3, inc["id"]),
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
            "SELECT * FROM items WHERE business_id = ? AND deleted_at IS NULL ORDER BY id", (biz,)
        )
        in_rows = tx.query_all(
            "SELECT l.item_id AS iid, COALESCE(SUM(l.qty + l.free_qty), 0) AS q "
            "FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id "
            "WHERE i.business_id = ? AND l.item_id IS NOT NULL "
            "AND i.deleted_at IS NULL "
            "AND i.type IN ('purchase', 'sale_return') GROUP BY l.item_id",
            (biz,),
        )
        out_rows = tx.query_all(
            "SELECT l.item_id AS iid, COALESCE(SUM(l.qty + l.free_qty), 0) AS q "
            "FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id "
            "WHERE i.business_id = ? AND l.item_id IS NOT NULL "
            "AND i.deleted_at IS NULL "
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
