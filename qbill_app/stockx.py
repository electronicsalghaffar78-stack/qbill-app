"""Stock extras: Godowns, Stock Transfer, item batches (Vyapar Stock).

Stock model: items.stock_qty is the total across all godowns; the
godown_stock table holds the per-godown split and always sums to the
total. Documents trade out of the Main Godown (see crud._apply_stock);
transfers move quantity between godowns without touching the total.
"""
from flask import Blueprint, jsonify, request

from . import db
from .auth import require_approved, require_session, require_unlocked
from .crud import (
    _business_id,
    _client_ref,
    _int_field,
    _sync_main_godown,
    ensure_main_godown,
)
from .security import ApiError, now_iso

bp = Blueprint("stockx", __name__, url_prefix="/api")

GODOWN_TYPES = ("Godown", "Retail Store", "Wholesale Store", "Assembly Plant", "Others")


def _body():
    return request.get_json(silent=True) or {}


def _godown_dict(tx, row):
    total = tx.query_one(
        "SELECT COALESCE(SUM(qty), 0) AS q FROM godown_stock WHERE godown_id = ?",
        (row["id"],),
    )["q"]
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["gtype"],
        "isMain": bool(row["is_main"]),
        "totalQty": total,
    }


@bp.get("/godowns")
@require_session
@require_approved
@require_unlocked
def list_godowns():
    with db.transaction() as tx:
        biz = _business_id(tx)
        ensure_main_godown(tx, biz)
        rows = tx.query_all(
            "SELECT * FROM godowns WHERE business_id = ? ORDER BY is_main DESC, id",
            (biz,),
        )
        result = [_godown_dict(tx, r) for r in rows]
    return jsonify(ok=True, godowns=result)


@bp.post("/godowns")
@require_session
@require_approved
@require_unlocked
def create_godown():
    data = _body()
    name = str(data.get("name") or "").strip()
    if not name:
        raise ApiError("Godown name is required.")
    gtype = str(data.get("type") or "Godown").strip()
    if gtype not in GODOWN_TYPES:
        raise ApiError("Godown type must be one of: {}.".format(", ".join(GODOWN_TYPES)))
    with db.transaction() as tx:
        biz = _business_id(tx)
        ensure_main_godown(tx, biz)
        gid = tx.insert(
            "INSERT INTO godowns (business_id, name, gtype, is_main, created_at) "
            "VALUES (?, ?, ?, 0, ?) RETURNING id",
            (biz, name, gtype, now_iso()),
        )
        row = tx.query_one("SELECT * FROM godowns WHERE id = ?", (gid,))
        result = _godown_dict(tx, row)
    return jsonify(ok=True, godown=result), 201


@bp.delete("/godowns/<int:godown_id>")
@require_session
@require_approved
@require_unlocked
def delete_godown(godown_id):
    """An empty, unused godown deletes cleanly. The Main Godown, a
    godown holding stock, and one named in past transfers are refused
    with the reason — their records are part of the stock history."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM godowns WHERE id = ? AND business_id = ?",
            (godown_id, biz),
        )
        if row is None:
            raise ApiError("Godown not found.", status=404)
        if row["is_main"]:
            raise ApiError("The Main Godown cannot be deleted.")
        held = tx.query_one(
            "SELECT COUNT(*) AS c FROM godown_stock "
            "WHERE godown_id = ? AND qty != 0",
            (godown_id,),
        )["c"]
        if held:
            raise ApiError(
                "This godown still holds stock — transfer it out "
                "before deleting the godown.")
        moved = tx.query_one(
            "SELECT COUNT(*) AS c FROM stock_transfers "
            "WHERE business_id = ? AND (from_godown_id = ? "
            "OR to_godown_id = ?)",
            (biz, godown_id, godown_id),
        )["c"]
        if moved:
            raise ApiError(
                "This godown appears in past stock transfers, so it "
                "cannot be deleted.")
        tx.execute(
            "DELETE FROM godown_stock WHERE godown_id = ?", (godown_id,))
        tx.execute("DELETE FROM godowns WHERE id = ?", (godown_id,))
    return jsonify(ok=True)


@bp.get("/godowns/<int:godown_id>/stock")
@require_session
@require_approved
@require_unlocked
def godown_stock(godown_id):
    """Stock Summary for one godown."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        godown = tx.query_one(
            "SELECT * FROM godowns WHERE id = ? AND business_id = ?",
            (godown_id, biz),
        )
        if godown is None:
            raise ApiError("Godown not found.", status=404)
        rows = tx.query_all(
            "SELECT gs.qty, i.id AS item_id, i.name, i.purchase_price_paisa "
            "FROM godown_stock gs JOIN items i ON i.id = gs.item_id "
            "WHERE gs.godown_id = ? ORDER BY i.id",
            (godown_id,),
        )
    return jsonify(
        ok=True,
        godown={"id": godown["id"], "name": godown["name"]},
        items=[
            {
                "id": r["item_id"],
                "name": r["name"],
                "qty": r["qty"],
                "valuePaisa": r["qty"] * r["purchase_price_paisa"],
            }
            for r in rows
        ],
    )


