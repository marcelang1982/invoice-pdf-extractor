# Invoice PDF Extractor

**Invoice PDFs from email → clean, validated data, with every exception flagged for review.**

*Built with Python, Pydantic and the Claude API.*

<!--
  DEMO PLACEHOLDER — replace this block with a GIF or screenshot.
  Suggested recording: run `python -m invoice_extractor extract samples/hard/pdfs`
  and capture the terminal as the summary table fills in, showing a few invoices
  marked `valid` and the flagged ones with their issue codes.
  Save as docs/demo.gif and uncomment the line below.
-->
<!-- ![Demo](docs/demo.gif) -->

---

## What it does

- **Reads invoice PDFs**, including scans with no text layer, invoices that run onto a second page, and layouts it has never seen before.
- **Returns structured data**: vendor, tax ID, invoice number, dates, currency, every line item, subtotal, tax and total.
- **Checks the arithmetic independently.** Line items are re-added, tax is recomputed, totals are reconciled. This runs in plain Python, not in the AI model, so the same invoice always produces the same verdict.
- **Flags exceptions instead of guessing.** A total that doesn't add up, a missing tax ID, a due date before the issue date, or the same invoice submitted twice — each is reported with a specific code and a plain-language explanation.
- **Reports what it cost.** Tokens and processing time are recorded per invoice, so the running cost is measured rather than estimated.

Nothing is silently corrected. If the document is wrong, the output says the document is wrong.

---

## Results

Measured over **3 full runs of each set**, because a single run can get an ambiguous field right by luck. "Worst" is the weakest of the three runs; "stability" is how often all three runs returned an identical value.

| | Clean set (20 invoices) | Hard set (11 invoices) |
| --- | --- | --- |
| Field accuracy — mean | **100.0%** | **100.0%** |
| Field accuracy — worst run | **100.0%** | **100.0%** |
| Stability across runs | **100.0%** | **100.0%** |
| Exceptions correctly caught | **6 / 6** | **3 / 3** |
| False alarms | **0** | **0** |
| Avg. tokens per invoice | 5,598 | 5,964 |
| Avg. cost per invoice | **≈ $0.0008** | **≈ $0.0009** |
| Full report | [clean_runs3.txt](reports/clean_runs3.txt) | [hard_runs3.txt](reports/hard_runs3.txt) |

**≈ $0.0008–0.0009 per invoice (under $1 per 1,000 invoices)** at Claude Haiku 5.5 list prices, Oct 2026.

Accuracy is counted field by field — 560 individual comparisons per run on the clean set, 393 on the hard set — not per document, so one bad line item cannot hide behind a correct header.

