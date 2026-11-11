"""Reports expansion + Accounting (Vyapar Reports / Chart Of Accounts).

Every report excludes Recycle-Bin (soft-deleted) documents. Money is
integer paisa throughout. The accounting module is a shop-scale
simplification: the Chart Of Accounts is seeded with the system
accounts whose balances are computed live from real data, and journal
entries post against any account with a strict Debit = Credit rule.
"""
from flask import Blueprint, jsonify, request

from . import db
from .auth import require_approved, require_session, require_unlocked
from .business import compute_dashboard, compute_pnl
from .crud import _business_id, _client_ref, _int_field, _party_dict, party_balance_map
from .security import ApiError, now_iso

bp = Blueprint("reports2", __name__, url_prefix="/api")


def _body():
    return request.get_json(silent=True) or {}


def _range():
    date_from = request.args.get("from") or "0000-01-01"
    date_to = request.args.get("to") or "9999-12-31"
    return date_from, date_to


def _party_names(tx, biz):
    return {
        r["id"]: r["name"]
        for r in tx.query_all(
            "SELECT id, name FROM parties WHERE business_id = ?", (biz,)
        )
    }


def _item_map(tx, biz):
    return {
        r["id"]: r
        for r in tx.query_all(
            "SELECT * FROM items WHERE business_id = ?", (biz,)
        )
    }


def _invoices(tx, biz, types=None, date_from=None, date_to=None):
    sql = ("SELECT * FROM invoices WHERE business_id = ? AND deleted_at IS NULL")
    params = [biz]
    if types:
        sql += " AND type IN ({})".format(", ".join("'{}'".format(t) for t in types))
    if date_from:
        sql += " AND substr(created_at, 1, 10) >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND substr(created_at, 1, 10) <= ?"
        params.append(date_to)
    sql += " ORDER BY id"
    return tx.query_all(sql, tuple(params))


# ------------------------------------------------------------- reports ----
@bp.get("/reports/all-transactions")
@require_session
@require_approved
@require_unlocked
def all_transactions():
    date_from, date_to = _range()
    with db.transaction() as tx:
        biz = _business_id(tx)
        names = _party_names(tx, biz)
        entries = []
        for inv in _invoices(tx, biz, date_from=date_from, date_to=date_to):
            entries.append({
                "date": inv["created_at"], "kind": inv["type"],
                "ref": inv["invoice_no"],
                "party": names.get(inv["party_id"]) or "Cash Sale",
                "amountPaisa": inv["total_paisa"],
                "paidPaisa": inv["paid_paisa"],
                "balancePaisa": inv["balance_paisa"],
            })
        for pay in tx.query_all(
            "SELECT * FROM payments WHERE business_id = ? "
            "AND substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ? "
            "ORDER BY id",
            (biz, date_from, date_to),
        ):
            entries.append({
                "date": pay["created_at"],
                "kind": "payment_in" if pay["direction"] == "in" else "payment_out",
                "ref": "", "party": names.get(pay["party_id"]) or "",
                "amountPaisa": pay["amount_paisa"], "paidPaisa": 0,
                "balancePaisa": 0,
            })
        for exp in tx.query_all(
            "SELECT * FROM expenses WHERE business_id = ? "
            "AND deleted_at IS NULL "
            "AND substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ? "
            "ORDER BY id",
            (biz, date_from, date_to),
        ):
            entries.append({
                "date": exp["created_at"], "kind": "expense", "ref": "",
                "party": exp["title"], "amountPaisa": exp["amount_paisa"],
                "paidPaisa": 0, "balancePaisa": 0,
            })
        for inc in tx.query_all(
            "SELECT * FROM other_incomes WHERE business_id = ? "
            "AND deleted_at IS NULL "
            "AND substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ? "
            "ORDER BY id",
            (biz, date_from, date_to),
        ):
            entries.append({
                "date": inc["created_at"], "kind": "other_income", "ref": "",
                "party": inc["title"], "amountPaisa": inc["amount_paisa"],
                "paidPaisa": 0, "balancePaisa": 0,
            })
    entries.sort(key=lambda e: e["date"])
    return jsonify(ok=True, entries=entries)


@bp.get("/reports/item-serial")
@require_session
@require_approved
@require_unlocked
def item_serial_report():
    """Item Serial Report: one row per bill line that carries a
    Serial No./IMEI (serial-tracked items)."""
    date_from, date_to = _range()
    with db.transaction() as tx:
        biz = _business_id(tx)
        names = _party_names(tx, biz)
        items = _item_map(tx, biz)
        rows = []
        for inv in _invoices(tx, biz, date_from=date_from, date_to=date_to):
            lines = tx.query_all(
                "SELECT * FROM invoice_lines WHERE invoice_id = ? "
                "AND serial_no != '' ORDER BY id",
                (inv["id"],),
            )
            for line in lines:
                item = items.get(line["item_id"])
                rows.append({
                    "date": inv["created_at"][:10],
                    "invoiceNo": inv["invoice_no"],
                    "item": (item or {}).get("name") or line["description"],
                    "serialNo": line["serial_no"],
                    "party": names.get(inv["party_id"]) or "Cash Sale",
                    "qty": line["qty"],
                })
    return jsonify(ok=True, rows=rows)


