"""Business settings store + Terms & Conditions library (Vyapar Settings).

All QBill settings live in one JSON document per business
(business_settings.data). DEFAULT_SETTINGS below is the single source of
truth for every key; GET merges the stored JSON over the defaults, PUT
merges the caller's keys into the stored JSON. Every toggle in the
Settings screens maps to a key here and is honoured by the server
(document engine, PDF, payments) and/or the frontend — no dead switches.
"""
import json

from flask import Blueprint, jsonify, request

from . import db
from .auth import require_approved, require_session, require_unlocked
from .crud import _business_id
from .security import ApiError, now_iso

bp = Blueprint("settings_api", __name__, url_prefix="/api")

DOC_TYPE_NAMES = [
    "sale", "purchase", "sale_return", "purchase_return", "estimate",
    "sale_order", "purchase_order", "challan", "proforma",
]

DEFAULT_PREFIXES = {
    "sale": "S", "purchase": "P", "sale_return": "SR",
    "purchase_return": "PR", "estimate": "EST", "sale_order": "SO",
    "purchase_order": "PO", "challan": "DC", "proforma": "PI",
    "payment_in": "PIN",
}

DEFAULT_SETTINGS = {
    # ---- Settings > GENERAL ----
    "enable_passcode": False,
    "decimal_places": 2,
    "stop_sale_negative_stock": False,
    "block_new_from_txn": False,
    "more_tx_estimate": True,
    "more_tx_proforma": True,
    "more_tx_sale_order": True,
    "more_tx_purchase_order": True,
    "more_tx_other_income": True,
    "more_tx_challan": True,
    "more_tx_goods_return_on_challan": False,
    "auto_backup": True,
    "audit_trail": True,
    # ---- Settings > TRANSACTION ----
    "billing_name_of_parties": True,
    "show_last_purchase_price": False,
    "customers_po_details": True,
    "txn_free_item_qty": True,
    "txn_quick_entry": False,
    "do_not_show_invoice_preview": False,
    "txn_passcode_edit_delete": False,
    "discount_during_payments": False,
    "link_payments_to_invoices": True,
    "due_dates_payment_terms": True,
    "show_profit_on_sale": False,
    "repeat_invoices": True,
    # Delta build #2 (Vyapar Settings > TRANSACTION):
    "txn_add_time": False,          # print the bill time in Invoice Details
    "txn_cash_sale_default": False,  # bill form opens in Cash mode
    "txn_wise_tax": True,           # per-line Tax % editable on the bill form
    "txn_wise_discount": True,      # per-line Disc % editable on the bill form
    "round_off_mode": "nearest",    # "nearest" | "one" (round up to Rs 1)
    "prefixes": dict(DEFAULT_PREFIXES),
    # ---- Settings > PRINT ----
    "print_theme": "Tally Theme",
    "print_color": "#f26522",
    "print_company_info": True,
    "print_names": {},
    "print_paper_size": "A4",
    "print_orientation": "Portrait",
    "print_copies": "Original",
    "print_extra_top_space": 0,
    "print_item_columns": ["item", "qty", "unit", "price", "discount",
                           "tax", "amount"],
    "print_total_qty": True,
    "print_received_balance": True,
    "print_party_balance": False,
    "print_tax_details": True,
    "print_you_saved": True,
    "print_amount_words": True,
    "print_terms": True,
    "print_signature": True,
    "signature_text": "",
    "print_payment_mode": True,
    "thermal_size": "80mm",
    "thermal_copies": 1,
    "thermal_auto_cut": True,
    # ---- Settings > TAXES ----
    "tax_enabled": True,
    "default_tax_bp": 0,
    # ---- Settings > TRANSACTION MESSAGE ----
    "msg_whatsapp_personal": True,
    "msg_include_balance": True,
    "msg_include_link": False,
    "msg_footer": "Thank you for your business.",
    "msg_auto": {"sale": True, "purchase": False, "payment_in": True,
                 "payment_out": False, "estimate": True},
    "msg_template": (
        "Dear [Party Name], your [Transaction Type] [Transaction Number] "
        "of Rs [Amount] has been recorded with [Business Name]."
    ),
    # ---- Settings > PARTY ----
    "party_grouping": True,
    "party_shipping_address": True,
    "party_manage_status": True,
    "party_reminder_enabled": True,
    "party_reminder_days": 3,
    "party_reminder_message": (
        "Dear [Party Name], a payment of Rs [Amount] is due for "
        "[Business Name]. Please pay at the earliest. Thank you."
    ),
    "party_extra_fields": [
        {"label": "Field 1", "enabled": False, "print": False},
        {"label": "Field 2", "enabled": False, "print": False},
        {"label": "Field 3", "enabled": False, "print": False},
        {"label": "Field 4", "enabled": False, "print": False},
    ],
    # ---- Settings > ITEM ----
    "item_enabled": True,
    "sell_type": "products_services",
    "barcode_scan": False,
    "stock_maintenance": True,
    "manufacturing": False,
    "low_stock_dialog": True,
    "items_unit": True,
    "default_unit": "Pcs",
    "item_category_enabled": True,
    "party_wise_item_rate": False,
    "item_description_enabled": True,
    "item_wise_tax": True,
    "item_wise_discount": True,
    "update_sale_price_from_txn": False,
    "item_extra_fields": [
        {"key": "mrp", "label": "MRP", "enabled": True},
        {"key": "serial", "label": "Serial No./IMEI No.", "enabled": False},
        {"key": "batch", "label": "Batch No.", "enabled": True},
        {"key": "exp", "label": "Exp Date", "enabled": True},
        {"key": "mfg", "label": "Mfg Date", "enabled": False},
        {"key": "model", "label": "Model No.", "enabled": False},
        {"key": "size", "label": "Size", "enabled": False},
    ],
    # ---- Settings > ACCOUNTING ----
    "accounting_enabled": False,
    # ---- Loyalty Points (Parties > Loyalty Points setup) ----
    "loyalty": {
        "enabled": False,
        "spend_per_point_paisa": 10000,
        "min_invoice_earn_paisa": 0,
        "expiry_days": 365,
        "redeem_value_paisa": 100,
        "max_redeem_pct": 50,
        "min_invoice_redeem_paisa": 0,
    },
    # ---- Online Store ----
    "store": {
        "enabled": False,
        "accept_orders": True,
        "min_order_paisa": 0,
        "delivery_charges_paisa": 0,
        "item_discounts": False,
        "link_stock": True,
        "name": "",
    },
    # ---- Home widgets ----
    "widgets": {
        "cash_in_hand": True,
        "bank_balance": True,
        "low_stock": True,
        "expiring_stock": True,
    },
}


