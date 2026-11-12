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


def _logo_image(business):
    """The business's own logo as an ImageReader, or None when the
    account has not set one (the header then stays text-only)."""
    data = (business or {}).get("logo_data")
    if not data:
        return None
    import base64
    import binascii

    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw:
        return None
    from reportlab.lib.utils import ImageReader

    return ImageReader(io.BytesIO(raw))


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

    # Build .22: with tax-inclusive pricing the line rates already
    # contain the tax, so a printed line amount never adds it again.
    inclusive = bool(settings.get("tax_inclusive"))
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
    logo_img = _logo_image(business) if show_company else None
    name_x = left + 46 if logo_img else left
    if show_company:
        if family == "band":
            pdf.setFillColorRGB(*accent)
            pdf.rect(0, y - 6, width, 44, stroke=0, fill=1)
            if logo_img:
                pdf.drawImage(logo_img, left, y - 3, width=38, height=38,
                              mask="auto")
            pdf.setFillColorRGB(1, 1, 1)
            pdf.setFont("Helvetica-Bold", 19)
            pdf.drawString(name_x, y + 14, business["name"] or "My Business")
            pdf.setFont("Helvetica", 9)
            contact = " · ".join(
                part for part in [business.get("address"), business.get("phone"),
                                  business.get("email")] if part
            )
            if contact:
                pdf.drawString(name_x, y - 1, contact[:110])
            pdf.setFillColorRGB(0, 0, 0)
            y -= 58
        else:
            if family == "box":
                pdf.setStrokeColorRGB(*accent)
                pdf.setLineWidth(1.4)
                pdf.rect(left - 8, y - 26, right - left + 16, 46, stroke=1, fill=0)
                pdf.setStrokeColorRGB(0, 0, 0)
                pdf.setLineWidth(1)
            if logo_img:
                pdf.drawImage(logo_img, left, y - 15, width=40, height=40,
                              mask="auto")
            pdf.setFont("Helvetica-Bold", 20)
            pdf.drawString(name_x, y, business["name"] or "My Business")
            y -= 18
            pdf.setFont("Helvetica", 10)
            contact = " · ".join(
                part for part in [business.get("address"), business.get("phone"),
                                  business.get("email")] if part
            )
            if contact:
                pdf.drawString(name_x, y, contact)
                y -= 6
            y -= 8
            pdf.setStrokeColorRGB(*accent)
            pdf.line(left, y, right, y)
            pdf.setStrokeColorRGB(0, 0, 0)
            y -= 24
    else:
        y -= 10

    # Build .22: the shop's NTN / STRN from Business Profile print
    # under the header when filled; empty prints nothing.
    tax_ids = []
    if business.get("ntn"):
        tax_ids.append("NTN: " + str(business["ntn"]))
    if business.get("strn"):
        tax_ids.append("STRN: " + str(business["strn"]))
    if tax_ids and show_company:
        pdf.setFont("Helvetica", 9)
        pdf.drawString(left, y + 10, " · ".join(tax_ids)[:110])
        y -= 4

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
    created = invoice.get("createdAt") or ""
    date_text = created[:10]
    if settings.get("txn_add_time") and len(created) >= 16:
        # Settings > TRANSACTION > Add Time on Transactions.
        date_text += " " + created[11:16]
    pdf.drawRightString(right, y, "Date: {}".format(date_text))
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
    # Party additional fields whose Settings "Show In Print" switch is
    # ON (build .20 parity: the switch existed in Settings but nothing
    # printed the values — a dead toggle). Label: value, under the
    # party block, only for fields the party actually has a value for.
    _extras = party.get("extras") or {}
    _extra_lines = []
    for _f in settings.get("party_extra_fields") or []:
        if not isinstance(_f, dict):
            continue
        if _f.get("enabled") and _f.get("show_in_print"):
            _v = _extras.get(_f.get("label") or "")
            if _v:
                _extra_lines.append("{}: {}".format(_f["label"], _v))
    if _extra_lines:
        pdf.setFont("Helvetica", 9)
        for _line in _extra_lines:
            pdf.drawString(left, y, _line[:110])
            y -= 12
        pdf.setFont("Helvetica", 10)
    y -= 2
    pdf.line(left, y, right, y)
    y -= 18

    # Item table — FIXED columns (round-3 fix, 2026-11-10). The old
    # Settings-driven picker stored print_item_columns as a boolean
    # dict ({qty: true, batch: false, ...}) which this renderer then
    # iterated as a column LIST: every dict key it recognised printed —
    # including unchecked Batch/Serial/MRP/Exp columns — while Item,
    # Price and Amount, not being picker options at all, vanished from
    # real bills (Qudoos's S-1 showed only the qty "2"). The printed
    # table is now always the columns a customer reads, and the
    # batch/serial/MRP/exp columns are gone from the PDF entirely (the
    # bill form no longer collects them):
    # # | Item | Qty | Unit | Price/Unit | Discount | Tax | Amount.
    col_defs = [
        ("num", "#", 0.05, "right"),
        ("item", "Item", 0.31, "left"),
        ("qty", "Qty", 0.08, "right"),
        ("unit", "Unit", 0.08, "left"),
        ("price", "Price/Unit", 0.13, "right"),
        ("discount", "Discount", 0.10, "right"),
        ("tax", "Tax", 0.10, "right"),
        ("amount", "Amount", 0.15, "right"),
    ]
    col_align = {c[0]: c[3] for c in col_defs}
    table_w = right - left
    xs = {}
    x = left
    for key, _label, frac, _align in col_defs:
        w = table_w * frac
        xs[key] = (x, x + w)
        x += w

    def draw_cell(col, text, bold=False, size=10, dy=0):
        x0, x1 = xs[col]
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        if col_align[col] == "right":
            pdf.drawRightString(x1 - 2, y + dy, str(text))
        else:
            pdf.drawString(x0 + 2, y + dy, str(text))

    def wrap_item_name(text):
        """Word-wrap an item name inside its column: long names print
        in full on extra lines instead of overflowing the row."""
        max_w = xs["item"][1] - xs["item"][0] - 4
        words = str(text).split()
        if not words:
            return [""]
        lines, cur = [], ""
        for word in words:
            trial = (cur + " " + word).strip()
            if pdf.stringWidth(trial, "Helvetica", 9) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines

    for key, label, _frac, _align in col_defs:
        draw_cell(key, label, bold=True)
    y -= 6
    pdf.line(left, y, right, y)
    y -= 16
    for row_no, line in enumerate(invoice["lines"], start=1):
        item = item_map.get(line.get("itemId")) or {}
        name = str(line["description"] or item.get("name") or "")
        name_lines = wrap_item_name(name)
        row_h = max(15, 11 * len(name_lines) + 4)
        if y - row_h < 170:
            pdf.showPage()
            y = height - 50
        values = {
            "num": str(row_no),
            "qty": _qty_text(line),
            "unit": item.get("unit") or "",
            "price": fmt(line["ratePaisa"]),
            "discount": fmt(line.get("discountPaisa") or 0) if line.get("discountPaisa") else "",
            "tax": fmt(line.get("taxPaisa") or 0) if line.get("taxPaisa") else "",
            "amount": fmt(line["amountPaisa"] - (line.get("discountPaisa") or 0)
                          + (0 if inclusive else (line.get("taxPaisa") or 0))),
        }
        for col, val in values.items():
            draw_cell(col, val)
        for j, part in enumerate(name_lines):
            draw_cell("item", part, size=9, dy=-(11 * j))
        y -= row_h
    y -= 4
    pdf.line(left, y, right, y)
    y -= 18

    totals_x = right - 190
    totals = [("Subtotal", invoice["subtotalPaisa"])]
    if invoice["discountPaisa"]:
        totals.append(("Discount", invoice["discountPaisa"]))
    if invoice.get("taxPaisa") and settings.get("print_tax_details", True):
        totals.append(("Tax (included)" if inclusive else "Tax",
                       invoice["taxPaisa"]))
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
    balance_block = opts.get("balance_block")
    if balance_block:
        # Build .22 (research P0-1): the shaded khata trust block —
        # Previous Balance + This Bill − Paid = Total Payable. When
        # the block prints it replaces the single current-balance
        # line below (its Total Payable is that same balance, in
        # "party owes us" space).
        y -= 2
        pdf.setFillColorRGB(0.93, 0.95, 0.98)
        pdf.rect(left, y - 62, right - left, 70, stroke=0, fill=1)
        pdf.setFillColorRGB(0, 0, 0)
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(left + 8, y, "Balance Summary")
        y -= 14
        for block_label, block_value in (
            ("Previous Balance", balance_block["previous"]),
            ("This Bill", balance_block["this_bill"]),
            ("Paid", balance_block["paid"]),
        ):
            pdf.setFont("Helvetica", 10)
            pdf.drawString(left + 8, y, block_label + ":")
            pdf.drawRightString(right - 8, y, fmt(block_value))
            y -= 13
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(left + 8, y, "Total Payable:")
        pdf.drawRightString(right - 8, y, fmt(balance_block["total"]))
        y -= 14
    elif settings.get("print_party_balance") and opts.get("party_balance") is not None:
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
    # Build .22 (research P0-3): the Business Profile payment rails
    # as a "Pay To:" block — Raast IBAN (with a scannable QR of the
    # IBAN), JazzCash / Easypaisa numbers and the free-text payment
    # details. Prints only when the profile carries payment info,
    # and not alongside the bank-account block above (one Pay To
    # block per bill; the bank block is the older structured route).
    profile_pay_lines = []
    if business.get("raast_iban"):
        profile_pay_lines.append("Raast IBAN: " + str(business["raast_iban"]))
    if business.get("jazzcash_number"):
        profile_pay_lines.append(
            "JazzCash: " + str(business["jazzcash_number"]))
    if business.get("easypaisa_number"):
        profile_pay_lines.append(
            "Easypaisa: " + str(business["easypaisa_number"]))
    for detail_line in str(business.get("payment_details") or "").splitlines():
        detail_line = detail_line.strip()
        if detail_line:
            profile_pay_lines.append(detail_line)
    if profile_pay_lines and not (bank and bank.get("print_details")):
        y -= 6
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(left, y, "Pay To:")
        y -= 14
        pdf.setFont("Helvetica", 9.5)
        for pay_line in profile_pay_lines:
            pdf.drawString(left, y, pay_line[:110])
            y -= 12
        if business.get("raast_iban"):
            try:
                pdf.drawImage(
                    _qr_image(str(business["raast_iban"])),
                    right - 84, y - 62, width=80, height=80, mask="auto",
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
    """Thermal receipt layout: 58/80/100mm roll widths (Settings >
    PRINT, or the per-print size choice — build .22)."""
    size = settings.get("thermal_size") or "80mm"
    width = {"58mm": 164, "80mm": 226, "100mm": 283}.get(size, 226)
    inclusive = bool(settings.get("tax_inclusive"))
    height = 1200
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(width, height), pageCompression=0)
    left = 8
    right = width - 8
    y = height - 24
    logo_img = _logo_image(business)
    if logo_img:
        pdf.drawImage(logo_img, width / 2 - 20, y - 28, width=40, height=40,
                      mask="auto")
        y -= 46
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
    # Build .22: NTN / STRN from Business Profile, when filled.
    tax_ids = []
    if business.get("ntn"):
        tax_ids.append("NTN: " + str(business["ntn"]))
    if business.get("strn"):
        tax_ids.append("STRN: " + str(business["strn"]))
    if tax_ids:
        pdf.drawCentredString(width / 2, y, " · ".join(tax_ids)[:52])
        y -= 11
    inv_type = invoice.get("type") or "sale"
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawCentredString(
        width / 2, y, DOC_TITLES.get(inv_type, "SALE INVOICE"))
    y -= 12
    pdf.setFont("Helvetica", 8.5)
    pdf.drawString(left, y, "No: " + invoice["invoiceNo"])
    created = invoice.get("createdAt") or ""
    date_text = created[:10]
    if settings.get("txn_add_time") and len(created) >= 16:
        date_text += " " + created[11:16]
    pdf.drawRightString(right, y, date_text)
    y -= 11
    pdf.drawString(left, y, "Party: " + party_name[:34])
    y -= 8
    pdf.line(left, y, right, y)
    y -= 12
    for _ln_idx, line in enumerate(invoice["lines"], 1):
        # Build .20 (defect D5): every printed line is numbered, the
        # same "#" the A4 bill carries — the thermal roll used to
        # print bare descriptions with no line numbers.
        pdf.drawString(left, y, "{}. {}".format(
            _ln_idx, str(line["description"])[:27]))
        y -= 10
        pdf.drawString(
            left + 8, y,
            "{} x {}".format(_qty_text(line), fmt(line["ratePaisa"])))
        amount = line["amountPaisa"] - (line.get("discountPaisa") or 0) \
            + (0 if inclusive else (line.get("taxPaisa") or 0))
        pdf.drawRightString(right, y, fmt(amount))
        y -= 12
    pdf.line(left, y, right, y)
    y -= 13
    for label, value in (
        ("Subtotal", invoice["subtotalPaisa"]),
        ("Discount", invoice["discountPaisa"]),
        ("Tax (incl.)" if inclusive else "Tax", invoice["taxPaisa"]),
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
    # Build .22: the same balance block as the A4 bill (sale + party).
    balance_block = opts.get("balance_block")
    if balance_block:
        y -= 2
        pdf.line(left, y, right, y)
        y -= 12
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(left, y, "Balance Summary")
        y -= 12
        for block_label, block_value in (
            ("Previous Balance", balance_block["previous"]),
            ("This Bill", balance_block["this_bill"]),
            ("Paid", balance_block["paid"]),
            ("Total Payable", balance_block["total"]),
        ):
            pdf.setFont(
                "Helvetica-Bold" if block_label == "Total Payable"
                else "Helvetica", 9)
            pdf.drawString(left, y, block_label)
            pdf.drawRightString(right, y, fmt(block_value))
            y -= 12
    # Build .22: Pay To block — the Business Profile payment rails,
    # falling back to the bank account marked for printing (the A4
    # bill's older route) so a thermal bill carries one too.
    pay_lines = []
    if business.get("raast_iban"):
        pay_lines.append("Raast IBAN: " + str(business["raast_iban"]))
    if business.get("jazzcash_number"):
        pay_lines.append("JazzCash: " + str(business["jazzcash_number"]))
    if business.get("easypaisa_number"):
        pay_lines.append("Easypaisa: " + str(business["easypaisa_number"]))
    for detail_line in str(business.get("payment_details") or "").splitlines():
        detail_line = detail_line.strip()
        if detail_line:
            pay_lines.append(detail_line)
    if not pay_lines:
        bank = opts.get("bank")
        if bank and bank.get("print_details"):
            if bank.get("holder_name"):
                pay_lines.append("Account Title: " + bank["holder_name"])
            if bank.get("account_number"):
                pay_lines.append("Account Number: " + bank["account_number"])
            if bank.get("iban"):
                pay_lines.append("IBAN: " + bank["iban"])
            if bank.get("qr_handle"):
                pay_lines.append("JazzCash / EasyPaisa: " + bank["qr_handle"])
    if pay_lines:
        y -= 2
        pdf.line(left, y, right, y)
        y -= 12
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(left, y, "Pay To:")
        y -= 12
        pdf.setFont("Helvetica", 8.5)
        max_chars = 30 if width < 200 else 46
        for pay_line in pay_lines:
            # Wrap, never truncate: a cut-off IBAN or account number
            # is useless for paying.
            for chunk_start in range(0, len(pay_line), max_chars):
                pdf.drawString(
                    left, y, pay_line[chunk_start:chunk_start + max_chars])
                y -= 11
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
    # Build .22: a thermal print can name its roll width for this one
    # print (58mm / 80mm / 100mm); without it the PRINT setting rules.
    thermal_size = request.args.get("size") or ""
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
        if thermal and thermal_size in ("58mm", "80mm", "100mm"):
            settings = dict(settings)
            settings["thermal_size"] = thermal_size
        if business.get("signature_text"):
            # Business Profile > Signature Text wins over the PRINT
            # setting when the owner has filled it in.
            settings = dict(settings)
            settings["signature_text"] = business["signature_text"]
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
        balance_block = None
        if row["party_id"] is not None:
            balances = party_balance_map(tx, biz)
            if settings.get("print_party_balance"):
                party_balance = balances.get(row["party_id"])
            # Build .22 (research P0-1): the khata trust block on every
            # SALE bill — Previous Balance + This Bill − Paid = Total
            # Payable, paisa-exact. Amounts here are in "the party owes
            # us" space (the negation of the party_balance_map sign):
            # the current balance already contains this bill and its
            # payments, so the previous balance is derived from it.
            if (row.get("type") or "sale") == "sale" \
                    and balances.get(row["party_id"]) is not None:
                owed_now = -balances[row["party_id"]]
                balance_block = {
                    "previous": owed_now - invoice["totalPaisa"]
                                + invoice["paidPaisa"],
                    "this_bill": invoice["totalPaisa"],
                    "paid": invoice["paidPaisa"],
                    "total": owed_now,
                }
        item_map = {}
        for line in invoice["lines"]:
            if line.get("itemId") is not None and line["itemId"] not in item_map:
                item = tx.query_one(
                    "SELECT name, unit FROM items WHERE id = ?",
                    (line["itemId"],),
                )
                if item:
                    item_map[line["itemId"]] = {
                        "name": item.get("name") or "",
                        "unit": item.get("unit") or "",
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
            "logo_data": business.get("logo_data"),
            "logo_mime": business.get("logo_mime"),
            "ntn": business.get("ntn"),
            "strn": business.get("strn"),
            "raast_iban": business.get("raast_iban"),
            "jazzcash_number": business.get("jazzcash_number"),
            "easypaisa_number": business.get("easypaisa_number"),
            "payment_details": business.get("payment_details"),
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
            "balance_block": balance_block,
            "payment_modes": [m["mode"] for m in modes if m.get("mode")],
            "thermal": thermal,
        },
    )
    resp = Response(data, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = (
        'attachment; filename="invoice-{}.pdf"'.format(invoice["invoiceNo"])
    )
    return resp