@bp.get("/reports/sale-orders")
@require_session
@require_approved
@require_unlocked
def sale_orders_report():
    """Sale Orders report: every Sale Order with its status."""
    date_from, date_to = _range()
    with db.transaction() as tx:
        biz = _business_id(tx)
        names = _party_names(tx, biz)
        rows = [
            {
                "date": inv["created_at"][:10],
                "invoiceNo": inv["invoice_no"],
                "party": names.get(inv["party_id"]) or "Cash Sale",
                "amountPaisa": inv["total_paisa"],
                "status": "Converted" if inv.get("converted_to_id") else "Open",
            }
            for inv in _invoices(tx, biz, types=("sale_order",),
                                 date_from=date_from, date_to=date_to)
        ]
    return jsonify(ok=True, rows=rows)


@bp.get("/reports/sale-order-items")
@require_session
@require_approved
@require_unlocked
def sale_order_items_report():
    """Sale Order Item report: the item lines inside Sale Orders."""
    date_from, date_to = _range()
    with db.transaction() as tx:
        biz = _business_id(tx)
        names = _party_names(tx, biz)
        items = _item_map(tx, biz)
        rows = []
        for inv in _invoices(tx, biz, types=("sale_order",),
                             date_from=date_from, date_to=date_to):
            lines = tx.query_all(
                "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY id",
                (inv["id"],),
            )
            for line in lines:
                item = items.get(line["item_id"])
                rows.append({
                    "date": inv["created_at"][:10],
                    "invoiceNo": inv["invoice_no"],
                    "item": (item or {}).get("name") or line["description"],
                    "party": names.get(inv["party_id"]) or "Cash Sale",
                    "qty": line["qty"],
                    "ratePaisa": line["rate_paisa"],
                    "amountPaisa": line["amount_paisa"],
                })
    return jsonify(ok=True, rows=rows)


@bp.get("/reports/bill-wise-profit")
@require_session
@require_approved
@require_unlocked
def bill_wise_profit():
    date_from, date_to = _range()
    with db.transaction() as tx:
        biz = _business_id(tx)
        names = _party_names(tx, biz)
        items = _item_map(tx, biz)
        rows = []
        for inv in _invoices(tx, biz, types=("sale",), date_from=date_from,
                             date_to=date_to):
            lines = tx.query_all(
                "SELECT * FROM invoice_lines WHERE invoice_id = ?", (inv["id"],)
            )
            cost = 0
            for line in lines:
                item = items.get(line["item_id"])
                if item:
                    cost += (line["qty"] + (line.get("free_qty") or 0)) \
                        * item["purchase_price_paisa"]
            rows.append({
                "invoiceNo": inv["invoice_no"], "date": inv["created_at"][:10],
                "party": names.get(inv["party_id"]) or "Cash Sale",
                "salePaisa": inv["total_paisa"], "costPaisa": cost,
                "profitPaisa": inv["total_paisa"] - cost,
            })
    return jsonify(
        ok=True, rows=rows,
        totalProfitPaisa=sum(r["profitPaisa"] for r in rows),
    )


@bp.get("/reports/cash-flow")
@require_session
@require_approved
@require_unlocked
def cash_flow():
    date_from, date_to = _range()
    with db.transaction() as tx:
        biz = _business_id(tx)
        days = {}

        def add(day, kind, amount):
            row = days.setdefault(
                day, {"date": day, "inPaisa": 0, "outPaisa": 0})
            row["inPaisa" if kind == "in" else "outPaisa"] += amount

        for pay in tx.query_all(
            "SELECT * FROM payments WHERE business_id = ? "
            "AND substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ?",
            (biz, date_from, date_to),
        ):
            add(pay["created_at"][:10], pay["direction"], pay["amount_paisa"])
        for exp in tx.query_all(
            "SELECT * FROM expenses WHERE business_id = ? "
            "AND deleted_at IS NULL "
            "AND substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ?",
            (biz, date_from, date_to),
        ):
            add(exp["created_at"][:10], "out", exp["amount_paisa"])
        for inc in tx.query_all(
            "SELECT * FROM other_incomes WHERE business_id = ? "
            "AND deleted_at IS NULL "
            "AND substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ?",
            (biz, date_from, date_to),
        ):
            add(inc["created_at"][:10], "in", inc["amount_paisa"])
    rows = [days[d] for d in sorted(days)]
    balance = 0
    for row in rows:
        balance += row["inPaisa"] - row["outPaisa"]
        row["netPaisa"] = row["inPaisa"] - row["outPaisa"]
        row["balancePaisa"] = balance
    return jsonify(ok=True, rows=rows, closingBalancePaisa=balance)


