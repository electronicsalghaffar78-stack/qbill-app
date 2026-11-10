"""Online Store (Vyapar): store settings, the Manage Items catalogue,
a public storefront with cart + Cash on Delivery ordering, and the
ONLINE ORDERS workflow (Convert to Sale / Deliver Order / Reopen).

The public storefront endpoints are intentionally unauthenticated —
they are the shop's public page — but they only expose listed items
and can only create orders, never read business data.
"""
from flask import Blueprint, jsonify, request

from . import db
from .auth import require_approved, require_session, require_unlocked
from .crud import _business_id, _insert_document, _settings
from .security import ApiError, now_iso
from .settings_api import get_settings, save_settings

bp = Blueprint("store", __name__, url_prefix="/api")
public_bp = Blueprint("store_public", __name__, url_prefix="/api/public")


def _body():
    return request.get_json(silent=True) or {}


def _store_settings(tx, biz):
    settings = get_settings(tx, biz)
    store = dict(settings.get("store") or {})
    if not store.get("name"):
        business = tx.query_one("SELECT name FROM businesses WHERE id = ?", (biz,))
        store["name"] = (business or {}).get("name") or ""
    return store


@bp.get("/store/settings")
@require_session
@require_approved
@require_unlocked
def get_store_settings():
    with db.transaction() as tx:
        biz = _business_id(tx)
        store = _store_settings(tx, biz)
    return jsonify(ok=True, store=store, businessId=biz)


@bp.put("/store/settings")
@require_session
@require_approved
@require_unlocked
def put_store_settings():
    patch = _body().get("store")
    if not isinstance(patch, dict):
        raise ApiError("Store settings must be an object.")
    with db.transaction() as tx:
        biz = _business_id(tx)
        settings = save_settings(tx, biz, {"store": patch})
        store = _store_settings(tx, biz)
    return jsonify(ok=True, store=store, businessId=biz)


@bp.get("/store/items")
@require_session
@require_approved
@require_unlocked
def list_store_items():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT i.*, s.store_price_paisa, s.in_stock AS store_in_stock, "
            "s.id AS store_row_id FROM items i "
            "LEFT JOIN store_items s ON s.item_id = i.id AND s.business_id = i.business_id "
            "WHERE i.business_id = ? AND i.deleted_at IS NULL ORDER BY i.id",
            (biz,),
        )
    return jsonify(
        ok=True,
        items=[
            {
                "itemId": r["id"],
                "name": r["name"],
                "salePricePaisa": r["sale_price_paisa"],
                "stockQty": r["stock_qty"],
                "listed": r["store_row_id"] is not None,
                "storePricePaisa": (
                    r["store_price_paisa"]
                    if r["store_price_paisa"] is not None
                    else r["sale_price_paisa"]
                ),
                "inStock": bool(r["store_in_stock"])
                if r["store_in_stock"] is not None else True,
            }
            for r in rows
        ],
    )


@bp.put("/store/items/<int:item_id>")
@require_session
@require_approved
@require_unlocked
def put_store_item(item_id):
    data = _body()
    with db.transaction() as tx:
        biz = _business_id(tx)
        item = tx.query_one(
            "SELECT * FROM items WHERE id = ? AND business_id = ?", (item_id, biz)
        )
        if item is None:
            raise ApiError("Item not found.", status=404)
        listed = data.get("listed", True)
        if not listed:
            tx.execute(
                "DELETE FROM store_items WHERE business_id = ? AND item_id = ?",
                (biz, item_id),
            )
            return jsonify(ok=True, listed=False)
        price = data.get("store_price_paisa", item["sale_price_paisa"])
        if isinstance(price, bool) or not isinstance(price, int) or price < 0:
            raise ApiError("Store price must be a whole number of paisa.")
        tx.execute(
            "INSERT INTO store_items (business_id, item_id, store_price_paisa, in_stock) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(business_id, item_id) "
            "DO UPDATE SET store_price_paisa = excluded.store_price_paisa, "
            "in_stock = excluded.in_stock",
            (biz, item_id, price, 1 if data.get("in_stock", True) else 0),
        )
    return jsonify(ok=True, listed=True)


# ------------------------------------------------------------ online orders --
def _order_dict(tx, row):
    lines = tx.query_all(
        "SELECT l.*, i.name AS item_name FROM store_order_lines l "
        "LEFT JOIN items i ON i.id = l.item_id WHERE l.order_id = ? ORDER BY l.id",
        (row["id"],),
    )
    return {
        "id": row["id"],
        "customerName": row["customer_name"],
        "phone": row["phone"],
        "address": row["address"],
        "totalPaisa": row["total_paisa"],
        "status": row["status"],
        "invoiceId": row["invoice_id"],
        "createdAt": row["created_at"],
        "lines": [
            {"itemId": l["item_id"], "itemName": l["item_name"] or "",
             "qty": l["qty"], "ratePaisa": l["rate_paisa"],
             "amountPaisa": l["amount_paisa"]}
            for l in lines
        ],
    }


