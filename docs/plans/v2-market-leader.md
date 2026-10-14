# Plan: V2 From a demo to the best purchase-entry solution for Indian pharma

Planned 2026-10-14 from four research passes made that day (competitor pricing, open OCR
models, GPU hosting, the Indian pharma market) and the production numbers. Owner approval
2026-10-14: "start using recommendations"; the owner provides real invoices, user interviews
and Marg export files, will create the GPU account later, and allowed reading production data.

## Where we start (measured)

- Production, 73 readings 2026-10-09..11: mean 1,273 input and 2,679 output tokens per call
  (p90 output 6,676). At the configured price ($0.30 / $2.50 per million) that is about
  **$0.007 per reading, ~95% of it output tokens**.
- Evals (generated invoices): ~$0.014 per single-page invoice; multi-page ~$0.05 per page,
  p50 140 s, 2 of 3 documents fully correct.
- Cause of the output size: every field is returned as `{"text": ..., "block_ids": [...]}`,
  15 fields per pharma line.
- Accuracy is only proven on invoices our own generator made (76/76 gate).

## Positioning

Not "OCR". **Verified purchase entry for Indian pharma**: any bill in any format (e-invoice QR,
distributor CSV/Excel, Marg export, digital PDF, scan, photo, WhatsApp, email) becomes a checked
entry - pharma rules, PO, duplicates, expiry, MRP - posted to Marg or Tally, with evidence and an
audit trail. Many distributor-to-chemist bills already move as files (Marg Purchase Import,
Retailio, Pharmarack, eVitalRx), so OCR is a fallback, not the headline.

What we will not claim: certifications (SOC 2, ISO 27001) or enterprise scale. Those stay gaps.

## Rules for every phase (so nothing is forgotten or broken)

1. **One branch and one PR per phase step**; merged only on the owner's explicit word.
2. **Tests first** (RED, then GREEN commits), as in C0-C16.
3. **Nothing downstream changes shape without a migration and a test.** New behaviour sits
   behind a setting or a version (`prompt_version`, `schema_version`) so a reading made before
   the change still opens, re-reads and exports.
4. **The regression checklist** below passes before a PR is opened, and again on the live site
   after deploying.
5. **ECC reviews** on every PR: python-reviewer, database-reviewer (when SQL or migrations
   change), security-reviewer (when input, auth or a new integration changes), typescript or
   react reviewer (web). Every CRITICAL and HIGH is fixed before merging.
6. **Model spend**: live eval runs are budgeted per step and recorded in the PR.
7. This file is updated as each step lands (status column), and `docs/progress.md` gets a line.

### Regression checklist

| Check | Command | Must be |
|---|---|---|
| Lint, format, types | `make lint` | clean |
| Unit and integration tests | `make test` (with `make up`) | all pass |
| Eval replay + gate | `make eval && make gate` | every floor met; invoice, scans, multipage, trust, coa, search, answers, mcp |
| Web | `npm run check` in docforge-web | types, lint, tests, build |
| Demo stack end to end | `make verify` | all pass |
| Live after deploy | the isolation e2e script (sign-up, upload, read, review, correct, sign, export, delete, account delete) | all pass, no 500 in logs |
| Cost | production `model_runs` average output tokens | not higher than before the step |

## Phase 0 - Know where we stand (1-2 weeks)

| Id | Step | Done when | Status |
|---|---|---|---|
| 0.1 | **Compact model output**: the model replies in a short wire format (each field one string `"<printed text>\|<block ids>"`, value first); code decodes it into the existing `Raw*` models, so normalising, rules, corrections, merge and storage do not change. Invoice first, then purchase order and CoA. | Eval gate passes on re-recorded live runs; output tokens per invoice down at least 2x; old readings still open and re-read | in progress |
| 0.2 | **Multi-page reading**: why 140 s and 2/3; fix (smaller paged replies come with 0.1, then the merge and the failing document) | multipage eval 3/3, p50 latency at least halved | |
| 0.3 | **Cost view from production**: real cost per page and per document type in the admin dashboard, plus fixed monthly infrastructure entered by hand | admin page shows it; numbers match `model_runs` | |
| 0.4 | **Real-invoice test set harness** (built: `evals/real.py`): `data/real/` (ignored by git), a labelling format and a scorer per field (exact match for GSTIN, HSN, batch, expiry, MRP, PTR, totals; row accuracy for lines), report kept outside the repo; only aggregate numbers are committed | harness runs on any folder of labelled PDFs; ready for the owner's invoices | |
| 0.5 | Run the real set when the owner's invoices arrive; publish the numbers | report with per-field accuracy | waiting on owner |

## Phase 1 - New reading engine (3-4 weeks)

