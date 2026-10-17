"""Command-line interface.

    python -m invoice_extractor extract samples/pdfs --out results.json

Progress goes to stderr and the summary table to stdout, so the table can be
piped or redirected on its own.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError, Settings
from .extractor import InvoiceExtractor, find_pdfs
from .models import BatchResult, ExtractionResult
from .pipeline import process_paths

DEFAULT_OUTPUT = Path("results.json")


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "extract":
        return _run_extract(args)

    parser.print_help()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m invoice_extractor",
        description="Extract and validate structured data from invoice PDFs.",
    )
    subparsers = parser.add_subparsers(dest="command")

    extract = subparsers.add_parser(
        "extract", help="Extract every PDF in a directory and validate the batch."
    )
    extract.add_argument("directory", type=Path, help="Directory holding the PDFs.")
    extract.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Where to write the JSON results (default: {DEFAULT_OUTPUT}).",
    )
    extract.add_argument(
        "--model",
        default=None,
        help="Override the model from the environment, e.g. claude-haiku-5-5.",
    )
    extract.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N files. Useful for a cheap smoke test.",
    )
    extract.add_argument(
        "--quiet", action="store_true", help="Suppress per-file progress on stderr."
    )

    return parser


def _run_extract(args: argparse.Namespace) -> int:
    """Handle `extract`: process the directory, print a table, write JSON."""
    try:
        paths = find_pdfs(args.directory)
    except NotADirectoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not paths:
        print(f"error: no PDFs found in {args.directory}", file=sys.stderr)
        return 2

    if args.limit is not None:
        paths = paths[: args.limit]

    settings = Settings.from_env()
    if args.model:
        settings = replace_model(settings, args.model)

    try:
        extractor = InvoiceExtractor(settings=settings)
        # Fail fast on a missing key instead of after the first API error.
        settings.require_api_key()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    total = len(paths)
    counter = {"done": 0}

    def report(result: ExtractionResult) -> None:
        counter["done"] += 1
        if args.quiet:
            return
        state = "ok" if result.succeeded else "FAILED"
        print(
            f"[{counter['done']}/{total}] {result.file} {state} "
            f"({result.usage.total_tokens} tokens, {result.duration_ms} ms)",
            file=sys.stderr,
        )

    batch = process_paths(paths, extractor=extractor, on_result=report)

    print_summary(batch)
    _write_json(batch, args.out)
    print(f"\nWrote {args.out}", file=sys.stderr)

    return 1 if batch.totals.failed else 0


def replace_model(settings: Settings, model: str) -> Settings:
    """Return a copy of `settings` with a different model."""
    return Settings(
        api_key=settings.api_key,
        model=model,
        max_retries=settings.max_retries,
        max_tokens=settings.max_tokens,
    )


def print_summary(batch: BatchResult, stream=None) -> None:
    """Print the per-file table, issue details, and batch totals."""
    stream = stream or sys.stdout

    name_width = max([len("FILE")] + [len(r.file) for r in batch.results])
    status_width = max(len("needs_review"), len("STATUS"))

    header = (
        f"{'FILE':<{name_width}}  {'STATUS':<{status_width}}  "
        f"{'TOKENS':>7}  ISSUES"
    )
    print(header, file=stream)
    print("-" * len(header), file=stream)

    for result in batch.results:
        codes = ", ".join(issue.code.value for issue in result.issues) or "-"
        print(
            f"{result.file:<{name_width}}  {result.status:<{status_width}}  "
            f"{result.usage.total_tokens:>7}  {codes}",
            file=stream,
        )

    adjusted = [r for r in batch.results if r.totals_path == "adjusted"]
    if adjusted:
        print(
            "\nTotals reconciled through document-level adjustments: "
            + ", ".join(r.file for r in adjusted),
            file=stream,
        )

    flagged = [r for r in batch.results if r.issues]
    if flagged:
        print("\nDetails:", file=stream)
        for result in flagged:
            print(f"\n  {result.file}", file=stream)
            for issue in result.issues:
                location = f" [{issue.field}]" if issue.field else ""
                print(f"    - {issue.code.value}{location}: {issue.message}", file=stream)
            if result.error:
                print(f"    - error: {result.error}", file=stream)

    totals = batch.totals
    print(
        f"\n{totals.files} files | {totals.valid} valid | "
        f"{totals.needs_review} needs_review | {totals.failed} failed",
        file=stream,
    )
    print(
        f"Tokens: {totals.input_tokens} in / {totals.output_tokens} out "
        f"({totals.total_tokens} total) | model: {batch.model}",
        file=stream,
    )


def _write_json(batch: BatchResult, path: Path) -> None:
    """Serialize a batch result to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(batch.model_dump_json(indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
