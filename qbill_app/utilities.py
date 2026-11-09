"""Utilities (Vyapar): Recycle Bin, Verify My Data, Export to Excel/CSV,
Bulk Import of Items & Parties, Open Anything search, and Backup To
Computer (download a snapshot file).

Exports are CSV — the format Excel opens directly (Vyapar's "Export to
Excel"). Import prices in files are in RUPEES and convert to paisa on
the way in; everything inside QBill stays integer paisa.
"""
import csv
import io
import json

from flask import Blueprint, Response, jsonify, request

from . import db
from .auth import require_approved, require_session, require_unlocked
from .crud import (
    DOC_TYPES,
    _apply_batch,
    _apply_stock,
    _business_id,
    _invoice_dict,
    _settings,
)
from .security import ApiError, now_iso

bp = Blueprint("utilities", __name__, url_prefix="/api")


def _body():
    return request.get_json(silent=True) or {}


# ------------------------------------------------------------ recycle bin --
@bp.get("/recycle-bin")
@require_session
@require_approved
@require_unlocked
def recycle_bin():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM invoices WHERE business_id = ? "
            "AND deleted_at IS NOT NULL ORDER BY deleted_at DESC",
            (biz,),
        )
        result = [_invoice_dict(tx, r) for r in rows]
    return jsonify(ok=True, invoices=result)


@bp.post("/recycle-bin/<int:invoice_id>/restore")
@require_session
@require_approved
@require_unlocked
def restore_invoice(invoice_id):
    with db.transaction() as tx:
        biz = _business_id(tx)
        settings = _settings(tx, biz)
        row = tx.query_one(
            "SELECT * FROM invoices WHERE id = ? AND business_id = ? "
            "AND deleted_at IS NOT NULL",
            (invoice_id, biz),
        )
        if row is None:
            raise ApiError("Document not found in the Recycle Bin.", status=404)
        meta = DOC_TYPES[row["type"]]
        if meta["stock"] and settings.get("stock_maintenance", True):
            lines = tx.query_all(
                "SELECT * FROM invoice_lines WHERE invoice_id = ?", (invoice_id,)
            )
            for line in lines:
                if line["item_id"] is not None:
                    units = line["qty"] + (line.get("free_qty") or 0)
                    _apply_stock(tx, biz, line["item_id"], meta["stock"] * units)
                    if line.get("batch_no"):
                        _apply_batch(tx, biz, line["item_id"], line["batch_no"],
                                     line.get("exp_date") or "",
                                     meta["stock"] * units)
        tx.execute(
            "UPDATE invoices SET deleted_at = NULL WHERE id = ?", (invoice_id,)
        )
        from .crud import _audit

        _audit(tx, biz, settings, invoice_id, "restored",
               "{} restored from Recycle Bin".format(row["invoice_no"]))
    return jsonify(ok=True)


@bp.delete("/recycle-bin/<int:invoice_id>")
@require_session
@require_approved
@require_unlocked
def permanently_delete_invoice(invoice_id):
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT id FROM invoices WHERE id = ? AND business_id = ? "
            "AND deleted_at IS NOT NULL",
            (invoice_id, biz),
        )
        if row is None:
            raise ApiError("Document not found in the Recycle Bin.", status=404)
        tx.execute("DELETE FROM invoice_lines WHERE invoice_id = ?", (invoice_id,))
        tx.execute("DELETE FROM payments WHERE invoice_id = ?", (invoice_id,))
        tx.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    return jsonify(ok=True)


