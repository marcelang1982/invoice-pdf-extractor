# Invoice PDF Extractor

Turns invoice PDFs into structured, validated data. Claude reads the document;
plain Python decides whether the result is trustworthy.

The split is the point. A language model is good at reading a page and bad at
being a calculator you can audit. So extraction is a single constrained API call
per PDF, and every correctness check — arithmetic, required fields, date
coherence, duplicate detection — runs deterministically afterwards, with no
tokens involved and no run-to-run variance.

**Phase 1 of 4: the extraction and validation engine.** Designed as a reusable
package; later phases (persistence, web UI, workflow automation) import it
rather than reimplement it.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # then add your ANTHROPIC_API_KEY

python scripts/generate_samples.py                                  # 20 sample PDFs
python -m invoice_extractor extract samples/pdfs --out results.json  # extract + validate
python scripts/evaluate.py --results results.json                   # score against ground truth
```

Cheap smoke test before spending tokens on all 20:

```bash
python -m invoice_extractor extract samples/pdfs --limit 2 --out /tmp/smoke.json
```

---

## How extraction works

Each PDF is sent as a base64 `document` content block, paired with **forced tool
use**: one tool, `record_invoice`, whose `input_schema` is generated directly
from the `Invoice` Pydantic model.

```python
tools = [{"name": "record_invoice", "input_schema": Invoice.model_json_schema()}]
tool_choice = {"type": "tool", "name": "record_invoice"}
```

Two consequences worth stating:

- **No JSON scraping.** Claude cannot answer in prose, so there is no
  markdown-fenced JSON to find and no parser to harden against chatty preambles.
- **The schema cannot drift from the model.** The contract Claude is held to and
  the type the response is parsed into are the same object.

Each call records tokens, attempt count, and wall time on the returned
`ExtractionResult`, so cost per invoice is a measurement rather than an estimate.

### Every field is optional, deliberately

`Invoice` makes all fields nullable. That looks lax, but it is what makes the
validator meaningful: an invoice missing its vendor tax ID has to come back as
an invoice with a `None` tax ID, not as a parse failure that aborts the run. The
model describes *what the document contains*; `validator.py` decides whether
that is acceptable.

The extraction prompt reinforces this — Claude is told never to recompute or
reconcile arithmetic. A total that doesn't add up must be transcribed as printed
so the validator can flag it.

---

## Validation rules

All pure Python, in `validator.py`. Any finding flips a file's status from
`valid` to `needs_review`.

| Code | Rule |
| --- | --- |
| `MISSING_REQUIRED_FIELD` | `vendor_name`, `vendor_tax_id`, `invoice_number`, `issue_date`, `currency`, `subtotal`, `total` must all be present |
| `NO_LINE_ITEMS` | The invoice has no billed lines |
| `LINE_ITEM_AMOUNT_MISMATCH` | A line's `amount` != `quantity x unit_price` |
| `LINE_ITEMS_SUM_MISMATCH` | Line amounts don't sum to `subtotal` |
| `TOTAL_MISMATCH` | `subtotal + tax_amount` != `total` |
| `TAX_RATE_OUT_OF_RANGE` | `tax_rate` outside 0–30% |
| `INVALID_DATE_FORMAT` | A date isn't parseable as ISO 8601 |
| `DUE_DATE_BEFORE_ISSUE_DATE` | Payment due before the invoice was issued |
| `DUPLICATE_INVOICE` | Another file in the batch shares the same vendor + invoice number |
| `EXTRACTION_FAILED` | Extraction exhausted its retries for this file |

Money comparisons use a **0.01 tolerance**, since invoices are rounded to cents
and floats are floats. A missing `tax_amount` is read as zero tax rather than
skipped, so `subtotal == total` is still enforced on untaxed invoices.

**Duplicates flag every member of the group**, not just the later ones. From a
batch's point of view there is no principled "original", and a human reviewing
the flag needs to see all the candidates.

---

## The sample corpus

`scripts/generate_samples.py` produces 20 fictitious invoices with reportlab —
no real company, person, or tax ID appears anywhere. Each PDF gets a matching
ground truth JSON in `samples/ground_truth/`.

The corpus is built to resist shortcuts:

- **Four distinct layouts** — `classic` (serif, fully ruled table), `modern_boxed`
  (colored header band, zebra striping), `minimal` (no rules at all),
  `two_column` (vendor/customer side by side).
- **Six vendors, two currencies** (USD and EUR), **1–8 line items**, and tax
  rates from 0% to 21%.
- **A "Bill To" block on every invoice.** The customer is noise the extractor
  must not mistake for the vendor.
- **Mixed date formats.** Two layouts print `2026-02-04`, two print
  `04 Feb 2026`; ground truth is always ISO. The extractor is instructed to
  normalize, so the corpus tests that it does.
- **Deterministic.** Seeded RNG, so regenerating gives byte-identical amounts.
  Quantities stay on half-unit steps and prices on cents, so
  `round(quantity * unit_price, 2)` is exact — the generator never manufactures
  rounding findings it didn't intend.

### Deliberate defects

Five invoices carry an injected defect, declared in their ground truth as
`expected_issues`. The defect is rendered *into the PDF* — the invoice with a
broken total genuinely shows the wrong number on the page.

| Sample | Defect | Expected finding |
| --- | --- | --- |
| 05 | Total doesn't add up (inflated by 40.00) | `TOTAL_MISMATCH` |
| 09 | Vendor tax ID absent from the document | `MISSING_REQUIRED_FIELD` |
| 12 | Due date 10 days before issue date | `DUE_DATE_BEFORE_ISSUE_DATE` |
| 16 | Tax rate of 87.5% (arithmetic kept self-consistent) | `TAX_RATE_OUT_OF_RANGE` |
| 18 | Sample 03 re-issued under a different layout | `DUPLICATE_INVOICE` |

That is 5 defects across **6 flagged files** — the duplicate implicates both
sample 18 and sample 03.

---

## CLI

```
python -m invoice_extractor extract <directory> [options]

  --out PATH      Where to write JSON results (default: results.json)
  --model NAME    Override the model from .env
  --limit N       Process at most N files
  --quiet         Suppress per-file progress
