"""Invoice PDF (reportlab) — bytes only, generated in memory, never stored.

Layout follows a real shop quotation: business name big at the top with
address/phone/email, invoice number + date, party name, item table, totals,
amount in words, Terms & Conditions, and a Payment Information line.

The endpoint sits behind the heavy-work gate (max 3 at once, honest
503 + Retry-After) so PDF rendering can never jam the server.
pageCompression=0 keeps the text plainly readable in the byte stream.
"""
import io

from flask import Blueprint, Response, g, jsonify
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from . import db
from .auth import require_approved, require_session, require_unlocked
from .crud import _business_id, _invoice_dict
from .heavy import heavy_endpoint
from .security import ApiError

bp = Blueprint("pdf", __name__, url_prefix="/api")

TERMS = [
    "1. Goods once sold will not be taken back or exchanged.",
    "2. Please check the goods at the time of delivery.",
    "3. Payment is due as agreed with the party.",
    "4. Warranty, where applicable, is as per the manufacturer's terms.",
]

DOC_TITLES = {
    "sale": "SALE INVOICE",
    "purchase": "PURCHASE INVOICE",
    "estimate": "ESTIMATE",
    "sale_order": "SALE ORDER",
    "purchase_order": "PURCHASE ORDER",
    "challan": "DELIVERY CHALLAN",
    "sale_return": "CREDIT NOTE",
    "purchase_return": "DEBIT NOTE",
    "proforma": "PROFORMA INVOICE",
}

# The 12 QBill print themes (Settings > PRINT). Each maps to one of
# three header families on the single reportlab engine, plus its own
# suggested accent colour (the chosen colour swatch wins when set).
THEMES = {
    "Tally Theme": "classic",
    "GST Theme 1": "band",
    "GST Theme 2": "band",
    "Modern Theme": "band",
    "Stylish Theme": "band",
    "Advanced GST Theme": "box",
    "Tax Invoice (Multi-Color) V1": "box",
    "Tax Invoice (Multi-Color) V2": "band",
    "Tax Invoice (Multi-Color) V3": "box",
    "Amazon Theme": "band",
    "Flipkart Theme": "band",
    "Bill Book Theme": "classic",
}


# ------------------------------------------------------- amount in words --
_ONES = [
    "Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
    "Nine", "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen",
    "Sixteen", "Seventeen", "Eighteen", "Nineteen",
]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy",
         "Eighty", "Ninety"]


def _below_thousand(n):
    parts = []
    if n >= 100:
        parts.append(_ONES[n // 100] + " Hundred")
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10])
        if n % 10:
            parts.append(_ONES[n % 10])
    elif n > 0:
        parts.append(_ONES[n])
    return " ".join(parts)


def _number_words(n):
    """Indian numbering (thousand / lakh / crore) — standard on local bills."""
    if n == 0:
        return "Zero"
    parts = []
    crore, n = divmod(n, 10_000_000)
    if crore:
        parts.append(_number_words(crore) + " Crore")
    lakh, n = divmod(n, 100_000)
    if lakh:
        parts.append(_below_thousand(lakh) + " Lakh")
    thousand, n = divmod(n, 1_000)
    if thousand:
        parts.append(_below_thousand(thousand) + " Thousand")
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def paisa_to_words(paisa):
    rupees, ps = divmod(int(paisa), 100)
    text = "Rupees " + _number_words(rupees)
    if ps:
        text += " and Paisa " + _number_words(ps)
    return text + " Only"


def fmt(paisa):
    """Display only — all arithmetic elsewhere stays integer paisa."""
    return "Rs {:,.2f}".format(paisa / 100)


# -------------------------------------------------------------- rendering --
def _hex_color(value, fallback=(0.95, 0.40, 0.13)):
    try:
        text = str(value or "").lstrip("#")
        if len(text) != 6:
            return fallback
        return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return fallback


def _qr_image(payload):
    """A real QR code (PNG bytes) for the invoice Pay To block."""
    import qrcode

    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    from reportlab.lib.utils import ImageReader

    return ImageReader(buf)


def _qty_text(line):
    qty = line["qty"]
    free = line.get("freeQty") or 0
    if free:
        return "{}+{}".format(qty, free)
    return str(qty)


