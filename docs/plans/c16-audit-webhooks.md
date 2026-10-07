# Plan: C16 audit log and webhook screens

Planned 2026-10-08 with the ECC planner, from `docs/research/08-feature-roadmap.md`: "Audit log
viewer and export in the UI" (section 7, P1) and "Webhook management screen with delivery log
and resend" (section 8, P1). Administrators get two new pages: the audit log (who did what, and
whether the hash chain holds) and webhooks (manage them and their deliveries). Run like the
earlier checkpoints: tests first, ECC reviewers, gate, records.

## Found in the code

- Creating and removing a webhook appends nothing to the audit log today.
- The webhook create reply holds the secret but has no `Cache-Control: no-store`.
- The application's role has no DELETE on `webhooks`, and deliveries reference webhooks;
  today `DELETE` sets `active = false`, so disable and delete are not yet separate.
- "Next retry" lives only in procrastinate's job table, which has no tenant scoping.
- The web proxy forwards only content-type, cache-control and retry-after, so
  `Content-Disposition` and `X-DocForge-Truncated` never reach the browser.
- `document.ready_for_chat` is emitted but missing from `EVENTS`, so it cannot be subscribed to.
- What `details` holds today is safe: hashes, counts, versions, outcomes, field paths, key names
  and roles, processing errors. No tokens, secrets, PINs, filenames or document text.
- `GET /v1/audit/verification` and `GET /v1/documents/{id}/audit` exist and are open to
  `documents:read`; they stay so.

## Decisions

1. **Administrators only, for both pages.** Every new endpoint requires `admin`; others get 403
   and the page says "Only administrators can see this page." The two existing read endpoints
   keep `documents:read` (integrators may depend on them).
2. **The audit page reads the log; it never changes it.** Newest first, filters, a cursor on
   the chain's `id`, the chain check on request, the filtered view exported as CSV.