```

Progress goes to stderr, the summary table to stdout, so the table can be piped
on its own. Exit code is `1` if any file failed extraction outright, `0`
otherwise — `needs_review` is a finding, not an error.

```
FILE             STATUS        TOKENS  ISSUES
--------------------------------------------------------
invoice_01.pdf   valid           2314  -
invoice_05.pdf   needs_review    2380  TOTAL_MISMATCH
...
```

---

## Evaluation

`scripts/evaluate.py` compares `results.json` against the ground truth and
reports two things:

- **Field accuracy** — per-field correct/total across the corpus, so you can see
  *which* fields the model gets wrong rather than a single opaque score.
  Comparison is normalized: whitespace and case for strings, 0.01 tolerance for
  money, ISO parsing for dates.
- **Defect detection** — recall on the expected issues, plus false positives
  (findings on invoices that should be clean). A validator that flags everything
  would score 100% recall; the false-positive count is what keeps it honest.

```bash
python scripts/evaluate.py --results results.json
python scripts/evaluate.py --results results.json --show-errors   # per-field diffs
```

---

## Layout

```
src/invoice_extractor/
  models.py      Invoice, LineItem, ValidationResult, batch result types
  extractor.py   PDF -> Invoice via base64 document block + forced tool use
  validator.py   Deterministic rules + batch duplicate detection
  pipeline.py    Extract-then-validate over a batch; the engine's entry point
  cli.py         python -m invoice_extractor extract ...
  config.py      Settings from .env
scripts/
  generate_samples.py   20 sample PDFs + ground truth
  evaluate.py           Score results against ground truth
tests/           Validator covered rule by rule; extractor fully mocked
```

### Using it as a library

```python
from invoice_extractor import InvoiceExtractor, process_directory, validate_invoice

batch = process_directory("samples/pdfs")
for result in batch.results:
    print(result.file, result.status, [i.code for i in result.issues])
print(batch.totals.total_tokens)
```

---

## Reliability

- **Retries** — 3 attempts with 1s/2s/4s backoff on connection errors, timeouts,
  rate limits, and 5xx. Also retried: a response that somehow arrives without
  the tool call, or whose payload fails schema validation, since a second
  attempt often succeeds. Token usage accumulates across attempts — a failed
  attempt that reached the API was still billed.
- **Failures are contained.** `extract_file` never raises for an extraction
  failure; the error lands on the result object so one unreadable PDF doesn't
  take down a 500-file batch. Those files come back as `needs_review` with
  `EXTRACTION_FAILED`.
- **The SDK's own retry layer is disabled** (`max_retries=0`) so backoff isn't
  applied twice.

## Testing

```bash
pytest
```

Tests never touch the network. The extractor is tested against a fake client
that returns canned `tool_use` responses, which also lets retry behavior and
token accounting be asserted directly. Backoff sleeps are injected, so the
suite runs instantly.

---

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | Required to extract |
| `MODEL` | `claude-haiku-4-5` | Any PDF-capable Claude model |
| `MAX_RETRIES` | `3` | Attempts per PDF |
| `MAX_TOKENS` | `4096` | Output cap; raise for very long invoices |

`.env` is gitignored. Existing environment variables take precedence over
`.env`, so CI and one-off overrides work without editing the file.

## Roadmap

| Phase | Scope |
| --- | --- |
| **1** | Extraction + validation engine (this repo) |
| 2 | Persistence and query layer |
| 3 | Web interface for review and correction |
| 4 | Workflow automation and integrations |

## License

MIT. All sample data is fictitious.
