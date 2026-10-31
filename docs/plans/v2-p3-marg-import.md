# V2 P3: Marg purchase import (format-profile engine)

Branch `v2-p3-marg-profiles` (stacked on `v2-p1-mappings`: migration 0045 follows P1's 0044).
P2's Tally XML is still waiting for its real Tally sample; the pre-flight it needs is built
here, once, for both.

## No Marg layout has been verified

**No real Marg import file has been supplied, and no file from DocForge has been imported into
Marg.** Following the task's one rule, no Marg column layout is written anywhere. What is built
is the engine, the Settings > Marg profile screen, and one profile labelled **SAMPLE** - a
made-up layout over made-up bills (`tests/fixtures/profiles/`), there to exercise the engine.
Its files are named `SAMPLE-docforge-marg-import-...` and the screen labels it as made up.

The words the product may use: "Marg purchase import", "import file", "Marg profile". Never
"direct integration", "auto-posts to Marg", "syncs with Marg" or "Marg compatible" - checked by
`tests/unit/test_marg_copy.py` (API strings, OpenAPI) and the web app's copy test.

### Verified Marg imports

None yet. When the pilot's operator imports a DocForge file into his own Marg and the stock
shows batch-wise with expiry and scheme quantity correct, record it here as a line of the form
`Verified Marg import: YYYY-MM-DD, Marg <version>` (with who checked which bills). The copy test
reads that line; until it exists, no text may claim more.

## What the pilot must supply

One purchase import file their Marg accepted (from their dealer, or the last supplier import),
with the paper bills it books. Then:

1. Settings > Marg profile > upload it: the layout is read (CSV or Excel, delimiter, header
   row) and each column offered a source ("Batch No" as the line's batch, "Exp" as its expiry
   in the pattern its values show). A person confirms each and saves.
2. "Check against a file Marg accepted" with the same bills: the engine makes its file for
   them and compares cell by cell. Iterate until it is exact.
3. Add that file (values replaced with synthetic ones) and its bills under
   `tests/fixtures/profiles/` with a golden test beside
   `test_the_engine_reproduces_the_sample_file_cell_for_cell` (AC-P3-1 for the real layout).

## Decisions taken here

- **Format, not books:** a run keeps `target` (whose names: tally / marg) and gains `format`:
  `mapped_csv` (P1's file, unchanged) or `marg_import`. A Marg import needs the workspace's
  active profile; with none there is no run (409 `no_active_profile`, the wording the task
  gives).
- **Who:** those who review make, edit and activate profiles (as for mappings, decided for
  P1); everyone in the workspace reads them and may run exports.
- **One active profile:** the first a workspace makes is active by itself; making another
  active stops the first (a partial unique index holds it to one).
- **CGST + SGST or IGST:** decided by the **seller's** state code (GSTIN) against the place of
  supply - the GST law's test (IGST Act ss. 7-8), and the review's own rule. P2's AC-P2-4 words
  it as "place of supply code vs buyer state code"; on a purchase the place of supply is nearly
  always the buyer's own state, so that wording would make every bill intra-state and refuse
  every IGST bill. P2 should use the same shared rule (`profiles.render.intra_state`).
- **Pre-flight is shared** (`docforge/preflight.py`) and applies to import formats (Marg now,
  Tally XML next); P1's mapped CSV is not held to it. The checks: invoice number, date and
  lines; a 2-digit place of supply; tax heads that fit the supply; the review's own totals
  rules (taxable value, tax, grand total - each within 0.01, in Decimal); tax at each line's
  rate matching the printed tax within 0.01, rounded line by line or rate by rate; quantity,
  taxable value and GST rate on every line; batch and expiry on every line of a medicine bill
  (the review's own test); and any line field the profile marks required. Each failure names
  the bill and, for a line, its number; the run makes no file.
- **Free quantity:** its own column, or with `free_qty: "row"` its own row after the line -
  quantity = the free quantity, rate, discount, taxable value, amount and tax nil, and a
  `row_kind` column ("billed" / "free", mapped by codes) - never added to the billed quantity.
  A per-line profile must carry the mapped item, batch, expiry and free quantity, or it is
  refused when saved.
- **Re-export warning:** each run that made a file records its bills
  (`export_run_documents`); a later run for the same books lists the earlier runs that booked
  any of its bills, and how many. A warning, never a refusal. Shared by every format.
- **Same bytes:** a run's file is stored with its SHA-256; a profile version is never changed
  (the database grants no UPDATE); an XLSX file's timestamps are fixed, so the same bills and
  version make the same bytes.

## What was built

- `profiles/spec.py`: the layout as data (`Profile`, `Column`, `Format`), the fields each
  source offers, and the SAMPLE layout.
- `profiles/render.py`: cells from booked bills (no queries: mappings are loaded in bulk by
  `resolve`), CSV through `csvsafe.write_table` (delimiter, quoting, header, CRLF, encoding),
  XLSX through openpyxl (text cells typed as text - never a formula; numbers and dates typed
  when the profile says so).
- `profiles/compare.py`: the golden-file runner - the engine's rows against an accepted file,
  cell by cell (numbers and dates in an Excel file compared by value).
- `profiles/sample.py`: reads an accepted file and offers sources and formats; saves nothing.
- `profiles/service.py`: profiles and their immutable versions; audit.
- `preflight.py`: above. `export_runs.py`: the `marg_import` format, the check runner, overlaps.
- Audit: `export_profile.created` (target, basis, columns), `.versioned` (version, basis,
  columns), `.activated` (version), `.checked` (version, bills, cells, equal); `export.created`
  gains format, profile version and how many bills were exported before. No values from a bill
  or a sample.

## API

| Route | Who |
| --- | --- |
| `GET /v1/settings/export-profiles`, `/{id}`, `/{id}/versions`, `/template`, `/fields` | readers |
| `POST /v1/settings/export-profiles/read-sample` (multipart `file`, 2 MB) | reviewers |
| `POST /v1/settings/export-profiles` (201; 409 name taken; 422 in words) | reviewers |
| `PUT /v1/settings/export-profiles/{id}` (the next version) | reviewers |
| `POST /v1/settings/export-profiles/{id}/activate` | reviewers |
| `POST /v1/settings/export-profiles/{id}/check` (multipart `file`, `document_ids`) | reviewers |
| `POST /v1/exports/runs` gains `format`; runs show `format`, `profile`, `overlaps` | readers |

## Migration 0045

`export_profiles` (unique name per workspace; one active per workspace and books),
`export_profile_versions` (insert and read only), `export_run_documents` (insert and read only;
indexed by workspace and bill), and on `export_runs`: `format` (checked), `profile_version_id`
(present exactly for a Marg import; indexed), `overlaps`. Row-level security on each;
`docforge_delete_account` erases them (the run's bills before the runs, the versions before the
profiles). Reversible.

## Measured (local Postgres 16; pair_001's 10-line bill cloned)

| Run | Time | Target |
| --- | --- | --- |
| 1,000 bills, 10,000 lines, CSV (2 MB) | 1.2 s | under 30 s |
| 2,000 bills (a run's limit), 20,000 lines, CSV (4 MB) | 2.4 s | - |
| 1,000 bills, 10,000 lines, XLSX (0.7 MB) | 2.0 s | under 30 s |

Including the approved-bill query, the mapping lookups, pre-flight, the overlap query, the file
and the record of its bills.

## Tests

- `tests/unit/test_profiles.py`: AC-P3-1 (the golden file, byte for byte), free quantity as a
  column and as a row, per-line profiles must carry item / batch / expiry / free, the tax
  split, formats, CSV options, formula safety, XLSX determinism and compare-by-value, reading
  samples.
- `tests/unit/test_preflight.py`: AC-P3-3 (batch by bill and line), AC-P3-5 (totals, rates,
  rounding by line or by rate, tax heads, place of supply).
- `tests/integration/test_marg_import.py`: AC-P3-2 (no profile, no run; 409 over HTTP),
  AC-P3-3 / 5 on real bills through the review, AC-P3-4 (versions, old runs' version and
  bytes), AC-P3-6 (the re-export warning, audit without names, another workspace never reads or
  uses a profile), the check runner, Excel profiles, roles.
- `tests/unit/test_marg_copy.py`: AC-P3-7. `test_account_deletion.py`: the new tables erased.

## Review (2026-10-31) and known limits

A code review found, and this branch fixes: a control character or over-long text crashing
an Excel export (now a pre-flight problem by bill, line and column); `required` on the
computed line number crashing pre-flight; workbooks that unpack to far more than their 2 MB
(refused above 50 MB unpacked, 500 parts; rows and columns read are bounded); concurrent
profile changes (serialised per workspace, so no deadlock and never two active); codes or
upper case lost in Excel number / date cells; an Excel cell's kind (date, number, text) not
compared by the check; CSV field-limit and over-long-number errors as 500s.

Known, kept, and to confirm against the real sample:

- Each line's CGST / SGST / IGST is rounded line by line. A bill that rounds its tax rate by
  rate passes pre-flight (its printed totals reconcile), but the line columns may add up to a
  few paise off its printed tax; the bill total columns carry the printed figures.
- `csvsafe` puts `'` before a text cell starting with `= + - @` (a party named "+Plus"), as for
  every CSV the product writes. If Marg's import is never opened in a spreadsheet, a profile
  option to write such cells as they are may be wanted.
- With free quantity on its own row, bill-level columns (totals) repeat on that row as on every
  line row; whether Marg sums them is for the real sample to show.
- An Excel file's bytes are fixed for a given zlib; the stored file, checked by its SHA-256, is
  what a download returns.

## Left for later

- **A real Marg sample** and its golden test; then the first verified import, recorded above.
- **Runs of 10,000 bills** (streamed / in the background): a run still takes at most 2,000
  bills (P1's limit), which it makes in about 2.4 s. A background run belongs with P2's Tally
  export, so both formats share it.
- **A supplier filter** on the export screen: the date range is the only filter today.
- Files in Windows-1252 (an old Marg may want it): only UTF-8 is written for now.
