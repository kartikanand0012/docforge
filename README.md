# DocForge

Document intelligence for regulated, document-heavy operations.

DocForge turns PDFs and scans into validated, structured records. Every extracted value links back to
its place on the page, and nothing extracted by a model becomes a record until a rule or a person
accepts it.

**Status:** checkpoint C6 complete. Organisations upload invoices and purchase orders (born-digital
or scanned) with API keys; each is parsed in an isolated process, extracted with the source of
every value, checked against its source, the rules and its purchase order, and marked `accept` or
`review`. Reviewers sign in to a web screen, correct values with reasons, and approve or reject
under a PIN signature; an approved invoice becomes a payment approval draft, sent to the
organisation's systems by signed webhook. Postgres row-level security keeps each organisation's
data apart under a database role that cannot lift it. Every step is in an append-only,
hash-chained audit log, anchored outside the database. See [docs/architecture.md](docs/architecture.md) for
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

Certificates of analysis (20 synthetic, one per invoice batch): 511 of 511 values read, 6 of 6
out-of-limit results caught, 3 of 3 certificates that still claim compliance flagged, none of the
14 clean ones flagged. An invoice whose batch failed is held back.

Search over 60 documents in two organisations, 124 generated questions: the right document in
the top five for 100% of questions with hybrid search (99% for keyword alone, 87% for vectors
alone), and never a document from the other organisation. The questions come from the same
documents the search was tuned on, so treat this as an upper bound.

`make eval` reproduces every report offline from recorded parses and model replies.

## Review screen

```bash
make up && make api          # API on :8000 (and `make worker` in another terminal)
echo 123456 | uv run python -m docforge.review add-reviewer --admin --name "Your Name" --email you@example.com
uv run python -m docforge.admin create-key --role integrator --name "my system"   # printed once
make web                     # review screen on http://localhost:3000
make e2e                     # the whole review in a browser, offline, on recorded documents
```

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