@bp.get("/reports/party-pnl")
@require_session
@require_approved
@require_unlocked
def party_pnl():
    with db.transaction() as tx:
        biz = _business_id(tx)
        items = _item_map(tx, biz)
        parties = tx.query_all(
            "SELECT * FROM parties WHERE business_id = ? ORDER BY id", (biz,)
        )
        sales = {}
        for inv in _invoices(tx, biz, types=("sale", "sale_return")):
            if inv["party_id"] is None:
                continue
            entry = sales.setdefault(
                inv["party_id"], {"sales": 0, "returns": 0, "cost": 0})
            if inv["type"] == "sale":
                entry["sales"] += inv["total_paisa"]
                lines = tx.query_all(
                    "SELECT * FROM invoice_lines WHERE invoice_id = ?",
                    (inv["id"],),
                )
                for line in lines:
                    item = items.get(line["item_id"])
                    if item:
                        entry["cost"] += (line["qty"] + (line.get("free_qty") or 0)) \
                            * item["purchase_price_paisa"]
            else:
                entry["returns"] += inv["total_paisa"]
        balances = party_balance_map(tx, biz)
        rows = []
        for party in parties:
            entry = sales.get(party["id"], {"sales": 0, "returns": 0, "cost": 0})
            net_sales = entry["sales"] - entry["returns"]
            rows.append({
                "partyId": party["id"], "party": party["name"],
                "salesPaisa": entry["sales"], "returnsPaisa": entry["returns"],
                "costPaisa": entry["cost"],
                "profitPaisa": net_sales - entry["cost"],
                "balancePaisa": balances.get(party["id"], 0),
            })
    return jsonify(ok=True, rows=rows)


@bp.get("/reports/all-parties")
@require_session
@require_approved
@require_unlocked
def all_parties_report():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM parties WHERE business_id = ? ORDER BY id", (biz,)
        )
        balances = party_balance_map(tx, biz)
    return jsonify(
        ok=True,
        parties=[_party_dict(r, balances.get(r["id"])) for r in rows],
        # Build .20 sign convention: negative balance = party owes us.
        totalReceivablePaisa=sum(-b for b in balances.values() if b < 0),
        totalPayablePaisa=sum(b for b in balances.values() if b > 0),
    )


@bp.get("/reports/party-report-by-item")
@require_session
@require_approved
@require_unlocked
def party_report_by_item():
    return _party_item_matrix(swap=False)


@bp.get("/reports/item-report-by-party")
@require_session
@require_approved
@require_unlocked
def item_report_by_party():
    return _party_item_matrix(swap=True)


def _party_item_matrix(swap):
    with db.transaction() as tx:
        biz = _business_id(tx)
        names = _party_names(tx, biz)
        items = _item_map(tx, biz)
        agg = {}
        for inv in _invoices(tx, biz, types=("sale",)):
            lines = tx.query_all(
                "SELECT * FROM invoice_lines WHERE invoice_id = ?", (inv["id"],)
            )
            for line in lines:
                if line["item_id"] is None:
                    continue
                key = (inv["party_id"], line["item_id"])
                entry = agg.setdefault(key, {"qty": 0, "amount": 0})
                entry["qty"] += line["qty"]
                entry["amount"] += line["amount_paisa"] - (line.get("discount_paisa") or 0)
        rows = []
        for (party_id, item_id), entry in sorted(agg.items()):
            row = {
                "party": names.get(party_id) or "Cash Sale",
                "item": (items.get(item_id) or {}).get("name") or "",
                "qty": entry["qty"], "amountPaisa": entry["amount"],
            }
            if swap:
                row = {"item": row["item"], "party": row["party"],
                       "qty": row["qty"], "amountPaisa": row["amountPaisa"]}
            rows.append(row)
    return jsonify(ok=True, rows=rows)


@bp.get("/reports/sale-purchase-by-party")
@require_session
@require_approved
@require_unlocked
def sale_purchase_by_party():
    with db.transaction() as tx:
        biz = _business_id(tx)
        names = _party_names(tx, biz)
        agg = {}
        for inv in _invoices(tx, biz, types=("sale", "purchase")):
            if inv["party_id"] is None:
                continue
            entry = agg.setdefault(inv["party_id"], {"sale": 0, "purchase": 0})
            entry["sale" if inv["type"] == "sale" else "purchase"] += inv["total_paisa"]
        rows = [
            {"party": names.get(pid) or "", "salePaisa": e["sale"],
             "purchasePaisa": e["purchase"]}
            for pid, e in sorted(agg.items())
        ]
    return jsonify(ok=True, rows=rows)


@bp.get("/reports/item-wise-pnl")
@require_session
@require_approved
@require_unlocked
def item_wise_pnl():
    with db.transaction() as tx:
        biz = _business_id(tx)
        items = _item_map(tx, biz)
        agg = {}
        for inv in _invoices(tx, biz, types=("sale", "purchase")):
            lines = tx.query_all(
                "SELECT * FROM invoice_lines WHERE invoice_id = ?", (inv["id"],)
            )
            for line in lines:
                if line["item_id"] is None:
                    continue
                entry = agg.setdefault(
                    line["item_id"],
                    {"soldQty": 0, "revenue": 0, "purchasedQty": 0, "cost": 0})
                units = line["qty"] + (line.get("free_qty") or 0)
                if inv["type"] == "sale":
                    entry["soldQty"] += units
                    entry["revenue"] += line["amount_paisa"] \
                        - (line.get("discount_paisa") or 0) \
                        + (line.get("tax_paisa") or 0)
                else:
                    entry["purchasedQty"] += units
                    entry["cost"] += line["amount_paisa"]
        rows = []
        for item_id, entry in sorted(agg.items()):
            item = items.get(item_id) or {}
            sold_cost = entry["soldQty"] * (item.get("purchase_price_paisa") or 0)
            rows.append({
                "itemId": item_id, "item": item.get("name") or "",
                "soldQty": entry["soldQty"],
                "purchasedQty": entry["purchasedQty"],
                "revenuePaisa": entry["revenue"],
                "costPaisa": sold_cost,
                "profitPaisa": entry["revenue"] - sold_cost,
                "stockQty": item.get("stock_qty") or 0,
            })
    return jsonify(ok=True, rows=rows)


