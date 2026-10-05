# TDD evidence: C6 multi-tenant product surface

Source plan: checkpoint C6 in `docs/build-plan.md`; journeys derived during this run.

## Journeys

1. As an organisation, nobody else can see or change my documents, whatever the API or the
   database is asked.
2. As an integrator, my system calls the API with a key that can do only what its role allows.
3. As a reviewer, I sign in to the review screen; nothing in my browser can be stolen to act as me.
4. As an organisation, my systems learn when a document is processed or approved, once each.
5. As an auditor, a rewritten log is caught even if the rewriter recomputed the hashes.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| RED keys, sessions, roles | `301f42c` | collection fails |
| GREEN | `f631efb` | auth tests pass |
| Admin reviewers; web sign-in and proxy | `b6fa512`, `e55e73b` | e2e 6 passed |
| RED row-level security | `04136ce` | fixtures and policies missing |
| GREEN | `aaf2476` | `test_rls.py` 12 passed; the whole suite as the app role |
| RED webhooks | `61c09bd`, `0683986` | collection fails |
| GREEN | `a69ca6f` | webhook tests pass, including the real queue |
| RED export and anchors | `61a8bb3` | collection fails |
| GREEN | `760766c` | 7 passed |
| RED review findings | `45eb1aa` | 19 failed |
| GREEN | `f718b41` | `make test` passes; e2e 6 passed |

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | Every route needs a credential; bad ones are refused | `test_auth.py` | integration, no ambient tenant |
| 2 | Another tenant's documents do not exist for it; an upload belongs to the key's tenant | `test_auth.py` | integration |
| 3 | Roles limit what a credential does; only a signed-in person corrects or signs, as themselves | `test_auth.py`, `test_api_review.py` | integration |
| 4 | Revoked keys, ended and expired sessions stop working; tokens stored only as hashes | `test_auth.py` | integration |
| 5 | Wrong PINs lock a reviewer wherever they come from; sign-in is limited per address | `test_auth.py` | integration |
| 6 | Every tenant table has row-level security; without a tenant nothing is visible; another tenant's rows cannot be written | `test_rls.py` | integration (Postgres) |
| 7 | The application's role cannot delete, truncate, alter, disable triggers or policies, change roles, update append-only tables, create organisations, or call trigger functions | `test_rls.py`, `test_privileges.py` | integration |
| 8 | Webhook destinations must be HTTPS and public; NAT64 refused; the checked address is the one used | `test_webhook_rules.py`, `test_webhooks.py` | unit, integration |
| 9 | Deliveries are signed; retried with one event id; never sent twice; one event emitted twice is one delivery; no lock held while sending | `test_webhooks.py` | integration |
| 10 | Processing and signing emit events, with the draft on approval | `test_webhooks.py` | integration |
| 11 | Webhook management is admin-only and tenant-scoped; a test event goes to the webhook named | `test_api_webhooks.py` | integration |
| 12 | Signed records export as CSV that cannot start a formula, and as JSON | `test_exports_and_anchors.py` | integration |
| 13 | A rewritten log that still chains is caught by its anchor; every organisation is anchored | `test_exports_and_anchors.py` | integration |
| 14 | Sign-in, session cookie, origin check and the post-login path are safe | `web/tests/unit/server.test.ts`, `next.test.ts`, e2e | unit, end to end |

## Coverage and known gaps

`make test` 1,348 passed, 93%. Gaps are listed under C6 in `docs/progress.md`.
