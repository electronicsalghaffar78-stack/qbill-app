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
import re
import zipfile
import xml.etree.ElementTree as ET

from flask import Blueprint, Response, jsonify, request

from . import db
from .auth import require_approved, require_session, require_unlocked
from .crud import (
    DOC_TYPES,
    _apply_batch,
    _apply_stock,
    _business_id,
    _invoice_dict,
    _item_dict,
    _party_dict,
    _settings,
    _sync_main_godown,
    party_balance_map,
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
        # Fix 4: the bin also holds soft-deleted parties, items,
        # expenses and income entries. Party balances are computed
        # with the deleted rows included so a binned party still
        # shows the balance it carried when it was deleted.
        balances = party_balance_map(tx, biz, include_deleted=True)
        parties = [
            _party_dict(r, balances.get(r["id"]))
            for r in tx.query_all(
                "SELECT * FROM parties WHERE business_id = ? "
                "AND deleted_at IS NOT NULL ORDER BY deleted_at DESC",
                (biz,),
            )
        ]
        items = [
            _item_dict(r)
            for r in tx.query_all(
                "SELECT * FROM items WHERE business_id = ? "
                "AND deleted_at IS NOT NULL ORDER BY deleted_at DESC",
                (biz,),
            )
        ]
        expenses = [
            {"id": r["id"], "title": r["title"],
             "category": r.get("category") or "",
             "amountPaisa": r["amount_paisa"],
             "createdAt": r["created_at"], "deletedAt": r["deleted_at"]}
            for r in tx.query_all(
                "SELECT * FROM expenses WHERE business_id = ? "
                "AND deleted_at IS NOT NULL ORDER BY deleted_at DESC",
                (biz,),
            )
        ]
        incomes = [
            {"id": r["id"], "title": r["title"],
             "category": r.get("category") or "",
             "amountPaisa": r["amount_paisa"],
             "createdAt": r["created_at"], "deletedAt": r["deleted_at"]}
            for r in tx.query_all(
                "SELECT * FROM other_incomes WHERE business_id = ? "
                "AND deleted_at IS NOT NULL ORDER BY deleted_at DESC",
                (biz,),
            )
        ]
    return jsonify(ok=True, invoices=result, parties=parties, items=items,
                   expenses=expenses, incomes=incomes)


_BIN_TABLES = {
    "parties": "parties",
    "items": "items",
    "expenses": "expenses",
    "incomes": "other_incomes",
}


@bp.post("/recycle-bin/<string:kind>/<int:row_id>/restore")
@require_session
@require_approved
@require_unlocked
def restore_record(kind, row_id):
    """Restore a soft-deleted party / item / expense / income entry:
    clearing deleted_at brings the exact record back everywhere."""
    table = _BIN_TABLES.get(kind)
    if table is None:
        raise ApiError("Unknown Recycle Bin section.", status=404)
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT id FROM {} WHERE id = ? AND business_id = ? "
            "AND deleted_at IS NOT NULL".format(table),
            (row_id, biz),
        )
        if row is None:
            raise ApiError("Record not found in the Recycle Bin.",
                           status=404)
        tx.execute(
            "UPDATE {} SET deleted_at = NULL WHERE id = ?".format(table),
            (row_id,),
        )
    return jsonify(ok=True)


