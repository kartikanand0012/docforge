# DocForge

Document intelligence for regulated, document-heavy operations.

DocForge turns PDFs and scans into validated, structured records. Every extracted value links back to
its place on the page, and nothing extracted by a model becomes a record until a rule or a person
accepts it.

**Status:** design complete, build not started. See [docs/architecture.md](docs/architecture.md).

## First workflows

- Pharma distributor invoice matched against its purchase order: batch, expiry, MRP/PTR, quantity and
  free-quantity schemes, GST/HSN, drug licence and line arithmetic.
- Certificate of analysis checked against specification limits and matched to the invoice.

## Licence

MIT. See [LICENSE](LICENSE).