3. **Details are shown from an allow-list per action** (`src/docforge/audit_actions.py`: each
   action's label and the detail keys that may be shown). Others are not sent ("1 detail
   hidden"). Verification still recomputes every hash from the whole entry.
4. **Names are looked up when read; the log never stores them.** `reviewer:<id>` becomes the
   reviewer's name, `key:<id>` the key's name and prefix, system actors a plain label; a
   document target is shown as its filename, read live, so names stay erasable.
5. **Webhooks: what exists stays, the gaps are filled.** Disable and enable, a real (soft)
   delete, rotate the secret, re-send a failed delivery, see the next retry. Every change goes
   into the hash-chained audit log, in the same transaction.
6. **Delete is soft**: `deleted_at`, out of the list, never enabled again, deliveries kept.
   `DELETE /v1/webhooks/{id}` keeps meaning "stops receiving".
7. **Re-send reuses the delivery and its event id**: a failed row back to `pending`, attempts
   0, a job queued; one `UPDATE ... WHERE status = 'failed' RETURNING`, so two clicks queue it
   once.
8. **Rotation is immediate**: `secret_version` up by one, the new secret shown once, every
   later attempt signed with it, retries included (see Needs the owner).
9. **No payloads on screen** (a `review.signed` payload carries the payment approval draft).

## Data (migration 0024)

- `audit_log`: indexes `(tenant_id, action, id)`, `(tenant_id, actor, id)` and
  `(tenant_id, occurred_at)`, built `CONCURRENTLY` in an autocommit block (a plain build would
  block appends, and with them processing). Triggers and grants unchanged.
- `webhooks`: `deleted_at`, `secret_rotated_at`; check `deleted_at IS NULL OR active = false`.
- `webhook_deliveries`: `last_attempt_at`, `next_attempt_at`, set from the same retry rule the
  queue uses (one shared object; a test pins that they agree). The page says "about".
- Row-level security unchanged. If C13 (paused) merges first, this renumbers.

## Audit log screen (`web/src/app/audit/page.tsx`)

- **Listed**: when (local, UTC in `dateTime`), who, action (label), target (link), allowed
  details; the entry number in small type, because the chain check names entries by it.
- **Filters**: action (from the registry), who (people, keys, system actors - read from
  `reviewers`, `api_keys` and constants, not by scanning the log), target, dates (from
  inclusive, to exclusive, sent as instants). Filters live in the URL, so a view can be shared.
- **Paging**: 50 a page, `id < before`; ids only grow, so nothing repeats or is skipped. No
  total count. "Load more".
- **Chain check**: a "Check the chain" card, on request only. Verified, verified with an
  outside anchor, no anchor yet (with `audit.py`'s stated limits), or broken at entry N with a
  link to it. 6 checks a minute per organisation, the existing endpoint included.
- **Export CSV**: the filtered view, newest first, up to 10,000 rows (`X-DocForge-Truncated`
  beyond). Columns `id, occurred_at, actor, actor_name, action, target_type, target_id,
  details, prev_hash, hash`, through the formula guard in `api/exports.py`. No filenames.
  Each export is appended as `audit.exported` with `{rows, truncated, filtered}`.
- **Never shown, on screen or in the CSV**: tokens (only prefixes are logged), webhook secrets,
  PINs, document text, questions, payloads, full webhook URLs (the log keeps the host only).

## Webhook screen (`web/src/app/webhooks/page.tsx`)

- **List**: URL (query shown as "?..."), events, active or disabled, last delivery (status,
  code, time), last rotation, who made it; Deliveries, Send test, Rotate, Disable or Enable,
  Delete.
- **Create**: URL and events as checkboxes from `GET /v1/webhooks/events`. The secret shown
  once with Copy and "Done, I have copied it", as on the AI agents page. At most 10 webhooks.
- **Rotate**: confirmation ("Receivers using the old secret will refuse deliveries until they
  have the new one"), then the new secret once.
- **Disable / enable**: a pending delivery fails at its next attempt with "the webhook was
  disabled" ("removed" is kept for deleted ones). Enable re-checks the destination.
- **Delete**: confirmation, then out of the list.
- **Send test**: 409 on a disabled webhook ("Enable it first").
- **Deliveries**: when, event, status, attempts, response code, error, next retry ("about 3
  minutes"), Re-send on failed rows; 50 a page with a cursor and a status filter; a note that
  receivers should ignore an event id they have already processed.
- **SSRF**: `check_destination` holds on create, enable, test and re-send (422 early), and
  before every attempt as now (the address checked is the one connected to; no redirects, no
  proxy, 30 s). No action takes a URL other than the stored one.
- **Limits**: test sends and re-sends share 10 a minute per organisation.

### Endpoints (all admin only)

| Endpoint | Purpose | Status |
| --- | --- | --- |
| `GET /v1/audit` | filters, `before`, `limit` 1-200 (50) -> items with names and labels, `next_before` | new |
| `GET /v1/audit/filters` | actions and actors for the dropdowns | new |
| `GET /v1/audit/export.csv` | the same filters, at most 10,000 rows, audited | new |
| `GET /v1/audit/verification` | unchanged; a per-organisation limit added | exists |
| `GET /v1/webhooks` | adds last delivery, last rotation, creator's name; deleted left out | changed |
| `POST /v1/webhooks` | adds `no-store`, an audit entry, the cap of 10 | changed |
| `GET /v1/webhooks/events` | events a webhook may subscribe to | new |
| `PATCH /v1/webhooks/{id}` | `{active}`: disable or enable (enable re-checks) | new |
| `DELETE /v1/webhooks/{id}` | soft delete, audited | changed |
| `POST /v1/webhooks/{id}/secret` | rotate; the new secret once, `no-store` | new |
| `POST /v1/webhooks/{id}/test` | refused when disabled; limited; audited | changed |
| `GET /v1/webhooks/{id}/deliveries` | adds ids and attempt times; cursor and status filter | changed |
| `POST /v1/webhooks/{id}/deliveries/{delivery_id}/resend` | failed only (409 otherwise); limited; audited | new |

Another organisation's webhook or delivery, or a delivery under another webhook: "No such
webhook." (404), as one that does not exist.

### Query cost

The list uses the matching `(tenant_id, ..., id)` index, `LIMIT 51`, under a 5 s
transaction-local `statement_timeout`; a test on 200,000 seeded entries asserts no sequential
scan on any single filter. Export: the same query, limit 10,001, in the thread pool.
Verification is linear, on request and limited. Three more indexes cost one B-tree write each
per append, under a lock already held.

### Audit entries added

`webhook.created` (`host`, `events`), `webhook.disabled` / `webhook.enabled` / `webhook.deleted`
(`host`), `webhook.secret_rotated` (`secret_version`), `webhook.test_sent` (`event_id`),
`webhook.delivery_resent` (`event_id`, `event_type`, `previous_attempts`, `previous_status`),
`audit.exported` (`rows`, `truncated`, `filtered`). Reads are not logged.

## Web

- `web/src/lib/api.ts`: the types and calls, and `auditExportUrl(filters)`.
- `web/src/lib/audit.ts`: labels, "N details hidden", values, local dates to instants, filters
  to and from the query string, the chain card's wording.
- `web/src/lib/webhooks.ts`: delivery status words, "about N minutes" / "due now", the URL
  with its query hidden, the host.
- Pages as `agents/page.tsx`: newest load wins, 403 message, loading and empty states, tables
  in `.table-scroll`, a polite live region, buttons disabled while their request runs, focus
  kept when a row's button disappears.
- Nav: "Audit log" and "Webhooks" after "AI agents".
- The proxy also forwards `content-disposition` and `x-docforge-truncated` (the list moved to
  `lib/server.ts` and tested), which also fixes the signed-records export's filename.

## Test-first order

1. Registry (unit): every action labelled with allowed keys; unknown keys dropped and counted;
   a source scan asserts every action passed to `audit.append` or `_audit` is registered.
2. Retry schedule (unit): `next_attempt_at` equals what the queue schedules, attempts 1-8.
3. Migration 0024 (integration): indexes valid, columns and check present, append-only
   triggers and isolation intact.
4. `GET /v1/audit` (integration): each filter and combined; date bounds; paging while entries
   are appended; bad cursor or date 422; admin only (reviewer, integrator, reader 403);
   another organisation never seen, even with its ids as the cursor; names looked up; plans on
   200,000 entries.
5. Nothing secret in the log (integration): the whole flow (sign in, a failed PIN, a key made
   and revoked, upload, process, correct, sign, a webhook made, rotated, tested, re-sent), then
   no PIN, `dfk_`/`dfs_`/`whsec_` token, filename, full URL or extracted text in any details.
6. Export (integration): columns, formula guard, truncation and its header, `audit.exported`
   appended and the chain still verifying.
7. Verification: the limit; a tampered chain reported broken at N through the endpoint.
8. Webhook service and API (integration, fake HTTP client): an audit entry per change and none
   on rollback; `no-store`; rotation signs with the new secret only and never shows it again;
   enable refuses a name now resolving to `10.0.0.1`; pending deliveries fail "disabled" or
   "removed"; delete hides, refuses enabling, keeps deliveries; test refused when disabled,
   limited, and a destination turned local is recorded failed without calling the client;
   re-send failed only, once under two clicks, same event id, attempts reset, SSRF re-checked,
   limited; `next_attempt_at` set on retry; the list's last delivery; 404 for another
   organisation's ids everywhere.
9. Web unit (vitest): `audit.test.ts`, `webhooks.test.ts`, the proxy's forwarded headers.
10. E2E: the audit page (verified chain, filter by "Review signed", export); "an administrator
    manages a webhook end to end" with a local receiver checking `DocForge-Signature`
    (`WEBHOOK_ALLOW_LOCAL` in the e2e stack only): make, test (delivered 200), rotate, test
    (verified with the new secret), disable (test refused), enable, delete.
11. ECC security-reviewer, python-reviewer, react-reviewer; then records.

## Gate

No eval or gate change: nothing here calls a model or touches extraction, search, chat or MCP.
If a gate metric moves, that is a bug in this checkpoint. The new guarantees are held by the
integration and e2e tests above.

## Needs the owner (recommendations in brackets)

- Both pages for administrators only, reviewers seeing neither; the two existing read
  endpoints stay open to `documents:read` [yes].
- Rotation immediate, or a 24-hour overlap signing with both secrets [immediate].
- Delete is soft and cannot be undone from the screen [yes].
- Export: the filtered view, at most 10,000 rows, signatories' names in, filenames out, each
  export logged [yes].
- URLs shown with the query hidden, the log keeping the host only, payloads never shown [yes].
- Limits: 10 webhooks per organisation, 10 test sends and re-sends a minute, 6 chain checks a
  minute [yes].
- Add `document.ready_for_chat` to the events a webhook may subscribe to [yes].

## Risks

- Re-sends and retries reach a receiver twice: the same event id each time, and receivers
  told to ignore repeats.
- Rotation breaks receivers until updated: a confirmation that says so, a test send after,
  the overlap option.
- A details key added later and not registered is hidden: intended (fails closed); the
  source-scan test makes the developer register it.
- Index builds on a large `audit_log`: concurrent; an invalid index is checked for.
- Long chain checks: on request, limited, in the thread pool.
- The nav grows to eleven entries: it wraps on phones; grouping belongs to the roles work.
- The e2e receiver needs `WEBHOOK_ALLOW_LOCAL` in the e2e stack only (refused in production).
- Old deliveries are not pruned: the list pages; retention is on the roadmap.

Estimate: 3 to 4 working days.
