"""Money: Bank Accounts, Cheques, Loan Accounts, expense categories and
items, and the Other Income module (Vyapar Cash & Bank + Expenses).

Bank balance = opening balance + deposits - withdrawals, where payments
recorded against a bank account also land here as transactions (see
business.create_payment). All money stays integer paisa.
"""
from flask import Blueprint, jsonify, request

from . import db
from .auth import require_approved, require_session, require_unlocked
from .crud import _business_id, _client_ref, _int_field
from .security import ApiError, now_iso

bp = Blueprint("money", __name__, url_prefix="/api")


def _body():
    return request.get_json(silent=True) or {}


def _guards(fn):
    return require_session(require_approved(require_unlocked(fn)))


# --------------------------------------------------------- bank accounts --
def _bank_balance(tx, account):
    delta = tx.query_one(
        "SELECT COALESCE(SUM(CASE WHEN kind = 'withdraw' THEN -amount_paisa "
        "ELSE amount_paisa END), 0) AS s FROM bank_transactions "
        "WHERE bank_account_id = ?",
        (account["id"],),
    )["s"]
    return account["opening_balance_paisa"] + delta


def _bank_dict(tx, row):
    return {
        "id": row["id"],
        "displayName": row["display_name"],
        "openingBalancePaisa": row["opening_balance_paisa"],
        "asOfDate": row["as_of_date"],
        "accountNumber": row["account_number"],
        "iban": row["iban"],
        "qrHandle": row["qr_handle"],
        "bankName": row["bank_name"],
        "holderName": row["holder_name"],
        "printDetails": bool(row["print_details"]),
        "printQr": bool(row["print_qr"]),
        "balancePaisa": _bank_balance(tx, row),
    }


@bp.post("/bank-accounts")
@require_session
@require_approved
@require_unlocked
def create_bank_account():
    data = _body()
    name = str(data.get("display_name") or "").strip()
    if not name:
        raise ApiError("Display Name is required.")
    client_ref = _client_ref(data)
    with db.transaction() as tx:
        biz = _business_id(tx)
        if client_ref:
            existing = tx.query_one(
                "SELECT * FROM bank_accounts WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:
                return jsonify(ok=True, bankAccount=_bank_dict(tx, existing)), 200
        aid = tx.insert(
            "INSERT INTO bank_accounts (business_id, display_name, "
            "opening_balance_paisa, as_of_date, account_number, iban, qr_handle, "
            "bank_name, holder_name, print_details, print_qr, created_at, client_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (biz, name,
             _int_field(data, "opening_balance_paisa", default=0),
             str(data.get("as_of_date") or "").strip(),
             str(data.get("account_number") or "").strip(),
             str(data.get("iban") or "").strip(),
             str(data.get("qr_handle") or "").strip(),
             str(data.get("bank_name") or "").strip(),
             str(data.get("holder_name") or "").strip(),
             1 if data.get("print_details") else 0,
             1 if data.get("print_qr") else 0,
             now_iso(), client_ref),
        )
        row = tx.query_one("SELECT * FROM bank_accounts WHERE id = ?", (aid,))
        result = _bank_dict(tx, row)
    return jsonify(ok=True, bankAccount=result), 201


@bp.get("/bank-accounts")
@require_session
@require_approved
@require_unlocked
def list_bank_accounts():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM bank_accounts WHERE business_id = ? ORDER BY id", (biz,)
        )
        result = [_bank_dict(tx, r) for r in rows]
    return jsonify(ok=True, bankAccounts=result)


@bp.put("/bank-accounts/<int:account_id>")
@require_session
@require_approved
@require_unlocked
def update_bank_account(account_id):
    data = _body()
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM bank_accounts WHERE id = ? AND business_id = ?",
            (account_id, biz),
        )
        if row is None:
            raise ApiError("Bank account not found.", status=404)
        tx.execute(
            "UPDATE bank_accounts SET display_name = ?, account_number = ?, "
            "iban = ?, qr_handle = ?, bank_name = ?, holder_name = ?, "
            "print_details = ?, print_qr = ? WHERE id = ?",
            (
                str(data.get("display_name", row["display_name"]) or "").strip()
                or row["display_name"],
                str(data.get("account_number", row["account_number"]) or "").strip(),
                str(data.get("iban", row["iban"]) or "").strip(),
                str(data.get("qr_handle", row["qr_handle"]) or "").strip(),
                str(data.get("bank_name", row["bank_name"]) or "").strip(),
                str(data.get("holder_name", row["holder_name"]) or "").strip(),
                1 if data.get("print_details", bool(row["print_details"])) else 0,
                1 if data.get("print_qr", bool(row["print_qr"])) else 0,
                account_id,
            ),
        )
        row = tx.query_one("SELECT * FROM bank_accounts WHERE id = ?", (account_id,))
        result = _bank_dict(tx, row)
    return jsonify(ok=True, bankAccount=result)


