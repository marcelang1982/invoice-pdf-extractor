"""Score an extraction run against a ground truth corpus.

Reports three things, deliberately kept separate:

  * **Field accuracy** - how often the extractor transcribed each field
    correctly, broken out per field rather than collapsed into one number. A
    single percentage hides whether the misses are all in one hard field.
  * **Defect detection** - recall on the issues the corpus expects, alongside
    unexpected findings. Recall alone is a bad metric: a validator that flags
    everything scores 100%. The unexpected-findings count is what keeps it
    honest.
  * **Token cost** - per invoice and per set, so accuracy can be weighed
    against what it costs to get.

Two corpora are scored separately, never pooled: `clean` is born-digital and
well typeset, `hard` is scanned, multipage, wrapped, messy and Argentine. A
combined average would hide exactly the difference worth measuring.

    python scripts/evaluate.py --set clean [--show-errors]
    python scripts/evaluate.py --set hard  [--show-errors]
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

from invoice_extractor import InvoiceExtractor, process_directory  # noqa: E402
from invoice_extractor.config import ConfigError  # noqa: E402
from invoice_extractor.models import BatchResult  # noqa: E402

#: Known sample sets: name -> (default results file, ground truth directory).
SETS: dict[str, tuple[str, str]] = {
    "clean": ("results.json", "samples/ground_truth"),
    "hard": ("results_hard.json", "samples/hard/ground_truth"),
}

#: Scalar invoice fields scored one-for-one.
SCALAR_FIELDS: tuple[str, ...] = (
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
)

#: Numeric fields compared with a tolerance rather than for equality.
NUMERIC_FIELDS = frozenset(
    {
        "subtotal",
        "taxable_base",
        "tax_rate",
        "tax_amount",
        "total",
        "quantity",
        "unit_price",
        "amount",
    }
)

#: Fields per line item, scored positionally.
LINE_FIELDS: tuple[str, ...] = ("description", "quantity", "unit_price", "amount")

#: Fields per document-level adjustment, scored positionally.
ADJUSTMENT_FIELDS: tuple[str, ...] = ("description", "amount", "kind")

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
class TokenRow:
    """What one invoice cost to extract."""

    file: str
    input_tokens: int
    output_tokens: int
    attempts: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class Report:
    """Everything the evaluation measured."""

    scalar: dict[str, FieldScore] = field(default_factory=dict)
    line_fields: dict[str, FieldScore] = field(default_factory=dict)
    line_count: FieldScore = field(default_factory=FieldScore)
    adjustment_fields: dict[str, FieldScore] = field(default_factory=dict)
    adjustment_count: FieldScore = field(default_factory=FieldScore)
    mismatches: list[Mismatch] = field(default_factory=list)

    expected_issues: int = 0
    detected_issues: int = 0
    missed: list[tuple[str, str]] = field(default_factory=list)
    unexpected: list[tuple[str, str]] = field(default_factory=list)

    status_correct: int = 0
    status_total: int = 0

    files_scored: int = 0
    files_failed: int = 0

    tokens: list[TokenRow] = field(default_factory=list)

    @property
    def field_accuracy(self) -> float:
        """Overall scalar + line-item field accuracy."""
        correct = sum(s.correct for s in self.scalar.values())
        total = sum(s.total for s in self.scalar.values())
        correct += sum(s.correct for s in self.line_fields.values())
        total += sum(s.total for s in self.line_fields.values())
        correct += sum(s.correct for s in self.adjustment_fields.values())
        total += sum(s.total for s in self.adjustment_fields.values())
        correct += self.line_count.correct + self.adjustment_count.correct
        total += self.line_count.total + self.adjustment_count.total
        return correct / total if total else 0.0

    @property
    def detection_rate(self) -> float:
        return self.detected_issues / self.expected_issues if self.expected_issues else 0.0


def evaluate(batch: BatchResult, truth_by_file: dict[str, dict]) -> Report:
    """Compare a batch run against ground truth."""
    report = Report(
        scalar={name: FieldScore() for name in SCALAR_FIELDS},
        line_fields={name: FieldScore() for name in LINE_FIELDS},
        adjustment_fields={name: FieldScore() for name in ADJUSTMENT_FIELDS},
    )

    for result in batch.results:
        truth = truth_by_file.get(result.file)
        if truth is None:
            continue

        report.files_scored += 1
        _score_issues(result, truth, report)
        report.tokens.append(
            TokenRow(
                file=result.file,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                attempts=result.attempts,
            )
        )

        if result.invoice is None:
            report.files_failed += 1
            # Count every field as missed so a failed file cannot inflate accuracy.
            for name in SCALAR_FIELDS:
                report.scalar[name].total += 1
            report.line_count.total += 1
            report.adjustment_count.total += 1
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

    _score_list(
        file, "line_items", LINE_FIELDS, expected, actual,
        report.line_count, report.line_fields, report,
    )
    _score_list(
        file, "adjustments", ADJUSTMENT_FIELDS, expected, actual,
        report.adjustment_count, report.adjustment_fields, report,
    )


def _score_list(
    file: str,
    key: str,
    fields: tuple[str, ...],
    expected: dict,
    actual: dict,
    count_score: FieldScore,
    field_scores: dict[str, FieldScore],
    report: Report,
) -> None:
    """Score a repeated sub-object (line items, adjustments) positionally."""
    expected_rows = expected.get(key) or []
    actual_rows = actual.get(key) or []

    count_score.total += 1
    if len(expected_rows) == len(actual_rows):
        count_score.correct += 1
    else:
        report.mismatches.append(
            Mismatch(file, f"{key} (count)", len(expected_rows), len(actual_rows))
        )

    # A short result scores its missing rows as wrong rather than skipping them.
    for index, expected_row in enumerate(expected_rows):
        actual_row = actual_rows[index] if index < len(actual_rows) else {}
        for name in fields:
            score = field_scores[name]
            score.total += 1
            if _matches(name, expected_row.get(name), actual_row.get(name)):
                score.correct += 1
            else:
                report.mismatches.append(
                    Mismatch(
                        file,
                        f"{key}[{index}].{name}",
                        expected_row.get(name),
                        actual_row.get(name),
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


def print_report(report: Report, *, show_errors: bool, set_name: str = "") -> None:
    """Print the evaluation summary."""
    if set_name:
        print("=" * 62)
        print(f"SET: {set_name}")
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
    _print_score("adjustments (count)", report.adjustment_count)
    for name in ADJUSTMENT_FIELDS:
        _print_score(f"adjustments[].{name}", report.adjustment_fields[name])

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

    print_cost(report)

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


def print_cost(report: Report) -> None:
    """Print token cost per invoice and for the set as a whole."""
    if not report.tokens:
        return

    print()
    print("=" * 62)
    print("TOKEN COST")
    print("=" * 62)
    print(f"{'FILE':<20} {'INPUT':>9} {'OUTPUT':>8} {'TOTAL':>9} {'TRIES':>6}")
    print("-" * 62)

    for row in report.tokens:
        print(
            f"{row.file:<20} {row.input_tokens:>9,} {row.output_tokens:>8,} "
            f"{row.total_tokens:>9,} {row.attempts:>6}"
        )

    count = len(report.tokens)
    total_in = sum(r.input_tokens for r in report.tokens)
    total_out = sum(r.output_tokens for r in report.tokens)
    totals = [r.total_tokens for r in report.tokens]
    retried = sum(1 for r in report.tokens if r.attempts > 1)

    print("-" * 62)
    print(
        f"{'SET TOTAL':<20} {total_in:>9,} {total_out:>8,} "
        f"{total_in + total_out:>9,}"
    )
    print(
        f"{'MEAN PER INVOICE':<20} {total_in // count:>9,} {total_out // count:>8,} "
        f"{(total_in + total_out) // count:>9,}"
    )
    print(f"{'MIN / MAX TOTAL':<20} {min(totals):>9,} {max(totals):>8,}")
    print(f"{'EXTRACTIONS RETRIED':<20} {retried:>9}")


# --------------------------------------------------------------------------- #
# Multi-run stability
# --------------------------------------------------------------------------- #


@dataclass
class Unstable:
    """One field instance the extractor did not reproduce across runs."""

    file: str
    path: str
    values: list[Any]


@dataclass
class MultiRunReport:
    """Accuracy and reproducibility measured over several extraction runs."""

    runs: int
    per_run: list[Report] = field(default_factory=list)
    stability: dict[str, FieldScore] = field(default_factory=dict)
    overall_stability: FieldScore = field(default_factory=FieldScore)
    unstable: list[Unstable] = field(default_factory=list)

    def mean(self, getter) -> float:
        return sum(getter(r) for r in self.per_run) / len(self.per_run)

    def worst(self, getter) -> float:
        return min(getter(r) for r in self.per_run)


def collect_values(
    batch: BatchResult, truth_by_file: dict[str, dict]
) -> dict[tuple[str, str], tuple[str, Any]]:
    """Record every scored value in a run, keyed by (file, field path).

    The ground truth supplies the index, so the key set is identical across
    runs even when a run extracts a different number of rows.
    """
    extracted = {r.file: r.invoice for r in batch.results}
    values: dict[tuple[str, str], tuple[str, Any]] = {}

    for file, truth in truth_by_file.items():
        if file not in extracted:
            continue
        actual = extracted[file].model_dump() if extracted[file] else {}
        expected = truth["invoice"]

        for name in SCALAR_FIELDS:
            values[(file, name)] = (name, _normalize(name, actual.get(name)))

        for key, fields in (("line_items", LINE_FIELDS), ("adjustments", ADJUSTMENT_FIELDS)):
            actual_rows = actual.get(key) or []
            values[(file, f"{key} (count)")] = (
                f"{key} (count)",
                len(actual_rows),
            )
            for index in range(len(expected.get(key) or [])):
                row = actual_rows[index] if index < len(actual_rows) else {}
                for name in fields:
                    values[(file, f"{key}[{index}].{name}")] = (
                        name,
                        _normalize(name, row.get(name)),
                    )

    return values


def measure_stability(
    runs: list[dict[tuple[str, str], tuple[str, Any]]],
) -> tuple[dict[str, FieldScore], FieldScore, list[Unstable]]:
    """Score how often every run produced the same value for the same field."""
    per_field: dict[str, FieldScore] = {}
    overall = FieldScore()
    unstable: list[Unstable] = []

    for key in runs[0]:
        field_name = runs[0][key][0]
        observed = [run.get(key, (field_name, None))[1] for run in runs]
        score = per_field.setdefault(field_name, FieldScore())

        score.total += 1
        overall.total += 1
        if all(value == observed[0] for value in observed):
            score.correct += 1
            overall.correct += 1
        else:
            unstable.append(Unstable(file=key[0], path=key[1], values=observed))

    return per_field, overall, unstable


def _normalize(name: str, value: Any) -> Any:
    """Normalize a value the same way scoring does, for like-for-like compares."""
    if value is None:
        return None
    if name in NUMERIC_FIELDS:
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return value
    text = _normalize_text(value)
    return _ID_SEPARATORS.sub("", text) if name == "vendor_tax_id" else text


def run_batches(pdf_dir: Path, runs: int) -> list[BatchResult]:
    """Extract the whole set `runs` times, reusing one extractor."""
    extractor = InvoiceExtractor()
    extractor.settings.require_api_key()

    batches: list[BatchResult] = []
    for index in range(1, runs + 1):
        print(f"run {index}/{runs} ...", file=sys.stderr, flush=True)
        batches.append(process_directory(pdf_dir, extractor=extractor))
    return batches


def print_multi_report(multi: MultiRunReport, *, set_name: str) -> None:
    """Print mean / worst accuracy plus reproducibility across runs."""
    runs = multi.runs
    print("=" * 66)
    print(f"SET: {set_name}   ({runs} runs)")
    print("=" * 66)
    print(f"FIELD ACCURACY AND STABILITY ACROSS {runs} RUNS")
    print("=" * 66)
    print(f"{'FIELD':<28} {'MEAN':>9} {'WORST':>9} {'STABILITY':>12}")
    print("-" * 66)

    def row(label: str, score_of, field_name: str) -> None:
        """One row: mean and worst accuracy across runs, plus stability."""
        scores = [score_of(report) for report in multi.per_run]
        exercised = any(score.total for score in scores)
        mean_text = (
            f"{sum(s.accuracy for s in scores) / len(scores):.1%}" if exercised else "n/a"
        )
        worst_text = f"{min(s.accuracy for s in scores):.1%}" if exercised else "n/a"

        stability = multi.stability.get(field_name)
        stable_text = (
            f"{stability.accuracy:.1%}" if stability and stability.total else "n/a"
        )
        print(f"{label:<28} {mean_text:>9} {worst_text:>9} {stable_text:>12}")

    for name in SCALAR_FIELDS:
        row(name, lambda r, n=name: r.scalar[n], name)
    row("line_items (count)", lambda r: r.line_count, "line_items (count)")
    for name in LINE_FIELDS:
        row(f"line_items[].{name}", lambda r, n=name: r.line_fields[n], name)
    row("adjustments (count)", lambda r: r.adjustment_count, "adjustments (count)")
    for name in ADJUSTMENT_FIELDS:
        row(f"adjustments[].{name}", lambda r, n=name: r.adjustment_fields[n], name)

    print("-" * 66)
    print(
        f"{'OVERALL':<28} {multi.mean(lambda r: r.field_accuracy):>8.1%} "
        f"{multi.worst(lambda r: r.field_accuracy):>9.1%} "
        f"{multi.overall_stability.accuracy:>11.1%}"
    )

    print()
    print("=" * 66)
    print("DEFECT DETECTION")
    print("=" * 66)
    first = multi.per_run[0]
    print(
        f"Expected issues detected: mean "
        f"{multi.mean(lambda r: r.detected_issues):.1f}/{first.expected_issues}, "
        f"worst run {int(multi.worst(lambda r: r.detected_issues))}/"
        f"{first.expected_issues}"
    )
    print(
        f"Status agreement:         mean "
        f"{multi.mean(lambda r: r.status_correct):.1f}/{first.status_total}, "
        f"worst run {int(multi.worst(lambda r: r.status_correct))}/{first.status_total}"
    )
    print(
        f"Unexpected findings:      mean "
        f"{multi.mean(lambda r: len(r.unexpected)):.1f}, worst run "
        f"{int(max(len(r.unexpected) for r in multi.per_run))}"
    )

    print()
    print("=" * 66)
    print("UNSTABLE FIELDS")
    print("=" * 66)
    if not multi.unstable:
        print(f"None. Every field reproduced identically across all {runs} runs.")
    else:
        print(
            f"{len(multi.unstable)} field instance(s) disagreed across runs. "
            "A single-run score on these is luck, not measurement.\n"
        )
        for item in multi.unstable:
            print(f"  {item.file}  {item.path}")
            for index, value in enumerate(item.values, start=1):
                print(f"      run {index}: {value!r}")

    print()
    print("=" * 66)
    print("TOKEN COST")
    print("=" * 66)
    totals = [
        sum(t.total_tokens for t in report.tokens) for report in multi.per_run
    ]
    inputs = [sum(t.input_tokens for t in report.tokens) for report in multi.per_run]
    outputs = [sum(t.output_tokens for t in report.tokens) for report in multi.per_run]
    files = len(multi.per_run[0].tokens)
    print(f"{'PER RUN':<22} {'INPUT':>10} {'OUTPUT':>9} {'TOTAL':>10}")
    print("-" * 66)
    for index, (i, o, t) in enumerate(zip(inputs, outputs, totals), start=1):
        print(f"{'run ' + str(index):<22} {i:>10,} {o:>9,} {t:>10,}")
    print("-" * 66)
    print(f"{'ALL RUNS':<22} {sum(inputs):>10,} {sum(outputs):>9,} {sum(totals):>10,}")
    print(
        f"{'MEAN PER INVOICE':<22} {sum(inputs) // (files * runs):>10,} "
        f"{sum(outputs) // (files * runs):>9,} "
        f"{sum(totals) // (files * runs):>10,}"
    )
    retried = sum(1 for r in multi.per_run for t in r.tokens if t.attempts > 1)
    print(f"{'EXTRACTIONS RETRIED':<22} {retried:>10}")

    print()
    print("=" * 66)
    print(f"{files} files scored, {runs} runs")


def _print_score(label: str, score: FieldScore) -> None:
    # A field the corpus never exercises has no accuracy, which is different
    # from an accuracy of zero.
    accuracy = f"{score.accuracy:.1%}" if score.total else "n/a"
    print(f"{label:<28} {score.correct:>9} {score.total:>7} {accuracy:>10}")


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
        "--set",
        dest="set_name",
        choices=sorted(SETS),
        default="clean",
        help="Which sample set to score (default: clean).",
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=None,
        help="Override the results file for the chosen set.",
    )
    parser.add_argument(
        "--ground-truth",
        type=Path,
        default=None,
        help="Override the ground truth directory for the chosen set.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Extract the set N times and report mean, worst and stability per "
            "field. N=1 (default) scores the existing results file instead. "
            "N>1 calls the API N times and costs N times as much."
        ),
    )
    parser.add_argument(
        "--show-errors",
        action="store_true",
        help="Print every field mismatch with expected and actual values.",
    )
    args = parser.parse_args(argv)

    if args.runs < 1:
        parser.error("--runs must be at least 1")

    default_results, default_truth = SETS[args.set_name]
    results_path = args.results or root / default_results
    truth_dir = args.ground_truth or root / default_truth
    pdf_dir = truth_dir.parent / "pdfs"

    try:
        truths = load_ground_truth(truth_dir)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.runs > 1:
        try:
            batches = run_batches(pdf_dir, args.runs)
        except (ConfigError, NotADirectoryError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        multi = MultiRunReport(
            runs=args.runs,
            per_run=[evaluate(batch, truths) for batch in batches],
        )
        if multi.per_run[0].files_scored == 0:
            print(
                f"error: nothing in {pdf_dir} matched the {args.set_name} corpus.",
                file=sys.stderr,
            )
            return 2

        values = [collect_values(batch, truths) for batch in batches]
        multi.stability, multi.overall_stability, multi.unstable = measure_stability(
            values
        )
        print_multi_report(multi, set_name=args.set_name)
        return 0

    if not results_path.is_file():
        print(
            f"error: {results_path} not found. Run the extractor first:\n"
            f"  python -m invoice_extractor extract {pdf_dir} --out {results_path}",
            file=sys.stderr,
        )
        return 2

    batch = BatchResult.model_validate_json(results_path.read_text(encoding="utf-8"))
    report = evaluate(batch, truths)

    if report.files_scored == 0:
        print(
            f"error: no results in {results_path.name} matched the {args.set_name} "
            "corpus. Did the run point at a different sample directory?",
            file=sys.stderr,
        )
        return 2

    print_report(report, show_errors=args.show_errors, set_name=args.set_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
