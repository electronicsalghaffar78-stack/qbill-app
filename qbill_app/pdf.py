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
def render_invoice_pdf(business, invoice, party_name):
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4, pageCompression=0)
    width, height = A4
    left, right = 40, width - 40
    y = height - 50

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
    pdf.line(left, y, right, y)
    y -= 24

    title = "SALE INVOICE" if (invoice.get("type") or "sale") == "sale" else "PURCHASE INVOICE"
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(left, y, title)
    pdf.setFont("Helvetica", 10)
    pdf.drawRightString(right, y, "Invoice No: {}".format(invoice["invoiceNo"]))
    y -= 16
    pdf.drawString(left, y, "Party: {}".format(party_name))
    pdf.drawRightString(
        right, y, "Date: {}".format((invoice.get("createdAt") or "")[:10])
    )
    y -= 10
    pdf.line(left, y, right, y)
    y -= 18

    cols = [left, left + 300, left + 360, left + 420]
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(cols[0], y, "Item")
    pdf.drawRightString(cols[1] + 40, y, "Qty")
    pdf.drawRightString(cols[2] + 50, y, "Rate")
    pdf.drawRightString(right, y, "Amount")
    y -= 6
    pdf.line(left, y, right, y)
    y -= 16
    pdf.setFont("Helvetica", 10)
    for line in invoice["lines"]:
        if y < 150:
            pdf.showPage()
            y = height - 50
            pdf.setFont("Helvetica", 10)
        pdf.drawString(cols[0], y, str(line["description"])[:52])
        pdf.drawRightString(cols[1] + 40, y, str(line["qty"]))
        pdf.drawRightString(cols[2] + 50, y, fmt(line["ratePaisa"]))
        pdf.drawRightString(right, y, fmt(line["amountPaisa"]))
        y -= 15
    y -= 4
    pdf.line(left, y, right, y)
    y -= 18

    totals = [
        ("Subtotal", invoice["subtotalPaisa"]),
        ("Discount", invoice["discountPaisa"]),
        ("Total", invoice["totalPaisa"]),
        ("Paid", invoice["paidPaisa"]),
        ("Balance", invoice["balancePaisa"]),
    ]
    for label, value in totals:
        pdf.setFont("Helvetica-Bold" if label in ("Total", "Balance") else "Helvetica", 10)
        pdf.drawRightString(cols[2] + 50, y, label + ":")
        pdf.drawRightString(right, y, fmt(value))
        y -= 15
    y -= 6
    pdf.setFont("Helvetica-Oblique", 9)
    pdf.drawString(left, y, "Amount in words: " + paisa_to_words(invoice["totalPaisa"]))
    y -= 22
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
    y -= 22
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(left, y, "Terms & Conditions")
    y -= 14
    pdf.setFont("Helvetica", 9)
    for term in TERMS:
        pdf.drawString(left, y, term)
        y -= 12
    y -= 10
    pdf.setFont("Helvetica-Oblique", 9)
    pdf.drawString(left, y, "Thank you for your business.")
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
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM invoices WHERE id = ? AND business_id = ?",
            (invoice_id, biz),
        )
        if row is None:
            raise ApiError("Invoice not found.", status=404)
        invoice = _invoice_dict(tx, row)
        business = tx.query_one("SELECT * FROM businesses WHERE id = ?", (biz,))
        party_name = "Cash Sale"
        if row["party_id"] is not None:
            party = tx.query_one(
                "SELECT name FROM parties WHERE id = ?", (row["party_id"],)
            )
            if party:
                party_name = party["name"]
    data = render_invoice_pdf(
        {
            "name": business["name"],
            "address": business.get("address"),
            "phone": business.get("phone"),
            "email": business.get("email"),
        },
        invoice,
        party_name,
    )
    resp = Response(data, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = (
        'attachment; filename="invoice-{}.pdf"'.format(invoice["invoiceNo"])
    )
    return resp