@bp.get("/reports/item-detail")
@require_session
@require_approved
@require_unlocked
def item_detail():
    item_id = request.args.get("item_id", type=int)
    with db.transaction() as tx:
        biz = _business_id(tx)
        items = _item_map(tx, biz)
        if item_id is None or item_id not in items:
            raise ApiError("Item not found.", status=404)
        item = items[item_id]
        movements = []
        for inv in _invoices(tx, biz):
            for line in tx.query_all(
                "SELECT * FROM invoice_lines WHERE invoice_id = ? AND item_id = ?",
                (inv["id"], item_id),
            ):
                movements.append({
                    "date": inv["created_at"], "kind": inv["type"],
                    "ref": inv["invoice_no"], "qty": line["qty"],
                    "freeQty": line.get("free_qty") or 0,
                    "ratePaisa": line["rate_paisa"],
                    "amountPaisa": line["amount_paisa"],
                    "batchNo": line.get("batch_no") or "",
                    "status": "Converted" if inv.get("converted_to_id") else "Open",
                })
        for tr in tx.query_all(
            "SELECT * FROM stock_transfers WHERE business_id = ? AND item_id = ?",
            (biz, item_id),
        ):
            movements.append({
                "date": tr["created_at"], "kind": "stock_transfer", "ref": "",
                "qty": tr["qty"], "freeQty": 0, "ratePaisa": 0, "amountPaisa": 0,
                "batchNo": "", "status": "",
            })
        batches = tx.query_all(
            "SELECT * FROM item_batches WHERE business_id = ? AND item_id = ? "
            "ORDER BY id",
            (biz, item_id),
        )
    movements.sort(key=lambda m: m["date"])
    from .crud import _item_dict

    return jsonify(
        ok=True, item=_item_dict(item), movements=movements,
        batches=[
            {"batchNo": b["batch_no"], "expDate": b["exp_date"],
             "mfgDate": b["mfg_date"], "qty": b["qty"]}
            for b in batches
        ],
    )


@bp.get("/reports/item-wise-discount")
@require_session
@require_approved
@require_unlocked
def item_wise_discount():
    with db.transaction() as tx:
        biz = _business_id(tx)
        items = _item_map(tx, biz)
        rows = tx.query_all(
            "SELECT l.item_id AS iid, COALESCE(SUM(l.discount_paisa), 0) AS d, "
            "COALESCE(SUM(l.qty), 0) AS q FROM invoice_lines l "
            "JOIN invoices i ON i.id = l.invoice_id "
            "WHERE i.business_id = ? AND i.deleted_at IS NULL "
            "AND l.item_id IS NOT NULL GROUP BY l.item_id",
            (biz,),
        )
        result = [
            {"item": (items.get(r["iid"]) or {}).get("name") or "",
             "qty": r["q"], "discountPaisa": r["d"]}
            for r in rows if r["d"]
        ]
    return jsonify(ok=True, rows=result)


@bp.get("/reports/discount-report")
@require_session
@require_approved
@require_unlocked
def discount_report():
    date_from, date_to = _range()
    with db.transaction() as tx:
        biz = _business_id(tx)
        names = _party_names(tx, biz)
        rows = []
        total = 0
        for inv in _invoices(tx, biz, date_from=date_from, date_to=date_to):
            if inv["discount_paisa"]:
                rows.append({
                    "invoiceNo": inv["invoice_no"], "type": inv["type"],
                    "date": inv["created_at"][:10],
                    "party": names.get(inv["party_id"]) or "Cash Sale",
                    "discountPaisa": inv["discount_paisa"],
                    "totalPaisa": inv["total_paisa"],
                })
                total += inv["discount_paisa"]
    return jsonify(ok=True, rows=rows, totalDiscountPaisa=total)


@bp.get("/reports/tax-report")
@require_session
@require_approved
@require_unlocked
def tax_report():
    date_from, date_to = _range()
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = []
        for inv_type in ("sale", "purchase", "sale_return", "purchase_return"):
            row = tx.query_one(
                "SELECT COALESCE(SUM(subtotal_paisa - discount_paisa), 0) AS base, "
                "COALESCE(SUM(tax_paisa), 0) AS tax, COUNT(*) AS c FROM invoices "
                "WHERE business_id = ? AND type = ? AND deleted_at IS NULL "
                "AND substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ?",
                (biz, inv_type, date_from, date_to),
            )
            if row["c"]:
                rows.append({
                    "type": inv_type, "documents": row["c"],
                    "taxablePaisa": row["base"], "taxPaisa": row["tax"],
                })
    return jsonify(
        ok=True, rows=rows,
        totalTaxPaisa=sum(r["taxPaisa"] for r in rows if r["type"] == "sale"),
    )


