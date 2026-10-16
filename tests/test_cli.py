"""CLI wiring: argument handling, the summary table, and the JSON artifact."""

from __future__ import annotations

import io

import pytest

from invoice_extractor import cli
from invoice_extractor.config import Settings
from invoice_extractor.models import (
    BatchResult,
    BatchTotals,
    FileResult,
    Invoice,
    IssueCode,
    TokenUsage,
    ValidationIssue,
)

#: Captured before any monkeypatching, so the stub below can delegate to the
#: real implementation without recursing into itself.
_REAL_FROM_ENV = Settings.from_env


@pytest.fixture
def batch() -> BatchResult:
    """A two-file batch: one clean, one flagged."""
    return BatchResult(
        model="claude-haiku-5-5",
        results=[
            FileResult(
                file="invoice_01.pdf",
                status="valid",
                invoice=Invoice(vendor_name="Northwind Logistics LLC"),
                usage=TokenUsage(input_tokens=2000, output_tokens=300),
            ),
            FileResult(
                file="invoice_05.pdf",
                status="needs_review",
                issues=[
                    ValidationIssue(
                        code=IssueCode.TOTAL_MISMATCH,
                        field="total",
                        message="Subtotal plus tax is 100.00, but total is 140.00.",
                    )
                ],
                invoice=Invoice(vendor_name="Helvetia Cloud Systems AG"),
                usage=TokenUsage(input_tokens=2100, output_tokens=320),
            ),
        ],
        totals=BatchTotals(
            files=2, valid=1, needs_review=1, input_tokens=4100, output_tokens=620
        ),
    )


@pytest.fixture
def pdf_dir(tmp_path):
    """A directory of placeholder PDFs; extraction itself is stubbed out."""
    for name in ("invoice_01.pdf", "invoice_05.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-1.4")
    return tmp_path


def run_cli(monkeypatch, argv, batch, env_key="test-key"):
    """Run `main` with extraction stubbed and the API key supplied.

    Settings are read without loading `.env`, so a developer's real credentials
    can never leak into a test run or change its outcome.
    """
    if env_key is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", env_key)

    monkeypatch.setattr(
        cli.Settings, "from_env", classmethod(lambda cls: _REAL_FROM_ENV(load=False))
    )

    captured: dict = {}

    def fake_process(paths, extractor=None, on_result=None):
        captured["paths"] = list(paths)
        captured["model"] = extractor.settings.model if extractor else None
        return batch

    monkeypatch.setattr(cli, "process_paths", fake_process)
    code = cli.main(argv)
    return code, captured


# --------------------------------------------------------------------------- #
# Summary table
# --------------------------------------------------------------------------- #


def test_table_lists_every_file_with_status_and_issues(batch):
    stream = io.StringIO()

    cli.print_summary(batch, stream=stream)
    output = stream.getvalue()

    assert "invoice_01.pdf" in output
    assert "invoice_05.pdf" in output
    assert "valid" in output
    assert "needs_review" in output
    assert "TOTAL_MISMATCH" in output


def test_clean_files_show_a_dash_instead_of_issue_codes(batch):
    stream = io.StringIO()

    cli.print_summary(batch, stream=stream)
    row = next(
        line for line in stream.getvalue().splitlines() if "invoice_01.pdf" in line
    )

    assert row.rstrip().endswith("-")


def test_issue_messages_appear_in_the_details_section(batch):
    stream = io.StringIO()

    cli.print_summary(batch, stream=stream)
    output = stream.getvalue()

    assert "Details:" in output
    assert "but total is 140.00" in output


def test_totals_line_reports_counts_tokens_and_model(batch):
    stream = io.StringIO()

    cli.print_summary(batch, stream=stream)
    output = stream.getvalue()

    assert "2 files | 1 valid | 1 needs_review | 0 failed" in output
    assert "4100 in / 620 out" in output
    assert "claude-haiku-5-5" in output


# --------------------------------------------------------------------------- #
# extract command
# --------------------------------------------------------------------------- #


def test_extract_writes_the_results_json(monkeypatch, tmp_path, pdf_dir, batch):
    out = tmp_path / "out" / "results.json"

    code, _ = run_cli(monkeypatch, ["extract", str(pdf_dir), "--out", str(out)], batch)

    assert code == 0
    restored = BatchResult.model_validate_json(out.read_text())
    assert [r.file for r in restored.results] == ["invoice_01.pdf", "invoice_05.pdf"]


def test_extract_passes_the_sorted_pdfs_through(monkeypatch, tmp_path, pdf_dir, batch):
    _, captured = run_cli(
        monkeypatch,
        ["extract", str(pdf_dir), "--out", str(tmp_path / "r.json")],
        batch,
    )

    assert [p.name for p in captured["paths"]] == ["invoice_01.pdf", "invoice_05.pdf"]


def test_limit_truncates_the_batch(monkeypatch, tmp_path, pdf_dir, batch):
    _, captured = run_cli(
        monkeypatch,
        ["extract", str(pdf_dir), "--out", str(tmp_path / "r.json"), "--limit", "1"],
        batch,
    )

    assert len(captured["paths"]) == 1


def test_model_flag_overrides_the_environment(monkeypatch, tmp_path, pdf_dir, batch):
    monkeypatch.setenv("MODEL", "claude-haiku-5-5")

    _, captured = run_cli(
        monkeypatch,
        ["extract", str(pdf_dir), "--out", str(tmp_path / "r.json"),
         "--model", "claude-sonnet-5"],
        batch,
    )

    assert captured["model"] == "claude-sonnet-5"


def test_failed_extractions_set_a_nonzero_exit_code(
    monkeypatch, tmp_path, pdf_dir, batch
):
    """`needs_review` is a finding; a failed extraction is an error."""
    batch.totals.failed = 1

    code, _ = run_cli(
        monkeypatch, ["extract", str(pdf_dir), "--out", str(tmp_path / "r.json")], batch
    )

    assert code == 1


def test_needs_review_alone_still_exits_zero(monkeypatch, tmp_path, pdf_dir, batch):
    code, _ = run_cli(
        monkeypatch, ["extract", str(pdf_dir), "--out", str(tmp_path / "r.json")], batch
    )

    assert code == 0


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


def test_missing_directory_is_reported(monkeypatch, tmp_path, batch, capsys):
    code, _ = run_cli(monkeypatch, ["extract", str(tmp_path / "nope")], batch)

    assert code == 2
    assert "error" in capsys.readouterr().err


def test_empty_directory_is_reported(monkeypatch, tmp_path, batch, capsys):
    code, _ = run_cli(monkeypatch, ["extract", str(tmp_path)], batch)

    assert code == 2
    assert "no PDFs found" in capsys.readouterr().err


def test_missing_api_key_fails_before_any_extraction(
    monkeypatch, tmp_path, pdf_dir, batch, capsys
):
    code, captured = run_cli(
        monkeypatch,
        ["extract", str(pdf_dir), "--out", str(tmp_path / "r.json")],
        batch,
        env_key=None,
    )

    assert code == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert "paths" not in captured


def test_no_subcommand_prints_help(capsys):
    assert cli.main([]) == 2
    assert "usage" in capsys.readouterr().out