def render_invoice_pdf(business, invoice, party_name, opts=None):
    opts = opts or {}
    settings = opts.get("settings") or {}
    if opts.get("thermal"):
        return _render_thermal(business, invoice, party_name, settings, opts)
    party = opts.get("party") or {}
    bank = opts.get("bank")
    terms = opts.get("terms") or []
    item_map = opts.get("item_map") or {}

    buf = io.BytesIO()
    landscape = (settings.get("print_orientation") == "Landscape"
                 and settings.get("print_paper_size") == "A4")
    pagesize = (A4[1], A4[0]) if landscape else A4
    if settings.get("print_paper_size") == "A5":
        pagesize = (A4[0] / 2, A4[1] / 2) if not landscape else (A4[1] / 2, A4[0] / 2)
    pdf = canvas.Canvas(buf, pagesize=pagesize, pageCompression=0)
    width, height = pagesize
    left, right = 40, width - 40
    accent = _hex_color(settings.get("print_color"))
    family = THEMES.get(settings.get("print_theme") or "Tally Theme", "classic")
    y = height - 50 - int(settings.get("print_extra_top_space") or 0) * 2

    show_company = settings.get("print_company_info", True)
    if show_company:
        if family == "band":
            pdf.setFillColorRGB(*accent)
            pdf.rect(0, y - 6, width, 44, stroke=0, fill=1)
            pdf.setFillColorRGB(1, 1, 1)
            pdf.setFont("Helvetica-Bold", 19)
            pdf.drawString(left, y + 14, business["name"] or "My Business")
            pdf.setFont("Helvetica", 9)
            contact = " · ".join(
                part for part in [business.get("address"), business.get("phone"),
                                  business.get("email")] if part
            )
            if contact:
                pdf.drawString(left, y - 1, contact[:110])
            pdf.setFillColorRGB(0, 0, 0)
            y -= 58
        else:
            if family == "box":
                pdf.setStrokeColorRGB(*accent)
                pdf.setLineWidth(1.4)
                pdf.rect(left - 8, y - 26, right - left + 16, 46, stroke=1, fill=0)
                pdf.setStrokeColorRGB(0, 0, 0)
                pdf.setLineWidth(1)
            pdf.setFont("Helvetica-Bold", 20)
            pdf.drawString(left, y, business["name"] or "My Business")
            y -= 18
            pdf.setFont("Helvetica", 10)
            contact = " · ".join(
                part for part in [business.get("address"), business.get("phone"),
                                  business.get("email")] if part
            )
            if contact:
                pdf.drawString(left, y, contact)
                y -= 6
            y -= 8
            pdf.setStrokeColorRGB(*accent)
            pdf.line(left, y, right, y)
            pdf.setStrokeColorRGB(0, 0, 0)
            y -= 24
    else:
        y -= 10

    names = settings.get("print_names") or {}
    inv_type = invoice.get("type") or "sale"
    title = names.get(inv_type) or DOC_TITLES.get(inv_type, "SALE INVOICE")
    pdf.setFont("Helvetica-Bold", 13)
    pdf.setFillColorRGB(*accent)
    pdf.drawString(left, y, title)
    pdf.setFillColorRGB(0, 0, 0)
    pdf.setFont("Helvetica", 10)
    pdf.drawRightString(right, y, "Invoice No: {}".format(invoice["invoiceNo"]))
    y -= 16
    party_label = "Estimate For" if inv_type == "estimate" else "Party"
    pdf.drawString(left, y, "{}: {}".format(party_label, party_name))
    pdf.drawRightString(
        right, y, "Date: {}".format((invoice.get("createdAt") or "")[:10])
    )
    y -= 14
    if settings.get("due_dates_payment_terms", True) and invoice.get("dueDate"):
        pdf.setFont("Helvetica", 9)
        pdf.drawString(left, y, "Due Date: {}".format(invoice["dueDate"]))
        if invoice.get("paymentTerms"):
            pdf.drawRightString(right, y, "Payment Terms: {}".format(invoice["paymentTerms"]))
        y -= 13
        pdf.setFont("Helvetica", 10)
    if settings.get("billing_name_of_parties", True):
        bill_addr = invoice.get("billingAddress") or party.get("billingAddress") or ""
        ship_addr = invoice.get("shippingAddress") or party.get("shippingAddress") or ""
        if bill_addr:
            pdf.setFont("Helvetica", 9)
            pdf.drawString(left, y, "Billing Address: " + bill_addr[:100])
            y -= 12
        if ship_addr:
            pdf.setFont("Helvetica", 9)
            pdf.drawString(left, y, "Shipping Address: " + ship_addr[:100])
            y -= 12
        pdf.setFont("Helvetica", 10)
    if invoice.get("transportName") or invoice.get("vehicleNo"):
        pdf.setFont("Helvetica", 9)
        bits = []
        if invoice.get("transportName"):
            bits.append("Transport: " + invoice["transportName"])
        if invoice.get("vehicleNo"):
            bits.append("Vehicle No: " + invoice["vehicleNo"])
        if invoice.get("deliveryDate"):
            bits.append("Delivery Date: " + invoice["deliveryDate"])
        if invoice.get("deliveryLocation"):
            bits.append("Delivery Location: " + invoice["deliveryLocation"])
        pdf.drawString(left, y, " · ".join(bits)[:115])
        y -= 13
        pdf.setFont("Helvetica", 10)
    y -= 2
    pdf.line(left, y, right, y)
    y -= 18

    # Item Table Customization (Settings > PRINT): chosen columns only.
    columns = settings.get("print_item_columns") or [
        "item", "qty", "unit", "price", "discount", "tax", "amount"]
    col_defs = {
        "item": ("Item", 0.34, "left"),
        "qty": ("Qty", 0.07, "right"),
        "unit": ("Unit", 0.07, "left"),
        "price": ("Rate", 0.11, "right"),
        "discount": ("Discount", 0.10, "right"),
        "tax": ("Tax", 0.10, "right"),
        "amount": ("Amount", 0.13, "right"),
        "batch": ("Batch No.", 0.10, "left"),
        "serial": ("Serial No.", 0.10, "left"),
        "mrp": ("MRP", 0.09, "right"),
        "exp": ("Exp Date", 0.09, "left"),
    }
    table_w = right - left
    total_frac = sum(col_defs[c][1] for c in columns if c in col_defs) or 1
    xs = {}
    x = left
    for col in columns:
        if col not in col_defs:
            continue
        w = table_w * col_defs[col][1] / total_frac
        xs[col] = (x, x + w)
        x += w

    def draw_cell(col, text, bold=False, size=10):
        x0, x1 = xs[col]
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        if col_defs[col][2] == "right":
            pdf.drawRightString(x1 - 2, y, str(text))
        else:
            pdf.drawString(x0 + 2, y, str(text))

    for col in columns:
        if col in col_defs:
            draw_cell(col, col_defs[col][0], bold=True)
    y -= 6
    pdf.line(left, y, right, y)
    y -= 16
    for line in invoice["lines"]:
        if y < 170:
            pdf.showPage()
            y = height - 50
        item = item_map.get(line.get("itemId")) or {}
        values = {
            "item": str(line["description"])[:46],
            "qty": _qty_text(line),
            "unit": item.get("unit") or "",
            "price": fmt(line["ratePaisa"]),
            "discount": fmt(line.get("discountPaisa") or 0) if line.get("discountPaisa") else "",
            "tax": fmt(line.get("taxPaisa") or 0) if line.get("taxPaisa") else "",
            "amount": fmt(line["amountPaisa"] - (line.get("discountPaisa") or 0)
                          + (line.get("taxPaisa") or 0)),
            "batch": line.get("batchNo") or "",
            "serial": line.get("serialNo") or "",
            "mrp": fmt(item.get("mrpPaisa") or 0) if item.get("mrpPaisa") else "",
            "exp": line.get("expDate") or "",
        }
        for col in columns:
            if col in values:
                draw_cell(col, values[col], size=9 if col == "item" else 10)
        y -= 15
    y -= 4
    pdf.line(left, y, right, y)
    y -= 18

    totals_x = right - 190
    totals = [("Subtotal", invoice["subtotalPaisa"])]
    if invoice["discountPaisa"]:
        totals.append(("Discount", invoice["discountPaisa"]))
    if invoice.get("taxPaisa") and settings.get("print_tax_details", True):
        totals.append(("Tax", invoice["taxPaisa"]))
    if invoice.get("loyaltyRedeemedPaisa"):
        totals.append(("Loyalty Redeemed", invoice["loyaltyRedeemedPaisa"]))
    totals.append(("Total", invoice["totalPaisa"]))
    if settings.get("print_received_balance", True) \
            and inv_type not in ("estimate", "sale_order", "purchase_order",
                                 "challan", "proforma"):
        totals.append(("Received", invoice["paidPaisa"]))
        totals.append(("Balance", invoice["balancePaisa"]))
    if settings.get("print_total_qty", True):
        total_qty = sum(
            l["qty"] + (l.get("freeQty") or 0) for l in invoice["lines"])
        totals.insert(0, ("Total Item Quantity", None))
        pdf.setFont("Helvetica", 10)
        pdf.drawRightString(totals_x + 90, y, "Total Item Quantity:")
        pdf.drawRightString(right, y, str(total_qty))
        y -= 15
    for label, value in totals:
        if value is None:
            continue
        pdf.setFont("Helvetica-Bold" if label in ("Total", "Balance") else "Helvetica", 10)
        pdf.drawRightString(totals_x + 90, y, label + ":")
        pdf.drawRightString(right, y, fmt(value))
        y -= 15
    if settings.get("print_party_balance") and opts.get("party_balance") is not None:
        pdf.setFont("Helvetica", 10)
        pdf.drawRightString(totals_x + 90, y, "Current Balance of Party:")
        pdf.drawRightString(right, y, fmt(opts["party_balance"]))
        y -= 15
    you_saved = (invoice["discountPaisa"] or 0) + (invoice.get("loyaltyRedeemedPaisa") or 0)
    if settings.get("print_you_saved", True) and you_saved:
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawRightString(totals_x + 90, y, "You Saved:")
        pdf.drawRightString(right, y, fmt(you_saved))
        y -= 15
    if invoice.get("loyaltyPointsEarned") or invoice.get("loyaltyPointsUsed"):
        pdf.setFont("Helvetica", 10)
        pdf.drawRightString(totals_x + 90, y, "Earned Points:")
        pdf.drawRightString(right, y, str(invoice.get("loyaltyPointsEarned") or 0))
        y -= 15
        pdf.drawRightString(totals_x + 90, y, "Available Points:")
        pdf.drawRightString(right, y, str(party.get("loyaltyPoints") or 0))
        y -= 15
    y -= 6
    if settings.get("print_amount_words", True):
        pdf.setFont("Helvetica-Oblique", 9)
        pdf.drawString(left, y, "Amount in words: " + paisa_to_words(invoice["totalPaisa"]))
        y -= 20
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(left, y, "Payment Information")
    y -= 14
    pdf.setFont("Helvetica", 10)
    pdf.drawString(
        left, y,
        "Paid: {} · Balance due: {}".format(
            fmt(invoice["paidPaisa"]), fmt(invoice["balancePaisa"])
        ),
    )
    y -= 15
    if settings.get("print_payment_mode", True) and opts.get("payment_modes"):
        pdf.setFont("Helvetica", 9)
        pdf.drawString(
            left, y, "Payment Mode: " + ", ".join(opts["payment_modes"]))
        y -= 13
    if bank and bank.get("print_details"):
        y -= 6
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(left, y, "Pay To:")
        y -= 14
        pdf.setFont("Helvetica", 9.5)
        pay_lines = []
        if bank.get("bank_name"):
            pay_lines.append("Bank: " + bank["bank_name"])
        if bank.get("holder_name"):
            pay_lines.append("Account Title: " + bank["holder_name"])
        if bank.get("account_number"):
            pay_lines.append("Account Number: " + bank["account_number"])
        if bank.get("iban"):
            pay_lines.append("IBAN: " + bank["iban"])
        if bank.get("qr_handle"):
            pay_lines.append("JazzCash / EasyPaisa: " + bank["qr_handle"])
        for pay_line in pay_lines:
            pdf.drawString(left, y, pay_line)
            y -= 12
        if bank.get("print_qr"):
            payload = bank.get("iban") or bank.get("qr_handle") \
                or bank.get("account_number") or ""
            if payload:
                try:
                    pdf.drawImage(
                        _qr_image(payload), right - 84, y - 62,
                        width=80, height=80, mask="auto",
                    )
                except Exception:
                    pass
    if business.get("tax_note"):
        y -= 8
        pdf.setFont("Helvetica", 9)
        pdf.drawString(left, y, "Tax Note: " + str(business["tax_note"])[:110])
        y -= 6
    if invoice.get("description"):
        y -= 8
        pdf.setFont("Helvetica", 9)
        pdf.drawString(left, y, "Description: " + str(invoice["description"])[:110])
        y -= 6
    if settings.get("print_terms", True):
        y -= 14
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(left, y, "Terms & Conditions")
        y -= 14
        pdf.setFont("Helvetica", 9)
        shown = terms or TERMS
        import re as _re

        for index, term in enumerate(shown, start=1):
            clean = _re.sub(r"^\d+\.\s*", "", str(term))
            pdf.drawString(left, y, "{}. {}".format(index, clean)[:112])
            y -= 12
    if settings.get("print_signature", True):
        y -= 16
        pdf.setFont("Helvetica", 10)
        signature = settings.get("signature_text") or "For {}".format(
            business["name"] or "My Business")
        pdf.drawRightString(right, y, signature)
        y -= 26
        pdf.setFont("Helvetica", 9)
        pdf.drawRightString(right, y, "Authorised Signatory")
    y -= 24
    pdf.setFont("Helvetica-Oblique", 9)
    pdf.drawString(left, y, "Thank you for your business.")
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