@bp.post("/bank-accounts/<int:account_id>/transact")
@require_session
@require_approved
@require_unlocked
def bank_transact(account_id):
    """Deposit to / Withdraw from a bank account."""
    data = _body()
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in ("deposit", "withdraw"):
        raise ApiError("Kind must be 'deposit' or 'withdraw'.")
    amount = _int_field(data, "amount_paisa", required=True, min_value=1)
    client_ref = _client_ref(data)
    with db.transaction() as tx:
        biz = _business_id(tx)
        account = tx.query_one(
            "SELECT * FROM bank_accounts WHERE id = ? AND business_id = ?",
            (account_id, biz),
        )
        if account is None:
            raise ApiError("Bank account not found.", status=404)
        if client_ref:
            existing = tx.query_one(
                "SELECT id FROM bank_transactions WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:
                return jsonify(ok=True, balancePaisa=_bank_balance(tx, account)), 200
        tx.execute(
            "INSERT INTO bank_transactions (business_id, bank_account_id, kind, "
            "amount_paisa, note, created_at, client_ref) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (biz, account_id, kind, amount,
             str(data.get("note") or "").strip(), now_iso(), client_ref),
        )
        balance = _bank_balance(tx, account)
    return jsonify(ok=True, balancePaisa=balance), 201


@bp.get("/bank-accounts/<int:account_id>/transactions")
@require_session
@require_approved
@require_unlocked
def bank_transactions(account_id):
    with db.transaction() as tx:
        biz = _business_id(tx)
        account = tx.query_one(
            "SELECT * FROM bank_accounts WHERE id = ? AND business_id = ?",
            (account_id, biz),
        )
        if account is None:
            raise ApiError("Bank account not found.", status=404)
        rows = tx.query_all(
            "SELECT * FROM bank_transactions WHERE bank_account_id = ? ORDER BY id",
            (account_id,),
        )
        result = _bank_dict(tx, account)
    return jsonify(
        ok=True,
        bankAccount=result,
        transactions=[
            {
                "id": r["id"],
                "kind": r["kind"],
                "amountPaisa": r["amount_paisa"],
                "note": r["note"],
                "createdAt": r["created_at"],
            }
            for r in rows
        ],
    )


# --------------------------------------------------------------- cheques ----
def _cheque_dict(row, party_names):
    return {
        "id": row["id"],
        "chequeNo": row["cheque_no"],
        "bankName": row["bank_name"],
        "amountPaisa": row["amount_paisa"],
        "partyId": row["party_id"],
        "partyName": party_names.get(row["party_id"]) or "",
        "status": row["status"],
        "dueDate": row["due_date"],
        "createdAt": row["created_at"],
    }


@bp.post("/cheques")
@require_session
@require_approved
@require_unlocked
def create_cheque():
    data = _body()
    amount = _int_field(data, "amount_paisa", required=True, min_value=1)
    client_ref = _client_ref(data)
    with db.transaction() as tx:
        biz = _business_id(tx)
        if client_ref:
            existing = tx.query_one(
                "SELECT * FROM cheques WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:
                names = _party_names(tx, biz)
                return jsonify(ok=True, cheque=_cheque_dict(existing, names)), 200
        cid = tx.insert(
            "INSERT INTO cheques (business_id, cheque_no, bank_name, amount_paisa, "
            "party_id, status, due_date, created_at, client_ref) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?) RETURNING id",
            (biz, str(data.get("cheque_no") or "").strip(),
             str(data.get("bank_name") or "").strip(), amount,
             data.get("party_id"), str(data.get("due_date") or "").strip(),
             now_iso(), client_ref),
        )
        row = tx.query_one("SELECT * FROM cheques WHERE id = ?", (cid,))
        names = _party_names(tx, biz)
    return jsonify(ok=True, cheque=_cheque_dict(row, names)), 201


def _party_names(tx, biz):
    return {
        r["id"]: r["name"]
        for r in tx.query_all(
            "SELECT id, name FROM parties WHERE business_id = ?", (biz,)
        )
    }


@bp.get("/cheques")
@require_session
@require_approved
@require_unlocked
def list_cheques():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM cheques WHERE business_id = ? ORDER BY id", (biz,)
        )
        names = _party_names(tx, biz)
    return jsonify(ok=True, cheques=[_cheque_dict(r, names) for r in rows])