@bp.get("/store/orders")
@require_session
@require_approved
@require_unlocked
def list_store_orders():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM store_orders WHERE business_id = ? ORDER BY id DESC",
            (biz,),
        )
        result = [_order_dict(tx, r) for r in rows]
    return jsonify(ok=True, orders=result)


@bp.post("/store/orders/<int:order_id>/convert")
@require_session
@require_approved
@require_unlocked
def convert_store_order(order_id):
    """Convert to Sale: the order becomes a Sale Invoice (stock moves,
    the customer becomes a party matched by phone)."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        settings = _settings(tx, biz)
        order = tx.query_one(
            "SELECT * FROM store_orders WHERE id = ? AND business_id = ?",
            (order_id, biz),
        )
        if order is None:
            raise ApiError("Order not found.", status=404)
        if order["invoice_id"]:
            raise ApiError("This order was already converted to a Sale Invoice.")
        party_id = None
        if order["phone"]:
            party = tx.query_one(
                "SELECT id FROM parties WHERE business_id = ? AND phone = ? "
                "ORDER BY id LIMIT 1",
                (biz, order["phone"]),
            )
            if party:
                party_id = party["id"]
        if party_id is None and order["customer_name"]:
            party_id = tx.insert(
                "INSERT INTO parties (business_id, name, phone, "
                "opening_balance_paisa, client_ref, created_at) "
                "VALUES (?, ?, ?, 0, NULL, ?) RETURNING id",
                (biz, order["customer_name"], order["phone"] or None, now_iso()),
            )
        order_lines = tx.query_all(
            "SELECT * FROM store_order_lines WHERE order_id = ? ORDER BY id",
            (order_id,),
        )
        lines = []
        subtotal = 0
        for line in order_lines:
            lines.append({
                "item_id": line["item_id"],
                "description": "",
                "qty": line["qty"],
                "rate_paisa": line["rate_paisa"],
                "amount_paisa": line["amount_paisa"],
                "discount_paisa": 0, "tax_paisa": 0,
                "discount_bp": None, "tax_bp": None,
                "free_qty": 0, "batch_no": "", "exp_date": "", "serial_no": "",
            })
            subtotal += line["amount_paisa"]
        for line in lines:
            if line["item_id"] is not None:
                item = tx.query_one(
                    "SELECT name FROM items WHERE id = ?", (line["item_id"],)
                )
                line["description"] = (item or {}).get("name") or "Item"
        delivery = int((_store_settings(tx, biz)).get("delivery_charges_paisa") or 0)
        if delivery and order["total_paisa"] == subtotal + delivery:
            # Delivery charges ride on the sale as their own line, so
            # the invoice total matches the order total exactly.
            lines.append({
                "item_id": None, "description": "Delivery Charges",
                "qty": 1, "rate_paisa": delivery, "amount_paisa": delivery,
                "discount_paisa": 0, "tax_paisa": 0,
                "discount_bp": None, "tax_bp": None,
                "free_qty": 0, "batch_no": "", "exp_date": "", "serial_no": "",
            })
            subtotal += delivery
        extra = {"online_order_id": order_id}
        inv_id = _insert_document(
            tx, biz, "sale", party_id, lines, subtotal, 0, 0, 0, None,
            settings=settings, extra=extra,
        )
        tx.execute(
            "UPDATE store_orders SET status = 'converted', invoice_id = ? WHERE id = ?",
            (inv_id, order_id),
        )
        order = tx.query_one("SELECT * FROM store_orders WHERE id = ?", (order_id,))
        result = _order_dict(tx, order)
    return jsonify(ok=True, order=result, invoiceId=inv_id), 201


@bp.post("/store/orders/<int:order_id>/status")
@require_session
@require_approved
@require_unlocked
def store_order_status(order_id):
    status = str(_body().get("status") or "").strip().lower()
    if status not in ("delivered", "pending"):
        raise ApiError("Status must be 'delivered' or 'pending' (Reopen).")
    with db.transaction() as tx:
        biz = _business_id(tx)
        order = tx.query_one(
            "SELECT * FROM store_orders WHERE id = ? AND business_id = ?",
            (order_id, biz),
        )
        if order is None:
            raise ApiError("Order not found.", status=404)
        tx.execute(
            "UPDATE store_orders SET status = ? WHERE id = ?", (status, order_id)
        )
        order = tx.query_one("SELECT * FROM store_orders WHERE id = ?", (order_id,))
        result = _order_dict(tx, order)
    return jsonify(ok=True, order=result)


# ------------------------------------------------------ public storefront ----
@public_bp.get("/store/<int:business_id>/logo")
def public_store_logo(business_id):
    """The shop's own logo for its public storefront. Only the image
    bytes are exposed — nothing else about the business."""
    import base64
    import binascii

    from flask import Response

    with db.transaction() as tx:
        business = tx.query_one(
            "SELECT logo_data, logo_mime FROM businesses WHERE id = ?",
            (business_id,),
        )
    if business is None or not business.get("logo_data"):
        raise ApiError("Store not found.", status=404)
    try:
        raw = base64.b64decode(business["logo_data"], validate=True)
    except (binascii.Error, ValueError):
        raise ApiError("Store not found.", status=404)
    resp = Response(raw, mimetype=business.get("logo_mime") or "image/png")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@public_bp.get("/store/<int:business_id>")
def public_store(business_id):
    with db.transaction() as tx:
        business = tx.query_one(
            "SELECT id, name, logo_data FROM businesses WHERE id = ?",
            (business_id,),
        )
        if business is None:
            raise ApiError("Store not found.", status=404)
        store = _store_settings(tx, business_id)
        if not store.get("enabled"):
            raise ApiError("This store is not open yet.", status=404)
        rows = tx.query_all(
            "SELECT i.id, i.name, i.category, i.stock_qty, "
            "s.store_price_paisa FROM store_items s "
            "JOIN items i ON i.id = s.item_id "
            "WHERE s.business_id = ? AND i.deleted_at IS NULL ORDER BY i.name",
            (business_id,),
        )
        link_stock = bool(store.get("link_stock", True))
        items = [
            {
                "itemId": r["id"], "name": r["name"],
                "category": r["category"] or "",
                "pricePaisa": r["store_price_paisa"],
                "inStock": (r["stock_qty"] > 0) if link_stock else True,
            }
            for r in rows
        ]
    return jsonify(
        ok=True,
        store={
            "name": store.get("name") or business["name"],
            "logoUrl": (
                "/api/public/store/{}/logo".format(business_id)
                if business.get("logo_data") else ""
            ),
            "acceptOrders": bool(store.get("accept_orders", True)),
            "minOrderPaisa": int(store.get("min_order_paisa") or 0),
            "deliveryChargesPaisa": int(store.get("delivery_charges_paisa") or 0),
        },
        items=items,
    )


@public_bp.post("/store/<int:business_id>/order")
def public_order(business_id):
    data = _body()
    name = str(data.get("customer_name") or "").strip()
    phone = str(data.get("phone") or "").strip()
    address = str(data.get("address") or "").strip()
    lines_in = data.get("lines")
    if not name or not phone:
        raise ApiError("Your name and phone number are required to place an order.")
    if not isinstance(lines_in, list) or not lines_in:
        raise ApiError("Your cart is empty.")
    with db.transaction() as tx:
        business = tx.query_one(
            "SELECT id FROM businesses WHERE id = ?", (business_id,)
        )
        if business is None:
            raise ApiError("Store not found.", status=404)
        store = _store_settings(tx, business_id)
        if not store.get("enabled"):
            raise ApiError("This store is not open yet.", status=404)
        if not store.get("accept_orders", True):
            raise ApiError("This store is not accepting online orders right now.")
        subtotal = 0
        lines = []
        for raw in lines_in:
            item_id = raw.get("item_id")
            qty = raw.get("qty")
            if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
                raise ApiError("Each item needs a quantity of at least 1.")
            listing = tx.query_one(
                "SELECT s.store_price_paisa, i.name FROM store_items s "
                "JOIN items i ON i.id = s.item_id "
                "WHERE s.business_id = ? AND s.item_id = ? AND i.deleted_at IS NULL",
                (business_id, item_id),
            )
            if listing is None:
                raise ApiError("An item in your cart is no longer available.")
            amount = qty * listing["store_price_paisa"]
            subtotal += amount
            lines.append((item_id, qty, listing["store_price_paisa"], amount))
        min_order = int(store.get("min_order_paisa") or 0)
        if subtotal < min_order:
            raise ApiError(
                "Minimum order amount is Rs {:,.2f}.".format(min_order / 100)
            )
        delivery = int(store.get("delivery_charges_paisa") or 0)
        total = subtotal + delivery
        oid = tx.insert(
            "INSERT INTO store_orders (business_id, customer_name, phone, "
            "address, total_paisa, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?) RETURNING id",
            (business_id, name, phone, address, total, now_iso()),
        )
        for item_id, qty, rate, amount in lines:
            tx.execute(
                "INSERT INTO store_order_lines (business_id, order_id, item_id, "
                "qty, rate_paisa, amount_paisa) VALUES (?, ?, ?, ?, ?, ?)",
                (business_id, oid, item_id, qty, rate, amount),
            )
    return jsonify(ok=True, orderId=oid, totalPaisa=total), 201