@bp.delete("/recycle-bin/<string:kind>/<int:row_id>")
@require_session
@require_approved
@require_unlocked
def permanently_delete_record(kind, row_id):
    """Permanent delete from the bin. Parties and items that real
    documents still reference are refused with the reason — deleting
    the row would break those bills; expenses and income entries are
    standalone and delete outright."""
    table = _BIN_TABLES.get(kind)
    if table is None:
        raise ApiError("Unknown Recycle Bin section.", status=404)
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT id FROM {} WHERE id = ? AND business_id = ? "
            "AND deleted_at IS NOT NULL".format(table),
            (row_id, biz),
        )
        if row is None:
            raise ApiError("Record not found in the Recycle Bin.",
                           status=404)
        if kind == "parties":
            refs = tx.query_one(
                "SELECT (SELECT COUNT(*) FROM invoices WHERE "
                "business_id = ? AND party_id = ?) + "
                "(SELECT COUNT(*) FROM payments WHERE "
                "business_id = ? AND party_id = ?) AS c",
                (biz, row_id, biz, row_id),
            )["c"]
            if refs:
                raise ApiError(
                    "This party has bills or payments in your books, "
                    "so it cannot be deleted forever — restore it, or "
                    "delete those records permanently first.")
        if kind == "items":
            refs = tx.query_one(
                "SELECT COUNT(*) AS c FROM invoice_lines "
                "WHERE item_id = ?",
                (row_id,),
            )["c"]
            if refs:
                raise ApiError(
                    "This item appears on saved bills, so it cannot "
                    "be deleted forever — it can stay in the Recycle "
                    "Bin, or be restored.")
            tx.execute("DELETE FROM item_batches WHERE item_id = ?",
                       (row_id,))
            tx.execute("DELETE FROM store_items WHERE item_id = ?",
                       (row_id,))
        tx.execute("DELETE FROM {} WHERE id = ?".format(table), (row_id,))
    return jsonify(ok=True)


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
                "SELECT * FROM items WHERE business_id = ? AND deleted_at IS NULL ORDER BY id", (biz,)
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
                "SELECT * FROM parties WHERE business_id = ? AND deleted_at IS NULL ORDER BY id", (biz,)
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
            "SELECT * FROM parties WHERE business_id = ? AND deleted_at IS NULL ORDER BY id", (biz,)
        ):
            if q in row["name"].lower() or q in (row["phone"] or "").lower():
                results.append({
                    "kind": "party", "id": row["id"], "label": row["name"],
                    "sub": row["phone"] or "Party", "nav": "parties",
                })
        for row in tx.query_all(
            "SELECT * FROM items WHERE business_id = ? AND deleted_at IS NULL ORDER BY id", (biz,)
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


# ------------------------------------------- import wizard (Fix 5) ------
# "Bring your old data": Vyapar (and every other billing app) locks its
# raw backup file, but every such app can EXPORT Excel/CSV. This wizard
# reads those exports: parse the file, auto-map the columns (Vyapar's
# export headers are known), preview Valid / Duplicates / Errors, then
# import. Duplicates are matched by phone+name (parties) and code+name
# (items) and default to SKIP; the user may choose Update per row.

_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_IMPORT_ROW_CAP = 2000

# Canonical field -> accepted header aliases (normalized: lowercase,
# non-alphanumerics collapsed). Vyapar's desktop exports use the first
# spellings; the rest cover generic exports from other software.
_PARTY_FIELDS = {
    "name": ["party name", "name", "customer name", "party", "customer",
             "client name"],
    "phone": ["phone number", "phone", "mobile", "mobile number",
              "contact number", "phone no", "contact"],
    "address": ["address", "party address", "billing address"],
    "opening_balance": ["opening balance", "opening bal", "balance",
                        "opening balance rs", "ob"],
    "email": ["email", "email id", "e mail"],
    "group": ["group", "party group", "category", "party category"],
}
_ITEM_FIELDS = {
    "name": ["item name", "name", "product name", "item", "product"],
    "item_code": ["item code", "code", "sku", "barcode", "item no"],
    "sale_price": ["sale price", "sales price", "selling price", "sp",
                   "sale price rs"],
    "purchase_price": ["purchase price", "buy price", "cost price", "pp",
                       "purchase price rs"],
    "wholesale_price": ["wholesale price", "wholesale"],
    "mrp": ["mrp", "max retail price", "maximum retail price"],
    "unit": ["unit", "uom", "base unit", "units"],
    "stock_qty": ["stock", "stock qty", "current stock", "opening stock",
                  "quantity", "qty", "stock quantity"],
    "tax_pct": ["tax", "tax rate", "tax percent", "gst", "gst rate",
                "tax percentage"],
    "category": ["category", "item category", "group"],
    "min_stock": ["min stock", "minimum stock", "reorder level",
                  "min stock to maintain"],
}


def _norm_header(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _suggest_mapping(kind, headers):
    fields = _PARTY_FIELDS if kind == "parties" else _ITEM_FIELDS
    normed = {h: _norm_header(h) for h in headers}
    mapping = {}
    used = set()
    for field, aliases in fields.items():
        mapping[field] = ""
        for alias in aliases:
            for header, norm in normed.items():
                if norm == alias and header not in used:
                    mapping[field] = header
                    used.add(header)
                    break
            if mapping[field]:
                break
    return mapping


def _xlsx_rows(data):
    """Minimal .xlsx reader (stdlib only): first worksheet -> rows of
    cell text. Handles shared strings, inline strings and numbers."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ApiError("That file is not a valid Excel (.xlsx) file.")
    shared = []
    if "xl/sharedStrings.xml" in archive.namelist():
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        for si in root.findall(_XLSX_NS + "si"):
            shared.append("".join(
                t.text or "" for t in si.iter(_XLSX_NS + "t")))
    sheets = sorted(n for n in archive.namelist()
                    if n.startswith("xl/worksheets/sheet")
                    and n.endswith(".xml"))
    if not sheets:
        raise ApiError("No worksheet found in that Excel file.")
    root = ET.fromstring(archive.read(sheets[0]))
    rows = []
    for row_el in root.iter(_XLSX_NS + "row"):
        cells = {}
        for c in row_el.findall(_XLSX_NS + "c"):
            ref = c.get("r") or ""
            letters = re.match(r"[A-Z]+", ref)
            if not letters:
                continue
            col = 0
            for ch in letters.group(0):
                col = col * 26 + (ord(ch) - 64)
            v = c.find(_XLSX_NS + "v")
            is_el = c.find(_XLSX_NS + "is")
            if c.get("t") == "s" and v is not None:
                try:
                    text = shared[int(v.text)]
                except (ValueError, IndexError):
                    text = ""
            elif is_el is not None:
                text = "".join(
                    t.text or "" for t in is_el.iter(_XLSX_NS + "t"))
            elif v is not None:
                text = v.text or ""
            else:
                text = ""
            cells[col - 1] = text
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])
    return rows


def _file_rows(upload):
    name = (upload.filename or "").lower()
    data = upload.read()
    if name.endswith(".xlsx"):
        grid = _xlsx_rows(data)
    elif name.endswith(".csv") or name.endswith(".txt"):
        text = data.decode("utf-8-sig", errors="replace")
        grid = [row for row in csv.reader(io.StringIO(text))]
    else:
        raise ApiError("Please choose an Excel (.xlsx) or CSV file.")
    grid = [row for row in grid if any(str(c).strip() for c in row)]
    if not grid:
        raise ApiError("That file has no rows in it.")
    if len(grid) - 1 > _IMPORT_ROW_CAP:
        raise ApiError(
            "That file has more than {} data rows — please split it "
            "and import in parts.".format(_IMPORT_ROW_CAP))
    headers = [str(h).strip() for h in grid[0]]
    rows = []
    for raw in grid[1:]:
        rows.append({headers[i]: (raw[i] if i < len(raw) else "")
                     for i in range(len(headers))})
    return headers, rows


@bp.post("/utilities/import/parse")
@require_session
@require_approved
@require_unlocked
def import_parse():
    kind = str(request.form.get("kind") or "items").lower()
    if kind not in ("items", "parties"):
        raise ApiError("Import kind must be items or parties.")
    upload = request.files.get("file")
    if upload is None:
        raise ApiError("Choose a file to import.")
    headers, rows = _file_rows(upload)
    return jsonify(ok=True, headers=headers, rows=rows,
                   mapping=_suggest_mapping(kind, headers))


def _digits(text):
    return re.sub(r"\D", "", str(text or ""))


def _phone_key(text):
    d = _digits(text)
    return d[-10:] if len(d) > 10 else d


def _wizard_normalize(kind, mapping, raw):
    def val(field):
        header = (mapping or {}).get(field) or ""
        return str(raw.get(header) or "").strip() if header else ""

    errors = []
    name = val("name")
    if not name:
        errors.append("Name is required.")
    if kind == "parties":
        data = {
            "name": name,
            "phone": val("phone"),
            "address": val("address"),
            "email": val("email"),
            "group": val("group"),
            "opening_balance_paisa": _rupees_to_paisa(
                val("opening_balance"), "Opening Balance", errors, 0),
        }
    else:
        qty_text = val("stock_qty")
        try:
            qty = int(float(qty_text)) if qty_text else 0
        except ValueError:
            errors.append("Stock Qty must be a number.")
            qty = 0
        tax_text = val("tax_pct").replace("%", "")
        try:
            tax_bp = int(round(float(tax_text) * 100)) if tax_text else 0
        except ValueError:
            errors.append("Tax % must be a number.")
            tax_bp = 0
        data = {
            "name": name,
            "item_code": val("item_code"),
            "category": val("category"),
            "unit": val("unit"),
            "sale_price_paisa": _rupees_to_paisa(
                val("sale_price"), "Sale Price", errors, 0),
            "purchase_price_paisa": _rupees_to_paisa(
                val("purchase_price"), "Purchase Price", errors, 0),
            "wholesale_price_paisa": _rupees_to_paisa(
                val("wholesale_price"), "Wholesale Price", errors, 0),
            "mrp_paisa": _rupees_to_paisa(val("mrp"), "MRP", errors, 0),
            "stock_qty": qty,
            "tax_bp": tax_bp,
            "min_stock": int(float(val("min_stock")))
            if val("min_stock") else 0,
        }
    # _rupees_to_paisa appends dict-shaped errors; flatten to text
    flat = [e["message"] if isinstance(e, dict) else e for e in errors]
    return data, flat


def _find_duplicate(tx, biz, kind, data):
    """Match an import row against existing records. Parties: same
    name AND same phone (digits, last 10). Items: same item code, or
    same name with no conflicting code. Returns the row or None."""
    if kind == "parties":
        rows = tx.query_all(
            "SELECT * FROM parties WHERE business_id = ? "
            "AND deleted_at IS NULL AND lower(name) = lower(?)",
            (biz, data["name"]),
        )
        for row in rows:
            if _phone_key(row.get("phone")) == _phone_key(data["phone"]):
                return row
        return None
    code = (data.get("item_code") or "").strip()
    if code:
        row = tx.query_one(
            "SELECT * FROM items WHERE business_id = ? "
            "AND deleted_at IS NULL AND item_code = ?",
            (biz, code),
        )
        if row is not None:
            return row
    rows = tx.query_all(
        "SELECT * FROM items WHERE business_id = ? "
        "AND deleted_at IS NULL AND lower(name) = lower(?)",
        (biz, data["name"]),
    )
    for row in rows:
        if not code or not (row.get("item_code") or "").strip() \
                or (row.get("item_code") or "").strip() == code:
            return row
    return None


@bp.post("/utilities/import/wizard-validate")
@require_session
@require_approved
@require_unlocked
def import_wizard_validate():
    data = _body()
    kind = str(data.get("kind") or "items").lower()
    if kind not in ("items", "parties"):
        raise ApiError("Import kind must be items or parties.")
    rows_in = data.get("rows")
    if not isinstance(rows_in, list):
        raise ApiError("Rows must be a list.")
    mapping = data.get("mapping") or {}
    out = []
    new_units = []
    with db.transaction() as tx:
        biz = _business_id(tx)
        for index, raw in enumerate(rows_in):
            norm, errors = _wizard_normalize(kind, mapping, raw or {})
            entry = {"index": index, "data": norm, "errors": errors}
            if errors:
                entry["status"] = "error"
            else:
                dup = _find_duplicate(tx, biz, kind, norm)
                if dup is not None:
                    entry["status"] = "duplicate"
                    entry["existingId"] = dup["id"]
                    entry["existingName"] = dup["name"]
                else:
                    entry["status"] = "new"
                if kind == "items" and norm.get("unit"):
                    new_units.append(norm["unit"])
            out.append(entry)
    counts = {"new": 0, "duplicate": 0, "error": 0}
    for entry in out:
        counts[entry["status"]] += 1
    return jsonify(ok=True, rows=out, counts=counts,
                   units=sorted(set(new_units)))


@bp.post("/utilities/import/wizard-confirm")
@require_session
@require_approved
@require_unlocked
def import_wizard_confirm():
    data = _body()
    kind = str(data.get("kind") or "items").lower()
    if kind not in ("items", "parties"):
        raise ApiError("Import kind must be items or parties.")
    batch = str(data.get("batch") or "wizard").strip()
    opening_date = str(data.get("opening_date") or "").strip()
    rows_in = data.get("rows")
    if not isinstance(rows_in, list):
        raise ApiError("Nothing to import.")
    created = updated = skipped = 0
    with db.transaction() as tx:
        biz = _business_id(tx)
        for index, entry in enumerate(rows_in):
            if not isinstance(entry, dict):
                skipped += 1
                continue
            norm = entry.get("data") or {}
            action = str(entry.get("action") or "create").lower()
            if action == "skip" or not str(norm.get("name") or "").strip():
                skipped += 1
                continue
            client_ref = "import:{}:{}:{}".format(batch, kind, index)
            dup = _find_duplicate(tx, biz, kind, norm)
            if dup is not None and action != "update":
                skipped += 1  # safe default, even on a re-run
                continue
            if kind == "parties":
                if dup is not None:
                    tx.execute(
                        "UPDATE parties SET name = ?, phone = ?, "
                        "email = ?, group_name = ?, "
                        "billing_address = ?, "
                        "opening_balance_paisa = ?, opening_date = ? "
                        "WHERE id = ?",
                        (norm["name"], norm["phone"] or None,
                         norm["email"], norm["group"], norm["address"],
                         norm["opening_balance_paisa"],
                         opening_date or dup.get("opening_date") or "",
                         dup["id"]),
                    )
                    updated += 1
                    continue
                exists = tx.query_one(
                    "SELECT id FROM parties WHERE business_id = ? "
                    "AND client_ref = ?",
                    (biz, client_ref),
                )
                if exists:
                    skipped += 1
                    continue
                tx.execute(
                    "INSERT INTO parties (business_id, name, phone, "
                    "opening_balance_paisa, client_ref, created_at, "
                    "group_name, email, billing_address, opening_date) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (biz, norm["name"], norm["phone"] or None,
                     norm["opening_balance_paisa"], client_ref, now_iso(),
                     norm["group"], norm["email"], norm["address"],
                     opening_date),
                )
                created += 1
            else:
                if dup is not None:
                    tx.execute(
                        "UPDATE items SET name = ?, item_code = ?, "
                        "category = ?, unit = ?, sale_price_paisa = ?, "
                        "purchase_price_paisa = ?, "
                        "wholesale_price_paisa = ?, mrp_paisa = ?, "
                        "stock_qty = ?, tax_bp = ?, min_stock = ? "
                        "WHERE id = ?",
                        (norm["name"], norm["item_code"], norm["category"],
                         norm["unit"], norm["sale_price_paisa"],
                         norm["purchase_price_paisa"],
                         norm["wholesale_price_paisa"], norm["mrp_paisa"],
                         norm["stock_qty"], norm["tax_bp"],
                         norm["min_stock"], dup["id"]),
                    )
                    _sync_main_godown(tx, biz, dup["id"])
                    updated += 1
                    continue
                exists = tx.query_one(
                    "SELECT id FROM items WHERE business_id = ? "
                    "AND client_ref = ?",
                    (biz, client_ref),
                )
                if exists:
                    skipped += 1
                    continue
                iid = tx.insert(
                    "INSERT INTO items (business_id, name, "
                    "sale_price_paisa, purchase_price_paisa, stock_qty, "
                    "client_ref, created_at, item_code, category, unit, "
                    "min_stock, wholesale_price_paisa, mrp_paisa, "
                    "tax_bp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?) RETURNING id",
                    (biz, norm["name"], norm["sale_price_paisa"],
                     norm["purchase_price_paisa"], norm["stock_qty"],
                     client_ref, now_iso(), norm["item_code"],
                     norm["category"], norm["unit"], norm["min_stock"],
                     norm["wholesale_price_paisa"], norm["mrp_paisa"],
                     norm["tax_bp"]),
                )
                _sync_main_godown(tx, biz, iid)
                created += 1
    return jsonify(ok=True, created=created, updated=updated,
                   skipped=skipped), 201
