# DocForge frontend handover

Everything a frontend needs to know about the DocForge backend, in one place: what the product
does, every screen it needs, the endpoints behind each, how sign-in works, the shapes of the
main requests and answers, live updates, errors, limits and states.

- **Full API contract:** [`docs/api/openapi.json`](api/openapi.json) (OpenAPI 3, every endpoint
  with its request and response schemas). Generate a typed client from it, or read it at
  `/docs` on a running API. This document explains what the schema cannot: flows, states and
  rules.
- **Backend state:** `v1-real-world` after the final sprint (2026-10-10): accounts, members, the platform administrator, deleting and costs (section 14). Endpoints under `/v1`, plus the MCP
  server for AI agents at `/v1/mcp` (not in the OpenAPI file; see section 13).

---

## 1. What DocForge does

DocForge reads business documents and turns them into checked, structured data a person can
trust, then lets people search and ask questions across them.

1. **Upload** a document: PDF, Word (DOCX), Excel (XLSX), PowerPoint (PPTX) or an image
   (PNG, JPEG, TIFF), up to 10 MB and 20 pages.
2. **Read**: office files are converted to PDF; the text and tables are parsed; for known types
   (invoice, purchase order, certificate of analysis) an AI model extracts the fields, each tied
   to where it is printed on the page.
3. **Check**: every value is checked against the page; an invoice is matched with its purchase
   order (two-way match); a certificate of analysis with the invoice that billed its batch.
   Doubtful values are flagged.
4. **Review**: a person sees each flagged value beside the page, corrects it if needed, and
   signs the record (approve or reject) with their PIN: an electronic signature.
5. **Search and ask**: every document is indexed. People search, or ask questions and get
   answers in which every statement rests on a quote found in the documents, with its page.
   Knowledge bases group documents to ask within.
6. **Integrate**: webhooks tell other systems when something happens; exports give signed
   records as CSV or JSON; AI agents (Claude Code and others) use DocForge through MCP.
7. **Trust**: every action goes into an append-only, hash-chained audit log, per organisation.

Every organisation's data is isolated in the database itself (row-level security).

---

## 2. How the web app talks to the API (keep this pattern)

```
Browser  ->  Next.js app (same origin)  ->  /api/v1/... route handler  ->  DocForge API /v1/...
              cookie df_session (HttpOnly,      adds Authorization: Bearer <session token>
              SameSite=strict)
```

- **The browser never holds a token.** Sign-in goes through `POST /api/session` on the web app,
  which calls `POST /v1/sessions` and stores the session token in an HttpOnly, SameSite=strict
  cookie. Every other call goes to the web app's `/api/v1/<path>`, a proxy
  (`web/src/app/api/v1/[...path]/route.ts`) that adds the token and refuses cross-site
  requests. Keep this: it is what makes the token unreachable by scripts.
- The proxy passes back these response headers only: `content-type`, `cache-control`,
  `retry-after`, `content-disposition`, `x-docforge-truncated`.
- A **401** from the proxy means the session ended: send the person to sign in, with the page
  to return to (`/login?next=/path`). The current app does this in `web/src/lib/api.ts`.
- The web app finds the API through `DOCFORGE_API_URL` (default `http://127.0.0.1:8000`).
- In production one host serves both: `/v1/*` and `/healthz` go to the API, everything else to
  the web app (`deploy/Caddyfile`). Integrations and AI agents call `/v1/*` directly with API
  keys.

---

## 3. Sign-in, roles and what each may do

| Who | How they sign in | Role | May |
| --- | --- | --- | --- |
| A person | Organisation name, email and PIN (`POST /v1/sessions` via the web app) | `reviewer` or `admin` | reviewer: read, review and sign; admin: everything |
| A self-service account's person | Email and password alone (section 14) | `member` | everything in its own private workspace except administration |
| The platform administrator | As a person of its organisation | its role, plus `platform_admin` | the platform screens; read-only looks into any workspace (`observer`, section 14.6) |
| A system (ERP, script) | API key `dfk_...` as `Authorization: Bearer` | `integrator` | read and upload, no review |
| An AI agent | API key, role `reader` | `reader` | read only, and only through `/v1/mcp` |

