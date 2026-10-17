"""Generate the `hard` sample set: 11 invoices built to break the extractor.

The `clean` corpus (scripts/generate_samples.py) is born-digital, perfectly
typeset, and single-page, so a 100% score there says little about real invoices.
This set deliberately introduces the conditions that actually appear in the
wild:

  * 3 scanned   - rasterized to 150dpi, blurred, rotated 1-2 degrees, speckled
                  and JPEG-compressed, then repackaged as an image-only PDF.
                  There is no text layer, so extraction must go through vision.
  * 2 multipage - the items table continues onto page 2, with totals only at
                  the end.
  * 2 wrapped   - descriptions long enough to wrap onto a second line inside
                  the table cell.
  * 2 messy     - totals printed at the top, a borderless detail table, plus
                  discount and shipping lines.
  * 2 Argentine - Factura A in Spanish: CUIT, single 21% IVA, dd/mm/yyyy dates
                  and comma decimal separators.

Three carry a deliberate defect, declared in ground truth exactly as in the
clean set. hard_09 carries a document-level discount and shipping charge in its
totals block: the invoice is arithmetically correct but only reconciles once
those adjustments are applied, so it exercises the adjusted totals path rather
than the direct one.

    python scripts/generate_hard_samples.py [--out-dir samples/hard] [--seed N]
    python scripts/generate_hard_samples.py --skip-pdfs   # ground truth only
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_samples import Customer, Vendor  # noqa: E402
from invoice_extractor.models import (  # noqa: E402
    Adjustment,
    AdjustmentKind,
    Invoice,
    IssueCode,
    LineItem,
)

PAGE_WIDTH, PAGE_HEIGHT = A4

#: Rasterization settings for the "scanned" invoices.
SCAN_DPI = 150


# --------------------------------------------------------------------------- #
# Parties
# --------------------------------------------------------------------------- #

NORTHWIND = Vendor(
    name="Northwind Logistics LLC",
    street="1180 Harbor Point Road",
    city="Tacoma, WA 98402, United States",
    tax_id="47-8821093",
    currency="USD",
    email="billing@northwind-logistics.example",
    tax_rate=8.25,
    catalog=(),
)

HELVETIA = Vendor(
    name="Helvetia Cloud Systems AG",
    street="Bahnhofstrasse 44",
    city="8001 Zurich, Switzerland",
    tax_id="CHE-412.883.019 MWST",
    currency="EUR",
    email="accounts@helvetia-cloud.example",
    tax_rate=21.0,
    catalog=(),
)

RAVENNA = Vendor(
    name="Ravenna Analytics BV",
    street="Keizersgracht 312",
    city="1016 EX Amsterdam, Netherlands",
    tax_id="NL004991237B01",
    currency="EUR",
    email="invoices@ravenna-analytics.example",
    tax_rate=21.0,
    catalog=(),
)

MERIDIAN = Vendor(
    name="Meridian Office Supply Co.",
    street="77 Caldwell Avenue, Suite 210",
    city="Columbus, OH 43215, United States",
    tax_id="83-2290145",
    currency="USD",
    email="ar@meridian-office.example",
    tax_rate=7.0,
    catalog=(),
)

CASCADE = Vendor(
    name="Cascade Freight Partners Inc",
    street="4402 Industrial Way",
    city="Portland, OR 97218, United States",
    tax_id="91-4410228",
    currency="USD",
    email="billing@cascade-freight.example",
    tax_rate=0.0,
    catalog=(),
)

PAMPA = Vendor(
    name="Talleres Graficos Pampa S.R.L.",
    street="Av. Rivadavia 4582, Piso 3",
    city="C1205AAB - Ciudad Autonoma de Buenos Aires",
    tax_id="30-71234567-8",
    currency="ARS",
    email="facturacion@graficospampa.example",
    tax_rate=21.0,
    catalog=(),
)

ANDES = Vendor(
    name="Distribuidora Andes S.A.",
    street="Ruta Provincial 82 Km 4,5",
    city="M5509 - Lujan de Cuyo, Mendoza",
    tax_id="30-69874521-3",
    currency="ARS",
    email="cobranzas@distandes.example",
    tax_rate=21.0,
    catalog=(),
)

BLUE_HARBOR = Customer(
    "Blue Harbor Robotics Inc", "920 Mercer Street", "Seattle, WA 98109, United States"
)
AURORA = Customer(
    "Aurora Medical Devices GmbH", "Gartenstrasse 9", "10115 Berlin, Germany"
)
STONEBRIDGE = Customer(
    "Stonebridge Capital Partners",
    "33 Threadneedle Street",
    "London EC2R 8AY, United Kingdom",
)
PINECREST = Customer(
    "Pinecrest Education Trust", "14 Oakfield Lane", "Austin, TX 78704, United States"
)
PLATA = Customer(
    "Consultora del Plata S.A.", "Reconquista 657, Piso 8", "C1003ABM - CABA"
)
SUR = Customer(
    "Cooperativa Agricola del Sur Ltda.", "Belgrano 1240", "B6000 - Junin, Bs. As."
)


@dataclass
class HardSample:
    """One hard-set invoice: the rendered document plus its ground truth."""

    index: int
    kind: str
    vendor: Vendor
    customer: Customer
    invoice: Invoice
    defect: str | None = None
    expected_issues: list[dict[str, str]] = field(default_factory=list)
    notes: str = ""
    #: Layout to rasterize through for `scanned` samples.
    scan_base: str = "classic"
    #: Degradation parameters for `scanned` samples.
    scan_angle: float = 0.0
    scan_quality: int = 60
    scan_noise: float = 0.06

    @property
    def file(self) -> str:
        return f"hard_{self.index:02d}.pdf"


# --------------------------------------------------------------------------- #
# Invoice assembly helpers
# --------------------------------------------------------------------------- #


def items(rows: list[tuple[str, float, float]]) -> list[LineItem]:
    """Build line items, computing each amount as quantity x unit price."""
    return [
        LineItem(
            description=description,
            quantity=quantity,
            unit_price=unit_price,
            amount=_round(quantity * unit_price),
        )
        for description, quantity, unit_price in rows
    ]


def assemble(
    vendor: Vendor,
    number: str,
    issue: str,
    due: str,
    line_items: list[LineItem],
    *,
    tax_rate: float | None = None,
    subtotal: float | None = None,
    tax_amount: float | None = None,
    total: float | None = None,
    tax_id: str | None = ...,  # type: ignore[assignment]
) -> Invoice:
    """Build an invoice, defaulting the arithmetic to a self-consistent one.

    Any of `subtotal` / `tax_amount` / `total` can be overridden to inject a
    defect or to model a document whose totals block our schema cannot express.
    """
    rate = vendor.tax_rate if tax_rate is None else tax_rate
    computed_subtotal = _round(sum(item.amount or 0.0 for item in line_items))
    computed_tax = _round(computed_subtotal * rate / 100.0)

    final_subtotal = computed_subtotal if subtotal is None else subtotal
    final_tax = computed_tax if tax_amount is None else tax_amount
    final_total = (
        _round(final_subtotal + final_tax) if total is None else total
    )

    return Invoice(
        vendor_name=vendor.name,
        vendor_tax_id=vendor.tax_id if tax_id is ... else tax_id,
        invoice_number=number,
        issue_date=issue,
        due_date=due,
        currency=vendor.currency,
        line_items=line_items,
        subtotal=final_subtotal,
        tax_rate=rate,
        tax_amount=final_tax,
        total=final_total,
    )


def issue(code: IssueCode, field_name: str) -> dict[str, str]:
    return {"code": code.value, "field": field_name}


def _round(value: float) -> float:
    return round(value + 0.0, 2)


# --------------------------------------------------------------------------- #
# The corpus
# --------------------------------------------------------------------------- #


def build_samples() -> list[HardSample]:
    """Define all 11 hard samples and their ground truth, explicitly.

    Nothing here is random: every amount, date and defect is written out so the
    ground truth can be reviewed by eye before a single API call is made.
    """
    samples: list[HardSample] = []

    # --- 1-3: scanned ----------------------------------------------------- #

    samples.append(
        HardSample(
            index=1,
            kind="scanned",
            vendor=NORTHWIND,
            customer=BLUE_HARBOR,
            scan_base="classic",
            scan_angle=1.4,
            scan_quality=58,
            scan_noise=0.06,
            invoice=assemble(
                NORTHWIND,
                "NL-2026-2041",
                "2026-04-07",
                "2026-05-07",
                items(
                    [
                        ("Freight forwarding, Seattle to Denver", 4.0, 612.50),
                        ("Container handling fee", 2.0, 148.00),
                        ("Customs documentation handling", 3.0, 92.40),
                        ("Last-mile delivery surcharge", 6.0, 54.80),
                    ]
                ),
            ),
        )
    )

    # Scanned *and* arithmetically broken: degraded input must not stop the
    # validator from catching a total that does not add up.
    broken = assemble(
        HELVETIA,
        "HC-2026-2088",
        "2026-04-12",
        "2026-05-12",
        items(
            [
                ("Managed Kubernetes cluster, monthly", 3.0, 1180.00),
                ("Object storage, per TB per month", 12.0, 31.50),
                ("Platform support, business tier", 2.0, 540.00),
            ]
        ),
    )
    samples.append(
        HardSample(
            index=2,
            kind="scanned",
            vendor=HELVETIA,
            customer=AURORA,
            scan_base="modern_boxed",
            scan_angle=-1.8,
            scan_quality=55,
            scan_noise=0.07,
            defect="total_mismatch",
            invoice=broken.model_copy(
                update={"total": _round((broken.total or 0.0) + 75.0)}
            ),
            expected_issues=[issue(IssueCode.TOTAL_MISMATCH, "total")],
        )
    )

    samples.append(
        HardSample(
            index=3,
            kind="scanned",
            vendor=RAVENNA,
            customer=STONEBRIDGE,
            scan_base="two_column",
            scan_angle=1.1,
            scan_quality=62,
            scan_noise=0.05,
            invoice=assemble(
                RAVENNA,
                "RA-2026-2103",
                "2026-03-30",
                "2026-04-29",
                items(
                    [
                        ("Data pipeline engineering, hourly", 38.0, 142.00),
                        ("Dashboard build, fixed scope", 1.0, 2250.00),
                        ("Data quality audit", 1.0, 1480.00),
                    ]
                ),
            ),
        )
    )

    # --- 4-5: multipage --------------------------------------------------- #

    samples.append(
        HardSample(
            index=4,
            kind="multipage",
            vendor=MERIDIAN,
            customer=PINECREST,
            invoice=assemble(
                MERIDIAN,
                "MO-2026-2155",
                "2026-02-18",
                "2026-03-20",
                items(
                    [
                        ("Ergonomic task chair, charcoal", 12.0, 289.00),
                        ("Height-adjustable desk, 160x80cm", 12.0, 612.00),
                        ("Monitor arm, dual mount", 24.0, 98.50),
                        ("Acoustic desk divider, felt", 18.0, 124.00),
                        ("Mobile pedestal drawer unit", 12.0, 215.00),
                        ("Cable management tray", 24.0, 38.00),
                        ("Desk lamp, LED, dimmable", 24.0, 54.00),
                        ("Whiteboard, magnetic, 120x90cm", 4.0, 168.00),
                        ("Archive boxes, pack of 25", 10.0, 41.00),
                        ("Floor protection mat", 12.0, 72.50),
                        ("Footrest, adjustable", 12.0, 46.00),
                        ("Power strip, 6-outlet, surge", 24.0, 29.50),
                        ("Delivery and installation", 1.0, 1450.00),
                        ("Old furniture removal", 1.0, 380.00),
                    ]
                ),
            ),
        )
    )

    # Multipage *and* missing a required field: the tax ID is absent from the
    # document entirely, on both pages.
    samples.append(
        HardSample(
            index=5,
            kind="multipage",
            vendor=CASCADE,
            customer=BLUE_HARBOR,
            defect="missing_tax_id",
            invoice=assemble(
                CASCADE,
                "CF-2026-2177",
                "2026-03-05",
                "2026-04-04",
                items(
                    [
                        ("LTL shipment, zone 4", 6.0, 418.00),
                        ("LTL shipment, zone 6", 4.0, 562.00),
                        ("Liftgate service", 10.0, 68.00),
                        ("Residential delivery fee", 8.0, 47.50),
                        ("Fuel surcharge", 10.0, 86.00),
                        ("Reconsignment fee", 2.0, 55.00),
                        ("Pallet wrapping and labeling", 20.0, 24.00),
                        ("Proof-of-delivery scanning", 20.0, 11.50),
                        ("Detention time, per hour", 6.0, 95.00),
                        ("Inside delivery", 4.0, 130.00),
                        ("Weekend delivery surcharge", 2.0, 175.00),
                        ("Hazmat handling", 1.0, 240.00),
                    ]
                ),
                tax_id=None,
            ),
            expected_issues=[issue(IssueCode.MISSING_REQUIRED_FIELD, "vendor_tax_id")],
        )
    )

    # --- 6-7: wrapped descriptions ---------------------------------------- #

    samples.append(
        HardSample(
            index=6,
            kind="wrapped",
            vendor=RAVENNA,
            customer=AURORA,
            invoice=assemble(
                RAVENNA,
                "RA-2026-2210",
                "2026-04-02",
                "2026-05-02",
                items(
                    [
                        (
                            "Design and implementation of the clinical telemetry "
                            "ingestion pipeline, including schema migration and "
                            "backfill of historical device readings",
                            1.0,
                            8400.00,
                        ),
                        (
                            "On-call support retainer covering incident response "
                            "outside business hours, billed monthly in advance",
                            3.0,
                            1150.00,
                        ),
                        (
                            "Data quality audit of the regulatory reporting "
                            "extract, with written remediation plan",
                            1.0,
                            2250.00,
                        ),
                    ]
                ),
            ),
        )
    )

    samples.append(
        HardSample(
            index=7,
            kind="wrapped",
            vendor=MERIDIAN,
            customer=STONEBRIDGE,
            invoice=assemble(
                MERIDIAN,
                "MO-2026-2232",
                "2026-04-15",
                "2026-05-15",
                items(
                    [
                        (
                            "Height-adjustable sit-stand desk with electric dual "
                            "motor column, 180x80cm walnut veneer top, assembled "
                            "on site",
                            8.0,
                            845.00,
                        ),
                        (
                            "Ergonomic mesh task chair with adjustable lumbar "
                            "support, 4D armrests and synchro-tilt mechanism",
                            8.0,
                            412.50,
                        ),
                        (
                            "Acoustic ceiling baffle in recycled PET felt, "
                            "1200x600mm, suspended mounting kit included",
                            16.0,
                            168.00,
                        ),
                        (
                            "Project management and floor-plan coordination with "
                            "the building facilities team",
                            1.0,
                            1250.00,
                        ),
                    ]
                ),
            ),
        )
    )

    # --- 8-9: messy layout ------------------------------------------------ #

    # Discount and shipping are rows *inside* the items table, so they are part
    # of the subtotal and every rule still holds.
    samples.append(
        HardSample(
            index=8,
            kind="messy",
            vendor=NORTHWIND,
            customer=PINECREST,
            invoice=assemble(
                NORTHWIND,
                "NL-2026-2260",
                "2026-04-20",
                "2026-05-20",
                items(
                    [
                        ("Freight forwarding, Tacoma to Austin", 3.0, 745.00),
                        ("Warehouse storage, per pallet per week", 20.0, 18.50),
                        ("Customs documentation handling", 2.0, 110.00),
                        ("Temperature-controlled transit", 1.0, 480.00),
                        ("Volume discount, Q2 contract", 1.0, -180.00),
                        ("Expedited shipping", 1.0, 95.00),
                    ]
                ),
            ),
        )
    )

    # Discount and shipping live in the *totals block*, so they are document-level
    # adjustments rather than line items. Tax is charged on
    # subtotal + adjustments, which only reconciles through the adjusted path.
    messy_lines = items(
        [
            ("Managed Kubernetes cluster, monthly", 2.0, 1180.00),
            ("Object storage, per TB per month", 15.0, 31.50),
            ("Egress bandwidth, per TB", 8.0, 44.00),
            ("Dedicated load balancer", 3.0, 86.00),
            ("Platform support, business tier", 1.0, 540.00),
        ]
    )
    messy_subtotal = _round(sum(item.amount or 0.0 for item in messy_lines))
    messy_discount = _round(-messy_subtotal * 0.10)
    messy_shipping = 120.00
    messy_base = _round(messy_subtotal + messy_discount + messy_shipping)
    messy_tax = _round(messy_base * 0.21)
    samples.append(
        HardSample(
            index=9,
            kind="messy",
            vendor=HELVETIA,
            customer=SUR,
            notes=(
                "Document-level discount and shipping, printed in the totals "
                "block rather than as line item rows. The invoice is "
                "arithmetically correct: tax is charged on subtotal + discount "
                "+ shipping, not on the subtotal. It reconciles only through "
                "the adjusted totals path, so no issue is expected. The "
                "document does not print a taxable base under its own label, so "
                "taxable_base stays null and the validator derives it."
            ),
            invoice=assemble(
                HELVETIA,
                "HC-2026-2284",
                "2026-04-24",
                "2026-05-24",
                messy_lines,
                subtotal=messy_subtotal,
                tax_amount=messy_tax,
                total=_round(messy_base + messy_tax),
            ).model_copy(
                update={
                    "adjustments": [
                        Adjustment(
                            description="Volume discount (10%)",
                            amount=messy_discount,
                            kind=AdjustmentKind.DISCOUNT,
                        ),
                        Adjustment(
                            description="Shipping and handling",
                            amount=messy_shipping,
                            kind=AdjustmentKind.SHIPPING,
                        ),
                    ]
                }
            ),
        )
    )

    # --- 10-11: Argentine Factura A --------------------------------------- #

    # Factura A prints "Importe Neto Gravado" under its own label, so
    # taxable_base is transcribed rather than derived. With no adjustments it
    # equals the subtotal, which is exactly what the document states.
    pampa = assemble(
        PAMPA,
        "0003-00014827",
        "2026-03-11",
        "2026-04-10",
        items(
            [
                ("Impresion offset folleteria A4, 4/4 color", 5000.0, 48.75),
                # Real "ñ", not an ASCII transliteration. "Disenio" is not a
                # Spanish word, so the model kept normalizing it to "Diseño"
                # and the corpus manufactured a failure no real document would
                # produce. Reportlab's standard fonts use WinAnsiEncoding,
                # which covers the accented Latin-1 range.
                ("Diseño y armado de original", 1.0, 86500.00),
                ("Laminado mate sobre tapa", 2000.0, 22.40),
                ("Flete y distribucion AMBA", 1.0, 54800.00),
            ]
        ),
    )
    samples.append(
        HardSample(
            index=10,
            kind="factura_a",
            vendor=PAMPA,
            customer=PLATA,
            invoice=pampa.model_copy(update={"taxable_base": pampa.subtotal}),
        )
    )

    argentine_broken = assemble(
        ANDES,
        "0007-00002391",
        "2026-02-26",
        "2026-02-11",
        items(
            [
                ("Vino Malbec Reserva 750ml, caja x6", 120.0, 28400.00),
                ("Aceite de oliva extra virgen 500ml, caja x12", 45.0, 61200.00),
                ("Conservas artesanales surtidas, caja x24", 30.0, 38900.00),
            ]
        ),
    )
    samples.append(
        HardSample(
            index=11,
            kind="factura_a",
            vendor=ANDES,
            customer=SUR,
            defect="due_before_issue",
            invoice=argentine_broken.model_copy(
                update={"taxable_base": argentine_broken.subtotal}
            ),
            expected_issues=[
                issue(IssueCode.DUE_DATE_BEFORE_ISSUE_DATE, "due_date")
            ],
        )
    )

    return samples


# --------------------------------------------------------------------------- #
# Shared drawing helpers
# --------------------------------------------------------------------------- #


def wrap_text(
    c: canvas.Canvas, text: str, font: str, size: float, max_width: float
) -> list[str]:
    """Greedy word wrap against the real rendered string width."""
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if c.stringWidth(trial, font, size) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def money(value: float | None, currency: str | None) -> str:
    """Format an amount for an English-language layout."""
    if value is None:
        return ""
    body = f"{abs(value):,.2f}"
    sign = "-" if value < 0 else ""
    return f"{sign}${body}" if currency == "USD" else f"{sign}{body} {currency}"


def ars(value: float | None) -> str:
    """Format an amount the Argentine way: dot thousands, comma decimals."""
    if value is None:
        return ""
    body = f"{abs(value):,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return f"{'-' if value < 0 else ''}$ {body}"


def qty(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def ars_qty(value: float | None) -> str:
    """Quantities in the Argentine layout use comma decimals too."""
    if value is None:
        return ""
    return f"{value:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def dmy(iso: str | None) -> str:
    """Render an ISO date as dd/mm/yyyy."""
    if not iso:
        return ""
    return date.fromisoformat(iso).strftime("%d/%m/%Y")


def footer(c: canvas.Canvas, sample: HardSample, left: float) -> None:
    c.setFont("Helvetica", 7.5)
    c.setFillColor(colors.HexColor("#666666"))
    c.drawString(
        left, 13 * mm, "This document is fictitious sample data generated for testing."
    )
    c.setFillColor(colors.black)


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #


def render_multipage(c: canvas.Canvas, sample: HardSample) -> None:
    """Items table split across two pages; totals only on the last page."""
    inv, vendor = sample.invoice, sample.vendor
    left, right = 20 * mm, PAGE_WIDTH - 20 * mm
    row_height = 6.5 * mm
    rows_page_one = 9

    def header(page: int, total_pages: int) -> float:
        c.setFont("Helvetica-Bold", 16 if page == 1 else 12)
        c.drawString(left, PAGE_HEIGHT - 25 * mm, vendor.name)
        c.setFont("Helvetica", 8.5)
        y = PAGE_HEIGHT - 31 * mm
        if page == 1:
            c.drawString(left, y, f"{vendor.street}, {vendor.city}")
            y -= 4.5 * mm
            if inv.vendor_tax_id:
                c.drawString(left, y, f"Tax ID: {inv.vendor_tax_id}")
                y -= 4.5 * mm
        c.setFont("Helvetica-Bold", 13)
        c.drawRightString(right, PAGE_HEIGHT - 25 * mm, "INVOICE")
        c.setFont("Helvetica", 9)
        c.drawRightString(right, PAGE_HEIGHT - 31 * mm, f"No. {inv.invoice_number}")
        c.drawRightString(
            right, PAGE_HEIGHT - 36 * mm, f"Page {page} of {total_pages}"
        )
        if page == 1:
            c.drawRightString(right, PAGE_HEIGHT - 41 * mm, f"Issued: {inv.issue_date}")
            c.drawRightString(right, PAGE_HEIGHT - 46 * mm, f"Due: {inv.due_date}")
            c.drawRightString(right, PAGE_HEIGHT - 51 * mm, f"Currency: {inv.currency}")
            y -= 4 * mm
            c.setFont("Helvetica-Bold", 9)
            c.drawString(left, y, "Bill To")
            y -= 5 * mm
            c.setFont("Helvetica", 9)
            c.drawString(left, y, sample.customer.name)
            y -= 4.5 * mm
            c.drawString(left, y, f"{sample.customer.street}, {sample.customer.city}")
            y -= 12 * mm
        else:
            y -= 8 * mm
        return y

    def table_header(y: float) -> float:
        qty_x, price_x, amount_x = right - 62 * mm, right - 34 * mm, right
        c.setFillColor(colors.HexColor("#E8E8E8"))
        c.rect(left, y - row_height, right - left, row_height, stroke=0, fill=1)
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 8.5)
        text_y = y - 4.5 * mm
        c.drawString(left + 2 * mm, text_y, "Description")
        c.drawRightString(qty_x, text_y, "Qty")
        c.drawRightString(price_x, text_y, "Unit Price")
        c.drawRightString(amount_x - 2 * mm, text_y, "Amount")
        return y - row_height

    def draw_rows(y: float, rows: list[LineItem]) -> float:
        qty_x, price_x, amount_x = right - 62 * mm, right - 34 * mm, right
        c.setFont("Helvetica", 8.5)
        c.setStrokeColor(colors.HexColor("#DDDDDD"))
        c.setLineWidth(0.4)
        for item in rows:
            text_y = y - 4.5 * mm
            c.drawString(left + 2 * mm, text_y, item.description or "")
            c.drawRightString(qty_x, text_y, qty(item.quantity))
            c.drawRightString(price_x, text_y, money(item.unit_price, inv.currency))
            c.drawRightString(amount_x - 2 * mm, text_y, money(item.amount, inv.currency))
            y -= row_height
            c.line(left, y, right, y)
        c.setStrokeColor(colors.black)
        return y

    page_one, page_two = inv.line_items[:rows_page_one], inv.line_items[rows_page_one:]

    y = header(1, 2)
    y = table_header(y)
    y = draw_rows(y, page_one)
    c.setFont("Helvetica-Oblique", 9)
    c.drawRightString(right, y - 7 * mm, "Continued on page 2")
    footer(c, sample, left)
    c.showPage()

    y = header(2, 2)
    y = table_header(y)
    y = draw_rows(y, page_two)

    y -= 10 * mm
    totals_left, totals_right = right - 70 * mm, right
    c.setFont("Helvetica", 10)
    for label, value in (
        ("Subtotal", money(inv.subtotal, inv.currency)),
        (f"Tax ({qty(inv.tax_rate)}%)", money(inv.tax_amount, inv.currency)),
    ):
        c.drawString(totals_left, y, label)
        c.drawRightString(totals_right, y, value)
        y -= 6.5 * mm
    c.setLineWidth(0.7)
    c.line(totals_left, y + 2.5 * mm, totals_right, y + 2.5 * mm)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(totals_left, y - 2 * mm, f"Total Due ({inv.currency})")
    c.drawRightString(totals_right, y - 2 * mm, money(inv.total, inv.currency))
    footer(c, sample, left)


def render_wrapped(c: canvas.Canvas, sample: HardSample) -> None:
    """Narrow description column, so every description wraps onto two lines."""
    inv, vendor = sample.invoice, sample.vendor
    left, right = 20 * mm, PAGE_WIDTH - 20 * mm
    desc_width = 88 * mm
    line_height = 4.4 * mm

    c.setFont("Helvetica-Bold", 16)
    c.drawString(left, PAGE_HEIGHT - 26 * mm, vendor.name)
    c.setFont("Helvetica", 8.5)
    c.drawString(left, PAGE_HEIGHT - 32 * mm, f"{vendor.street}, {vendor.city}")
    if inv.vendor_tax_id:
        c.drawString(left, PAGE_HEIGHT - 36.5 * mm, f"Tax ID: {inv.vendor_tax_id}")

    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(right, PAGE_HEIGHT - 26 * mm, "INVOICE")
    c.setFont("Helvetica", 9)
    c.drawRightString(right, PAGE_HEIGHT - 32 * mm, f"No. {inv.invoice_number}")
    c.drawRightString(right, PAGE_HEIGHT - 37 * mm, f"Issued: {inv.issue_date}")
    c.drawRightString(right, PAGE_HEIGHT - 42 * mm, f"Due: {inv.due_date}")
    c.drawRightString(right, PAGE_HEIGHT - 47 * mm, f"Currency: {inv.currency}")

    y = PAGE_HEIGHT - 58 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(left, y, "Bill To")
    c.setFont("Helvetica", 9)
    c.drawString(left + 20 * mm, y, f"{sample.customer.name}, {sample.customer.city}")

    y -= 12 * mm
    qty_x, price_x, amount_x = right - 58 * mm, right - 32 * mm, right

    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(left + 2 * mm, y - 4.5 * mm, "Description")
    c.drawRightString(qty_x, y - 4.5 * mm, "Qty")
    c.drawRightString(price_x, y - 4.5 * mm, "Unit Price")
    c.drawRightString(amount_x - 2 * mm, y - 4.5 * mm, "Amount")
    c.setLineWidth(0.6)
    c.line(left, y - 6.5 * mm, right, y - 6.5 * mm)
    y -= 6.5 * mm

    for item in inv.line_items:
        lines = wrap_text(c, item.description or "", "Helvetica", 8.5, desc_width)
        row_height = line_height * len(lines) + 3 * mm
        c.setFont("Helvetica", 8.5)
        for offset, text in enumerate(lines):
            c.drawString(left + 2 * mm, y - 4.5 * mm - offset * line_height, text)
        text_y = y - 4.5 * mm
        c.drawRightString(qty_x, text_y, qty(item.quantity))
        c.drawRightString(price_x, text_y, money(item.unit_price, inv.currency))
        c.drawRightString(amount_x - 2 * mm, text_y, money(item.amount, inv.currency))
        y -= row_height
        c.setStrokeColor(colors.HexColor("#DDDDDD"))
        c.setLineWidth(0.4)
        c.line(left, y, right, y)
        c.setStrokeColor(colors.black)

    y -= 10 * mm
    totals_left = right - 70 * mm
    c.setFont("Helvetica", 10)
    for label, value in (
        ("Subtotal", money(inv.subtotal, inv.currency)),
        (f"Tax ({qty(inv.tax_rate)}%)", money(inv.tax_amount, inv.currency)),
    ):
        c.drawString(totals_left, y, label)
        c.drawRightString(right, y, value)
        y -= 6.5 * mm
    c.setLineWidth(0.7)
    c.line(totals_left, y + 2.5 * mm, right, y + 2.5 * mm)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(totals_left, y - 2 * mm, f"Total Due ({inv.currency})")
    c.drawRightString(right, y - 2 * mm, money(inv.total, inv.currency))
    footer(c, sample, left)


def render_messy(c: canvas.Canvas, sample: HardSample) -> None:
    """Totals printed at the top, borderless detail table underneath.

    For hard_09 the discount and shipping lines sit in this top block, where the
    `Invoice` schema has nowhere to put them.
    """
    inv, vendor = sample.invoice, sample.vendor
    left, right = 20 * mm, PAGE_WIDTH - 20 * mm

    c.setFont("Helvetica-Bold", 9)
    c.drawString(left, PAGE_HEIGHT - 22 * mm, "AMOUNT DUE")
    c.setFont("Helvetica-Bold", 24)
    c.drawString(left, PAGE_HEIGHT - 32 * mm, money(inv.total, inv.currency))
    c.setFont("Helvetica", 9)
    c.drawString(left, PAGE_HEIGHT - 38 * mm, f"by {inv.due_date}  -  {inv.currency}")

    c.setFont("Helvetica", 9)
    box_y = PAGE_HEIGHT - 22 * mm
    rows: list[tuple[str, str]] = [("Subtotal", money(inv.subtotal, inv.currency))]
    # Adjustments are printed here, in the totals block, which is precisely what
    # makes them adjustments rather than line items.
    for adjustment in inv.adjustments:
        rows.append(
            (adjustment.description or "", money(adjustment.amount, inv.currency))
        )
    rows.append((f"IVA {qty(inv.tax_rate)}%", money(inv.tax_amount, inv.currency)))
    rows.append(("TOTAL", money(inv.total, inv.currency)))

    for index, (label, value) in enumerate(rows):
        if index == len(rows) - 1:
            c.setFont("Helvetica-Bold", 9.5)
            c.setLineWidth(0.5)
            c.line(right - 62 * mm, box_y + 4 * mm, right, box_y + 4 * mm)
        c.drawString(right - 62 * mm, box_y, label)
        c.drawRightString(right, box_y, value)
        box_y -= 5.5 * mm
        c.setFont("Helvetica", 9)

    y = min(PAGE_HEIGHT - 48 * mm, box_y - 8 * mm)
    c.setLineWidth(0.5)
    c.line(left, y, right, y)
    y -= 8 * mm

    c.setFont("Helvetica-Bold", 10)
    c.drawString(left, y, vendor.name)
    c.setFont("Helvetica", 8.5)
    y -= 4.5 * mm
    c.drawString(left, y, f"{vendor.street}, {vendor.city}")
    y -= 4.5 * mm
    if inv.vendor_tax_id:
        c.drawString(left, y, f"Tax ID: {inv.vendor_tax_id}")
    c.drawRightString(right, y + 9 * mm, f"Invoice {inv.invoice_number}")
    c.drawRightString(right, y + 4.5 * mm, f"Issued {inv.issue_date}")
    c.drawRightString(right, y, f"Billed to {sample.customer.name}")

    y -= 14 * mm
    c.setFont("Helvetica", 7.5)
    c.setFillColor(colors.HexColor("#888888"))
    c.drawString(left, y, "DETAIL")
    c.setFillColor(colors.black)

    y -= 7 * mm
    qty_x, price_x, amount_x = right - 56 * mm, right - 30 * mm, right
    for item in inv.line_items:
        c.setFont("Helvetica", 9)
        c.drawString(left, y, item.description or "")
        c.drawRightString(qty_x, y, qty(item.quantity))
        c.drawRightString(price_x, y, money(item.unit_price, inv.currency))
        c.drawRightString(amount_x, y, money(item.amount, inv.currency))
        y -= 6.5 * mm

    footer(c, sample, left)


def render_factura_a(c: canvas.Canvas, sample: HardSample) -> None:
    """Argentine Factura A: Spanish labels, CUIT, dd/mm/yyyy, comma decimals."""
    inv, vendor = sample.invoice, sample.vendor
    left, right = 15 * mm, PAGE_WIDTH - 15 * mm
    mid = PAGE_WIDTH / 2

    top = PAGE_HEIGHT - 15 * mm
    box_height = 34 * mm
    c.setLineWidth(0.9)
    c.rect(left, top - box_height, right - left, box_height)
    c.line(mid, top - box_height, mid, top)

    # Letter badge straddling the divider, as on the real form.
    badge = 11 * mm
    c.setFillColor(colors.white)
    c.rect(mid - badge / 2, top - 16 * mm, badge, badge, stroke=1, fill=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(mid, top - 13.5 * mm, "A")
    c.setFont("Helvetica", 5.5)
    c.drawCentredString(mid, top - 19 * mm, "COD. 01")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(left + 5 * mm, top - 9 * mm, vendor.name)
    c.setFont("Helvetica", 7.5)
    c.drawString(left + 5 * mm, top - 15 * mm, f"Domicilio: {vendor.street}")
    c.drawString(left + 5 * mm, top - 19.5 * mm, vendor.city)
    c.drawString(
        left + 5 * mm, top - 24 * mm, "Condicion frente al IVA: Responsable Inscripto"
    )
    c.drawString(left + 5 * mm, top - 28.5 * mm, "Ingresos Brutos: Convenio Multilateral")

    c.setFont("Helvetica-Bold", 13)
    c.drawString(mid + 8 * mm, top - 9 * mm, "FACTURA")
    c.setFont("Helvetica", 8)
    punto, numero = (inv.invoice_number or "0000-00000000").split("-")
    c.drawString(mid + 8 * mm, top - 15 * mm, f"Punto de Venta: {punto}")
    c.drawString(mid + 48 * mm, top - 15 * mm, f"Comp. Nro: {numero}")
    c.drawString(mid + 8 * mm, top - 20 * mm, f"Fecha de Emision: {dmy(inv.issue_date)}")
    if inv.vendor_tax_id:
        c.drawString(mid + 8 * mm, top - 25 * mm, f"CUIT: {inv.vendor_tax_id}")
    c.drawString(mid + 8 * mm, top - 30 * mm, "Inicio de Actividades: 01/08/2014")

    y = top - box_height - 6 * mm
    c.setLineWidth(0.6)
    c.rect(left, y - 20 * mm, right - left, 20 * mm)
    c.setFont("Helvetica", 7.5)
    c.drawString(left + 4 * mm, y - 5 * mm, f"Senor/es: {sample.customer.name}")
    c.drawString(left + 4 * mm, y - 10 * mm, f"Domicilio: {sample.customer.street}, {sample.customer.city}")
    c.drawString(left + 4 * mm, y - 15 * mm, "Condicion frente al IVA: Responsable Inscripto")
    c.drawString(mid + 30 * mm, y - 5 * mm, "CUIT: 33-55512349-9")
    c.drawString(mid + 30 * mm, y - 10 * mm, "Condicion de venta: Cuenta Corriente 30 dias")
    c.drawString(mid + 30 * mm, y - 15 * mm, f"Moneda: {inv.currency}")

    y -= 28 * mm
    row_height = 6.5 * mm
    code_x = left + 3 * mm
    desc_x = left + 22 * mm
    qty_x, price_x, amount_x = right - 72 * mm, right - 40 * mm, right - 3 * mm

    c.setFillColor(colors.HexColor("#E4E4E4"))
    c.rect(left, y - row_height, right - left, row_height, stroke=1, fill=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 7.5)
    header_y = y - 4.3 * mm
    c.drawString(code_x, header_y, "Codigo")
    c.drawString(desc_x, header_y, "Producto / Servicio")
    c.drawRightString(qty_x, header_y, "Cantidad")
    c.drawRightString(price_x, header_y, "Precio Unit.")
    c.drawRightString(amount_x, header_y, "Subtotal")
    y -= row_height

    c.setFont("Helvetica", 7.5)
    for index, item in enumerate(inv.line_items, start=1):
        text_y = y - 4.3 * mm
        c.drawString(code_x, text_y, f"{index:04d}")
        c.drawString(desc_x, text_y, item.description or "")
        c.drawRightString(qty_x, text_y, ars_qty(item.quantity))
        c.drawRightString(price_x, text_y, ars(item.unit_price))
        c.drawRightString(amount_x, text_y, ars(item.amount))
        y -= row_height
    c.setLineWidth(0.4)
    c.line(left, y, right, y)

    y -= 12 * mm
    label_x, value_x = right - 78 * mm, right - 3 * mm
    c.setFont("Helvetica", 8.5)
    c.drawString(label_x, y, "Importe Neto Gravado:")
    c.drawRightString(value_x, y, ars(inv.taxable_base))
    y -= 6 * mm
    c.drawString(label_x, y, f"IVA {qty(inv.tax_rate)}%:")
    c.drawRightString(value_x, y, ars(inv.tax_amount))
    y -= 6 * mm
    c.drawString(label_x, y, "Vencimiento del Pago:")
    c.drawRightString(value_x, y, dmy(inv.due_date))
    y -= 8 * mm
    c.setLineWidth(0.7)
    c.line(label_x, y + 4 * mm, value_x, y + 4 * mm)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(label_x, y, "Importe Total:")
    c.drawRightString(value_x, y, ars(inv.total))

    c.setFont("Helvetica", 7.5)
    c.drawString(left, 24 * mm, f"CAE N: 7410{sample.index:04d}8832190")
    c.drawString(left, 19 * mm, f"Fecha de Vto. de CAE: {dmy(inv.due_date)}")
    footer(c, sample, left)


# --------------------------------------------------------------------------- #
# Scanned-document pipeline
# --------------------------------------------------------------------------- #


def render_scan_source(c: canvas.Canvas, sample: HardSample) -> None:
    """Draw the pristine page that will then be rasterized and degraded."""
    from generate_samples import Sample as CleanSample
    from generate_samples import (
        _render_classic,
        _render_modern_boxed,
        _render_two_column,
    )

    proxy = CleanSample(
        index=sample.index,
        file=sample.file,
        layout=sample.scan_base,
        defect=sample.defect,
        vendor=sample.vendor,
        customer=sample.customer,
        invoice=sample.invoice,
        expected_issues=[],
    )
    {
        "classic": _render_classic,
        "modern_boxed": _render_modern_boxed,
        "two_column": _render_two_column,
    }[sample.scan_base](c, proxy)


def degrade_to_scan(source: Path, destination: Path, sample: HardSample,
                    rng: random.Random) -> None:
    """Rasterize a PDF and re-wrap it as a degraded, image-only PDF.

    The output has no text layer at all, so the extractor has to read it the way
    it would read a phone photo or a flatbed scan.
    """
    document = pdfium.PdfDocument(str(source))
    image = document[0].render(scale=SCAN_DPI / 72).to_pil().convert("RGB")
    document.close()

    # Scanner optics: slight defocus before anything else.
    image = image.filter(ImageFilter.GaussianBlur(0.45))

    # Paper never sits square on the glass.
    image = image.rotate(
        sample.scan_angle, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255)
    )

    # Sensor speckle. Deterministic given the seed.
    width, height = image.size
    speckle = Image.frombytes("L", (width, height), rng.randbytes(width * height))
    image = Image.blend(image, speckle.convert("RGB"), sample.scan_noise)

    # Most scanners hand back something closer to grayscale than to true color.
    image = image.convert("L").convert("RGB")

    # Bake in JPEG artifacts, then wrap the compressed pixels into a PDF.
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=sample.scan_quality)
    buffer.seek(0)
    compressed = Image.open(buffer)
    compressed.load()
    compressed.save(destination, "PDF", resolution=SCAN_DPI)


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

RENDERERS = {
    "multipage": render_multipage,
    "wrapped": render_wrapped,
    "messy": render_messy,
    "factura_a": render_factura_a,
}


def render_sample(sample: HardSample, pdf_dir: Path, seed: int) -> Path:
    """Render one hard sample to its final PDF.

    The speckle RNG is seeded per sample rather than shared, so rendering
    one invoice in isolation produces exactly the same bytes as rendering
    the whole set. Without that, `--only` would silently alter the noise.
    """
    destination = pdf_dir / sample.file
    rng = random.Random(seed + sample.index)

    if sample.kind == "scanned":
        staging = pdf_dir / f".{sample.file}.source"
        c = canvas.Canvas(str(staging), pagesize=A4)
        c.setTitle(f"Invoice {sample.invoice.invoice_number}")
        render_scan_source(c, sample)
        c.showPage()
        c.save()
        degrade_to_scan(staging, destination, sample, rng)
        staging.unlink()
        return destination

    c = canvas.Canvas(str(destination), pagesize=A4)
    c.setTitle(f"Invoice {sample.invoice.invoice_number}")
    c.setAuthor(sample.vendor.name)
    RENDERERS[sample.kind](c, sample)
    c.showPage()
    c.save()
    return destination


def write_ground_truth(sample: HardSample, truth_dir: Path) -> Path:
    """Write ground truth in the same shape the clean set uses."""
    path = truth_dir / f"{Path(sample.file).stem}.json"
    payload = {
        "file": sample.file,
        "layout": sample.kind,
        "defect": sample.defect,
        "expected_status": "needs_review" if sample.expected_issues else "valid",
        "expected_issues": sample.expected_issues,
        "invoice": sample.invoice.model_dump(),
    }
    if sample.notes:
        payload["notes"] = sample.notes
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, default=root / "samples" / "hard")
    parser.add_argument(
        "--seed", type=int, default=20260417, help="Seed for the scanner speckle."
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="STEM",
        help=(
            "Regenerate only these samples, e.g. --only hard_10. Keeps a "
            "one-invoice fix from churning every other PDF."
        ),
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

    samples = build_samples()

    if args.only:
        wanted = {stem.removesuffix(".pdf") for stem in args.only}
        unknown = wanted - {Path(s.file).stem for s in samples}
        if unknown:
            parser.error(f"unknown sample(s): {', '.join(sorted(unknown))}")
        samples = [s for s in samples if Path(s.file).stem in wanted]

    for sample in samples:
        path = (
            pdf_dir / sample.file
            if args.skip_pdfs
            else render_sample(sample, pdf_dir, args.seed)
        )
        write_ground_truth(sample, truth_dir)
        marks = []
        if sample.defect:
            marks.append(f"defect={sample.defect}")
        if sample.invoice.adjustments:
            marks.append(f"{len(sample.invoice.adjustments)} adjustments")
        suffix = f"  <- {', '.join(marks)}" if marks else ""
        print(
            f"{sample.file}  {sample.kind:<10} {sample.invoice.currency}  "
            f"{len(sample.invoice.line_items)} items  "
            f"{path.stat().st_size // 1024}KB{suffix}"
        )

    defects = sum(1 for s in samples if s.defect)
    flagged = sum(1 for s in samples if s.expected_issues)
    adjusted = sum(1 for s in samples if s.invoice.adjustments)
    print(
        f"\n{'PDFs left untouched' if args.skip_pdfs else f'{len(samples)} PDFs in {pdf_dir}'}\n"
        f"{len(samples)} ground truth files in {truth_dir}\n"
        f"{defects} deliberate defects ({flagged} files flagged), "
        f"{adjusted} with document-level adjustments"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
