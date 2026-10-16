"""Score a `results.json` run against the ground truth corpus.

Reports two things, deliberately kept separate:

  * **Field accuracy** - how often the extractor transcribed each field
    correctly, broken out per field rather than collapsed into one number. A
    single percentage hides whether the misses are all in one hard field.
  * **Defect detection** - recall on the issues the corpus expects, alongside
    unexpected findings. Recall alone is a bad metric: a validator that flags
    everything scores 100%. The unexpected-findings count is what keeps it
    honest.

    python scripts/evaluate.py --results results.json [--show-errors]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from invoice_extractor.models import BatchResult  # noqa: E402

#: Scalar invoice fields scored one-for-one.
SCALAR_FIELDS: tuple[str, ...] = (
    "vendor_name",
    "vendor_tax_id",
    "invoice_number",
    "issue_date",
    "due_date",
    "currency",
    "subtotal",
    "tax_rate",
    "tax_amount",
    "total",
)

#: Numeric fields compared with a tolerance rather than for equality.
NUMERIC_FIELDS = frozenset(
    {"subtotal", "tax_rate", "tax_amount", "total", "quantity", "unit_price", "amount"}
)

#: Fields per line item, scored positionally.
LINE_FIELDS: tuple[str, ...] = ("description", "quantity", "unit_price", "amount")

TOLERANCE = 0.01

#: Separators stripped before comparing tax IDs, since "47-8821093" and
#: "478821093" are the same identifier printed two ways.
_ID_SEPARATORS = re.compile(r"[.\-\s]")
_WHITESPACE = re.compile(r"\s+")


@dataclass
class FieldScore:
    """Correct/total counters for one field across the corpus."""

    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass
class Mismatch:
    """One field the extractor got wrong."""

    file: str
    field: str
    expected: Any
    actual: Any


@dataclass
class Report:
    """Everything the evaluation measured."""

    scalar: dict[str, FieldScore] = field(default_factory=dict)
    line_fields: dict[str, FieldScore] = field(default_factory=dict)
    line_count: FieldScore = field(default_factory=FieldScore)
    mismatches: list[Mismatch] = field(default_factory=list)

    expected_issues: int = 0
    detected_issues: int = 0
    missed: list[tuple[str, str]] = field(default_factory=list)
    unexpected: list[tuple[str, str]] = field(default_factory=list)

    status_correct: int = 0
    status_total: int = 0

    files_scored: int = 0
    files_failed: int = 0

    @property
    def field_accuracy(self) -> float:
        """Overall scalar + line-item field accuracy."""
        correct = sum(s.correct for s in self.scalar.values())
        total = sum(s.total for s in self.scalar.values())
        correct += sum(s.correct for s in self.line_fields.values())
        total += sum(s.total for s in self.line_fields.values())
        correct += self.line_count.correct
        total += self.line_count.total
        return correct / total if total else 0.0

    @property
    def detection_rate(self) -> float:
        return self.detected_issues / self.expected_issues if self.expected_issues else 0.0


def evaluate(batch: BatchResult, truth_by_file: dict[str, dict]) -> Report:
    """Compare a batch run against ground truth."""
    report = Report(
        scalar={name: FieldScore() for name in SCALAR_FIELDS},
        line_fields={name: FieldScore() for name in LINE_FIELDS},
    )

    for result in batch.results:
        truth = truth_by_file.get(result.file)
        if truth is None:
            continue

        report.files_scored += 1
        _score_issues(result, truth, report)

        if result.invoice is None:
            report.files_failed += 1
            # Count every field as missed so a failed file cannot inflate accuracy.
            for name in SCALAR_FIELDS:
                report.scalar[name].total += 1
            report.line_count.total += 1
            continue

        _score_fields(result.file, result.invoice.model_dump(), truth["invoice"], report)

    return report


def _score_fields(
    file: str, actual: dict, expected: dict, report: Report
) -> None:
    """Score scalar fields, line-item count, and per-line fields for one file."""
    for name in SCALAR_FIELDS:
        score = report.scalar[name]
        score.total += 1
        if _matches(name, expected.get(name), actual.get(name)):
            score.correct += 1
        else:
            report.mismatches.append(
                Mismatch(file, name, expected.get(name), actual.get(name))
            )

    expected_lines = expected.get("line_items") or []
    actual_lines = actual.get("line_items") or []

    report.line_count.total += 1
    if len(expected_lines) == len(actual_lines):
        report.line_count.correct += 1
    else:
        report.mismatches.append(
            Mismatch(file, "line_items (count)", len(expected_lines), len(actual_lines))
        )

    # Score positionally over the expected lines. A short result scores the
    # missing lines as wrong rather than quietly skipping them.
    for index, expected_line in enumerate(expected_lines):
        actual_line = actual_lines[index] if index < len(actual_lines) else {}
        for name in LINE_FIELDS:
            score = report.line_fields[name]
            score.total += 1
            if _matches(name, expected_line.get(name), actual_line.get(name)):
                score.correct += 1
            else:
                report.mismatches.append(
                    Mismatch(
                        file,
                        f"line_items[{index}].{name}",
                        expected_line.get(name),
                        actual_line.get(name),
                    )
                )


def _score_issues(result: Any, truth: dict, report: Report) -> None:
    """Score validation findings for one file against its expected issues."""
    expected_codes = {issue["code"] for issue in truth.get("expected_issues", [])}
    actual_codes = {issue.code.value for issue in result.issues}

    report.expected_issues += len(expected_codes)
    report.detected_issues += len(expected_codes & actual_codes)

    for code in sorted(expected_codes - actual_codes):
        report.missed.append((result.file, code))
    for code in sorted(actual_codes - expected_codes):
        report.unexpected.append((result.file, code))

    report.status_total += 1
    if result.status == truth.get("expected_status"):
        report.status_correct += 1


def _matches(name: str, expected: Any, actual: Any) -> bool:
    """Compare one field, normalized for its type."""
    if expected is None or actual is None:
        return expected is None and actual is None

    if name in NUMERIC_FIELDS:
        try:
            return abs(float(expected) - float(actual)) <= TOLERANCE + 1e-9
        except (TypeError, ValueError):
            return False

    left, right = _normalize_text(expected), _normalize_text(actual)
    if name == "vendor_tax_id":
        left = _ID_SEPARATORS.sub("", left)
        right = _ID_SEPARATORS.sub("", right)
    return left == right


def _normalize_text(value: Any) -> str:
    """Collapse whitespace and case so formatting differences aren't errors."""
    return _WHITESPACE.sub(" ", str(value)).strip().casefold()


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def print_report(report: Report, *, show_errors: bool) -> None:
    """Print the evaluation summary."""
    print("=" * 62)
    print("FIELD ACCURACY")
    print("=" * 62)
    print(f"{'FIELD':<28} {'CORRECT':>9} {'TOTAL':>7} {'ACCURACY':>10}")
    print("-" * 62)

    for name in SCALAR_FIELDS:
        _print_score(name, report.scalar[name])
    _print_score("line_items (count)", report.line_count)
    for name in LINE_FIELDS:
        _print_score(f"line_items[].{name}", report.line_fields[name])

    print("-" * 62)
    print(f"{'OVERALL':<28} {'':>9} {'':>7} {report.field_accuracy:>9.1%}")

    print()
    print("=" * 62)
    print("DEFECT DETECTION")
    print("=" * 62)
    print(
        f"Expected issues detected: {report.detected_issues}/"
        f"{report.expected_issues} ({report.detection_rate:.1%})"
    )
    print(
        f"Status agreement:         {report.status_correct}/{report.status_total} "
        f"({_ratio(report.status_correct, report.status_total):.1%})"
    )
    print(f"Unexpected findings:      {len(report.unexpected)}")

    if report.missed:
        print("\nMissed:")
        for file, code in report.missed:
            print(f"  {file}  {code}")

    if report.unexpected:
        print("\nUnexpected (flagged but not in ground truth):")
        for file, code in report.unexpected:
            print(f"  {file}  {code}")

    print()
    print("=" * 62)
    print(
        f"{report.files_scored} files scored"
        + (f", {report.files_failed} failed extraction" if report.files_failed else "")
    )

    if show_errors and report.mismatches:
        print("\nField mismatches:")
        for item in report.mismatches:
            print(f"  {item.file}  {item.field}")
            print(f"      expected: {item.expected!r}")
            print(f"      actual:   {item.actual!r}")
    elif report.mismatches:
        print(f"{len(report.mismatches)} field mismatches (--show-errors for detail)")