| Id | Step | Done when |
|---|---|---|
| 1.1 | **E-invoice QR**: find the QR on the page, decode it, verify the JWT signature (RS256) against the NIC public keys, and use its GSTINs, invoice number, date, total, item count and IRN as verified values; a mismatch with the printed values is a rule failure | tests with genuine and tampered QR codes; values marked "verified from e-invoice QR" in review |
| 1.2 | **Router**: e-invoice QR -> text layer (Docling, no OCR) -> OCR model for scans and photos; the route is recorded on each reading | every route has a test; the reading shows its route |
| 1.3 | **OCR model service** (needs the owner's GPU account): PaddleOCR-VL-1.6 on Modal (L4, GPU snapshot), called by the worker in batches; the current RapidOCR path stays as the fallback when the service is down | scans eval at least equal to today; p50 under 5 s per page |
| 1.4 | **Bake-off on the real set**: today's path vs PaddleOCR-VL-1.6 vs Qianfan-OCR (direct to JSON) vs Unlimited-OCR (long documents only) | a written decision with per-field numbers and cost per 1,000 pages |

## Phase 2 - Every format in, posted where customers work (4-5 weeks)

| Id | Step | Done when |
|---|---|---|
| 2.1 | Distributor CSV / Excel bills and Marg export files as input (needs the owner's sample files), with a column mapping kept per supplier | the owner's samples import with no manual mapping after the first |
| 2.2 | Email-in (a per-workspace address) | a forwarded PDF becomes a document |
| 2.3 | WhatsApp intake (a photo or PDF to a business number) | a sent bill becomes a document; reply says what happened |
| 2.4 | Tally Prime posting: a small agent on the customer's PC posts purchase vouchers over Tally's XML interface (port 9000), with ledger and item mapping | a signed reading appears as a voucher in a test Tally company |
| 2.5 | Marg purchase import file, Busy Excel/XML, Zoho Books API (in that order) | each produces a file or call the target accepts |

## Phase 3 - Pharma checks nobody else does (3-4 weeks)

| Id | Step |
|---|---|
| 3.1 | Scheme and free quantity checked against the terms agreed with each supplier |
| 3.2 | Bill against purchase order line by line (builds on PO v2) |
| 3.3 | Duplicate bills by IRN, or by GSTIN + invoice number |
| 3.4 | Near-expiry flag at receipt (a workspace setting, e.g. under 6 months) |
| 3.5 | MRP against NPPA ceiling prices for scheduled drugs (a curated, dated list) |
| 3.6 | Schedule H1 items flagged on the purchase bill |
| 3.7 | Supplier memory: corrections per supplier make the next bill from them more accurate |
| 3.8 | GSTR-2B matching through a GSP with the taxpayer's OTP consent (cost quoted first) |

## Phase 4 - Lead the niche (ongoing)

- Fine-tune PaddleOCR-VL or Qwen3-VL-2B on corrected readings (only with each workspace's
  consent); the correction data is the lasting advantage.
- India hosting option (Jarvislabs / E2E for the GPU); DPDP processor agreement before
  13 May 2027; written security policies so a SOC 2 audit is possible later.
- Pricing: about INR 299-499 a month for chemists, INR 1,500-3,000 for distributors and CA
  firms, by volume.

## Model choice (research 2026-10-14)

| Model | Why | Role |
|---|---|---|
| PaddleOCR-VL-1.6 (Baidu, 0.9B, Apache-2.0) | OmniDocBench v1.6 96.33, ~2 GB VRAM, Hindi | first OCR candidate |
| Qianfan-OCR (Baidu, 4B, Apache-2.0) | reads key-value fields straight to JSON | challenger to OCR + Gemini |
| Unlimited-OCR (Baidu, 3B MoE, MIT) | one pass over dozens of pages; v1.6 93.92 | long documents only; open bugs on repetition and rotated pages, no Indic claim, special vLLM image |
| Gemini 3.5 Flash-Lite | today's extractor | stays for the JSON step until a local model beats it |

Hosting: Railway has no GPUs. Modal L4 with GPU snapshots (about $0.0003/s, $30 a month free)
or RunPod serverless, with pages sent in batches; about $0.10-0.60 per 1,000 pages batched.

## Owner inputs

| Input | Needed for | Status |
|---|---|---|
| 100-200 real invoices (redacted if needed) | 0.5, 1.4 | owner collecting |
| 3-5 interviews (distributor, chemist, CA) | pricing, 2.x order | owner |
| Marg export files | 2.1, 2.5 | owner |
| GPU account (Modal) | 1.3 | later |
| Production read access | 0.3 | granted 2026-10-14 |

## How to run the real-invoice set (step 0.4, for the owner)

Everything below stays on this machine under `data/real/` (ignored by git).

1. Put the PDFs in `data/real/pdfs/` with distinct file names.
2. Upload the same files to a workspace where you are an administrator (your own `kartik`
   organisation; a self-service sign-up is a member and cannot make API keys).
3. In review, check **every** field of each invoice against the paper, correct what is wrong
   (lines included), and approve. A rejected invoice is not used.
4. On the Agents page make a read-only API key, then:

   ```
   export DOCFORGE_API_KEY=...   # read from the environment, never typed as an argument
   uv run python -m docforge.evals.real pull --api https://<the API's address>
   uv run python -m docforge.evals.real run            # reads each PDF live (Gemini key in .env)
   uv run python -m docforge.evals.real run --mode replay   # again later, free, from recordings
   ```

5. `data/real/report.json` has every field with the bills' values (private);
   `data/real/summary.json` has counts only and is the part to share or commit.
