"""Money: Bank Accounts, Cheques, Loan Accounts, expense categories and
items, and the Other Income module (Vyapar Cash & Bank + Expenses).

Bank balance = opening balance + deposits - withdrawals, where payments
recorded against a bank account also land here as transactions (see
business.create_payment). All money stays integer paisa.
"""
from datetime import datetime

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


@bp.delete("/bank-accounts/<int:account_id>")
@require_session
@require_approved
@require_unlocked
def delete_bank_account(account_id):
    """An unused bank account deletes cleanly. One with entries or
    linked payments is refused with the reason: removing it would
    orphan its statement and break the balances built on it."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM bank_accounts WHERE id = ? AND business_id = ?",
            (account_id, biz),
        )
        if row is None:
            raise ApiError("Bank account not found.", status=404)
        entries = tx.query_one(
            "SELECT COUNT(*) AS c FROM bank_transactions "
            "WHERE business_id = ? AND bank_account_id = ?",
            (biz, account_id),
        )["c"]
        linked = tx.query_one(
            "SELECT COUNT(*) AS c FROM payments "
            "WHERE business_id = ? AND bank_account_id = ?",
            (biz, account_id),
        )["c"]
        if entries or linked:
            raise ApiError(
                "This bank account has entries, so it cannot be "
                "deleted — its statement is part of your books.")
        tx.execute("DELETE FROM bank_accounts WHERE id = ?", (account_id,))
    return jsonify(ok=True)


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


@bp.post("/bank-accounts/<int:account_id>/transfer")
@require_session
@require_approved
@require_unlocked
def bank_transfer(account_id):
    """The Deposit/Withdraw menu (Vyapar Bank Accounts): Bank to Cash
    Transfer, Cash to Bank Transfer, Bank to Bank Transfer and Adjust
    Bank Balance. Every action writes real bank_transactions rows; the
    cash side of the first two is a real cash payment row, so Cash in
    Hand moves too and both sides show in the Day Book."""
    data = _body()
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in ("bank_to_cash", "cash_to_bank", "bank_to_bank", "adjust"):
        raise ApiError(
            "Kind must be bank_to_cash, cash_to_bank, bank_to_bank or adjust.")
    client_ref = _client_ref(data)
    note = str(data.get("note") or "").strip()
    with db.transaction() as tx:
        biz = _business_id(tx)
        account = tx.query_one(
            "SELECT * FROM bank_accounts WHERE id = ? AND business_id = ?",
            (account_id, biz),
        )
        if account is None:
            raise ApiError("Bank account not found.", status=404)
        if client_ref:
            done = tx.query_one(
                "SELECT id FROM bank_transactions WHERE business_id = ? "
                "AND client_ref IN (?, ?, ?, ?)",
                (biz, client_ref + ":bank", client_ref + ":adj",
                 client_ref + ":from", client_ref + ":to"),
            )
            if done is not None:
                return jsonify(
                    ok=True, balancePaisa=_bank_balance(tx, account)), 200

        def bank_row(acc_id, row_kind, amount, label, suffix):
            tx.execute(
                "INSERT INTO bank_transactions (business_id, "
                "bank_account_id, kind, amount_paisa, note, created_at, "
                "client_ref) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (biz, acc_id, row_kind, amount,
                 note or label, now_iso(),
                 "{}:{}".format(client_ref, suffix) if client_ref else None),
            )

        def cash_row(direction, amount, label):
            tx.execute(
                "INSERT INTO payments (business_id, party_id, invoice_id, "
                "direction, amount_paisa, client_ref, created_at, mode, "
                "note) VALUES (?, NULL, NULL, ?, ?, ?, ?, 'cash', ?)",
                (biz, direction, amount,
                 "{}:cash".format(client_ref) if client_ref else None,
                 now_iso(), note or label),
            )

        if kind == "adjust":
            target = _int_field(data, "adjust_to_paisa", required=True,
                                min_value=0)
            delta = target - _bank_balance(tx, account)
            if delta > 0:
                bank_row(account_id, "deposit", delta,
                         "Adjust Bank Balance", "adj")
            elif delta < 0:
                bank_row(account_id, "withdraw", -delta,
                         "Adjust Bank Balance", "adj")
        else:
            amount = _int_field(data, "amount_paisa", required=True,
                                min_value=1)
            if kind == "bank_to_cash":
                bank_row(account_id, "withdraw", amount,
                         "Bank to Cash Transfer — {}".format(
                             account["display_name"]), "bank")
                cash_row("in", amount,
                         "Bank to Cash Transfer — {}".format(
                             account["display_name"]))
            elif kind == "cash_to_bank":
                bank_row(account_id, "deposit", amount,
                         "Cash to Bank Transfer — {}".format(
                             account["display_name"]), "bank")
                cash_row("out", amount,
                         "Cash to Bank Transfer — {}".format(
                             account["display_name"]))
            else:  # bank_to_bank
                to_id = data.get("to_account_id")
                target_acct = tx.query_one(
                    "SELECT * FROM bank_accounts WHERE id = ? "
                    "AND business_id = ?",
                    (to_id, biz),
                )
                if target_acct is None:
                    raise ApiError("Target bank account not found.")
                if to_id == account_id:
                    raise ApiError(
                        "Source and target bank accounts must be different.")
                bank_row(account_id, "withdraw", amount,
                         "Bank to Bank Transfer — to {}".format(
                             target_acct["display_name"]), "from")
                bank_row(to_id, "deposit", amount,
                         "Bank to Bank Transfer — from {}".format(
                             account["display_name"]), "to")
        balance = _bank_balance(tx, account)
        to_balance = None
        if kind == "bank_to_bank":
            target_acct = tx.query_one(
                "SELECT * FROM bank_accounts WHERE id = ?",
                (data.get("to_account_id"),),
            )
            to_balance = _bank_balance(tx, target_acct)
    return jsonify(
        ok=True, balancePaisa=balance, toBalancePaisa=to_balance), 201


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


@bp.delete("/cheques/<int:cheque_id>")
@require_session
@require_approved
@require_unlocked
def delete_cheque(cheque_id):
    """Manual cheque-book entries are standalone records: deleting one
    removes only the book entry (bill cheques live on their payments
    and are untouched)."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM cheques WHERE id = ? AND business_id = ?",
            (cheque_id, biz),
        )
        if row is None:
            raise ApiError("Cheque not found.", status=404)
        tx.execute("DELETE FROM cheques WHERE id = ?", (cheque_id,))
    return jsonify(ok=True)