def _render_thermal(business, invoice, party_name, settings, opts):
    """Thermal receipt layout: 58/80/100mm roll widths."""
    size = settings.get("thermal_size") or "80mm"
    width = {"58mm": 164, "80mm": 226, "100mm": 283}.get(size, 226)
    height = 1200
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(width, height), pageCompression=0)
    left = 8
    right = width - 8
    y = height - 24
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawCentredString(width / 2, y, (business["name"] or "My Business")[:30])
    y -= 13
    pdf.setFont("Helvetica", 8)
    contact = " · ".join(
        part for part in [business.get("address"), business.get("phone")] if part
    )
    if contact:
        pdf.drawCentredString(width / 2, y, contact[:52])
        y -= 11
    inv_type = invoice.get("type") or "sale"
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawCentredString(
        width / 2, y, DOC_TITLES.get(inv_type, "SALE INVOICE"))
    y -= 12
    pdf.setFont("Helvetica", 8.5)
    pdf.drawString(left, y, "No: " + invoice["invoiceNo"])
    pdf.drawRightString(right, y, (invoice.get("createdAt") or "")[:10])
    y -= 11
    pdf.drawString(left, y, "Party: " + party_name[:34])
    y -= 8
    pdf.line(left, y, right, y)
    y -= 12
    for line in invoice["lines"]:
        pdf.drawString(left, y, str(line["description"])[:30])
        y -= 10
        pdf.drawString(
            left + 8, y,
            "{} x {}".format(_qty_text(line), fmt(line["ratePaisa"])))
        amount = line["amountPaisa"] - (line.get("discountPaisa") or 0) \
            + (line.get("taxPaisa") or 0)
        pdf.drawRightString(right, y, fmt(amount))
        y -= 12
    pdf.line(left, y, right, y)
    y -= 13
    for label, value in (
        ("Subtotal", invoice["subtotalPaisa"]),
        ("Discount", invoice["discountPaisa"]),
        ("Tax", invoice["taxPaisa"]),
        ("Total", invoice["totalPaisa"]),
        ("Paid", invoice["paidPaisa"]),
        ("Balance", invoice["balancePaisa"]),
    ):
        if label not in ("Subtotal", "Total") and not value:
            continue
        pdf.setFont("Helvetica-Bold" if label == "Total" else "Helvetica", 9)
        pdf.drawString(left, y, label)
        pdf.drawRightString(right, y, fmt(value))
        y -= 12
    y -= 4
    pdf.setFont("Helvetica-Oblique", 8)
    pdf.drawCentredString(width / 2, y, "Thank you for your business.")
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