# ---------------------------------------------------------- stock transfer --
def _transfer_dict(row, godown_names, item_names):
    return {
        "id": row["id"],
        "itemId": row["item_id"],
        "itemName": item_names.get(row["item_id"]) or "",
        "fromGodownId": row["from_godown_id"],
        "fromGodown": godown_names.get(row["from_godown_id"]) or "",
        "toGodownId": row["to_godown_id"],
        "toGodown": godown_names.get(row["to_godown_id"]) or "",
        "qty": row["qty"],
        "note": row["note"],
        "createdAt": row["created_at"],
    }


@bp.post("/stock-transfers")
@require_session
@require_approved
@require_unlocked
def create_transfer():
    data = _body()
    item_id = data.get("item_id")
    from_id = data.get("from_godown_id")
    to_id = data.get("to_godown_id")
    qty = _int_field(data, "qty", required=True, min_value=1)
    client_ref = _client_ref(data)
    with db.transaction() as tx:
        biz = _business_id(tx)
        if client_ref:
            existing = tx.query_one(
                "SELECT * FROM stock_transfers WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:
                return jsonify(ok=True, transfer=_transfer_dict(
                    existing, _godown_names(tx, biz), _item_names(tx, biz))), 200
        item = tx.query_one(
            "SELECT * FROM items WHERE id = ? AND business_id = ?", (item_id, biz)
        )
        if item is None:
            raise ApiError("Item not found.")
        for gid, label in ((from_id, "Source"), (to_id, "Destination")):
            godown = tx.query_one(
                "SELECT id FROM godowns WHERE id = ? AND business_id = ?",
                (gid, biz),
            )
            if godown is None:
                raise ApiError("{} godown not found.".format(label))
        if from_id == to_id:
            raise ApiError("Source and destination godowns must be different.")
        # Make sure both godowns have a stock row for this item, then
        # check the source actually holds enough.
        _ensure_godown_row(tx, biz, from_id, item)
        _ensure_godown_row(tx, biz, to_id, item)
        source = tx.query_one(
            "SELECT qty FROM godown_stock WHERE godown_id = ? AND item_id = ?",
            (from_id, item_id),
        )
        if (source or {}).get("qty", 0) < qty:
            raise ApiError(
                "Source godown holds only {} of this item.".format(
                    (source or {}).get("qty", 0))
            )
        tx.execute(
            "UPDATE godown_stock SET qty = qty - ? WHERE godown_id = ? AND item_id = ?",
            (qty, from_id, item_id),
        )
        tx.execute(
            "UPDATE godown_stock SET qty = qty + ? WHERE godown_id = ? AND item_id = ?",
            (qty, to_id, item_id),
        )
        tid = tx.insert(
            "INSERT INTO stock_transfers (business_id, item_id, from_godown_id, "
            "to_godown_id, qty, note, created_at, client_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (biz, item_id, from_id, to_id, qty,
             str(data.get("note") or "").strip(), now_iso(), client_ref),
        )
        row = tx.query_one("SELECT * FROM stock_transfers WHERE id = ?", (tid,))
        result = _transfer_dict(row, _godown_names(tx, biz), _item_names(tx, biz))
    return jsonify(ok=True, transfer=result), 201