# --------------------------------------------------------- verify my data --
@bp.get("/utilities/verify")
@require_session
@require_approved
@require_unlocked
def verify_data():
    checks = []
    with db.transaction() as tx:
        biz = _business_id(tx)
        bad_totals = tx.query_one(
            "SELECT COUNT(*) AS c FROM invoices WHERE business_id = ? "
            "AND total_paisa != subtotal_paisa - discount_paisa + tax_paisa "
            "- loyalty_redeemed_paisa",
            (biz,),
        )["c"]
        checks.append({
            "name": "Invoice totals add up",
            "ok": bad_totals == 0,
            "detail": "{} invoice(s) with a mismatched total".format(bad_totals)
            if bad_totals else "All invoice totals are exact.",
        })
        bad_paid = tx.query_one(
            "SELECT COUNT(*) AS c FROM invoices i WHERE i.business_id = ? "
            "AND i.deleted_at IS NULL AND i.paid_paisa != COALESCE("
            "(SELECT SUM(p.amount_paisa) FROM payments p "
            " WHERE p.invoice_id = i.id), 0)",
            (biz,),
        )["c"]
        checks.append({
            "name": "Invoice payments match",
            "ok": bad_paid == 0,
            "detail": "{} invoice(s) whose paid amount differs from their "
                      "payments".format(bad_paid)
            if bad_paid else "Every invoice's paid amount matches its payments.",
        })
        orphans = tx.query_one(
            "SELECT COUNT(*) AS c FROM invoice_lines l "
            "LEFT JOIN invoices i ON i.id = l.invoice_id WHERE i.id IS NULL",
            (),
        )["c"]
        checks.append({
            "name": "No orphan bill lines",
            "ok": orphans == 0,
            "detail": "{} orphan line(s)".format(orphans) if orphans
            else "All bill lines belong to a document.",
        })
        godown_mismatch = tx.query_one(
            "SELECT COUNT(*) AS c FROM items i WHERE i.business_id = ? AND "
            "EXISTS (SELECT 1 FROM godown_stock gs WHERE gs.item_id = i.id) "
            "AND i.stock_qty != COALESCE((SELECT SUM(gs.qty) FROM godown_stock gs "
            "WHERE gs.item_id = i.id), 0)",
            (biz,),
        )["c"]
        checks.append({
            "name": "Godown stock matches total stock",
            "ok": godown_mismatch == 0,
            "detail": "{} item(s) whose godown quantities do not add up".format(
                godown_mismatch)
            if godown_mismatch else "Godown quantities add up to total stock.",
        })
        negative = tx.query_one(
            "SELECT COUNT(*) AS c FROM parties WHERE business_id = ? "
            "AND loyalty_points < 0",
            (biz,),
        )["c"]
        checks.append({
            "name": "Loyalty points are never negative",
            "ok": negative == 0,
            "detail": "{} party(ies) with negative points".format(negative)
            if negative else "All loyalty balances are healthy.",
        })
    return jsonify(ok=True, checks=checks)