# --------------------------------------------------------------- endpoint --
@bp.get("/invoices/<int:invoice_id>/pdf")
@require_session
@require_approved
@require_unlocked
@heavy_endpoint
def invoice_pdf(invoice_id):
    from flask import request

    from .crud import party_balance_map
    from .settings_api import get_settings, seed_terms

    thermal = request.args.get("format") == "thermal"
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM invoices WHERE id = ? AND business_id = ? "
            "AND deleted_at IS NULL",
            (invoice_id, biz),
        )
        if row is None:
            raise ApiError("Invoice not found.", status=404)
        invoice = _invoice_dict(tx, row)
        business = tx.query_one("SELECT * FROM businesses WHERE id = ?", (biz,))
        settings = get_settings(tx, biz)
        party_row = None
        party_name = "Cash Sale"
        if row["party_id"] is not None:
            from .crud import _party_dict

            raw_party = tx.query_one(
                "SELECT * FROM parties WHERE id = ?", (row["party_id"],)
            )
            if raw_party:
                party_name = raw_party["name"]
                party_row = _party_dict(raw_party)
        party_balance = None
        if settings.get("print_party_balance") and row["party_id"] is not None:
            party_balance = party_balance_map(tx, biz).get(row["party_id"])
        item_map = {}
        for line in invoice["lines"]:
            if line.get("itemId") is not None and line["itemId"] not in item_map:
                item = tx.query_one(
                    "SELECT unit, mrp_paisa FROM items WHERE id = ?",
                    (line["itemId"],),
                )
                if item:
                    item_map[line["itemId"]] = {
                        "unit": item.get("unit") or "",
                        "mrpPaisa": item.get("mrp_paisa") or 0,
                    }
        bank = tx.query_one(
            "SELECT * FROM bank_accounts WHERE business_id = ? "
            "AND print_details = 1 ORDER BY id LIMIT 1",
            (biz,),
        )
        modes = tx.query_all(
            "SELECT DISTINCT mode FROM payments WHERE invoice_id = ?",
            (invoice_id,),
        )
        # Terms resolution: the document's chosen term wins; then the
        # business's own terms text (Settings > TRANSACTION); then the
        # Terms & Conditions library entries for this document type.
        terms = []
        if row.get("terms_id"):
            term = tx.query_one(
                "SELECT * FROM terms_library WHERE id = ?", (row["terms_id"],)
            )
            if term:
                terms = ["{}: {}".format(term["header"], term["description"])
                         if term["description"] else term["header"]]
        if not terms:
            custom = [
                line.strip()
                for line in str(business.get("terms") or "").splitlines()
                if line.strip()
            ]
            terms = custom
        if not terms:
            seed_terms(tx, biz)
            inv_type = row.get("type") or "sale"
            lib = tx.query_all(
                "SELECT * FROM terms_library WHERE business_id = ? ORDER BY id",
                (biz,),
            )
            import json as _json

            for entry in lib:
                try:
                    applies = _json.loads(entry["applies"] or "[]")
                except ValueError:
                    applies = []
                if inv_type in applies:
                    terms.append(
                        "{}: {}".format(entry["header"], entry["description"])
                        if entry["description"] else entry["header"]
                    )
    data = render_invoice_pdf(
        {
            "name": business["name"],
            "address": business.get("address"),
            "phone": business.get("phone"),
            "email": business.get("email"),
            "tax_note": business.get("tax_note"),
            "terms": business.get("terms"),
        },
        invoice,
        party_name,
        opts={
            "settings": settings,
            "party": party_row or {},
            "bank": bank,
            "terms": terms,
            "item_map": item_map,
            "party_balance": party_balance,
            "payment_modes": [m["mode"] for m in modes if m.get("mode")],
            "thermal": thermal,
        },
    )
    resp = Response(data, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = (
        'attachment; filename="invoice-{}.pdf"'.format(invoice["invoiceNo"])
    )
    return resp