def _ensure_godown_row(tx, biz, godown_id, item):
    row = tx.query_one(
        "SELECT id FROM godown_stock WHERE godown_id = ? AND item_id = ?",
        (godown_id, item["id"]),
    )
    if row is not None:
        return
    main_id = ensure_main_godown(tx, biz)
    if godown_id == main_id:
        _sync_main_godown(tx, biz, item["id"])
        return
    tx.execute(
        "INSERT INTO godown_stock (business_id, godown_id, item_id, qty) "
        "VALUES (?, ?, ?, 0)",
        (biz, godown_id, item["id"]),
    )


def _godown_names(tx, biz):
    return {
        r["id"]: r["name"]
        for r in tx.query_all(
            "SELECT id, name FROM godowns WHERE business_id = ?", (biz,)
        )
    }


def _item_names(tx, biz):
    return {
        r["id"]: r["name"]
        for r in tx.query_all(
            "SELECT id, name FROM items WHERE business_id = ?", (biz,)
        )
    }


@bp.get("/stock-transfers")
@require_session
@require_approved
@require_unlocked
def list_transfers():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM stock_transfers WHERE business_id = ? ORDER BY id",
            (biz,),
        )
        result = [
            _transfer_dict(r, _godown_names(tx, biz), _item_names(tx, biz))
            for r in rows
        ]
    return jsonify(ok=True, transfers=result)


# ---------------------------------------------------------------- batches ----
def _batch_dict(row):
    return {
        "id": row["id"],
        "itemId": row["item_id"],
        "batchNo": row["batch_no"],
        "expDate": row["exp_date"],
        "mfgDate": row["mfg_date"],
        "qty": row["qty"],
        "createdAt": row["created_at"],
    }


@bp.get("/items/<int:item_id>/batches")
@require_session
@require_approved
@require_unlocked
def list_batches(item_id):
    with db.transaction() as tx:
        biz = _business_id(tx)
        item = tx.query_one(
            "SELECT id FROM items WHERE id = ? AND business_id = ?", (item_id, biz)
        )
        if item is None:
            raise ApiError("Item not found.", status=404)
        rows = tx.query_all(
            "SELECT * FROM item_batches WHERE business_id = ? AND item_id = ? "
            "ORDER BY id",
            (biz, item_id),
        )
    return jsonify(ok=True, batches=[_batch_dict(r) for r in rows])


@bp.post("/items/<int:item_id>/batches")
@require_session
@require_approved
@require_unlocked
def add_batch(item_id):
    """Add Stock - Batches: a new batch also adds its quantity to the
    item's stock (and to the Main Godown)."""
    data = _body()
    qty = _int_field(data, "qty", required=True, min_value=1)
    batch_no = str(data.get("batch_no") or "").strip()
    if not batch_no:
        raise ApiError("Batch No. is required.")
    client_ref = _client_ref(data)
    with db.transaction() as tx:
        biz = _business_id(tx)
        item = tx.query_one(
            "SELECT * FROM items WHERE id = ? AND business_id = ?", (item_id, biz)
        )
        if item is None:
            raise ApiError("Item not found.", status=404)
        if client_ref:
            existing = tx.query_one(
                "SELECT * FROM item_batches WHERE business_id = ? AND client_ref = ?",
                (biz, client_ref),
            )
            if existing is not None:
                return jsonify(ok=True, batch=_batch_dict(existing)), 200
        bid = tx.insert(
            "INSERT INTO item_batches (business_id, item_id, batch_no, exp_date, "
            "mfg_date, qty, created_at, client_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "RETURNING id",
            (biz, item_id, batch_no, str(data.get("exp_date") or "").strip(),
             str(data.get("mfg_date") or "").strip(), qty, now_iso(), client_ref),
        )
        from .crud import _apply_stock

        _apply_stock(tx, biz, item_id, qty)
        row = tx.query_one("SELECT * FROM item_batches WHERE id = ?", (bid,))
    return jsonify(ok=True, batch=_batch_dict(row)), 201
