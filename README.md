# DocForge

Document intelligence for regulated, document-heavy operations.

DocForge turns PDFs and scans into validated, structured records. Every extracted value links back to
its place on the page, and nothing extracted by a model becomes a record until a rule or a person
accepts it.

**Status:** checkpoint C4 complete. An invoice or purchase order is uploaded as a born-digital
PDF or a scan, stored by content hash, queued, and parsed in an isolated process (OCR with
deskewing for scans; long documents a few pages at a time). It is extracted into a typed record
with the source block of every field, page by page for documents longer than one page. Each value
is checked against the text it cites, the invoice is checked by deterministic rules and compared
with its purchase order, and the document is marked `accept` or `review` with reasons. Each step
is written to an append-only, hash-chained audit log. The review screen is not built yet. See [docs/architecture.md](docs/architecture.md) for
the design and [docs/progress.md](docs/progress.md) for what has been built and measured.

## Measured so far

On 20 synthetic pharma invoices (clean, born-digital, one page, two layouts) with
`gemini-3.5-flash-lite`: 2472 of 2472 printed fields correct, and 99.15% of values cite a block
covering their true position. Model latency per invoice: p50 14.4 s, p95 26.6 s. This shows the
pipeline works end to end; it is not a claim about scans or real invoices.

Trust checks on the same documents plus 9 pairs with one seeded defect each (wrong batch, bad
arithmetic, expired stock, price above MRP, bad GSTIN, quantity, rate and free quantity against
the order): 9 of 9 defects caught. Of the 20 correct pairs, 12 were accepted with no review; the
other 8 had correct values but a citation pointing at a neighbouring cell, so they would go to a
person. That review rate is the number to improve next.

The same 20 invoices as scans, read by OCR: 2471 of 2472 fields correct on a clean 150 dpi
scan; 2424 of 2472 (98.06%) on a poor one (110 dpi, 1.8° crooked, blurred, noisy). On the poor
scans 5 invoices with a misread value (mostly a letter in a product name) passed their own checks;
none of those also agreed with its purchase order, so the order match is what stops them.

Three long invoices (45, 80 and 120 lines over 8 pages), read a page at a time: two fully correct;
the third lost one line where the parser merged two table rows, and its checks sent it to review.

A 301-page invoice parses in its isolated process with a peak of 3.4 GB in 24 minutes on a laptop
CPU; a 31-page scan in 7 minutes (4.0 GB).

`make eval` reproduces every report offline from recorded parses and model replies.

## Development

Needs Docker, [uv](https://docs.astral.sh/uv/) and `make`.

```bash
make up      # install dependencies, start Postgres + pgvector and MinIO, apply migrations
make test    # all tests (integration tests use the services started by `make up`)
make lint    # ruff and mypy
make eval    # re-run the extraction and trust evals offline and rewrite evals/baselines/
make api     # run the API on http://127.0.0.1:8000 (needs GEMINI_API_KEY in .env)
make down    # stop the services
```

With `make api` and `make worker` both running:

```bash
# Upload: returns at once with the document id. The same file again returns the same document.
curl -F "file=@tests/fixtures/synthetic/pair_001/invoice.pdf" http://127.0.0.1:8000/v1/documents

curl http://127.0.0.1:8000/v1/documents/<id>              # status and versions
curl http://127.0.0.1:8000/v1/documents/<id>/extraction   # the record, once extracted
curl http://127.0.0.1:8000/v1/documents/<id>/assessment   # accept or review, with page boxes and the match
curl http://127.0.0.1:8000/v1/documents/<id>/audit        # every step taken
curl http://127.0.0.1:8000/v1/audit/verification          # recompute the audit hash chain
```

`POST /v1/extractions` is a stateless preview: it extracts in the request and stores nothing.

The API has no authentication yet, so keep it bound to localhost. The first `make test` downloads
Docling's layout and table models.

Settings come from the environment or a `.env` file; see [.env.example](.env.example). The defaults
work with the local services as they are.

`tests/fixtures/synthetic/` holds 20 invented pharma invoice and purchase-order pairs with
ground-truth labels. `make generate` rebuilds them from a fixed seed.

## First workflows

- Pharma distributor invoice matched against its purchase order: batch, expiry, MRP/PTR, quantity and
  free-quantity schemes, GST/HSN, drug licence and line arithmetic.
- Certificate of analysis checked against specification limits and matched to the invoice.

## Licence

MIT. See [LICENSE](LICENSE).
