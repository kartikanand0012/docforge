# TDD evidence: C16 audit log and webhook screens

Source plan: `docs/plans/c16-audit-webhooks.md` (ECC planner). Owner: build C16 with the plan's
default decisions (2026-10-08).

## Journeys

1. As an administrator, I see who did what (filtered by action, person, target and dates),
   check that the hash chain still holds, and export the filtered view for an auditor.
2. As an administrator, I make a webhook and copy its secret once, send it a test, see its
   deliveries and when the next attempt is due, send a failed one again, rotate its secret,
   disable, enable and delete it - each change recorded in the audit log.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| RED audit log | `8e8d2d2` | registry and audit-log tests fail to import |
| GREEN | `409540e` | 17 tests |
| RED webhooks | `7b4ac89` | webhook management tests fail to import |
| GREEN | `c0f2cbf` | 1,498 tests across the touched modules and units |
| RED web | `32741e5` | helper and proxy tests fail |
| GREEN | `bb16d95` | web 83; build |
| e2e | `61acb5e` | 12/12, with a real signed delivery verified before and after rotation |
| React review | `139ebc3` | helper tests seen failing first; web 86; e2e 12/12 |
| RED Python and security review | `685b287` | 10 failing |
| GREEN | (with this record) | 1,963 Python; web 86; e2e 12/12; gate 76/76 |

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | Every action the code appends is registered with a label; only allowed details are shown, the rest counted; nothing secret or personal is allowed | `tests/unit/test_audit_actions.py` | unit |
| 2 | Entries newest first with names looked up when read; filters alone and together; pages never repeat or skip while entries arrive; another organisation never seen | `tests/integration/test_audit_log.py` | integration |
| 3 | Export: the filtered view, formula-safe, at most 10,000 rows (cut said), itself logged, chain still verifying | `tests/integration/test_audit_log.py` | integration |
| 4 | Admins only; bad or out-of-range filters and NULs refused; chain checks limited per caller; listing and exporting limited | `tests/integration/test_audit_log.py` | integration |
| 5 | Every webhook change audited with the host only, none on a failed change; nothing secret ever written to the log | `tests/integration/test_webhook_management.py` | integration |
| 6 | Rotation signs every later attempt; enabling re-checks the destination; pending deliveries fail as disabled or removed; soft delete keeps deliveries; no test to a disabled webhook | `tests/integration/test_webhook_management.py` | integration |
| 7 | Only a failed delivery is re-sent, once, with its event id and fresh attempts; foreign or unknown ids not found | `tests/integration/test_webhook_management.py` | integration |
| 8 | The next attempt is kept as the queue schedules it, attempt by attempt | `tests/integration/test_webhook_management.py` | integration |
| 9 | At most ten webhooks, even when two are made at once; sends, makes and enables limited; an unsafe destination refused without saying what it resolves to | `tests/integration/test_webhook_management.py` | integration |
| 10 | Migration 0024: columns and valid indexes | `tests/integration/test_webhook_management.py` | integration |
| 11 | Filters to the URL and back, local days as instants and back, chain wording, details as text; URLs shown without path, query or user; next attempt in words; the proxy passes a download's name and the cut flag | `web/tests/unit/{audit,webhooks,server}.test.ts` | unit |
| 12 | The audit page shows the signed review by its reviewer, checks the chain, exports; a webhook made, tested and verified by a local receiver before and after rotation, disabled, enabled, deleted | `web/tests/e2e/review.spec.ts` | e2e |
