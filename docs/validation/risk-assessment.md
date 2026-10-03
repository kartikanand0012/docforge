# Risk assessment

Failure modes of the system itself, the controls built in, and what is left to the
organisation. Severity and likelihood are the supplier's estimate; the organisation assesses
them again for its own use.

| # | Failure | Effect | Controls in the system | Left to the organisation |
| --- | --- | --- | --- | --- |
| 1 | The model reads a value wrong | A wrong value in a record | Value checked against its cited text (R4); rules (R5); comparison with the order (R6); review routing (R9); measured accuracy with a gate (R19) | Review thresholds from its own measurements; periodic sample review of accepted records |
| 2 | The model invents a value not on the page | A value with no source | Citation required and checked; unprinted fields measured (0 invented on the eval set) | As 1 |
| 3 | A scan is poor | More misreads | OCR with deskew; every misread on poor scans was caught by the order match in the eval | Scan quality standards at intake |
| 4 | A certificate's limit or conclusion is written in an unexpected way | A failed result passing | "Not evaluated" for anything not fully understood, routed to a person (R7, R8) | Review of every certificate flagged; release stays with the qualified person |
| 5 | A record is changed after review | A signature over different content | Signature covers the record's hash; a stale record cannot be signed (R11, R12) | — |
| 6 | Someone signs as someone else | A false signature | Per-person accounts; PIN re-entered per signature; lockout after five wrong PINs; sessions expire after 8 hours (R15) | Identity checks before issuing accounts; PIN policy; no shared accounts |
| 7 | Audit history is altered | Lost evidence | Append-only tables enforced by the database; hash chain; head anchored outside the database (R13) | Separate storage credentials for anchors; retention policy |
| 8 | One organisation sees another's data | Disclosure | Row-level security under a role that cannot lift it; tested on every route (R14) | — |
| 9 | A prompt or model change makes extraction worse | Silent drop in quality | Pinned model and prompt; recorded evals in CI; gate against floors and the base branch (R19) | Change control: re-run the gate and its own performance checks on a change |
| 10 | The model provider is down or over quota | Delay | Retries; the document waits, nothing is recorded from a failed call (R18); ops alerts | Supplier qualification; capacity planning |
| 11 | Data is lost | Lost records | Originals in object storage, records in Postgres | Backups and restore tests (not part of the demo deployment) |