@bp.get("/reports/tax-rate-report")
@require_session
@require_approved
@require_unlocked
def tax_rate_report():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT COALESCE(l.tax_bp, 0) AS bp, "
            "COALESCE(SUM(l.amount_paisa - l.discount_paisa), 0) AS base, "
            "COALESCE(SUM(l.tax_paisa), 0) AS tax "
            "FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id "
            "WHERE i.business_id = ? AND i.deleted_at IS NULL AND i.type = 'sale' "
            "GROUP BY COALESCE(l.tax_bp, 0) ORDER BY bp",
            (biz,),
        )
        result = [
            {"taxRatePct": r["bp"] / 100.0, "taxablePaisa": r["base"],
             "taxPaisa": r["tax"]}
            for r in rows
        ]
    return jsonify(ok=True, rows=result)


@bp.get("/reports/sales-graph")
@require_session
@require_approved
@require_unlocked
def sales_graph():
    period = (request.args.get("period") or "daily").lower()
    key_len = {"daily": 10, "weekly": 10, "monthly": 7, "yearly": 4}.get(period, 10)
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT substr(created_at, 1, ?) AS bucket, "
            "COALESCE(SUM(total_paisa), 0) AS s FROM invoices "
            "WHERE business_id = ? AND type = 'sale' AND deleted_at IS NULL "
            "GROUP BY bucket ORDER BY bucket",
            (key_len, biz),
        )
    points = [{"label": r["bucket"], "amountPaisa": r["s"]} for r in rows]
    if period == "weekly":
        from datetime import datetime, timedelta

        weeks = {}
        for point in points:
            try:
                day = datetime.strptime(point["label"], "%Y-%m-%d")
            except ValueError:
                continue
            monday = (day - timedelta(days=day.weekday())).strftime("%Y-%m-%d")
            weeks[monday] = weeks.get(monday, 0) + point["amountPaisa"]
        points = [
            {"label": "Week of {}".format(d), "amountPaisa": weeks[d]}
            for d in sorted(weeks)
        ]
    return jsonify(ok=True, period=period, points=points)


@bp.get("/reports/expense")
@require_session
@require_approved
@require_unlocked
def expense_report():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM expenses WHERE business_id = ? AND deleted_at IS NULL ORDER BY id", (biz,)
        )
        from .business import _expense_dict

        result = [_expense_dict(r) for r in rows]
    return jsonify(
        ok=True, rows=result,
        totalPaisa=sum(r["amountPaisa"] for r in result),
    )


@bp.get("/reports/expense-category")
@require_session
@require_approved
@require_unlocked
def expense_category_report():
    return _expense_group("category")


@bp.get("/reports/expense-item")
@require_session
@require_approved
@require_unlocked
def expense_item_report():
    return _expense_group("expense_item")


def _expense_group(column):
    with db.transaction() as tx:
        biz = _business_id(tx)
        types = {
            r["name"]: r["ctype"]
            for r in tx.query_all(
                "SELECT * FROM expense_categories WHERE business_id = ?", (biz,)
            )
        }
        rows = tx.query_all(
            "SELECT {c} AS name, COALESCE(SUM(amount_paisa), 0) AS s, COUNT(*) AS n "
            "FROM expenses WHERE business_id = ? AND deleted_at IS NULL GROUP BY {c} ORDER BY s DESC".format(
                c=column),
            (biz,),
        )
        result = [
            {"name": r["name"] or "(None)",
             "type": types.get(r["name"], "") if column == "category" else "",
             "count": r["n"], "amountPaisa": r["s"]}
            for r in rows
        ]
    return jsonify(ok=True, rows=result)


@bp.get("/reports/other-income")
@require_session
@require_approved
@require_unlocked
def other_income_report():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT category AS name, COALESCE(SUM(amount_paisa), 0) AS s, "
            "COUNT(*) AS n FROM other_incomes WHERE business_id = ? AND deleted_at IS NULL "
            "GROUP BY category ORDER BY s DESC",
            (biz,),
        )
        total = tx.query_one(
            "SELECT COALESCE(SUM(amount_paisa), 0) AS s FROM other_incomes "
            "WHERE business_id = ? AND deleted_at IS NULL",
            (biz,),
        )["s"]
    return jsonify(
        ok=True,
        rows=[{"name": r["name"] or "(None)", "count": r["n"],
               "amountPaisa": r["s"]} for r in rows],
        totalPaisa=total,
    )


