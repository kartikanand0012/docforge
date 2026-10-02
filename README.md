# DocForge

Document intelligence for regulated, document-heavy operations.

DocForge turns PDFs and scans into validated, structured records. Every extracted value links back to
its place on the page, and nothing extracted by a model becomes a record until a rule or a person
accepts it.

**Status:** checkpoint C0 (foundations) complete; no document processing yet. See
[docs/architecture.md](docs/architecture.md) for the design and [docs/progress.md](docs/progress.md)
for what has been built.

## Development

Needs Docker, [uv](https://docs.astral.sh/uv/) and `make`.

```bash
make up      # install dependencies, start Postgres + pgvector and MinIO, apply migrations
make test    # all tests (integration tests use the services started by `make up`)
make lint    # ruff and mypy
make down    # stop the services
```

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
