"""Detail page helpers: placing each finding next to the field it concerns."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..models import Invoice, ValidationIssue

#: Matches the engine's scoped field paths, e.g. "line_items[2].amount".
_SCOPED = re.compile(r"^(?P<list>line_items|adjustments)\[(?P<index>\d+)\]\.(?P<field>\w+)$")


@dataclass
class PlacedIssues:
    """Findings sorted by where they should appear on the page."""

    #: Keyed by header field name, e.g. "total".
    header: dict[str, list[ValidationIssue]] = field(default_factory=dict)
    #: Keyed by line item index.
    line_items: dict[int, list[ValidationIssue]] = field(default_factory=dict)
    #: Keyed by adjustment index.
    adjustments: dict[int, list[ValidationIssue]] = field(default_factory=dict)
    #: Anything with no field, or a field the page does not render.
    general: list[ValidationIssue] = field(default_factory=list)

    def all(self) -> list[ValidationIssue]:
        """Every placed issue, for the summary block at the top."""
        found: list[ValidationIssue] = list(self.general)
        for group in (self.header, self.line_items, self.adjustments):
            for issues in group.values():
                found.extend(issues)
        return found


#: Header fields the detail page renders, and so can highlight.
HEADER_FIELDS = frozenset(
    {
        "vendor_name",
        "vendor_tax_id",
        "invoice_number",
        "issue_date",
        "due_date",
        "currency",
        "subtotal",
        "taxable_base",
        "tax_rate",
        "tax_amount",
        "total",
        "line_items",
        "adjustments",
    }
)


def place_issues(invoice: Invoice) -> PlacedIssues:
    """Sort an invoice's findings by the part of the page they belong to.

    A finding the page cannot anchor falls through to `general` rather than
    being dropped: an issue nobody sees is worse than one in the wrong place.
    """
    placed = PlacedIssues(
        header=defaultdict(list), line_items=defaultdict(list),
        adjustments=defaultdict(list),
    )

    for issue in invoice.issues:
        scoped = _SCOPED.match(issue.field or "")
        if scoped is not None:
            index = int(scoped.group("index"))
            target = (
                placed.line_items
                if scoped.group("list") == "line_items"
                else placed.adjustments
            )
            target[index].append(issue)
        elif issue.field in HEADER_FIELDS:
            placed.header[issue.field].append(issue)
        else:
            placed.general.append(issue)

    placed.header = dict(placed.header)
    placed.line_items = dict(placed.line_items)
    placed.adjustments = dict(placed.adjustments)
    return placed


def extracted_invoice(invoice: Invoice) -> dict:
    """The invoice payload out of the stored extraction, or an empty one."""
    payload = invoice.extraction or {}
    return payload.get("invoice") or {}