@bp.post("/cheques/<int:cheque_id>/status")
@require_session
@require_approved
@require_unlocked
def cheque_status(cheque_id):
    status = str(_body().get("status") or "").strip().lower()
    if status not in ("pending", "cleared", "bounced"):
        raise ApiError("Status must be pending, cleared or bounced.")
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM cheques WHERE id = ? AND business_id = ?",
            (cheque_id, biz),
        )
        if row is None:
            raise ApiError("Cheque not found.", status=404)
        tx.execute("UPDATE cheques SET status = ? WHERE id = ?", (status, cheque_id))
        row = tx.query_one("SELECT * FROM cheques WHERE id = ?", (cheque_id,))
        names = _party_names(tx, biz)
    return jsonify(ok=True, cheque=_cheque_dict(row, names))


# ----------------------------------------------------------------- loans ----
def _loan_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "lender": row["lender"],
        "principalPaisa": row["principal_paisa"],
        "paidPaisa": row["paid_paisa"],
        "balancePaisa": row["principal_paisa"] - row["paid_paisa"],
        "interestBp": row["interest_bp"],
        "startDate": row["start_date"],
        "note": row["note"],
        "createdAt": row["created_at"],
    }


@bp.post("/loans")
@require_session
@require_approved
@require_unlocked
def create_loan():
    data = _body()
    name = str(data.get("name") or "").strip()
    if not name:
        raise ApiError("Loan account name is required.")
    client_ref = _client_ref(data)
    with db.transaction() as tx:
        biz = _business_id(tx)
        if client_ref:
            existing = tx.query_one(
                "SELECT * FROM loan_accounts WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:
                return jsonify(ok=True, loan=_loan_dict(existing)), 200
        lid = tx.insert(
            "INSERT INTO loan_accounts (business_id, name, lender, principal_paisa, "
            "paid_paisa, interest_bp, start_date, note, created_at, client_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (biz, name, str(data.get("lender") or "").strip(),
             _int_field(data, "principal_paisa", required=True, min_value=0),
             _int_field(data, "paid_paisa", default=0, min_value=0),
             _int_field(data, "interest_bp", default=0, min_value=0),
             str(data.get("start_date") or "").strip(),
             str(data.get("note") or "").strip(), now_iso(), client_ref),
        )
        row = tx.query_one("SELECT * FROM loan_accounts WHERE id = ?", (lid,))
    return jsonify(ok=True, loan=_loan_dict(row)), 201


@bp.get("/loans")
@require_session
@require_approved
@require_unlocked
def list_loans():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM loan_accounts WHERE business_id = ? ORDER BY id", (biz,)
        )
    return jsonify(ok=True, loans=[_loan_dict(r) for r in rows])


@bp.post("/loans/<int:loan_id>/pay")
@require_session
@require_approved
@require_unlocked
def pay_loan(loan_id):
    amount = _int_field(_body(), "amount_paisa", required=True, min_value=1)
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM loan_accounts WHERE id = ? AND business_id = ?",
            (loan_id, biz),
        )
        if row is None:
            raise ApiError("Loan account not found.", status=404)
        if row["paid_paisa"] + amount > row["principal_paisa"]:
            raise ApiError("Payment is more than the loan balance.")
        tx.execute(
            "UPDATE loan_accounts SET paid_paisa = paid_paisa + ? WHERE id = ?",
            (amount, loan_id),
        )
        row = tx.query_one("SELECT * FROM loan_accounts WHERE id = ?", (loan_id,))
    return jsonify(ok=True, loan=_loan_dict(row))


