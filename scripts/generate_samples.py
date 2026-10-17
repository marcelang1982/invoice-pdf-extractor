"""Generate the synthetic sample corpus: 20 invoice PDFs plus ground truth.

All data is fictitious. Four visually distinct layouts, two currencies, and a
spread of line-item counts keep the corpus from rewarding a layout-specific
shortcut. Five invoices carry a deliberate defect, declared in their ground
truth as `expected_issues`, so `scripts/evaluate.py` can measure detection.

Two layouts print dates as `YYYY-MM-DD` and two as `DD Mon YYYY`, while ground
truth is always ISO. That is intentional: the extractor is instructed to
normalize dates, and the corpus should test it.

    python scripts/generate_samples.py [--out-dir samples] [--seed 20260216]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

# Import the package from src/ without requiring an install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from invoice_extractor.models import Invoice, IssueCode, LineItem  # noqa: E402

PAGE_WIDTH, PAGE_HEIGHT = A4

LAYOUTS = ("classic", "modern_boxed", "minimal", "two_column")

#: Layouts that print dates as "04 Feb 2026" instead of "2026-02-04".
LONG_DATE_LAYOUTS = frozenset({"modern_boxed", "two_column"})

SAMPLE_COUNT = 20


class Defect:
    """Deliberate defects injected into specific samples."""

    TOTAL_MISMATCH = "total_mismatch"
    MISSING_TAX_ID = "missing_tax_id"
    DUE_BEFORE_ISSUE = "due_before_issue"
    ABSURD_TAX_RATE = "absurd_tax_rate"
    DUPLICATE = "duplicate"


#: Sample index (1-based) -> defect. Fixed so the corpus is reproducible and
#: reviewable. Index 18 duplicates index 3, so both get flagged.
DEFECT_PLAN: dict[int, str] = {
    5: Defect.TOTAL_MISMATCH,
    9: Defect.MISSING_TAX_ID,
    12: Defect.DUE_BEFORE_ISSUE,
    16: Defect.ABSURD_TAX_RATE,
    18: Defect.DUPLICATE,
}

#: The sample that index 18 duplicates.
DUPLICATE_SOURCE_INDEX = 3


@dataclass(frozen=True)
class Vendor:
    """A fictitious issuing company."""

    name: str
    street: str
    city: str
    tax_id: str
    currency: str
    email: str
    tax_rate: float
    catalog: tuple[tuple[str, float, float], ...]  # (description, min, max) unit price


@dataclass(frozen=True)
class Customer:
    """A fictitious billed party. Noise the extractor must not mistake for the vendor."""

    name: str
    street: str
    city: str


VENDORS: tuple[Vendor, ...] = (
    Vendor(
        name="Northwind Logistics LLC",
        street="1180 Harbor Point Road",
        city="Tacoma, WA 98402, United States",
        tax_id="47-8821093",
        currency="USD",
        email="billing@northwind-logistics.example",
        tax_rate=8.25,
        catalog=(
            ("Freight forwarding, Seattle to Denver", 420.0, 980.0),
            ("Container handling fee", 85.0, 190.0),
            ("Warehouse storage, per pallet per week", 12.0, 28.0),
            ("Customs documentation handling", 60.0, 145.0),
            ("Last-mile delivery surcharge", 35.0, 90.0),
            ("Pallet wrapping and labeling", 18.0, 44.0),
            ("Temperature-controlled transit", 240.0, 610.0),
            ("Proof-of-delivery scanning", 9.0, 22.0),
        ),
    ),
    Vendor(
        name="Helvetia Cloud Systems AG",
        street="Bahnhofstrasse 44",
        city="8001 Zurich, Switzerland",
        tax_id="CHE-412.883.019 MWST",
        currency="EUR",
        email="accounts@helvetia-cloud.example",
        tax_rate=21.0,
        catalog=(
            ("Managed Kubernetes cluster, monthly", 640.0, 1480.0),
            ("Object storage, per TB per month", 18.0, 42.0),
            ("Egress bandwidth, per TB", 22.0, 65.0),
            ("Platform support, business tier", 290.0, 760.0),
            ("Dedicated load balancer", 48.0, 120.0),
        ),
    ),
    Vendor(
        name="Meridian Office Supply Co.",
        street="77 Caldwell Avenue, Suite 210",
        city="Columbus, OH 43215, United States",
        tax_id="83-2290145",
        currency="USD",
        email="ar@meridian-office.example",
        tax_rate=7.0,
        catalog=(
            ("Ergonomic task chair, charcoal", 185.0, 420.0),
            ("Height-adjustable desk, 160x80cm", 390.0, 870.0),
            ("Monitor arm, dual mount", 64.0, 145.0),
            ("Archive boxes, pack of 25", 28.0, 55.0),
            ("Whiteboard, magnetic, 120x90cm", 95.0, 210.0),
            ("Desk lamp, LED, dimmable", 32.0, 78.0),
            ("Acoustic desk divider, felt", 72.0, 165.0),
            ("Mobile pedestal drawer unit", 140.0, 320.0),
            ("Cable management tray", 24.0, 58.0),
        ),
    ),
    Vendor(
        name="Delacroix Design Studio SARL",
        street="18 Rue des Petites Ecuries",
        city="75010 Paris, France",
        tax_id="FR40992441870",
        currency="EUR",
        email="facturation@delacroix-studio.example",
        tax_rate=20.0,
        catalog=(
            ("Brand identity workshop, per day", 1150.0, 1900.0),
            ("Senior designer, hourly", 95.0, 165.0),
            ("Illustration set, licensed", 480.0, 1250.0),
            ("Design system documentation", 720.0, 1600.0),
            ("Print-ready artwork preparation", 240.0, 580.0),
        ),
    ),
    Vendor(
        name="Ravenna Analytics BV",
        street="Keizersgracht 312",
        city="1016 EX Amsterdam, Netherlands",
        tax_id="NL004991237B01",
        currency="EUR",
        email="invoices@ravenna-analytics.example",
        tax_rate=21.0,
        catalog=(
            ("Data pipeline engineering, hourly", 110.0, 180.0),
            ("Dashboard build, fixed scope", 1400.0, 2800.0),
            ("Model monitoring retainer, monthly", 680.0, 1450.0),
            ("Data quality audit", 950.0, 2100.0),
        ),
    ),
    Vendor(
        name="Cascade Freight Partners Inc",
        street="4402 Industrial Way",
        city="Portland, OR 97218, United States",
        tax_id="91-4410228",
        currency="USD",
        email="billing@cascade-freight.example",
        tax_rate=0.0,
        catalog=(
            ("LTL shipment, zone 4", 310.0, 720.0),
            ("Liftgate service", 45.0, 110.0),
            ("Residential delivery fee", 38.0, 95.0),
            ("Fuel surcharge", 52.0, 160.0),
            ("Reconsignment fee", 40.0, 85.0),
        ),
    ),
)

CUSTOMERS: tuple[Customer, ...] = (
    Customer("Blue Harbor Robotics Inc", "920 Mercer Street", "Seattle, WA 98109, United States"),
    Customer("Aurora Medical Devices GmbH", "Gartenstrasse 9", "10115 Berlin, Germany"),
    Customer("Pinecrest Education Trust", "14 Oakfield Lane", "Austin, TX 78704, United States"),
    Customer("Lumiere Retail Group SA", "6 Avenue Marceau", "75008 Paris, France"),
    Customer("Stonebridge Capital Partners", "33 Threadneedle Street", "London EC2R 8AY, United Kingdom"),
    Customer("Westfall Agricultural Co-op", "2201 County Road 18", "Fargo, ND 58102, United States"),
)


@dataclass
class Sample:
    """One generated invoice: the rendered document plus its ground truth."""

    index: int
    file: str
    layout: str
    defect: str | None
    vendor: Vendor
    customer: Customer
    invoice: Invoice
    expected_issues: list[dict[str, str]]
    #: True when the tax ID is withheld from both the PDF and the ground truth.
    omit_tax_id: bool = False


# --------------------------------------------------------------------------- #
# Invoice construction
# --------------------------------------------------------------------------- #


def build_samples(seed: int) -> list[Sample]:
    """Build all 20 sample specs, defects included, deterministically."""
    rng = random.Random(seed)
    samples: list[Sample] = []

    for index in range(1, SAMPLE_COUNT + 1):
        defect = DEFECT_PLAN.get(index)
        layout = LAYOUTS[(index - 1) % len(LAYOUTS)]

        if defect == Defect.DUPLICATE:
            samples.append(_build_duplicate(index, layout, samples))
            continue

        samples.append(_build_sample(index, layout, defect, rng))

    _mark_duplicate_source(samples)
    return samples


def _build_sample(
    index: int, layout: str, defect: str | None, rng: random.Random
) -> Sample:
    """Build one non-duplicate sample, applying its defect if any."""
    vendor = VENDORS[(index - 1) % len(VENDORS)]
    customer = CUSTOMERS[(index * 3) % len(CUSTOMERS)]

    issue_date = date(2026, 1, 6) + timedelta(days=rng.randrange(0, 75))
    due_date = issue_date + timedelta(days=rng.choice((14, 21, 30, 45)))
    line_items = _build_line_items(vendor, rng)

    subtotal = _round(sum(item.amount or 0.0 for item in line_items))
    tax_rate = vendor.tax_rate
    tax_amount = _round(subtotal * tax_rate / 100.0)
    total = _round(subtotal + tax_amount)

    invoice = Invoice(
        vendor_name=vendor.name,
        vendor_tax_id=vendor.tax_id,
        invoice_number=_invoice_number(vendor, index, rng),
        issue_date=issue_date.isoformat(),
        due_date=due_date.isoformat(),
        currency=vendor.currency,
        line_items=line_items,
        subtotal=subtotal,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        total=total,
    )

    expected: list[dict[str, str]] = []
    omit_tax_id = False

    if defect == Defect.TOTAL_MISMATCH:
        # The printed total is simply wrong: subtotal + tax no longer reaches it.
        invoice = invoice.model_copy(update={"total": _round(total + 40.0)})
        expected.append(_issue(IssueCode.TOTAL_MISMATCH, "total"))

    elif defect == Defect.MISSING_TAX_ID:
        invoice = invoice.model_copy(update={"vendor_tax_id": None})
        omit_tax_id = True
        expected.append(_issue(IssueCode.MISSING_REQUIRED_FIELD, "vendor_tax_id"))

    elif defect == Defect.DUE_BEFORE_ISSUE:
        invoice = invoice.model_copy(
            update={"due_date": (issue_date - timedelta(days=10)).isoformat()}
        )
        expected.append(_issue(IssueCode.DUE_DATE_BEFORE_ISSUE_DATE, "due_date"))

    elif defect == Defect.ABSURD_TAX_RATE:
        # Keep the arithmetic self-consistent so only the rate itself is at fault.
        absurd_rate = 87.5
        absurd_tax = _round(subtotal * absurd_rate / 100.0)
        invoice = invoice.model_copy(
            update={
                "tax_rate": absurd_rate,
                "tax_amount": absurd_tax,
                "total": _round(subtotal + absurd_tax),
            }
        )
        expected.append(_issue(IssueCode.TAX_RATE_OUT_OF_RANGE, "tax_rate"))

    return Sample(
        index=index,
        file=_file_name(index),
        layout=layout,
        defect=defect,
        vendor=vendor,
        customer=customer,
        invoice=invoice,
        expected_issues=expected,
        omit_tax_id=omit_tax_id,
    )


def _build_duplicate(index: int, layout: str, built: list[Sample]) -> Sample:
    """Re-issue an earlier invoice verbatim under a different layout."""
    source = next(s for s in built if s.index == DUPLICATE_SOURCE_INDEX)
    return Sample(
        index=index,
        file=_file_name(index),
        layout=layout,
        defect=Defect.DUPLICATE,
        vendor=source.vendor,
        customer=source.customer,
        invoice=source.invoice.model_copy(deep=True),
        expected_issues=[_issue(IssueCode.DUPLICATE_INVOICE, "invoice_number")],
    )


def _mark_duplicate_source(samples: list[Sample]) -> None:
    """Add the duplicate finding to the original too: both need review."""
    source = next(s for s in samples if s.index == DUPLICATE_SOURCE_INDEX)
    issue = _issue(IssueCode.DUPLICATE_INVOICE, "invoice_number")
    if issue not in source.expected_issues:
        source.expected_issues.append(issue)


def _build_line_items(vendor: Vendor, rng: random.Random) -> list[LineItem]:
    """Pick 1-8 catalog lines with clean two-decimal arithmetic.

    Quantities stay on half-unit steps and prices on cents so that
    `round(quantity * unit_price, 2)` is exact. Otherwise the generator would
    manufacture rounding findings the corpus never intended.
    """
    # Weighted so the corpus is not dominated by one- and two-line invoices.
    count = rng.choice((1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 7, 8))
    chosen = rng.sample(vendor.catalog, min(count, len(vendor.catalog)))

    items: list[LineItem] = []
    for description, low, high in chosen:
        quantity = rng.choice((1.0, 1.0, 2.0, 2.5, 3.0, 4.0, 6.0, 8.0, 10.0, 12.0))
        unit_price = _round(rng.uniform(low, high))
        items.append(
            LineItem(
                description=description,
                quantity=quantity,
                unit_price=unit_price,
                amount=_round(quantity * unit_price),
            )
        )
    return items


def _invoice_number(vendor: Vendor, index: int, rng: random.Random) -> str:
    """Build a vendor-styled invoice number."""
    prefix = "".join(word[0] for word in vendor.name.split()[:2]).upper()
    return f"{prefix}-2026-{1000 + index * 7 + rng.randrange(0, 6):04d}"


def _issue(code: IssueCode, field: str) -> dict[str, str]:
    return {"code": code.value, "field": field}


def _file_name(index: int) -> str:
    return f"invoice_{index:02d}.pdf"


def _round(value: float) -> float:
    return round(value + 0.0, 2)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render_sample(sample: Sample, pdf_dir: Path) -> Path:
    """Render one sample to a PDF using its assigned layout."""
    path = pdf_dir / sample.file
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"Invoice {sample.invoice.invoice_number}")
    c.setAuthor(sample.vendor.name)

    renderer = {
        "classic": _render_classic,
        "modern_boxed": _render_modern_boxed,
        "minimal": _render_minimal,
        "two_column": _render_two_column,
    }[sample.layout]
    renderer(c, sample)

    c.showPage()
    c.save()
    return path


def _render_classic(c: canvas.Canvas, sample: Sample) -> None:
    """Serif, centered masthead, fully ruled table, ISO dates."""
    inv, vendor = sample.invoice, sample.vendor
    left, right = 20 * mm, PAGE_WIDTH - 20 * mm
    y = PAGE_HEIGHT - 25 * mm

    c.setFont("Times-Bold", 18)
    c.drawCentredString(PAGE_WIDTH / 2, y, vendor.name)
    y -= 6 * mm
    c.setFont("Times-Roman", 9)
    c.drawCentredString(PAGE_WIDTH / 2, y, f"{vendor.street} | {vendor.city}")
    y -= 4.5 * mm
    c.drawCentredString(
        PAGE_WIDTH / 2, y, _join((_tax_id_label(sample), vendor.email), "  |  ")
    )

    y -= 10 * mm
    c.setLineWidth(1.2)
    c.line(left, y, right, y)
    y -= 10 * mm

    c.setFont("Times-Bold", 14)
    c.drawString(left, y, "INVOICE")

    c.setFont("Times-Roman", 10)
    meta = [
        ("Invoice No.", inv.invoice_number or ""),
        ("Issue Date", _date_text(sample, inv.issue_date)),
        ("Due Date", _date_text(sample, inv.due_date)),
        ("Currency", inv.currency or ""),
    ]
    meta_y = y
    for label, value in meta:
        c.drawRightString(right - 42 * mm, meta_y, f"{label}:")
        c.drawRightString(right, meta_y, value)
        meta_y -= 5 * mm

    y -= 8 * mm
    c.setFont("Times-Bold", 10)
    c.drawString(left, y, "Bill To")
    y -= 5 * mm
    c.setFont("Times-Roman", 10)
    c.drawString(left, y, sample.customer.name)
    y -= 4.5 * mm
    c.drawString(left, y, f"{sample.customer.street}, {sample.customer.city}")

    y = min(y, meta_y) - 12 * mm
    y = _draw_ruled_table(c, sample, left, right, y)
    _draw_totals_block(c, sample, right - 70 * mm, right, y - 8 * mm, font="Times")
    _draw_footer(c, sample, left, font="Times-Italic")


def _render_modern_boxed(c: canvas.Canvas, sample: Sample) -> None:
    """Colored header band, boxed meta panel, zebra-striped table, long dates."""
    inv, vendor = sample.invoice, sample.vendor
    left, right = 18 * mm, PAGE_WIDTH - 18 * mm
    band = colors.HexColor("#1F3A5F")
    accent = colors.HexColor("#EDF1F7")

    c.setFillColor(band)
    c.rect(0, PAGE_HEIGHT - 40 * mm, PAGE_WIDTH, 40 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 19)
    c.drawString(left, PAGE_HEIGHT - 22 * mm, vendor.name)
    c.setFont("Helvetica", 8.5)
    c.drawString(left, PAGE_HEIGHT - 28 * mm, f"{vendor.street}, {vendor.city}")
    c.drawString(
        left,
        PAGE_HEIGHT - 33 * mm,
        _join((_tax_id_label(sample), vendor.email), "   "),
    )
    c.setFont("Helvetica-Bold", 26)
    c.drawRightString(right, PAGE_HEIGHT - 24 * mm, "INVOICE")
    c.setFillColor(colors.black)

    y = PAGE_HEIGHT - 52 * mm
    panel_height = 26 * mm
    c.setFillColor(accent)
    c.rect(left, y - panel_height, right - left, panel_height, stroke=0, fill=1)
    c.setFillColor(colors.black)

    c.setFont("Helvetica-Bold", 8)
    c.drawString(left + 5 * mm, y - 7 * mm, "INVOICE NUMBER")
    c.drawString(left + 55 * mm, y - 7 * mm, "ISSUE DATE")
    c.drawString(left + 95 * mm, y - 7 * mm, "DUE DATE")
    c.drawString(left + 140 * mm, y - 7 * mm, "CURRENCY")
    c.setFont("Helvetica", 10)
    c.drawString(left + 5 * mm, y - 13 * mm, inv.invoice_number or "")
    c.drawString(left + 55 * mm, y - 13 * mm, _date_text(sample, inv.issue_date))
    c.drawString(left + 95 * mm, y - 13 * mm, _date_text(sample, inv.due_date))
    c.drawString(left + 140 * mm, y - 13 * mm, inv.currency or "")

    c.setFont("Helvetica-Bold", 8)
    c.drawString(left + 5 * mm, y - 20 * mm, "BILL TO")
    c.setFont("Helvetica", 9)
    c.drawString(left + 25 * mm, y - 20 * mm, f"{sample.customer.name}, {sample.customer.city}")

    y -= panel_height + 12 * mm
    y = _draw_zebra_table(c, sample, left, right, y)
    _draw_totals_block(c, sample, right - 72 * mm, right, y - 8 * mm, boxed=True)
    _draw_footer(c, sample, left)


def _render_minimal(c: canvas.Canvas, sample: Sample) -> None:
    """Flush-left sans-serif, one hairline rule, no table borders, ISO dates."""
    inv, vendor = sample.invoice, sample.vendor
    left, right = 22 * mm, PAGE_WIDTH - 22 * mm
    y = PAGE_HEIGHT - 30 * mm

    c.setFont("Helvetica-Bold", 15)
    c.drawString(left, y, vendor.name)
    y -= 5.5 * mm
    c.setFont("Helvetica", 8.5)
    c.setFillColor(colors.HexColor("#555555"))
    c.drawString(
        left, y, _join((vendor.street, vendor.city, _tax_id_label(sample)), " - ")
    )
    c.setFillColor(colors.black)

    y -= 14 * mm
    c.setFont("Helvetica", 10)
    c.drawString(
        left,
        y,
        f"Invoice {inv.invoice_number}  -  issued {_date_text(sample, inv.issue_date)}"
        f"  -  due {_date_text(sample, inv.due_date)}  -  {inv.currency}",
    )
    y -= 6 * mm
    c.drawString(left, y, f"For {sample.customer.name} ({sample.customer.city})")

    y -= 6 * mm
    c.setStrokeColor(colors.HexColor("#CCCCCC"))
    c.setLineWidth(0.4)
    c.line(left, y, right, y)
    c.setStrokeColor(colors.black)

    y -= 12 * mm
    y = _draw_borderless_table(c, sample, left, right, y)
    _draw_totals_block(c, sample, right - 60 * mm, right, y - 10 * mm, rules=False)
    _draw_footer(c, sample, left)


def _render_two_column(c: canvas.Canvas, sample: Sample) -> None:
    """Vendor left, customer right, narrow table, totals under a divider."""
    inv, vendor = sample.invoice, sample.vendor
    left, right = 20 * mm, PAGE_WIDTH - 20 * mm
    mid = PAGE_WIDTH / 2 + 5 * mm
    y = PAGE_HEIGHT - 26 * mm

    c.setFont("Helvetica-Bold", 20)
    c.drawString(left, y, "INVOICE")
    c.setFont("Helvetica", 9)
    c.drawRightString(right, y, f"No. {inv.invoice_number}")
    c.drawRightString(right, y - 5 * mm, f"Issued: {_date_text(sample, inv.issue_date)}")
    c.drawRightString(right, y - 10 * mm, f"Due: {_date_text(sample, inv.due_date)}")
    c.drawRightString(right, y - 15 * mm, f"Amounts in {inv.currency}")

    y -= 26 * mm
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(left, y, "FROM")
    c.drawString(mid, y, "TO")
    y -= 5.5 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(left, y, vendor.name)
    c.drawString(mid, y, sample.customer.name)
    y -= 5 * mm
    c.setFont("Helvetica", 8.5)
    c.drawString(left, y, vendor.street)
    c.drawString(mid, y, sample.customer.street)
    y -= 4.5 * mm
    c.drawString(left, y, vendor.city)
    c.drawString(mid, y, sample.customer.city)
    y -= 4.5 * mm
    c.drawString(left, y, _tax_id_label(sample))

    y -= 14 * mm
    y = _draw_ruled_table(c, sample, left, right - 8 * mm, y, header_fill=True)

    divider_y = y - 6 * mm
    c.setLineWidth(0.8)
    c.line(left, divider_y, right, divider_y)
    _draw_totals_block(c, sample, left, left + 70 * mm, divider_y - 8 * mm, rules=False)
    _draw_footer(c, sample, left)


# --------------------------------------------------------------------------- #
# Shared drawing helpers
# --------------------------------------------------------------------------- #

_COLUMN_LABELS = ("Description", "Qty", "Unit Price", "Amount")


def _draw_ruled_table(
    c: canvas.Canvas,
    sample: Sample,
    left: float,
    right: float,
    y: float,
    *,
    header_fill: bool = False,
) -> float:
    """Draw a fully ruled items table. Returns the y below the table."""
    inv = sample.invoice
    serif = sample.layout == "classic"
    bold = "Times-Bold" if serif else "Helvetica-Bold"
    plain = "Times-Roman" if serif else "Helvetica"
    row_height = 7 * mm
    rows = len(inv.line_items)

    qty_x, price_x, amount_x = right - 62 * mm, right - 34 * mm, right

    if header_fill:
        c.setFillColor(colors.HexColor("#EEEEEE"))
        c.rect(left, y - row_height, right - left, row_height, stroke=0, fill=1)
        c.setFillColor(colors.black)

    c.setLineWidth(0.6)
    c.setFont(bold, 9)
    text_y = y - 4.8 * mm
    c.drawString(left + 2 * mm, text_y, _COLUMN_LABELS[0])
    c.drawRightString(qty_x, text_y, _COLUMN_LABELS[1])
    c.drawRightString(price_x, text_y, _COLUMN_LABELS[2])
    c.drawRightString(amount_x - 2 * mm, text_y, _COLUMN_LABELS[3])

    c.setFont(plain, 9)
    for row, item in enumerate(inv.line_items, start=1):
        text_y = y - row_height * row - 4.8 * mm
        c.drawString(left + 2 * mm, text_y, item.description or "")
        c.drawRightString(qty_x, text_y, _qty_text(item.quantity))
        c.drawRightString(price_x, text_y, _money(item.unit_price, inv.currency))
        c.drawRightString(amount_x - 2 * mm, text_y, _money(item.amount, inv.currency))

    bottom = y - row_height * (rows + 1)
    for index in range(rows + 2):
        line_y = y - row_height * index
        c.line(left, line_y, right, line_y)
    for column_x in (left, qty_x + 2 * mm, price_x + 2 * mm, right):
        c.line(column_x, y, column_x, bottom)

    return bottom


def _draw_zebra_table(
    c: canvas.Canvas, sample: Sample, left: float, right: float, y: float
) -> float:
    """Draw an items table with alternating row fills and no vertical rules."""
    inv = sample.invoice
    row_height = 7.5 * mm
    qty_x, price_x, amount_x = right - 62 * mm, right - 32 * mm, right

    c.setFillColor(colors.HexColor("#1F3A5F"))
    c.rect(left, y - row_height, right - left, row_height, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 9)
    text_y = y - 5.2 * mm
    c.drawString(left + 3 * mm, text_y, _COLUMN_LABELS[0].upper())
    c.drawRightString(qty_x, text_y, _COLUMN_LABELS[1].upper())
    c.drawRightString(price_x, text_y, _COLUMN_LABELS[2].upper())
    c.drawRightString(amount_x - 3 * mm, text_y, _COLUMN_LABELS[3].upper())
    c.setFillColor(colors.black)

    for row, item in enumerate(inv.line_items, start=1):
        row_top = y - row_height * row
        if row % 2 == 1:
            c.setFillColor(colors.HexColor("#F6F8FB"))
            c.rect(left, row_top - row_height, right - left, row_height, stroke=0, fill=1)
            c.setFillColor(colors.black)
        text_y = row_top - 5.2 * mm
        c.setFont("Helvetica", 9)
        c.drawString(left + 3 * mm, text_y, item.description or "")
        c.drawRightString(qty_x, text_y, _qty_text(item.quantity))
        c.drawRightString(price_x, text_y, _money(item.unit_price, inv.currency))
        c.drawRightString(amount_x - 3 * mm, text_y, _money(item.amount, inv.currency))

    return y - row_height * (len(inv.line_items) + 1)


def _draw_borderless_table(
    c: canvas.Canvas, sample: Sample, left: float, right: float, y: float
) -> float:
    """Draw an items table with no fills and no rules at all."""
    inv = sample.invoice
    row_height = 7 * mm
    qty_x, price_x, amount_x = right - 58 * mm, right - 30 * mm, right

    c.setFont("Helvetica", 7.5)
    c.setFillColor(colors.HexColor("#777777"))
    c.drawString(left, y, _COLUMN_LABELS[0].upper())
    c.drawRightString(qty_x, y, _COLUMN_LABELS[1].upper())
    c.drawRightString(price_x, y, _COLUMN_LABELS[2].upper())
    c.drawRightString(amount_x, y, _COLUMN_LABELS[3].upper())
    c.setFillColor(colors.black)

    for row, item in enumerate(inv.line_items, start=1):
        text_y = y - row_height * row
        c.setFont("Helvetica", 9.5)
        c.drawString(left, text_y, item.description or "")
        c.drawRightString(qty_x, text_y, _qty_text(item.quantity))
        c.drawRightString(price_x, text_y, _money(item.unit_price, inv.currency))
        c.drawRightString(amount_x, text_y, _money(item.amount, inv.currency))

    return y - row_height * (len(inv.line_items) + 1)


def _draw_totals_block(
    c: canvas.Canvas,
    sample: Sample,
    left: float,
    right: float,
    y: float,
    *,
    font: str = "Helvetica",
    boxed: bool = False,
    rules: bool = True,
) -> None:
    """Draw subtotal / tax / total. Grand total is always visually emphasized."""
    inv = sample.invoice
    bold = f"{font}-Bold"
    plain = "Times-Roman" if font == "Times" else font
    row_height = 6.5 * mm

    rows = [
        ("Subtotal", _money(inv.subtotal, inv.currency)),
        (f"Tax ({_qty_text(inv.tax_rate)}%)", _money(inv.tax_amount, inv.currency)),
    ]

    if boxed:
        height = row_height * (len(rows) + 1) + 4 * mm
        c.setFillColor(colors.HexColor("#EDF1F7"))
        c.rect(left, y - height, right - left, height, stroke=0, fill=1)
        c.setFillColor(colors.black)
        left += 4 * mm
        right -= 4 * mm
        y -= 3 * mm

    c.setFont(plain, 10)
    for label, value in rows:
        c.drawString(left, y, label)
        c.drawRightString(right, y, value)
        y -= row_height

    if rules:
        c.setLineWidth(0.6)
        c.line(left, y + 2.5 * mm, right, y + 2.5 * mm)

    c.setFont(bold, 12)
    c.drawString(left, y - 1.5 * mm, f"Total Due ({inv.currency})")
    c.drawRightString(right, y - 1.5 * mm, _money(inv.total, inv.currency))


def _draw_footer(c: canvas.Canvas, sample: Sample, left: float, *, font: str = "Helvetica") -> None:
    """Draw payment terms. Fictitious bank details, no extractable fields."""
    c.setFont(font, 8)
    c.setFillColor(colors.HexColor("#666666"))
    c.drawString(
        left,
        18 * mm,
        f"Payment by bank transfer to {sample.vendor.name} - "
        f"IBAN XX00 0000 0000 0000 0000 - Ref {sample.invoice.invoice_number}",
    )
    c.drawString(
        left, 14 * mm, "This document is fictitious sample data generated for testing."
    )
    c.setFillColor(colors.black)


def _tax_id_label(sample: Sample) -> str:
    """`Tax ID: <value>`, or empty when the sample deliberately omits it.

    The whole label is dropped rather than left dangling with a placeholder, so
    the missing-field sample genuinely has nothing to extract.
    """
    tax_id = sample.invoice.vendor_tax_id
    return f"Tax ID: {tax_id}" if tax_id else ""


def _join(parts: tuple[str, ...], separator: str) -> str:
    """Join the non-empty parts, so a dropped field leaves no stray separator."""
    return separator.join(part for part in parts if part)


def _date_text(sample: Sample, iso: str | None) -> str:
    """Format a date for this layout: ISO, or `DD Mon YYYY` on some layouts."""
    if not iso:
        return ""
    if sample.layout not in LONG_DATE_LAYOUTS:
        return iso
    return date.fromisoformat(iso).strftime("%d %b %Y")


def _money(value: float | None, currency: str | None) -> str:
    """Format an amount with its currency marker."""
    if value is None:
        return ""
    body = f"{value:,.2f}"
    return f"${body}" if currency == "USD" else f"{body} EUR"


def _qty_text(value: float | None) -> str:
    """Render a quantity or rate without a trailing `.0`."""
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


# --------------------------------------------------------------------------- #
# Ground truth
# --------------------------------------------------------------------------- #


def write_ground_truth(sample: Sample, truth_dir: Path) -> Path:
    """Write the ground truth JSON next to the generated PDF."""
    path = truth_dir / f"{Path(sample.file).stem}.json"
    payload = {
        "file": sample.file,
        "layout": sample.layout,
        "defect": sample.defect,
        "expected_status": "needs_review" if sample.expected_issues else "valid",
        "expected_issues": sample.expected_issues,
        "invoice": sample.invoice.model_dump(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "samples",
        help="Directory holding pdfs/ and ground_truth/ (default: ./samples).",
    )
    parser.add_argument(
        "--seed", type=int, default=20260216, help="Seed for reproducible amounts."
    )
    parser.add_argument(
        "--skip-pdfs",
        action="store_true",
        help=(
            "Rewrite ground truth without re-rendering the PDFs. Renders embed "
            "a creation timestamp, so re-rendering churns every file even when "
            "nothing about the invoice changed."
        ),
    )
    args = parser.parse_args(argv)

    pdf_dir = args.out_dir / "pdfs"
    truth_dir = args.out_dir / "ground_truth"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)

    samples = build_samples(args.seed)
    for sample in samples:
        if not args.skip_pdfs:
            render_sample(sample, pdf_dir)
        write_ground_truth(sample, truth_dir)
        flag = f"  <- {sample.defect}" if sample.defect else ""
        print(
            f"{sample.file}  {sample.layout:<13} {sample.invoice.currency}  "
            f"{len(sample.invoice.line_items)} items{flag}"
        )

    defective = sum(1 for s in samples if s.defect)
    print(
        f"\n{len(samples)} PDFs in {pdf_dir}\n"
        f"{len(samples)} ground truth files in {truth_dir}\n"
        f"{defective} invoices carry a deliberate defect "
        f"({sum(1 for s in samples if s.expected_issues)} files flagged in total)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
