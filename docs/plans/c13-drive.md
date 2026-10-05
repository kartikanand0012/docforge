# Plan: C13 Google Drive folder sync

Planned 2026-10-05 with the ECC planner, from `docs/research/06-knowledge-chat-connectors.md`
section 5 and the owner's decision: option A, the customer shares a folder with DocForge's
service account. Run like the earlier checkpoints: tests first, ECC reviewers, gate, records.

## Decisions

1. **Client:** plain HTTPS with `httpx` and `google-auth`, both already installed through
   `google-genai`, so no new package. Scope `drive.readonly`. The base URL is a constant.
   Every call supports shared drives.
2. **A changed file becomes a new document, not a new version.** A document's identity is its
   content hash. The synced item points at the new document; the old one leaves the knowledge
   base but stays in the organisation. The run records old to new.
3. **Google Docs, Sheets and Slides** are exported to DOCX, XLSX and PPTX (PDF otherwise).
   Exports are not byte-stable, so a native file is exported again only when its Drive
   `version` changes. Binary files are compared by `md5Checksum`.
4. **A full listing is the truth; `changes.list` is only a hint.** One service account sees
   every tenant's changes, so each run lists the folder tree and compares it with what is held.
5. **Removals only after a complete listing.** If the root is gone (403 or 404) or the listing
   fails partway, the run is `access_lost` or `failed` and nothing is removed.
6. **Shortcuts are never followed:** one could lead outside the folder, to another tenant's
   shared files.

## Ownership proof (the key risk)

Every tenant shares one service account, so tenant B could paste the id of a folder tenant A
shared with it. Each connection gets a random 128-bit token. The customer creates a file
named `docforge-verify-<token>` in the folder (or puts the token in its description). Verify
and every run check that it is still there; if not, the source is `paused` and nothing is
removed. The verification file is never ingested. Messages never reveal whether another tenant
has connected a folder.

Remaining risk, documented: someone with edit rights on another organisation's folder can sync
it, which they could already read. A per-tenant service account is the enterprise option.

Input checks: a folder id matches `^[A-Za-z0-9_-]{10,128}$` (this also guards the Drive query).
URLs are parsed, never fetched: only `drive.google.com/drive/(u/N/)folders/<id>` and
`open?id=<id>`.

## Data (migration 0018)

- `sync_sources`: one connected folder feeding one knowledge base. It holds the provider, the
  folder id and name, the verification token, and a status (`pending_verification`, `active`,
  `paused`, `access_lost`, `disconnected`). It also tracks the next sync time and failures.
- `synced_items`: one per Drive file. It holds the name, MIME type, md5, Drive version, size and
  the document it became, plus its state (`active`, `skipped`, `failed`, `removed`) and why.
- `sync_runs`: one per run, with its trigger, its status (`running`, `succeeded`, `partial`,
  `failed`, `interrupted`) and its counts. Errors are kept in a form safe to show.
- Row-level security as in 0017, tenant-consistent keys, and functions that find due sources
  across tenants for the scheduler.

## The sync job

A `sync` queue with one job per source at a time, run every 5 minutes and on demand, by a
worker of concurrency 1 (all tenants share one Drive quota). A run:

1. Claims the source.
2. Checks the token.
3. Lists within limits (1,000 files, depth 10, 500 folders, 5 sources per tenant).
4. Plans with a pure `plan_sync(listing, items)`: new, changed, renamed, gone, unchanged, and
   unsupported.
5. Fetches and ingests at most 100 files per run, each committed on its own so a redelivered job
   only does what is left.
6. Removes gone and replaced files from the knowledge base unless another item still needs them.
7. Records the counts.

Rate limits (429, or 403 rate errors) back off with jitter and `Retry-After`, then the queue
retries the job.

## API and web

| Endpoint | Purpose | Who |
| --- | --- | --- |
| `GET /v1/drive/service-account` | The email to share with | reader |
| `POST /v1/collections/{id}/drive/sources` | Connect a folder: returns the token and instructions | admin |
| `POST .../sources/{sid}/verify` | Activate and queue the first sync | admin |
| `POST .../sources/{sid}/sync` | Sync now (202) | writer |
| `GET .../sources`, `.../runs`, `.../items` | Status, history, skipped files | reader |
| `DELETE .../sources/{sid}` | Disconnect; documents stay | admin |

A "Google Drive" section on a knowledge base's page shows:

- the email, with a copy button;
- the folder, the token instructions and a Verify button;
- the status, the last run, Sync now, the history and the skipped files;
- Disconnect.

## Test-first order

1. Folder references and settings (unit).
2. `plan_sync` and a `FakeDrive` (unit).
3. Migration, row-level security and isolation (integration).
4. Connect, verify, disconnect; ownership proof (integration).
5. Runs: add, change, rename, remove; partial listing removes nothing; unshared means
   `access_lost`; crash and redelivery; caps; conflicts.
6. Queue, periodic job and worker.
7. The HTTPS client against a mock transport: pagination, backoff, exports, size cap, nothing
   secret logged.
8. API, web, e2e with the fake Drive.
9. Eval harness: scripted cycles, in the gate.
10. A live smoke test, skipped without credentials. Then reviews and records.

**Gate:**

- Adding, changing and removing a file each show in the knowledge base after one run.
- A second run changes nothing and downloads nothing.
- Nothing leaks between tenants.

## Needs the owner

- A Google Cloud project with the Drive API enabled.
- A service account with no roles, and its JSON key as a deployment secret.
- A test folder.
- A check of Google's policy for service-account access.
- Agreement that only admins connect and disconnect.

Everything else is built and tested with the fake Drive.

## Risks

- **Exports change bytes:** mitigated by re-exporting only on a version change.
- **One quota for all tenants:** mitigated by concurrency 1, caps and backoff.
- **Removing a file can remove a document someone also added by hand.** No origin is recorded
  for knowledge-base membership; this is documented, or an origin column is added.
- **A large Sheet over the page limit** fails visibly.

Estimate: 4 to 5 working days.