# ---------------------------------------------------------------- exports --
def _csv_response(filename, header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = \
        'attachment; filename="{}"'.format(filename)
    return resp


@bp.get("/utilities/export")
@require_session
@require_approved
@require_unlocked
def export_data():
    kind = (request.args.get("kind") or "items").lower()
    with db.transaction() as tx:
        biz = _business_id(tx)
        if kind == "items":
            rows = tx.query_all(
                "SELECT * FROM items WHERE business_id = ? ORDER BY id", (biz,)
            )
            return _csv_response(
                "qbill-items.csv",
                ["Name", "Item Code", "Category", "Unit", "Sale Price (Rs)",
                 "Purchase Price (Rs)", "Wholesale Price (Rs)", "MRP (Rs)",
                 "Stock Qty", "Min Stock"],
                [
                    [r["name"], r.get("item_code") or "", r.get("category") or "",
                     r.get("unit") or "", r["sale_price_paisa"] / 100,
                     r["purchase_price_paisa"] / 100,
                     (r.get("wholesale_price_paisa") or 0) / 100,
                     (r.get("mrp_paisa") or 0) / 100, r["stock_qty"],
                     r.get("min_stock") or 0]
                    for r in rows
                ],
            )
        if kind == "parties":
            from .crud import party_balance_map

            rows = tx.query_all(
                "SELECT * FROM parties WHERE business_id = ? ORDER BY id", (biz,)
            )
            balances = party_balance_map(tx, biz)
            return _csv_response(
                "qbill-parties.csv",
                ["Name", "Phone", "Group", "Email", "Opening Balance (Rs)",
                 "Current Balance (Rs)", "Loyalty Points"],
                [
                    [r["name"], r["phone"] or "", r.get("group_name") or "",
                     r.get("email") or "", r["opening_balance_paisa"] / 100,
                     balances.get(r["id"], 0) / 100, r.get("loyalty_points") or 0]
                    for r in rows
                ],
            )
        if kind == "transactions":
            rows = tx.query_all(
                "SELECT * FROM invoices WHERE business_id = ? "
                "AND deleted_at IS NULL ORDER BY id",
                (biz,),
            )
            names = {
                r["id"]: r["name"]
                for r in tx.query_all(
                    "SELECT id, name FROM parties WHERE business_id = ?", (biz,)
                )
            }
            return _csv_response(
                "qbill-transactions.csv",
                ["Date", "Type", "Number", "Party", "Subtotal (Rs)",
                 "Discount (Rs)", "Tax (Rs)", "Total (Rs)", "Paid (Rs)",
                 "Balance (Rs)"],
                [
                    [r["created_at"][:10], r["type"], r["invoice_no"],
                     names.get(r["party_id"]) or "Cash Sale",
                     r["subtotal_paisa"] / 100, r["discount_paisa"] / 100,
                     r["tax_paisa"] / 100, r["total_paisa"] / 100,
                     r["paid_paisa"] / 100, r["balance_paisa"] / 100]
                    for r in rows
                ],
            )
    raise ApiError("Export kind must be items, parties or transactions.")


# ------------------------------------------------------------ bulk import --
ITEM_TEMPLATE = (
    "name,sale_price,purchase_price,stock_qty,item_code,category,unit,min_stock\n"
    "Solar Panel 550W,1500.50,1400,100,SP-550,Solar Panels,Pcs,5\n"
)
PARTY_TEMPLATE = (
    "name,phone,opening_balance,group,email\n"
    "Ahmed Traders,0300-1234567,0,Retail,ahmed@example.com\n"
)


@bp.get("/utilities/import-template")
@require_session
@require_approved
@require_unlocked
def import_template():
    kind = (request.args.get("kind") or "items").lower()
    if kind == "parties":
        resp = Response(PARTY_TEMPLATE, mimetype="text/csv")
        resp.headers["Content-Disposition"] = \
            'attachment; filename="qbill-import-parties.csv"'
        return resp
    resp = Response(ITEM_TEMPLATE, mimetype="text/csv")
    resp.headers["Content-Disposition"] = \
        'attachment; filename="qbill-import-items.csv"'
    return resp


def _rupees_to_paisa(raw, field, errors, row_no):
    text = str(raw or "").strip().replace(",", "")
    if text == "":
        return 0
    try:
        return int(round(float(text) * 100))
    except ValueError:
        errors.append({"row": row_no, "message": "{} must be a number.".format(field)})
        return 0


def _validate_import(kind, rows_in):
    valid = []
    errors = []
    for index, raw in enumerate(rows_in):
        row_no = index + 2  # +1 for the header, +1 for 1-based rows
        if not isinstance(raw, dict):
            errors.append({"row": row_no, "message": "Row is not an object."})
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            errors.append({"row": row_no, "message": "Name is required."})
            continue
        if kind == "items":
            qty_text = str(raw.get("stock_qty") or "").strip()
            try:
                qty = int(float(qty_text)) if qty_text else 0
            except ValueError:
                errors.append({"row": row_no, "message": "Stock Qty must be a number."})
                continue
            valid.append({
                "name": name,
                "sale_price_paisa": _rupees_to_paisa(
                    raw.get("sale_price"), "Sale Price", errors, row_no),
                "purchase_price_paisa": _rupees_to_paisa(
                    raw.get("purchase_price"), "Purchase Price", errors, row_no),
                "stock_qty": qty,
                "item_code": str(raw.get("item_code") or "").strip(),
                "category": str(raw.get("category") or "").strip(),
                "unit": str(raw.get("unit") or "").strip(),
                "min_stock": int(float(raw["min_stock"]))
                if str(raw.get("min_stock") or "").strip() else 0,
            })
        else:
            valid.append({
                "name": name,
                "phone": str(raw.get("phone") or "").strip(),
                "opening_balance_paisa": _rupees_to_paisa(
                    raw.get("opening_balance"), "Opening Balance", errors, row_no),
                "group": str(raw.get("group") or "").strip(),
                "email": str(raw.get("email") or "").strip(),
            })
    return valid, errors


@bp.post("/utilities/import/validate")
@require_session
@require_approved
@require_unlocked
def import_validate():
    data = _body()
    kind = str(data.get("kind") or "items").lower()
    if kind not in ("items", "parties"):
        raise ApiError("Import kind must be items or parties.")
    rows_in = data.get("rows")
    if not isinstance(rows_in, list):
        raise ApiError("Rows must be a list.")
    valid, errors = _validate_import(kind, rows_in)
    return jsonify(ok=True, valid=valid, errors=errors)


@bp.post("/utilities/import/confirm")
@require_session
@require_approved
@require_unlocked
def import_confirm():
    data = _body()
    kind = str(data.get("kind") or "items").lower()
    batch = str(data.get("batch") or "import").strip()
    rows_in = data.get("rows")
    if kind not in ("items", "parties") or not isinstance(rows_in, list):
        raise ApiError("Nothing to import.")
    created = 0
    with db.transaction() as tx:
        biz = _business_id(tx)
        for index, raw in enumerate(rows_in):
            client_ref = "import:{}:{}:{}".format(batch, kind, index)
            if kind == "items":
                existing = tx.query_one(
                    "SELECT id FROM items WHERE business_id = ? AND client_ref = ?",
                    (biz, client_ref),
                )
                if existing:
                    continue
                tx.execute(
                    "INSERT INTO items (business_id, name, sale_price_paisa, "
                    "purchase_price_paisa, stock_qty, client_ref, created_at, "
                    "item_code, category, unit, min_stock) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (biz, raw["name"], raw.get("sale_price_paisa") or 0,
                     raw.get("purchase_price_paisa") or 0,
                     raw.get("stock_qty") or 0, client_ref, now_iso(),
                     raw.get("item_code") or "", raw.get("category") or "",
                     raw.get("unit") or "", raw.get("min_stock") or 0),
                )
            else:
                existing = tx.query_one(
                    "SELECT id FROM parties WHERE business_id = ? AND client_ref = ?",
                    (biz, client_ref),
                )
                if existing:
                    continue
                tx.execute(
                    "INSERT INTO parties (business_id, name, phone, "
                    "opening_balance_paisa, client_ref, created_at, group_name, "
                    "email) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (biz, raw["name"], raw.get("phone") or None,
                     raw.get("opening_balance_paisa") or 0, client_ref,
                     now_iso(), raw.get("group") or "", raw.get("email") or ""),
                )
            created += 1
    return jsonify(ok=True, imported=created), 201