def _print_score(label: str, score: FieldScore) -> None:
    print(f"{label:<28} {score.correct:>9} {score.total:>7} {score.accuracy:>9.1%}")


def _ratio(correct: int, total: int) -> float:
    return correct / total if total else 0.0


def load_ground_truth(directory: Path) -> dict[str, dict]:
    """Load every ground truth file, keyed by the PDF filename it describes."""
    truths: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        truths[payload["file"]] = payload
    if not truths:
        raise FileNotFoundError(
            f"No ground truth JSON in {directory}. "
            "Run scripts/generate_samples.py first."
        )
    return truths


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--results",
        type=Path,
        default=root / "results.json",
        help="Path to the results.json produced by the CLI.",
    )
    parser.add_argument(
        "--ground-truth",
        type=Path,
        default=root / "samples" / "ground_truth",
        help="Directory of ground truth JSON files.",
    )
    parser.add_argument(
        "--show-errors",
        action="store_true",
        help="Print every field mismatch with expected and actual values.",
    )
    args = parser.parse_args(argv)

    if not args.results.is_file():
        print(
            f"error: {args.results} not found. Run the extractor first:\n"
            f"  python -m invoice_extractor extract samples/pdfs --out {args.results}",
            file=sys.stderr,
        )
        return 2

    try:
        truths = load_ground_truth(args.ground_truth)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    batch = BatchResult.model_validate_json(args.results.read_text(encoding="utf-8"))
    report = evaluate(batch, truths)

    if report.files_scored == 0:
        print(
            "error: no results matched the ground truth corpus. Did the run use "
            "a different sample directory?",
            file=sys.stderr,
        )
        return 2

    print_report(report, show_errors=args.show_errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