# -------------------------------------------------------- cash adjust ----
@bp.post("/cash-adjustments")
@require_session
@require_approved
@require_unlocked
def cash_adjustment():
    """Cash In Hand > Adjust Cash (Vyapar): an Add Cash / Remove Cash
    correction recorded as a plain cash payment row with no party and
    no invoice, so it flows into Cash in Hand, the Day Book and All
    Transactions like any other cash movement."""
    data = _body()
    direction = str(data.get("direction") or "").strip().lower()
    if direction not in ("in", "out"):
        raise ApiError("Direction must be 'in' (Add Cash) or 'out' (Remove Cash).")
    amount = _int_field(data, "amount_paisa", required=True, min_value=1)
    date = str(data.get("date") or "").strip()
    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise ApiError("Adjustment Date must be YYYY-MM-DD.")
        created_at = "{}T12:00:00+00:00".format(date)
    else:
        created_at = now_iso()
    description = str(data.get("description") or "").strip()
    note = "Cash Adjustment — {}".format(description) if description \
        else "Cash Adjustment"
    client_ref = _client_ref(data)
    with db.transaction() as tx:
        biz = _business_id(tx)
        if client_ref:
            existing = tx.query_one(
                "SELECT * FROM payments WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:
                return jsonify(ok=True, payment={
                    "id": existing["id"],
                    "direction": existing["direction"],
                    "amountPaisa": existing["amount_paisa"],
                    "createdAt": existing["created_at"],
                    "note": existing.get("note") or "",
                }), 200
        pid = tx.insert(
            "INSERT INTO payments (business_id, party_id, invoice_id, "
            "direction, amount_paisa, client_ref, created_at, mode, note) "
            "VALUES (?, NULL, NULL, ?, ?, ?, ?, 'cash', ?) RETURNING id",
            (biz, direction, amount, client_ref, created_at, note),
        )
        row = tx.query_one("SELECT * FROM payments WHERE id = ?", (pid,))
    return jsonify(ok=True, payment={
        "id": row["id"], "direction": row["direction"],
        "amountPaisa": row["amount_paisa"], "createdAt": row["created_at"],
        "note": row.get("note") or "",
    }), 201


@bp.get("/cheque-payments")
@require_session
@require_approved
@require_unlocked
def cheque_payments():
    """Cheques page (Cash & Bank): every payment made by cheque, split
    Received (direction in) / Paid (direction out), with the party and
    the cheque/reference number."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        names = _party_names(tx, biz)
        invoices = {
            r["id"]: r["invoice_no"]
            for r in tx.query_all(
                "SELECT id, invoice_no FROM invoices WHERE business_id = ?",
                (biz,),
            )
        }
        rows = tx.query_all(
            "SELECT * FROM payments WHERE business_id = ? AND mode = 'cheque' "
            "AND (invoice_id IS NULL OR invoice_id IN "
            "     (SELECT id FROM invoices WHERE deleted_at IS NULL)) "
            "ORDER BY id",
            (biz,),
        )
    received, paid = [], []
    for r in rows:
        entry = {
            "id": r["id"],
            "date": (r["created_at"] or "")[:10],
            "party": names.get(r["party_id"]) or "",
            "refNo": r.get("cheque_no") or r.get("reference_no") or "",
            "amountPaisa": r["amount_paisa"],
            "invoiceNo": invoices.get(r["invoice_id"]) or "",
        }
        (received if r["direction"] == "in" else paid).append(entry)
    return jsonify(
        ok=True, received=received, paid=paid,
        receivedTotalPaisa=sum(e["amountPaisa"] for e in received),
        paidTotalPaisa=sum(e["amountPaisa"] for e in paid),
    )


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


@bp.delete("/loans/<int:loan_id>")
@require_session
@require_approved
@require_unlocked
def delete_loan(loan_id):
    """A loan with no repayments deletes cleanly; once money has been
    paid against it, the record is part of the books and stays."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM loan_accounts WHERE id = ? AND business_id = ?",
            (loan_id, biz),
        )
        if row is None:
            raise ApiError("Loan account not found.", status=404)
        if (row["paid_paisa"] or 0) > 0:
            raise ApiError(
                "This loan already has repayments recorded, so it "
                "cannot be deleted.")
        tx.execute("DELETE FROM loan_accounts WHERE id = ?", (loan_id,))
    return jsonify(ok=True)


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
            "SELECT * FROM other_incomes WHERE business_id = ? AND deleted_at IS NULL ORDER BY id", (biz,)
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


@bp.delete("/other-income/<int:income_id>")
@require_session
@require_approved
@require_unlocked
def delete_other_income(income_id):
    """Delete moves an income entry to the Recycle Bin (Utilities): it
    stops counting in the dashboard, P&L and Day Book until restored."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM other_incomes WHERE id = ? AND business_id = ? "
            "AND deleted_at IS NULL",
            (income_id, biz),
        )
        if row is None:
            raise ApiError("Income entry not found.", status=404)
        tx.execute(
            "UPDATE other_incomes SET deleted_at = ? WHERE id = ?",
            (now_iso(), income_id),
        )
    return jsonify(ok=True)
