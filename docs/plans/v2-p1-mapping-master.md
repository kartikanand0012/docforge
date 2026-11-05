# V2 P1: the mapping master (Tally / Marg names per workspace)

Branch `v2-p1-mappings` (stacked on `v2-p0-visible-flags`: migration 0044 follows P0's 0043).

## Why

An export that makes "ABC Pharma (New)" beside the books' "ABC Pharma" gets the tool thrown
out after a good demo. P1 remembers, per workspace, how its own books name each supplier and
item, and never guesses. The Tally and Marg formats (P2, P3) build on the run and the master
made here.

## Decisions (owner, 2026-10-27)

- **Export run:** P1 adds a stored export run that writes a mapped purchase CSV (one row per
  bill line, under the mapped names). P2/P3 add the Tally XML and Marg formats to the same run.
- **Who changes mappings:** those who review (reviewer, member, admin), as with Schedule H1
  marks; everyone in the workspace reads them. Runs: anyone who may read documents, as the
  existing exports.
- **Item key:** printed product name + pack (`product_key` of each), not the HSN. The printed
  HSN is kept and shown; a preferred HSN replaces it in the export.

## What was built

- `mappings/keys.py`: GSTIN key (upper case, no spaces), item key, name words for matching
  (dosage-form spellings as one, "250 mg" as "250mg", company and pharmacopoeia words dropped).
- `mappings/suggest.py`: ranks names to offer, never applies one. Same GSTIN (1.0), same
  words in any case, punctuation or order (0.95), then shared words weighted by rarity, with a
  word's first four letters or a strength's number counting for most of a word (at most 0.9).
  At least one word must be shared (not only a strength or pack); another strength counts for
  much less. At most five, none below 0.5.
- `mappings/masters_csv.py`: the masters CSV (name, type, optional gstin, any column order,
  BOM and blank lines ignored, at most 20,000 rows / 2 MB). Bad rows by file line (header is
  line 1) with a reason; good rows kept; a name already held, or repeated, is "already known".
- `mappings/service.py`: CRUD with plain errors; a second mapping for a GSTIN or item is
  `MappingExists` naming the first (409 with `existing_id`); `resolve` works out what a set
  of bills needs mapped for Tally or Marg (a mapping with no name for those books is a task
  with its `mapping_id`; a bill with no or an invalid seller GSTIN, or a line with no product
  name, is a problem to correct on the bill); suggestions from the workspace's masters and its
  other mappings, worked out from an index kept per workspace while its names stand.
- `export_runs.py`: a run over invoices approved in a range of India's dates (newest version,
  live, at most 366 days and 2,000 bills). Anything unmapped: status `needs_mapping`, the task
  list stored, no file. Otherwise the CSV through `csvsafe` (decimals as signed, dates
  YYYY-MM-DD, expiry YYYY-MM), stored at `exports/{tenant}/{run}.csv` with its SHA-256; a
  download is checked against the hash. Runs are insert-only rows.
- `review/service.py`: `effective_invoice` - a bill's values as corrected, without a review
  read's checks (for the suggestions of one document).
- Audit: `mapping.created` (kind, source), `mapping.updated` (kind, the fields changed),
  `mapping.deleted` (kind), `mapping.imported` (counts), `export.created` (target, status,
  counts, SHA-256), `export.downloaded` (SHA-256). Never a name from a bill.

## API

| Route | Who |
| --- | --- |
| `GET /v1/settings/mappings/suppliers` and `/items` (`q`, `limit` 1-200, `after`; `total`) | readers |
| `POST` (201; 409 + `existing_id`), `PATCH /{id}`, `DELETE /{id}` on each | reviewers |
| `POST /v1/settings/mappings/import` (multipart `file`) | reviewers |
| `GET /v1/settings/mappings/suggestions?document_id=` or `?export_run_id=` (`target` for a document) | readers |
| `POST /v1/exports/runs` `{target, from, to}`; `GET /v1/exports/runs`, `/{id}`, `/{id}/file` (409 when no file) | readers |

Confirming a suggestion is a POST with `source: "suggestion"`; the suggestions route only
reads.

## Migration 0044

`supplier_mappings` (unique tenant + seller GSTIN), `item_mappings` (unique tenant + item
key), `mapping_masters` (unique tenant + kind + name key), `export_runs` (insert and read
only; checks that a ready run has its file and no tasks, and the reverse). Row-level security
on each; each list has a (tenant, sort key, id) index in the "C" collation its cursor uses;
`docforge_delete_account` deletes all four; `exports/` is erased with the workspace's files.
Reversible.

## Measured (local Postgres 16; 2,000 suppliers, 20,000 items, 22,000 master names)

| What | p95 | Target |
| --- | --- | --- |
| Supplier / item list, first page, page 6 / 42 of 200 | 6-15 ms | under 200 ms |
| Item search "cefixime tab 500" | 26 ms | under 200 ms |
| Suggestions, one item (index kept) | 11 ms | under 200 ms |
| Suggestions, 10-line bill (index kept) | 47 ms | - |
| Suggestions, 50-line bill + its supplier (index kept) | 172 ms | under 2 s |
| Same, first request after a name changed (index built) | 487 ms (one item: 326 ms) | under 2 s |

The index for 40,000 item names holds about 41 MB; a process keeps three (the workspace,
kind and books, while its names' count and newest change stand).

## Tests

`tests/unit/test_mapping_keys.py`, `tests/unit/test_masters_csv.py`,
`tests/integration/test_mappings.py` (AC-P1-1, 2, 3, 4, 6, 7, the file's columns, audit,
the kept index), `tests/integration/test_api_mappings.py` (HTTP, roles, plain errors,
AC-P1-5 with two workspaces), `test_account_deletion.py` (the four tables and the export
file are erased).

## Left for later

- P2/P3: Tally XML and Marg import formats over the same run and resolution.
- The review screen of one bill does not yet offer its mappings (the suggestions route takes
  a `document_id` for that).
- Clearing imported master names (only erased with the workspace for now).