A 100% score on a synthetic corpus shows the pipeline works end to end — extraction, validation and scoring all hold together. It is not a forecast for your documents. Read [Honest limitations](#honest-limitations) before expecting the same numbers on real invoices.

The reports are the raw output of the scoring script, committed so the numbers can be checked rather than taken on trust.

---

## What the hard set covers

The clean set is what invoices look like in a demo: generated digitally, neatly typeset, one page. A perfect score there proves very little. The hard set is what actually arrives in an inbox:

| | What it tests |
| --- | --- |
| **Scanned documents** | Rasterized, slightly out of focus, rotated a degree or two, speckled and JPEG-compressed, with **no text layer at all** — the content has to be read visually, like a photo |
| **Multi-page invoices** | The item table continues onto page 2, with totals only at the very end |
| **Wrapped descriptions** | Long item descriptions split across two or three lines inside the same table cell |
| **Disordered layouts** | Totals printed at the top of the page, item tables with no borders |
| **Discounts and shipping** | The same discount and shipping charge printed two ways: as rows in the item table, and in the totals block where they change what the tax is actually calculated on |
| **Argentine Factura A** | Spanish labels, CUIT, `dd/mm/yyyy` dates, `1.234,56` comma decimals, and invoice numbers split across two fields (`Punto de Venta` + `Comp. Nro`) that have to be recomposed as `0007-00002391` |

Three of these invoices carry a deliberate error, so the checks are measured on documents where something really is wrong.

---

## How validation works

Extraction uses an AI model. **Validation does not.** Every check below is ordinary Python: no model call, no variability, no per-check cost.

| Check | What it catches |
| --- | --- |
| Required fields | A missing vendor, tax ID, invoice number, date, currency or total |
| Line item maths | A line whose amount isn't quantity × unit price |
| Subtotal | Line items that don't add up to the stated subtotal |
| Totals | Subtotal and tax that don't reach the stated total |
| Tax rate | A rate outside a plausible 0–30% |
| Dates | A payment due date that precedes the issue date |
| Duplicates | The same vendor and invoice number appearing twice in one batch, or matching an invoice already stored. Rejected and failed invoices are not treated as originals, so a resubmission after a rejection passes cleanly |

### Totals are reconciled two ways

Not every invoice charges tax on its subtotal. When an order-level discount or shipping charge is printed in the totals block, it changes the base the tax applies to — and a perfectly correct invoice would fail a naive `subtotal + tax = total` check.

So totals are reconciled in two passes:

1. **Direct** — `subtotal + tax = total`. The ordinary case.
2. **Adjusted** — only attempted when the invoice actually carries discounts, shipping or a stated taxable base. All three must hold: the adjustments reach the taxable base, the tax matches the rate applied to that base, and base plus tax reaches the total.

If neither closes, the invoice is flagged, and the message explains what both passes tried. The output records which pass succeeded, so a reviewer can see *why* an unusual invoice was accepted.

---

## Honest limitations

- **The sample corpus is synthetic.** Every invoice here was generated for testing — no real company, person or tax ID appears anywhere. The "scanned" invoices are controlled degradations of clean renders: blur, rotation, grain and JPEG artefacts applied deliberately. That is harder than a clean PDF and genuinely exercises visual reading, but it is **not** the same as a photo of creased paper under uneven light, or a document that has been through a fax and two photocopiers. **These numbers should be re-measured on your real invoices before being relied on.**
- **One tax rate per invoice.** An invoice charging 21% on some lines and 10.5% on others cannot currently be represented. This is common on Argentine Factura A and on EU invoices mixing standard and reduced rates.
- **Argentine CUITs in the samples deliberately fail the check digit**, so they cannot belong to a real company. A test enforces this for every CUIT in the generators.
- **No handwriting.** Handwritten amounts, annotations and signatures are out of scope.
- **One currency per invoice**, taken as printed. No exchange-rate conversion.
- **No withholdings or retentions** (IIBB, Ganancias), and no line-level tax.
- **Results vary between AI models.** The figures above are for one specific model; a cheaper or more expensive one will shift both accuracy and cost.

---

## Run the web app

Phase 2 adds a database and a browser interface on top of the engine: upload
PDFs, watch them process, review what was flagged, and export the result.

```bash
cp .env.example .env        # then edit .env and fill in ANTHROPIC_API_KEY
docker compose up --build
```

Then open `http://127.0.0.1:8000`. Click **Load sample invoices** to run the
31 bundled samples through it, or drop your own PDFs on the upload screen.

### Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | Required. Read from `.env`. |
| `MODEL` | `claude-haiku-5-5` | Any PDF-capable Claude model. |
| `APP_PORT` | `8000` | Host port the app is published on, always bound to `127.0.0.1`. Set it if something else already has 8000: `APP_PORT=8001 docker compose up --build`. |
| `EXTRACTION_CONCURRENCY` | `4` | How many PDFs are sent to the API at once. Uploading a folder queues the rest, which stay visible as `processing` until a slot frees. Raise it for throughput, lower it to stay inside a rate limit. |

| Screen | What it does |
| --- | --- |
| Upload | Drag and drop several PDFs at once. Each is extracted in the background; the list updates itself as results arrive. |
| Invoices | Every invoice with its status, review state and findings. Filter by either, and export what you are looking at as CSV. |
| Detail | The PDF beside the data pulled out of it, with each finding highlighted on the field it concerns. Approve or reject, with an optional note. |

Two verdicts are tracked separately. **Status** is the machine's: `valid`,
`needs_review` or `failed`. **Review** is yours: `pending`, `approved` or
`rejected`. Approving a flagged invoice records your decision without pretending
the arithmetic now adds up, so the next person still sees why it was flagged.

Re-uploading a PDF that is already stored does not re-extract it, so pointing
the uploader at the same folder twice is safe and costs nothing.

**There is no authentication.** The port is published on `127.0.0.1` only, so
the app is reachable from your own machine and nowhere else. Putting it on a
network means putting a login in front of it first.

---

## Data privacy

Invoice PDFs are sent to the Anthropic API for the extraction step. Everything after that — validation, scoring, results and any storage you add — runs on your own infrastructure.

If your invoices contain sensitive data, review Anthropic's commercial data terms before sending real documents. If they cannot leave your network at all, tell me and we'll look at the options.

---

## Roadmap

| Phase | Scope | Status |
| --- | --- | --- |
| **1** | Extraction and validation engine | **Done** |
| **2** | Database plus a web interface for uploading and reviewing | **Done** |
| 3 | Automation: n8n reading invoice attachments straight from Gmail | Planned |
| — | Multiple tax rates on a single invoice | Not scheduled |

---

## Quickstart

```bash
# 1. Set up
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Add your API key
cp .env.example .env        # then edit .env and fill in ANTHROPIC_API_KEY

# 3. Generate the sample invoices
python scripts/generate_samples.py          # 20 clean invoices
python scripts/generate_hard_samples.py     # 11 hard invoices

# 4. Extract and validate
python -m invoice_extractor extract samples/pdfs --out results.json

# 5. Score the result against known-correct data
python scripts/evaluate.py --set clean
```

To try the hard set instead:

```bash
python -m invoice_extractor extract samples/hard/pdfs --out results_hard.json
python scripts/evaluate.py --set hard --show-errors
```

To measure stability the way the results table does, add `--runs 3`. Each run calls the API, so three runs cost three times as much:

```bash
python scripts/evaluate.py --set hard --runs 3
```

Point it at your own invoices by passing any folder of PDFs:

```bash
python -m invoice_extractor extract /path/to/your/invoices --out results.json
```

Run the test suite — 131 tests, none of which call the API — with `pytest`.

---

## Need this for your invoices?

This is a working engine rather than a finished product, and adapting it is the point. The usual work is supporting the specific formats your suppliers actually send, exporting straight into your accounting system or ERP, and reading invoices automatically from your inbox instead of by hand.

Contact me on Upwork: https://www.upwork.com/freelancers/~01037b3ffff940717b

Send me a few sample invoices and the system you need the data in, and I'll reply with how I'd adapt it.

---

All rights reserved. This repository is a portfolio demo; the code is shared for review only.

All sample data is fictitious.
