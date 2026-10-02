# DocForge

Document intelligence for regulated, document-heavy operations.

DocForge turns PDFs and scans into validated, structured records. Every extracted value links back to
its place on the page, and nothing extracted by a model becomes a record until a rule or a person
accepts it.

**Status:** checkpoint C1 complete. A born-digital invoice PDF goes in through the API and a typed,
schema-validated record comes out, with the source block of every field. Storage, verification,
scans and the review screen are not built yet. See [docs/architecture.md](docs/architecture.md) for
the design and [docs/progress.md](docs/progress.md) for what has been built and measured.

## Measured so far

On 20 synthetic pharma invoices (clean, born-digital, one page, two layouts) with
`gemini-3.5-flash-lite`: 2472 of 2472 printed fields correct, and 99.15% of values cite a block
covering their true position. Model latency per invoice: p50 14.4 s, p95 26.6 s. This shows the
pipeline works end to end; it is not a claim about scans or real invoices.
`make eval` reproduces the report offline from recorded parses and model replies.

## Development

Needs Docker, [uv](https://docs.astral.sh/uv/) and `make`.

```bash
make up      # install dependencies, start Postgres + pgvector and MinIO, apply migrations
make test    # all tests (integration tests use the services started by `make up`)
make lint    # ruff and mypy
make eval    # re-run the invoice eval offline and rewrite evals/baselines/invoice.json
make api     # run the API on http://127.0.0.1:8000 (needs GEMINI_API_KEY in .env)
make down    # stop the services
```

With the API running:

```bash
curl -F "file=@tests/fixtures/synthetic/pair_001/invoice.pdf" \
  "http://127.0.0.1:8000/v1/extractions?include_blocks=true"
```

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