# --------------------------------------------------------- open anything ----
@bp.get("/search")
@require_session
@require_approved
@require_unlocked
def open_anything():
    """Open Anything (Ctrl+F): one search over parties, items and
    transactions."""
    q = (request.args.get("q") or "").strip().lower()
    results = []
    if not q:
        return jsonify(ok=True, results=results)
    with db.transaction() as tx:
        biz = _business_id(tx)
        for row in tx.query_all(
            "SELECT * FROM parties WHERE business_id = ? ORDER BY id", (biz,)
        ):
            if q in row["name"].lower() or q in (row["phone"] or "").lower():
                results.append({
                    "kind": "party", "id": row["id"], "label": row["name"],
                    "sub": row["phone"] or "Party", "nav": "parties",
                })
        for row in tx.query_all(
            "SELECT * FROM items WHERE business_id = ? ORDER BY id", (biz,)
        ):
            if q in row["name"].lower() or q in (row.get("item_code") or "").lower():
                results.append({
                    "kind": "item", "id": row["id"], "label": row["name"],
                    "sub": "Item · Stock {}".format(row["stock_qty"]),
                    "nav": "items",
                })
        for row in tx.query_all(
            "SELECT * FROM invoices WHERE business_id = ? "
            "AND deleted_at IS NULL ORDER BY id DESC",
            (biz,),
        ):
            if q in row["invoice_no"].lower():
                results.append({
                    "kind": "invoice", "id": row["id"],
                    "label": row["invoice_no"],
                    "sub": "{} · Rs {:,.2f}".format(
                        row["type"], row["total_paisa"] / 100),
                    "nav": "salebook",
                })
    return jsonify(ok=True, results=results[:30])


# ---------------------------------------------------- backup to computer ----
@bp.get("/backups/<int:backup_id>/download")
@require_session
@require_approved
@require_unlocked
def download_backup(backup_id):
    """Backup To Computer: download one snapshot as a JSON file."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM auto_backups WHERE id = ? AND business_id = ?",
            (backup_id, biz),
        )
        if row is None:
            raise ApiError("Backup not found.", status=404)
        payload = row["payload"]
        date = (row["created_at"] or "")[:10]
    resp = Response(payload, mimetype="application/json")
    resp.headers["Content-Disposition"] = \
        'attachment; filename="qbill-backup-{}.json"'.format(date)
    return resp