def _merge(base, over):
    out = dict(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            merged = dict(out[key])
            merged.update(value)
            out[key] = merged
        else:
            out[key] = value
    return out


def get_settings(tx, business_id):
    row = tx.query_one(
        "SELECT data FROM business_settings WHERE business_id = ?",
        (business_id,),
    )
    stored = {}
    if row is not None:
        try:
            stored = json.loads(row["data"] or "{}")
        except ValueError:
            stored = {}
    return _merge(DEFAULT_SETTINGS, stored)


def save_settings(tx, business_id, patch):
    current = get_settings(tx, business_id)
    merged = _merge(current, patch)
    # Store only the delta against defaults to keep the row small.
    delta = {}
    for key, value in merged.items():
        if value != DEFAULT_SETTINGS.get(key):
            delta[key] = value
    existing = tx.query_one(
        "SELECT business_id FROM business_settings WHERE business_id = ?",
        (business_id,),
    )
    if existing is None:
        tx.execute(
            "INSERT INTO business_settings (business_id, data) VALUES (?, ?)",
            (business_id, json.dumps(delta)),
        )
    else:
        tx.execute(
            "UPDATE business_settings SET data = ? WHERE business_id = ?",
            (json.dumps(delta), business_id),
        )
    return merged


def _body():
    return request.get_json(silent=True) or {}


@bp.get("/settings")
@require_session
@require_approved
@require_unlocked
def get_settings_endpoint():
    with db.transaction() as tx:
        settings = get_settings(tx, _business_id(tx))
    return jsonify(ok=True, settings=settings)


@bp.put("/settings")
@require_session
@require_approved
@require_unlocked
def put_settings_endpoint():
    patch = _body().get("settings")
    if not isinstance(patch, dict):
        raise ApiError("Settings must be an object.")
    with db.transaction() as tx:
        settings = save_settings(tx, _business_id(tx), patch)
    return jsonify(ok=True, settings=settings)


# ------------------------------------------------ Terms & Conditions ----
SEED_TERMS = [
    ("Goods Return Policy",
     "Goods once sold will not be taken back or exchanged.",
     ["sale", "sale_order", "estimate", "proforma", "challan"]),
    ("Delivery Check",
     "Please check the goods at the time of delivery.",
     ["sale", "purchase", "challan", "sale_order"]),
    ("Payment Terms",
     "Payment is due as agreed with the party.",
     ["sale", "purchase", "estimate", "proforma"]),
    ("Warranty",
     "Warranty, where applicable, is as per the manufacturer's terms.",
     ["sale", "estimate", "proforma"]),
    ("Overdue Interest",
     "Interest @ 18% per annum will be charged on overdue payments.",
     ["sale"]),
    ("Bill Verification",
     "Please verify all items and amounts on this bill before signing.",
     ["sale", "challan", "purchase"]),
]


def _term_dict(row):
    try:
        applies = json.loads(row["applies"] or "[]")
    except ValueError:
        applies = []
    return {
        "id": row["id"],
        "header": row["header"],
        "description": row["description"],
        "applies": applies,
    }


def seed_terms(tx, business_id):
    count = tx.query_one(
        "SELECT COUNT(*) AS c FROM terms_library WHERE business_id = ?",
        (business_id,),
    )["c"]
    if count:
        return
    for header, description, applies in SEED_TERMS:
        tx.execute(
            "INSERT INTO terms_library (business_id, header, description, "
            "applies, created_at) VALUES (?, ?, ?, ?, ?)",
            (business_id, header, description, json.dumps(applies), now_iso()),
        )


@bp.get("/terms")
@require_session
@require_approved
@require_unlocked
def list_terms():
    with db.transaction() as tx:
        biz = _business_id(tx)
        seed_terms(tx, biz)
        rows = tx.query_all(
            "SELECT * FROM terms_library WHERE business_id = ? ORDER BY id",
            (biz,),
        )
    return jsonify(ok=True, terms=[_term_dict(r) for r in rows])


@bp.post("/terms")
@require_session
@require_approved
@require_unlocked
def create_term():
    data = _body()
    header = str(data.get("header") or "").strip()
    if not header:
        raise ApiError("Terms header is required.")
    applies = data.get("applies") or ["sale"]
    applies = [a for a in applies if a in DOC_TYPE_NAMES] or ["sale"]
    with db.transaction() as tx:
        biz = _business_id(tx)
        tid = tx.insert(
            "INSERT INTO terms_library (business_id, header, description, "
            "applies, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
            (biz, header, str(data.get("description") or "").strip(),
             json.dumps(applies), now_iso()),
        )
        row = tx.query_one("SELECT * FROM terms_library WHERE id = ?", (tid,))
    return jsonify(ok=True, term=_term_dict(row)), 201


@bp.put("/terms/<int:term_id>")
@require_session
@require_approved
@require_unlocked
def update_term(term_id):
    data = _body()
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM terms_library WHERE id = ? AND business_id = ?",
            (term_id, biz),
        )
        if row is None:
            raise ApiError("Terms not found.", status=404)
        applies = data.get("applies")
        if applies is not None:
            applies = [a for a in applies if a in DOC_TYPE_NAMES] or ["sale"]
        tx.execute(
            "UPDATE terms_library SET header = ?, description = ?, applies = ? "
            "WHERE id = ?",
            (
                str(data.get("header", row["header"]) or "").strip() or row["header"],
                str(data.get("description", row["description"]) or "").strip(),
                json.dumps(applies) if applies is not None else row["applies"],
                term_id,
            ),
        )
        row = tx.query_one("SELECT * FROM terms_library WHERE id = ?", (term_id,))
    return jsonify(ok=True, term=_term_dict(row))


@bp.delete("/terms/<int:term_id>")
@require_session
@require_approved
@require_unlocked
def delete_term(term_id):
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT id FROM terms_library WHERE id = ? AND business_id = ?",
            (term_id, biz),
        )
        if row is None:
            raise ApiError("Terms not found.", status=404)
        tx.execute("DELETE FROM terms_library WHERE id = ?", (term_id,))
    return jsonify(ok=True)