Permissions behind the roles: `documents:read`, `documents:write` (upload, reprocess, knowledge
base changes), `review` (correct, sign), `admin` (keys, webhooks, audit log, unanswered
questions, AI agents).

- **Sign in:** `POST /v1/sessions` `{tenant, email, pin}` -> `{token, expires_in_seconds}`
  (8 hours). Wrong details: 401 with one message whatever was wrong. Repeated failures: 429
  with `Retry-After`; five wrong PINs lock a reviewer for 15 minutes.
- **Sign out:** `DELETE /v1/sessions/current`.
- **Who is signed in:** `GET /v1/sessions/current` -> `{kind, name, email, role, organisation, credential, platform_admin, workspace}` (`kind` is `session` for a person; `email` and `credential` are null for a key). Use it for the person's name, to say "PIN" or "password" (`credential`), to show administration only when `role` is `admin`, and the platform screens only when `platform_admin` (section 14).
- **Correcting and signing ask for the PIN again** (`email` + `pin` in the body): a signature
  is a deliberate act, not a click.
- Admin-only screens answer **403** to others; show "Only administrators can see this page."
  The navigation does not hide them by role today (a good improvement).

---

## 4. Conventions every screen relies on

- **Errors:** always JSON `{"detail": "<a sentence a person can read>"}`, or for a validation
  error (422) `{"detail": [{"loc": [...], "msg": "...", ...}]}` - show "Some of what was
  entered is not valid" plus the field. Status codes: 400 bad request, 401 not signed in, 403
  not allowed, 404 not found (also for another organisation's ids), 409 conflict (state does
  not allow it), 413 too large, 415 file type not accepted, 422 invalid, 429 too many (with
  `Retry-After` where it applies), 503 a service (model, database, storage, queue) unavailable:
  try again later.
- **Ids** are UUIDs, except audit entries (integers) and API key prefixes (12 hex characters).
- **Times** are ISO 8601 in UTC. Show them in the person's local time, with the UTC instant in
  `<time dateTime>`.
- **Paging** is by cursor, never by page number, so nothing repeats or is skipped while new
  items arrive: pass the last item's cursor (`before` / `next_before`) to get the next page.
  `null` means there is no more.
- **Secrets are shown once** (API key tokens, webhook secrets): show them with a Copy button
  and "Done, I have copied it"; never store them; the replies are `Cache-Control: no-store`.
- **Limits** (429): searching 60/min per person; questions 20/min per person, 2 in flight,
  plus daily limits per organisation and person; open document event streams 5 per person;
  webhook test sends and re-sends 10/min, makes and enables 20/min; audit list 60/min, export
  6/min; chain checks 6/min per person; AI agent calls 60/min and 4 at once per key.

---

## 5. Screens

The current web app (`web/src/app`) has every screen below; they work and are accessible but
plain. Each section lists the endpoints a new design needs.

### 5.1 Sign in - `/login`
`POST /api/session` (web app) -> `POST /v1/sessions`. Fields: organisation, email, PIN. On
success go to `next` or the review queue.

### 5.2 Review queue (home) - `/`
`GET /v1/review/queue` -> `[{document_id, doc_type, filename, version_no, created_at, reasons,
match_status}]`: documents waiting for a person, oldest first, each with why (values flagged,
checks failed, a mismatch with its order). Click opens the document.

### 5.3 Documents - `/documents`
`GET /v1/documents?limit=&before=&doc_type=&stage=` -> `{items, next_before}`. Each item has
`id, filename, doc_type, status, stage, ready_for_chat, page_count, created_at`. Filters by
type and stage. Shows where each document is (section 6).