@bp.get("/reports/bank-statement")
@require_session
@require_approved
@require_unlocked
def bank_statement():
    account_id = request.args.get("account_id", type=int)
    with db.transaction() as tx:
        biz = _business_id(tx)
        account = None
        if account_id:
            account = tx.query_one(
                "SELECT * FROM bank_accounts WHERE id = ? AND business_id = ?",
                (account_id, biz),
            )
        if account is None:
            accounts = tx.query_all(
                "SELECT * FROM bank_accounts WHERE business_id = ? ORDER BY id LIMIT 1",
                (biz,),
            )
            account = accounts[0] if accounts else None
        if account is None:
            return jsonify(ok=True, account=None, rows=[])
        rows = tx.query_all(
            "SELECT * FROM bank_transactions WHERE bank_account_id = ? ORDER BY id",
            (account["id"],),
        )
        balance = account["opening_balance_paisa"]
        result = [{
            "date": account["as_of_date"], "note": "Opening Balance",
            "depositPaisa": account["opening_balance_paisa"] if balance >= 0 else 0,
            "withdrawPaisa": -account["opening_balance_paisa"] if balance < 0 else 0,
            "balancePaisa": balance,
        }]
        for row in rows:
            if row["kind"] == "withdraw":
                balance -= row["amount_paisa"]
                dep, wd = 0, row["amount_paisa"]
            else:
                balance += row["amount_paisa"]
                dep, wd = row["amount_paisa"], 0
            result.append({
                "date": row["created_at"][:10], "note": row["note"],
                "depositPaisa": dep, "withdrawPaisa": wd,
                "balancePaisa": balance,
            })
    return jsonify(
        ok=True,
        account={"id": account["id"], "displayName": account["display_name"]},
        rows=result,
    )


@bp.get("/reports/low-stock")
@require_session
@require_approved
@require_unlocked
def low_stock_report():
    """Low Stock Summary: stock below the item's Min Stock To Maintain."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM items WHERE business_id = ? AND deleted_at IS NULL AND "
            "((min_stock > 0 AND stock_qty < min_stock) OR "
            " (min_stock = 0 AND stock_qty <= 0)) ORDER BY name",
            (biz,),
        )
    return jsonify(
        ok=True,
        rows=[
            {"itemId": r["id"], "item": r["name"], "stockQty": r["stock_qty"],
             "minStock": r["min_stock"] or 0}
            for r in rows
        ],
    )


@bp.get("/reports/item-batch")
@require_session
@require_approved
@require_unlocked
def item_batch_report():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT b.*, i.name AS item_name FROM item_batches b "
            "JOIN items i ON i.id = b.item_id WHERE b.business_id = ? AND i.deleted_at IS NULL ORDER BY i.name, b.id",
            (biz,),
        )
    return jsonify(
        ok=True,
        rows=[
            {"item": r["item_name"], "batchNo": r["batch_no"],
             "expDate": r["exp_date"], "mfgDate": r["mfg_date"], "qty": r["qty"]}
            for r in rows
        ],
    )


@bp.get("/reports/stock-detail")
@require_session
@require_approved
@require_unlocked
def stock_detail_report():
    """Stock Detail: quantity of every item in every godown."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT gs.qty, g.name AS godown, i.name AS item, "
            "i.purchase_price_paisa FROM godown_stock gs "
            "JOIN godowns g ON g.id = gs.godown_id "
            "JOIN items i ON i.id = gs.item_id "
            "WHERE gs.business_id = ? AND i.deleted_at IS NULL ORDER BY g.id, i.name",
            (biz,),
        )
    return jsonify(
        ok=True,
        rows=[
            {"godown": r["godown"], "item": r["item"], "qty": r["qty"],
             "valuePaisa": r["qty"] * r["purchase_price_paisa"]}
            for r in rows
        ],
    )


@bp.get("/reports/store")
@require_session
@require_approved
@require_unlocked
def store_report():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(total_paisa), 0) AS s "
            "FROM store_orders WHERE business_id = ? GROUP BY status",
            (biz,),
        )
    return jsonify(
        ok=True,
        rows=[{"status": r["status"], "orders": r["n"], "amountPaisa": r["s"]}
              for r in rows],
        totalOrders=sum(r["n"] for r in rows),
        totalPaisa=sum(r["s"] for r in rows),
    )


# ------------------------------------------------------------ accounting ----
SEED_ACCOUNTS = [
    ("Cash in Hand", "Assets"),
    ("Bank Accounts", "Assets"),
    ("Accounts Receivable", "Assets"),
    ("Stock in Hand", "Assets"),
    ("Accounts Payable", "Liabilities"),
    ("Loan Accounts", "Liabilities"),
    ("Owner's Capital", "Capital"),
    ("Sales Income", "Income"),
    ("Other Income", "Income"),
    ("Purchases", "Expenses"),
    ("Expenses", "Expenses"),
]
ACCOUNT_GROUPS = ("Assets", "Liabilities", "Capital", "Income", "Expenses")


def _seed_accounts(tx, biz):
    count = tx.query_one(
        "SELECT COUNT(*) AS c FROM chart_accounts WHERE business_id = ?", (biz,)
    )["c"]
    if count:
        return
    for name, group in SEED_ACCOUNTS:
        tx.execute(
            "INSERT INTO chart_accounts (business_id, name, group_name, created_at) "
            "VALUES (?, ?, ?, ?)",
            (biz, name, group, now_iso()),
        )


def _accounting_on(tx, biz):
    from .settings_api import get_settings

    return bool(get_settings(tx, biz).get("accounting_enabled"))


def _journal_sums(tx, biz):
    rows = tx.query_all(
        "SELECT account_id, COALESCE(SUM(debit_paisa), 0) AS d, "
        "COALESCE(SUM(credit_paisa), 0) AS c FROM journal_lines "
        "WHERE business_id = ? GROUP BY account_id",
        (biz,),
    )
    return {r["account_id"]: (r["d"], r["c"]) for r in rows}