# ------------------------------------------- expense categories & items ----
@bp.get("/expense-categories")
@require_session
@require_approved
@require_unlocked
def list_expense_categories():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM expense_categories WHERE business_id = ? ORDER BY name",
            (biz,),
        )
    return jsonify(
        ok=True,
        categories=[
            {"id": r["id"], "name": r["name"], "type": r["ctype"]} for r in rows
        ],
    )


@bp.post("/expense-categories")
@require_session
@require_approved
@require_unlocked
def create_expense_category():
    data = _body()
    name = str(data.get("name") or "").strip()
    if not name:
        raise ApiError("Category name is required.")
    ctype = str(data.get("type") or "Indirect Expense").strip()
    if ctype not in ("Direct Expense", "Indirect Expense"):
        raise ApiError("Type must be 'Direct Expense' or 'Indirect Expense'.")
    with db.transaction() as tx:
        biz = _business_id(tx)
        tx.execute(
            "INSERT INTO expense_categories (business_id, name, ctype) "
            "VALUES (?, ?, ?) ON CONFLICT(business_id, name) "
            "DO UPDATE SET ctype = excluded.ctype",
            (biz, name, ctype),
        )
        row = tx.query_one(
            "SELECT * FROM expense_categories WHERE business_id = ? AND name = ?",
            (biz, name),
        )
    return jsonify(
        ok=True, category={"id": row["id"], "name": row["name"], "type": row["ctype"]}
    ), 201


@bp.get("/expense-items")
@require_session
@require_approved
@require_unlocked
def list_expense_items():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM expense_items WHERE business_id = ? ORDER BY name",
            (biz,),
        )
    return jsonify(
        ok=True, items=[{"id": r["id"], "name": r["name"]} for r in rows]
    )


@bp.post("/expense-items")
@require_session
@require_approved
@require_unlocked
def create_expense_item():
    name = str(_body().get("name") or "").strip()
    if not name:
        raise ApiError("Expense item name is required.")
    with db.transaction() as tx:
        biz = _business_id(tx)
        tx.execute(
            "INSERT INTO expense_items (business_id, name) VALUES (?, ?) "
            "ON CONFLICT(business_id, name) DO NOTHING",
            (biz, name),
        )
        row = tx.query_one(
            "SELECT * FROM expense_items WHERE business_id = ? AND name = ?",
            (biz, name),
        )
    return jsonify(ok=True, item={"id": row["id"], "name": row["name"]}), 201


# ------------------------------------------------------------ other income --
def _income_dict(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "category": row["category"],
        "amountPaisa": row["amount_paisa"],
        "partyId": row["party_id"],
        "note": row["note"],
        "createdAt": row["created_at"],
    }


@bp.post("/other-income")
@require_session
@require_approved
@require_unlocked
def create_other_income():
    data = _body()
    title = str(data.get("title") or "").strip()
    if not title:
        raise ApiError("Income title is required.")
    amount = _int_field(data, "amount_paisa", required=True, min_value=1)
    category = str(data.get("category") or "").strip()
    client_ref = _client_ref(data)
    with db.transaction() as tx:
        biz = _business_id(tx)
        if client_ref:
            existing = tx.query_one(
                "SELECT * FROM other_incomes WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:
                return jsonify(ok=True, income=_income_dict(existing)), 200
        if category:
            tx.execute(
                "INSERT INTO income_categories (business_id, name) VALUES (?, ?) "
                "ON CONFLICT(business_id, name) DO NOTHING",
                (biz, category),
            )
        iid = tx.insert(
            "INSERT INTO other_incomes (business_id, title, category, amount_paisa, "
            "party_id, note, created_at, client_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "RETURNING id",
            (biz, title, category, amount, data.get("party_id"),
             str(data.get("note") or "").strip(), now_iso(), client_ref),
        )
        row = tx.query_one("SELECT * FROM other_incomes WHERE id = ?", (iid,))
    return jsonify(ok=True, income=_income_dict(row)), 201


@bp.get("/other-income")
@require_session
@require_approved
@require_unlocked
def list_other_income():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM other_incomes WHERE business_id = ? ORDER BY id", (biz,)
        )
        cats = tx.query_all(
            "SELECT name FROM income_categories WHERE business_id = ? ORDER BY name",
            (biz,),
        )
    return jsonify(
        ok=True,
        incomes=[_income_dict(r) for r in rows],
        categories=[c["name"] for c in cats],
    )