### 5.4 Upload - `/upload`
`POST /v1/documents` (multipart: `file`, `doc_type` = `invoice` | `purchase_order` | `coa` |
`general`) -> `{created, document, version}`. `created: false` means the same file was
already uploaded (identity is the content hash): open the existing one. Errors: 409 the same
file is already stored as another type; 413 too large (10 MB) or too many pages (20); 415 file
type not accepted; 422 unknown document type or a PDF that cannot be read; 503 the queue is
full or storage is unavailable. Then follow progress live (section 7).

### 5.5 A document - `/documents/{id}`
The heart of the product. Needs:

- `GET /v1/documents/{id}` -> `{document, versions}` (each reading of it is a version).
- `GET /v1/documents/{id}/timeline` -> `[{stage, at, detail}]`, and live:
  `GET /v1/documents/{id}/events` (SSE, section 7).
- `GET /v1/documents/{id}/pages/{n}` -> PNG of page n (from the original, or the PDF made from
  an office file). Draw boxes on it from the field and citation coordinates
  (`web/src/lib/geometry.ts` turns them into screen positions).
- `GET /v1/documents/{id}/review` -> `ReviewOut`: `decision`, `blockers`, `record` (the
  fields), `assessment` (each field's confidence, checks, flags), `editable_paths`,
  `corrections`, `match_status`, `discrepancies`, `counterpart_document_id`, `review` (the
  signature if signed), `signature_valid`, `record_sha256`, `meanings` (what a signature may
  mean), `superseded`, `certificates` (CoAs linked by batch).
- A purchase order's `record` (since `purchase-order-2`) also has each line's `hsn` and
  `amount` and the order's `total`; older orders show them as not printed. A total that is
  not the sum of the line amounts fails the `order.total` check, and the match adds
  `order.total` (the order total is neither the invoice's grand total nor its taxable value)
  and `line.hsn` (a matched line's HSN differs) - all warnings: shown, never blocking.
- `POST /v1/documents/{id}/corrections` `{path, text, reason, email, pin,
  expected_record_sha256?}` -> `ReviewOut`. `path` is one of `editable_paths` (e.g.
  `invoice_no`, `lines[0].qty`). Send the `record_sha256` you showed as
  `expected_record_sha256`: if the record changed meanwhile you get 409 ("The record changed
  after you opened it. Look at it again before correcting.") and must reload. It is optional;
  without it the correction is made as before.
- `POST /v1/documents/{id}/review` `{outcome: approved|rejected, meaning, reason,
  override_reason, expected_record_sha256, email, pin}` -> the signed record. Send the
  `record_sha256` you showed: if the record changed meanwhile you get 409 and must reload.
  `override_reason` is required to approve despite blockers.
- `GET /v1/documents/{id}/extraction` and `/assessment` (the raw extraction and checks; 404
  until there is one), `GET /v1/documents/{id}/audit` (this document's audit trail).
- `POST /v1/documents/{id}/reprocess` (read it again; 409 while a reading is under way).
- **Review claims** (so two people never change one document at once). A claim is a lease
  of 5 minutes on a document by one reviewer:
  - `POST /v1/documents/{id}/claim` `{take_over: false}` -> `ClaimOut` `{reviewer_name,
    claimed_at, expires_at, mine}`. Call it when an unsigned version is on screen and every
    60 s while it stays open: it claims the document, or renews your claim (`claimed_at`
    kept). If someone else holds a live claim it is returned unchanged with `mine: false`;
    keep polling and the claim becomes yours once theirs runs out. `take_over: true` moves
    it to you: administrators only (403 "Only an administrator can take over a review.").
    409 on a signed version. Signed-in reviewers only (an API key gets 403).
  - `DELETE /v1/documents/{id}/claim` -> 204: ends your own claim (on leaving the page);
    someone else's is left alone, still 204.
  - `ReviewOut.claim` is the live claim (`mine` relative to you) or null;
    `GET /v1/review/queue` items carry `claimed_by`: the name of another reviewer who has it
    open now, else null.
  - While another reviewer's claim lasts, corrections, signing and reprocess answer 409
    "{name} is reviewing this document. Wait until they finish, or ask an administrator to
    take over." An expired claim, or none, blocks nothing; signing ends the signer's claim.
    The audit log shows "Review started" (`review.claimed`) and "Review taken over"
    (`review.taken_over`, with `from_reviewer`).
- A **chat panel** asking about this one document (section 5.7 with `document_id`).
- A **general** document (no fields) shows its pages, timeline and chat only.

### 5.6 Search - `/search`
`GET /v1/search?q=&mode=hybrid|keyword&k=&doc_type=&document_id=&collection_id=` ->
`{query, mode, words_only, results: [{document_id, filename, doc_type, page, text, score,
matched_words, boxes}]}`. `words_only: true` means meaning-based search was unavailable and
only words were matched: say so. `matched_words` is true when the passage holds at least one
of the query's words ("Contains your words") and false when it is only close in meaning
("Similar in meaning"); when no result has it, nothing printed the words: say "No passage
contains these words; closest in meaning:" rather than showing them as finds. Passages
printing a code the query names always come first. Link each result to its page with the
boxes drawn.

### 5.7 Chat - `/chat` (and inside documents and knowledge bases)
- `POST /v1/chat` `{question, document_id?, collection_id?, conversation_id?}` -> `AnswerOut`:
  `{conversation_id, message_id, status, text, citations, dropped_citations,
  dropped_statements, reason, reason_detail, words_only}`.
- Prefer the stream `POST /v1/chat/stream` (same body; SSE, section 7): show each stage
  ("Searching", "Reading 8 passages", "Checking quotes"), then the answer.
- `status`: `supported` (every statement checked), `partly_supported` (some statements or
  quotes dropped: say how many), `unsupported` (withheld: its quotes or figures did not hold),
  `not_found` (the documents do not say). For the last two show `reason` in words:
  `no_passages` (nothing matched in this scope), `not_in_passages` (with
  `reason_detail.missing` - what was missing - and `documents` read), `quotes_not_found`,
  `figures_not_in_quotes`, `wording_not_in_passages`, `model_error`, `not_recorded`;
  `reason_detail.held_back` = passages held back because they read like instructions to an AI.
  `web/src/lib/chat.ts` has the current wording for each.
- Each **citation**: `{document_id, filename, doc_type, page, quote, boxes}` - show the quote
  and link to the page with the boxes outlined.
- Follow-ups pass `conversation_id`; a conversation keeps its scope (one document, one
  knowledge base, or everything). 409 if its document or knowledge base was deleted; 422 if
  the scope is changed mid-conversation.
- Conversations: `GET /v1/conversations`, `GET /v1/conversations/{id}` (messages),
  `DELETE /v1/conversations/{id}`.

### 5.8 Knowledge bases - `/collections`, `/collections/{id}`
`GET/POST /v1/collections` `{name, description}`, `PATCH /v1/collections/{id}`,
`DELETE /v1/collections/{id}` (documents are kept), `GET/POST
/v1/collections/{id}/documents` `{document_ids}` (up to 500 at once), `DELETE
/v1/collections/{id}/documents/{document_id}`. Names are unique per organisation, whatever
their case (409). A chat inside it asks within its documents only.

### 5.9 Unanswered questions - `/questions` (admin)
`GET /v1/questions/unanswered` -> `{questions: [{message_id, question, reason, missing,
documents, owner, conversation_id, created_at}], by_reason: {reason: count}}`: what the
organisation asked that its documents could not answer, and why - often a document worth
adding.

### 5.10 AI agents - `/agents` (admin)
`GET/POST /v1/api-keys` (POST `{name}` makes a read-only `reader` key; the token is in the
reply only; at most 20 keys), `DELETE /v1/api-keys/{prefix}` (revoke),
`GET /v1/agent-calls` (latest calls: key, tool, outcome, time). Show the endpoint
(`<origin>/v1/mcp`) and the command:
`claude mcp add --transport http docforge <origin>/v1/mcp --header "Authorization: Bearer <key>"`.

### 5.11 Audit log - `/audit` (admin)
`GET /v1/audit?action=&actor=&target_type=&target_id=&from=&to=&before=&limit=` ->
`{items: [{id, occurred_at, actor, actor_name, action, action_label, target_type, target_id,
target_label, details, hidden_details}], next_before}`. `GET /v1/audit/filters` -> the
choices (actions with labels; people, keys and system). `GET /v1/audit/export.csv` (same
filters; `X-DocForge-Truncated: true` when cut at 10,000 rows - say so).
`GET /v1/audit/verification` -> `{consistent, entries, first_bad_id, reason,
anchors_checked}`: "Verified: N entries, chain intact" or "Broken at entry N: reason"; with
`anchors_checked: 0` add that a full rewrite would not be detected without an outside anchor.
Keep filters in the URL. `from` is inclusive, `to` exclusive: send the start of the day after
the last chosen day.

### 5.12 Webhooks - `/webhooks` (admin)
`GET /v1/webhooks` (each with `last_delivery`, `secret_rotated_at`), `GET
/v1/webhooks/events` (what may be subscribed to), `POST /v1/webhooks` `{url, events}` ->
`{id, secret}` (shown once; at most 10; https and public addresses only - 422 otherwise),
`PATCH /v1/webhooks/{id}` `{active}`, `DELETE /v1/webhooks/{id}` (kept as a record),
`POST /v1/webhooks/{id}/secret` (rotate: new secret once; receivers refuse deliveries until
updated - warn first), `POST /v1/webhooks/{id}/test` (409 when disabled),
`GET /v1/webhooks/{id}/deliveries?before=&status=` -> `[{id, event_id, event_type, status,
attempts, last_status, last_error, delivered_at, created_at, last_attempt_at,
next_attempt_at}]`, `POST /v1/webhooks/{id}/deliveries/{delivery_id}/resend` (failed only).
Show URLs with their path and query hidden (they can hold a receiver's token).

### 5.13 Evals and cost - `/evals`
`GET /v1/evals` -> the measured quality and cost of each eval suite (fields correct, wrong
answers, cross-tenant leaks, tokens and cost per document). A credibility page: "measured, not
claimed".

### 5.14 Exports
`GET /v1/exports/documents.csv` and `/documents.json` - every signed record of the
organisation (CSV is formula-safe; at most 10,000, `X-DocForge-Truncated` when cut).

---

## 6. A document's life: stages and statuses

`document.stage` (where it is now), in order:

| Stage | Means | Show |
| --- | --- | --- |
| `stored` | received, waiting to be read | Queued |
| `converting` | an office file or image made into a PDF | Converting |
| `parsing` | text and tables read | Reading |
| `extracting` | fields read by the model | Extracting |
| `checking` | values checked, matched with its order | Checking |
| `indexing` | made searchable | Indexing |
| `processed` | finished, not searchable (no index) | Done |
| `ready` | finished and searchable: chat can use it (`ready_for_chat: true`) | Ready |
| `retrying` | a temporary failure; it will try again | Retrying |
| `failed` | it could not be read; `detail` says why in plain words | Failed |

`document.status` is the business state: `received` -> `processing` -> `extracted` (fields
ready for review) or `failed`. A review's outcome lives in the review (`approved` / `rejected`).
`decision` in a review is DocForge's own assessment (whether it can be approved, or needs a
person).

---

## 7. Live updates (Server-Sent Events)

Both streams are `text/event-stream`; read them with `fetch` and a stream reader (not
`EventSource`, which cannot send a POST or keep the cookie pattern simply). A last event may
arrive without its blank line at the end: handle it. `web/src/lib/chat.ts` `readEvents` does
this.

- **A document's progress:** `GET /v1/documents/{id}/events` -> `event: stage`
  `data: {stage, at, detail?}` for each stage reached, then it ends at `ready`, `processed` or
  `failed` (or after 10 minutes: fall back to polling `/timeline`). At most 5 open per person
  (429: poll instead).
- **A chat answer:** `POST /v1/chat/stream` -> `event: stage` `data: {stage: searching}` /
  `{stage: reading, passages: 8}` / `{stage: checking}`, then `event: answer` `data:
  AnswerOut`, or `event: error` `data: {status, detail}`. Stopping the stream does not stop the
  question being answered and stored; it can be read in its conversation.

---

## 8. Webhook events (for integrators; shown on the webhooks screen)

Events: `document.processed`, `document.ready_for_chat`, `review.signed`, `webhook.test`.
Each delivery is a POST with JSON `{id, type, created_at, data}`, the header
`DocForge-Event-Id` (the same on every retry) and `DocForge-Signature: t=<unix>,v1=<hex>`
(HMAC-SHA256 of `"<t>." + body` with the webhook's secret). A 2xx answer is delivered;
anything else is retried for about half an hour.

`review.signed` data: `{document_id, doc_type, version_no, outcome, meaning, signed_by,
signed_at, record_sha256, draft}`. `draft` is null unless an invoice was approved; then it is
the payment approval draft `{type: "payment_approval_draft", status: "draft", approval_status:
"approved", payee {name, gstin}, invoice_no, invoice_date, amount, currency, po_no,
approved_by, approved_at, record_sha256}`. `status` is the draft's own lifecycle and is
always `draft`: DocForge never pays, the receiving system (the ERP) acts on it.
`approval_status` is the decision. The draft is not part of what the signature covers.

---

## 9. States every screen should handle

Loading; empty ("No documents yet", "No entries match these filters"); error (`role="alert"`
with the `detail`); 403 for admin pages; 401 -> sign in; 429 -> "wait a minute" (use
`Retry-After`); 503 -> "try again shortly"; a stale answer from an older request must never
overwrite a newer one (the current pages keep a counter of loads and show only the newest).

---

## 10. Accessibility the current app already meets (keep it)

Every input labelled; tables with `th scope="col"`; status messages in polite live regions;
focus moved to new content (a secret, a new answer) and kept when a row disappears; buttons
named per row ("Delete the webhook to ..."); usable at phone width (wide tables scroll inside
themselves); `prefers-color-scheme` respected.

---

## 11. Running it locally

- Backend and web together: the demo stack in `.deploytest/` (`docker compose -p
  docforge-demo ...`), at https://localhost. It replays recorded model answers, so it needs no
  AI key.
- Web only, against that API: `cd web && npm run dev` with `DOCFORGE_API_URL` pointing at the
  API.
- Tests: `make test` (backend), `make web-check` (types, lint, unit, build), `make e2e`
  (browser).

---

## 12. Where to look in the code

| What | Where |
| --- | --- |
| API contract | `docs/api/openapi.json` |
| Every endpoint | `src/docforge/api/*.py` |
| Current pages | `web/src/app/**/page.tsx` |
| API client and types | `web/src/lib/api.ts` |
| Chat stream reading, answer wording | `web/src/lib/chat.ts` |
| Page coordinates to screen boxes | `web/src/lib/geometry.ts` |
| Session proxy | `web/src/app/api/session/route.ts`, `web/src/app/api/v1/[...path]/route.ts`, `web/src/lib/server.ts` |
| What was built, measured and limited, checkpoint by checkpoint | `docs/progress.md` |

---

## 13. AI agents (MCP) - not a screen, but part of the product

`POST /v1/mcp` (Streamable HTTP, JSON-RPC) with a `reader` key. Six read-only tools:
`list_knowledge_bases`, `list_documents`, `search_documents`, `ask` (the same checked,
cited answers as chat), `get_document` (fields), `get_page_text`. The agents page (5.10) makes
the keys; nothing else in the UI is needed.

---

## 14. Accounts, members, the platform administrator, costs and deleting (final sprint)

Everything here is in `docs/api/openapi.json`. **The rule behind all of it: no one can learn
anything about another person's documents.** Each self-service account is its own private
workspace (a tenant), separated by the same row-level security as organisations; a deleted
document is invisible to the application's own database role.

### 14.1 Sign up - `POST /v1/accounts`
Only when the deployment sets `SIGNUP_ENABLED=true`; otherwise **404** (hide the sign-up link
when it is 404). Body `{"name": 1..120 chars, "email", "password": 10..128 chars, not all
spaces}` -> **201** `{token, expires_in_seconds}`: the person is signed in, as after
`POST /v1/sessions`. It makes a private workspace shown as "<name>'s workspace" with the
person in it as a `member`.

| Status | `detail` | When |
| --- | --- | --- |
| 409 | `An account with this email already exists.` | the email (case and spaces aside) has an account |
| 422 | a sentence (e.g. `A password is 10 to 128 characters.`, `That is not an email address.`) or the usual validation list | a field is not acceptable; extra fields are refused |
| 429 + `Retry-After` | `Too many accounts were made from this address. Try again later.` | 5 attempts an hour per client address (every attempt counts) |
| 429 + `Retry-After` | `New accounts are paused for today. Try again tomorrow.` | `SIGNUPS_PER_DAY` (default 200) accounts in the last 24 hours |

### 14.2 Sign in - `POST /v1/sessions`
- An account's person: `{"email", "password"}` (no `tenant`). Wrong email or wrong password:
  **401** `The email or password is not right.` - the same for both, on purpose.
- An organisation's reviewer, as before: `{"tenant", "email", "pin"}`; 401
  `The organisation, email or PIN is not right.`
- Send exactly one of `pin` and `password` (either field carries either secret); both or
  neither is 422. Five wrong secrets lock the person for 15 minutes; too many failures from one
  address give 429 with `Retry-After`.
- Correcting and signing still ask for the secret again: a member sends its **password** in
  the same `pin` field (up to 128 characters) with its `email`.

### 14.3 Who is signed in - `GET /v1/sessions/current`
`{kind, name, email, role, organisation, credential, platform_admin, workspace}`:
- `role`: `member` (an account's person), `reviewer`, `admin`, `integrator`, `reader`, or
  `observer` (the platform administrator looking into a workspace, 14.6).
- `credential`: `"password"` (say "password" in the sign-in, correction and signing forms) or
  `"pin"`; `null` for an API key.
- `workspace`: `"personal"` or `"organisation"`. `organisation` is the name to show - for a
  personal workspace "<name>'s workspace".
- `platform_admin`: show the platform screens (14.6) only when true.

### 14.4 What a member may do
Role `member` = `documents:read`, `documents:write`, `review`: upload, list and read its
documents, delete them, reprocess, correct, sign, claim, search, chat, knowledge bases, exports
and the eval page. **403** on every administrator's route: `/v1/api-keys*`,
`/v1/agent-calls`, `/v1/audit`, `/v1/audit/filters`, `/v1/audit/export.csv`,
`/v1/questions/unanswered`, `/v1/webhooks*`, `/v1/platform/*`, and the stateless preview
`POST /v1/extractions`. Hide those in the navigation for `member`.

A free workspace's caps (organisations are not held to them), each **429** with this `detail`:

| Cap (setting, default) | `detail` |
| --- | --- |
| live documents (`MEMBER_MAX_DOCUMENTS`, 30) | `This free workspace holds up to 30 documents. Delete one to upload another.` |
| new uploads a day, UTC, deleted ones included (`MEMBER_UPLOADS_PER_DAY`, 15) | `This free workspace takes up to 15 uploads a day. Try again tomorrow.` (+ `Retry-After`, seconds to midnight UTC) |
| questions a day, UTC (`MEMBER_QUESTIONS_PER_DAY`, 40) | `This free workspace has 40 questions a day. Try again tomorrow.` (also as the `error` event of `/v1/chat/stream`) |

Uploading a file the workspace already holds is not a new upload (200, `created: false`).

### 14.5 Deleting a document - `DELETE /v1/documents/{id}`
`documents:write` (member, admin, integrator) -> **204**. **404** when unknown, already deleted
or not the caller's; **409** while another reviewer has it claimed. The document disappears
everywhere: lists, the queue, every `/v1/documents/{id}/...` read (404), page images, search,
chat (it is no longer read or cited; a conversation about it answers 409, and earlier answers
read back without its quotes), knowledge bases and their counts, exports, agents' tools,
matching (its counterpart shows `no_counterpart`), stats and the platform figures. It is kept
for its audit trail (`document.deleted`, "Document deleted"), and the same file can be uploaded
again as a new document. Ask "Delete this document? It disappears from search and chat." first.

### 14.6 Platform administrator (the owner)
Made with `python -m docforge.review make-platform-admin --tenant <org> --email <email>`
(or by the admin role with `PLATFORM_ADMIN_TENANT` and `PLATFORM_ADMIN_EMAIL`). Only that
person's **session** reaches these; everyone else (members, organisation admins, any API
key) gets **403**.

- `GET /v1/platform/overview` -> `{"totals": {workspaces, accounts, documents, pages, signed,
  questions, model_cost_usd, documents_last_7_days, signups_last_7_days}, "workspaces":
  [{tenant_id, organisation, kind, owner_name, owner_email, created_at, last_active_at,
  documents, pages, signed, questions, model_cost_usd, sign_ins}]}` - most recently active
  first; `last_active_at` is the newest audit entry or sign-in. Counts only; deleted documents
  are left out; no document names or contents.
- `GET /v1/platform/activity?limit=50&before=<id>&tenant_id=<uuid>` -> `{"items": [{id,
  occurred_at, tenant_id, organisation, actor_name, action, action_label, target_label}],
  "next_before": int | null}`, newest first across every workspace (`limit` 1..200).
  `target_label` is the kind of thing ("Document", "Knowledge base", "Person", ...), never a
  name; no details are sent.
- **Looking into a workspace (read-only):** send `X-DocForge-Workspace: <tenant_id>` with the
  administrator's session on **GET** requests; the request is served in that workspace as
  role `observer` (`documents:read`). Any other method with the header, any route outside
  `/v1` and `/v1/mcp` -> **403**; the header from anyone but a platform administrator -> **403**
  (it is never ignored); an unknown or malformed workspace -> **404** `No such workspace.`
  The review queue and review screen (they need `review`) and administration are 403 to an
  observer; a member's conversations are not visible to it. Each look is audited in that
  workspace (`platform.workspace_viewed`) at most once an hour. Show a clear "Viewing <name>'s
  workspace - read only" banner and drop the header to leave.

### 14.7 Costs and the workspace's figures
- `GET /v1/documents/{id}` adds `cost: {model_cost_usd, input_tokens, output_tokens,
  model_calls}` over every reading of the document (thinking tokens count as output), and its
  `document.model_cost_usd`; each item of `GET /v1/documents` has `model_cost_usd`. It is
  `null` when a model's price is not configured (`MODEL_PRICES`): show "not priced", not $0.
- `GET /v1/stats` (any credential that can read) -> `{documents, ready, awaiting_review,
  signed, failed, documents_last_7_days, median_read_seconds, model_cost_usd}` for the caller's
  workspace only, deleted documents left out. `ready` counts documents read and finished
  (`ready` or `processed`); `awaiting_review` is the review queue's length; `median_read_seconds`
  is from upload (or reprocess) to a finished reading, `null` before the first;
  `model_cost_usd` covers readings and questions.