def _system_balances(tx, biz):
    """Live balances for the seeded system accounts, from real data."""
    dash = compute_dashboard(tx, biz)
    balances = party_balance_map(tx, biz)
    # Build .20 sign convention: negative balance = party owes us.
    receivable = sum(-b for b in balances.values() if b < 0)
    payable = sum(b for b in balances.values() if b > 0)
    loans = tx.query_one(
        "SELECT COALESCE(SUM(principal_paisa - paid_paisa), 0) AS s "
        "FROM loan_accounts WHERE business_id = ?",
        (biz,),
    )["s"]
    pnl = compute_pnl(tx, biz, "0000-01-01", "9999-12-31")
    return {
        "Cash in Hand": dash["cashInHandPaisa"],
        "Bank Accounts": dash["bankBalancePaisa"],
        "Accounts Receivable": receivable,
        "Stock in Hand": dash["stockValuePaisa"],
        "Accounts Payable": payable,
        "Loan Accounts": loans,
        "Sales Income": pnl["salesPaisa"],
        "Other Income": pnl["otherIncomePaisa"],
        "Purchases": pnl["purchasesPaisa"],
        "Expenses": pnl["expensesPaisa"],
    }


@bp.get("/accounts")
@require_session
@require_approved
@require_unlocked
def list_accounts():
    with db.transaction() as tx:
        biz = _business_id(tx)
        _seed_accounts(tx, biz)
        rows = tx.query_all(
            "SELECT * FROM chart_accounts WHERE business_id = ? ORDER BY id",
            (biz,),
        )
        journal = _journal_sums(tx, biz)
        system = _system_balances(tx, biz)
        result = []
        for row in rows:
            debit, credit = journal.get(row["id"], (0, 0))
            result.append({
                "id": row["id"], "name": row["name"], "group": row["group_name"],
                "systemBalancePaisa": system.get(row["name"]),
                "journalDebitPaisa": debit, "journalCreditPaisa": credit,
            })
    return jsonify(ok=True, accounts=result, groups=list(ACCOUNT_GROUPS))


@bp.post("/accounts")
@require_session
@require_approved
@require_unlocked
def create_account():
    data = _body()
    name = str(data.get("name") or "").strip()
    group = str(data.get("group_name") or "").strip()
    if not name:
        raise ApiError("Account name is required.")
    if group not in ACCOUNT_GROUPS:
        raise ApiError("Group must be one of: {}.".format(", ".join(ACCOUNT_GROUPS)))
    with db.transaction() as tx:
        biz = _business_id(tx)
        if not _accounting_on(tx, biz):
            raise ApiError(
                "The Accounting module is off. Turn it on in Settings > "
                "ACCOUNTING first."
            )
        _seed_accounts(tx, biz)
        aid = tx.insert(
            "INSERT INTO chart_accounts (business_id, name, group_name, created_at) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            (biz, name, group, now_iso()),
        )
        row = tx.query_one("SELECT * FROM chart_accounts WHERE id = ?", (aid,))
    return jsonify(
        ok=True,
        account={"id": row["id"], "name": row["name"], "group": row["group_name"]},
    ), 201


@bp.post("/journal")
@require_session
@require_approved
@require_unlocked
def create_journal():
    data = _body()
    lines_in = data.get("lines")
    if not isinstance(lines_in, list) or len(lines_in) < 2:
        raise ApiError("A journal entry needs at least two lines.")
    client_ref = _client_ref(data)
    with db.transaction() as tx:
        biz = _business_id(tx)
        if not _accounting_on(tx, biz):
            raise ApiError(
                "The Accounting module is off. Turn it on in Settings > "
                "ACCOUNTING first."
            )
        _seed_accounts(tx, biz)
        if client_ref:
            existing = tx.query_one(
                "SELECT id FROM journal_entries WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:
                return jsonify(ok=True, entryId=existing["id"]), 200
        total_debit = 0
        total_credit = 0
        lines = []
        for raw in lines_in:
            account_id = raw.get("account_id")
            account = tx.query_one(
                "SELECT id FROM chart_accounts WHERE id = ? AND business_id = ?",
                (account_id, biz),
            )
            if account is None:
                raise ApiError("Account not found.")
            debit = _int_field(raw, "debit_paisa", default=0, min_value=0)
            credit = _int_field(raw, "credit_paisa", default=0, min_value=0)
            if debit and credit:
                raise ApiError("A line is either a Debit or a Credit, not both.")
            total_debit += debit
            total_credit += credit
            lines.append((account_id, debit, credit))
        if total_debit == 0 or total_debit != total_credit:
            raise ApiError(
                "Total Debit and Total Credit must match before a journal "
                "entry can be saved."
            )
        eid = tx.insert(
            "INSERT INTO journal_entries (business_id, entry_date, narration, "
            "total_paisa, created_at, client_ref) VALUES (?, ?, ?, ?, ?, ?) "
            "RETURNING id",
            (biz, str(data.get("entry_date") or "").strip()
             or now_iso()[:10],
             str(data.get("narration") or "").strip(), total_debit, now_iso(),
             client_ref),
        )
        for account_id, debit, credit in lines:
            tx.execute(
                "INSERT INTO journal_lines (business_id, entry_id, account_id, "
                "debit_paisa, credit_paisa) VALUES (?, ?, ?, ?, ?)",
                (biz, eid, account_id, debit, credit),
            )
    return jsonify(ok=True, entryId=eid), 201


@bp.get("/journal")
@require_session
@require_approved
@require_unlocked
def list_journal():
    with db.transaction() as tx:
        biz = _business_id(tx)
        _seed_accounts(tx, biz)
        accounts = {
            r["id"]: r["name"]
            for r in tx.query_all(
                "SELECT id, name FROM chart_accounts WHERE business_id = ?", (biz,)
            )
        }
        entries = tx.query_all(
            "SELECT * FROM journal_entries WHERE business_id = ? ORDER BY id",
            (biz,),
        )
        result = []
        for entry in entries:
            lines = tx.query_all(
                "SELECT * FROM journal_lines WHERE entry_id = ? ORDER BY id",
                (entry["id"],),
            )
            result.append({
                "id": entry["id"], "date": entry["entry_date"],
                "narration": entry["narration"],
                "totalPaisa": entry["total_paisa"],
                "lines": [
                    {"account": accounts.get(l["account_id"]) or "",
                     "debitPaisa": l["debit_paisa"],
                     "creditPaisa": l["credit_paisa"]}
                    for l in lines
                ],
            })
    return jsonify(ok=True, entries=result)


@bp.get("/reports/trial-balance")
@require_session
@require_approved
@require_unlocked
def trial_balance():
    """Trial Balance with the Vyapar view options:
    - working=1: the WORKING trial balance — every chart account is
      listed (including the untouched Owner's Capital seed) and each
      row also carries its journal-only debit/credit columns.
    - zero=0: hide accounts whose balance is exactly 0 (the default
      zero=1 keeps today's full listing)."""
    working = request.args.get("working") in ("1", "true")
    show_zero = request.args.get("zero", "1") not in ("0", "false")
    with db.transaction() as tx:
        biz = _business_id(tx)
        _seed_accounts(tx, biz)
        accounts = tx.query_all(
            "SELECT * FROM chart_accounts WHERE business_id = ? ORDER BY id",
            (biz,),
        )
        journal = _journal_sums(tx, biz)
        system = _system_balances(tx, biz)
        rows = []
        total_debit = 0
        total_credit = 0
        for account in accounts:
            name, group = account["name"], account["group_name"]
            j_debit, j_credit = journal.get(account["id"], (0, 0))
            live = system.get(name)
            # A system account's balance is its live data balance plus
            # whatever journal lines were posted to it directly.
            net = (live if live is not None else 0) + j_debit - j_credit
            if group in ("Assets", "Expenses"):
                debit, credit = (net, 0) if net >= 0 else (0, -net)
            else:
                debit, credit = (0, net) if net >= 0 else (-net, 0)
            if name == "Owner's Capital" and live is None and not (j_debit or j_credit):
                if not working:
                    continue  # plugged below as the balancing figure
            if not show_zero and debit == 0 and credit == 0:
                continue
            row = {"account": name, "group": group,
                   "debitPaisa": debit, "creditPaisa": credit}
            if working:
                row["journalDebitPaisa"] = j_debit
                row["journalCreditPaisa"] = j_credit
            rows.append(row)
            total_debit += debit
            total_credit += credit
        diff = total_debit - total_credit
        if diff != 0:
            rows.append({
                "account": "Owner's Capital & Retained Earnings",
                "group": "Capital",
                "debitPaisa": -diff if diff < 0 else 0,
                "creditPaisa": diff if diff > 0 else 0,
            })
            if diff > 0:
                total_credit += diff
            else:
                total_debit += -diff
    return jsonify(
        ok=True, rows=rows, totalDebitPaisa=total_debit,
        totalCreditPaisa=total_credit,
    )


@bp.get("/reports/balance-sheet")
@require_session
@require_approved
@require_unlocked
def balance_sheet():
    with db.transaction() as tx:
        biz = _business_id(tx)
        system = _system_balances(tx, biz)
        pnl = compute_pnl(tx, biz, "0000-01-01", "9999-12-31")
        assets = [
            {"name": "Cash in Hand", "amountPaisa": system["Cash in Hand"]},
            {"name": "Bank Accounts", "amountPaisa": system["Bank Accounts"]},
            {"name": "Accounts Receivable",
             "amountPaisa": system["Accounts Receivable"]},
            {"name": "Stock in Hand", "amountPaisa": system["Stock in Hand"]},
        ]
        liabilities = [
            {"name": "Accounts Payable",
             "amountPaisa": system["Accounts Payable"]},
            {"name": "Loan Accounts", "amountPaisa": system["Loan Accounts"]},
        ]
        total_assets = sum(a["amountPaisa"] for a in assets)
        total_liab = sum(l["amountPaisa"] for l in liabilities)
        profit = pnl["profitPaisa"]
        capital = total_assets - total_liab - profit
    return jsonify(
        ok=True,
        assets=assets, totalAssetsPaisa=total_assets,
        liabilities=liabilities, totalLiabilitiesPaisa=total_liab,
        capitalPaisa=capital, profitPaisa=profit,
        totalLiabilitiesCapitalPaisa=total_liab + capital + profit,
    )
