# Research: document status, chat with documents, knowledge bases, Google Drive connector

Asked for on 2026-10-03, after testing the local demo. This note records:
- what DocForge already has;
- what the new features need;
- what the external services allow (checked 2026-10-03);
- a staged plan as checkpoints C10–C13.

## 1. What already exists (so the plan builds on it)

| Asked for | Already there | Missing |
| --- | --- | --- |
| Upload goes to S3, then the pipeline | Upload stores the original in object storage (S3, or MinIO locally), records a version, and queues the work. Parse, extract, check, then a search indexing job | Only PDFs; only the three typed document packs (invoice, order, certificate) |
| Status the user can follow | `document.status`: received → processing → extracted / failed. The audit trail records every step | No "indexing" or "ready to chat" status: the index job changes no status. The UI does not show a live timeline |
| Chat about a document | Hybrid search with page citations, per tenant, measured (held-out recall@5 0.88) | No answer generation, no conversations, no "I don't know" (the held-out eval showed hybrid search never abstains) |
| Knowledge base | All of an organisation's documents are searchable together | No collections ("knowledge bases") to group documents and scope chat |
| Google Drive connector | Webhooks out, API keys, a queue that can run sync jobs | No connectors, no OAuth token storage, no sync state |

## 2. Status timeline

**Design.**
- **Stages:** `uploaded → stored → parsing → extracting → checking → indexing → ready`, plus `needs review` and `failed` (with the reason).
- **Separate the record status from the readiness to chat.** A record can need review and still be ready to chat with. So the fields are:
  - `processing_stage`, the pipeline stage;
  - `record_status`: accepted / needs review / signed;
  - `ready_for_chat`, true once indexed.
- **How the screen learns of changes:** each stage writes a row to a `document_events` table, in the same transaction as the work. The UI uses Server-Sent Events (`GET /v1/documents/{id}/events`), falling back to polling. SSE is one-way, works through Caddy, and needs no WebSocket server.
- **Webhooks and the audit trail:** the existing `document.processed` webhook gains `document.ready_for_chat`. The audit trail already holds the rest.

**Cost:** small. The index job sets the stage. The events table needs a migration plus row-level security, like every other table.

## 3. Chat with a document (retrieval-augmented answers)

**Flow:**
1. Retrieve the top chunks with the existing hybrid search, scoped to the document or knowledge base.
2. Gemini answers with structured output: `{answer, citations: [chunk ids], unanswerable: bool}`.
3. Check each citation against its chunk text, the way the trust layer already checks extracted values, so an answer cannot cite a passage that does not support it.
4. Return the answer with page boxes. The UI highlights them on the page image, as the review screen already does.

**Risks this project has already measured or met:**
- **No abstention:** hybrid search always returns something. The answer step must be allowed to say "not in these documents", and a no-answer question set must check that it does.
- **Prompt injection:** document text is data, never instructions. The extraction prompt's fencing is reused; the chat model gets no tools.
- **Cost:** about 8–10 chunks of context per question, a few thousand tokens. At the Flash-Lite price checked for C8, that is a fraction of a cent per question. It is metered per question with the C8 cost tracking, and capped per organisation.
- **Tenancy:** retrieval already runs under row-level security. Conversations are stored per tenant, with RLS like everything else.

**Evaluation (must exist before it ships, as in C8):** a question set over the synthetic documents scored on:
- answer correctness;
- citation support (does the cited text contain the answer);
- abstention on unanswerable questions;
- no cross-tenant leaks.

It is recorded live once and replayed in CI behind the same gate.

## 4. Knowledge bases

- **Model:** a `collections` table (a knowledge base), with a many-to-many link to documents. A document can be in several knowledge bases.
- **Chat scope:** one document, one knowledge base, or the whole organisation.
- **General documents, not only the typed packs:** a document type `general`, which is parsed and indexed but not field-extracted, so anyone can upload contracts, SOPs and manuals. Docling already reads DOCX, PPTX, HTML and images as well as PDF; the upload check and the parser options need widening.
- **Permissions:** per organisation for now (row-level security). Per-collection roles can come later.

## 5. Google Drive connector

**What Google allows (checked 2026-10-03):**
- **Restricted scopes:**
  - `drive` and `drive.readonly`, which an app needs to read all files or watch arbitrary folders, are restricted scopes.
  - They need restricted-scope OAuth app verification.
  - If the data is stored on our servers, they also need a security assessment. That is a paid, yearly third-party (CASA) review.
  - [Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth).
- **`drive.file`:**
  - This scope is non-sensitive: no verification burden.
  - It covers only files the user picks with the Google Picker, or that the app creates.
  - Whether picking a folder grants access to the files in it, and to files added later, is unclear: sources disagree ([community thread](https://groups.google.com/g/google-apps-script-community/c/68PKyPeb0jA), [2025 write-up](https://www.agenticfabriq.com/blog/google-drive-agent-access)). It needs a one-day spike before we rely on it.
- **Change notifications:**
  - `changes.watch` and `files.watch` call an HTTPS endpoint with a valid certificate.
  - A changes channel lasts at most a week, a file channel a day.
  - Channels must be renewed by hand.
  - A notification carries no content, only a signal to call the API ([push notifications](https://developers.google.com/workspace/drive/api/guides/push)).
  - The reliable pattern is `changes.getStartPageToken`, then `changes.list` from a stored token ([tracking changes](https://developers.google.com/workspace/drive/api/guides/manage-changes)). Push is then only a hint to sync sooner.
- **Google Docs** are not files: they are exported (`files.export`) to PDF or DOCX, then parsed like any upload.

**Three ways to connect, by verification burden:**

| Option | How it works | Verification | Fits |
| --- | --- | --- | --- |
| A. Share with a service account | The customer shares a Drive folder (or shared drive) with DocForge's service-account email. We list and sync it with the service account's own credentials | No user OAuth consent screen, so the restricted-scope review does not apply; confirm with Google's policy before launch | B2B: an admin shares one folder. Simplest and most robust |
| B. OAuth + Picker with `drive.file` | Each user picks files (or a folder, if the spike shows that works) | None (non-sensitive scope) | Per-user picking; folder sync is uncertain |
| C. OAuth with `drive.readonly` | Full read and watch of the user's Drive | Restricted: verification plus a yearly security assessment | Only once there are paying customers who need it |

**Recommendation: A first, B as the per-user option, C only if a customer requires it.**

**Sync engine (the same shape for every connector):**
- **Data:**
  - `connections`: per tenant; encrypted credentials or the service-account binding.
  - `sync_sources`: the folder to sync, which knowledge base it feeds, and the last change token.
  - `synced_items`: the external file id and its version, mapped to our document.
- **The job:** a `sync` queue job runs every few minutes, and sooner on a push notification. It:
  1. calls `changes.list` from the stored token;
  2. downloads or exports new and changed files;
  3. ingests them through the normal upload path (so the content hash removes duplicates);
  4. records the next token.
- **Deletes and moves:**
  - A deleted or moved-out file is removed from the knowledge base. The record stays: the audit trail is append-only.
  - A changed file becomes a new version, as reprocessing already does.
- **Other connectors later:** OneDrive and SharePoint (Microsoft Graph delta queries) and Dropbox follow the same tables and job; only the client differs.

## 6. Staged plan

| Checkpoint | Delivers | Gate (measured, as before) |
| --- | --- | --- |
| C10 Status and general documents | Stage timeline with live updates (SSE), `ready_for_chat`, `document.ready_for_chat` webhook, `general` document type, DOCX/PPTX/image upload | Every stage visible in the UI within 2 s of happening; a DOCX goes from upload to ready; the e2e test follows a document to "ready" |
| C11 Chat with a document | Answers with checked citations and page highlights, abstention, conversation history, per-question cost | Answer eval: correctness, citation support, abstention, 0 cross-tenant leaks; gated in CI |
| C12 Knowledge bases | Collections, chat across a knowledge base or the organisation, KB management UI | Retrieval and answer evals across 2+ knowledge bases; isolation tests |
| C13 Google Drive connector | Service-account folder sync (A), then Picker (B); sync status in the UI; deletes and changes handled | Adding, changing and removing a file in a test folder each show up in the knowledge base within one sync cycle; re-sync is idempotent |

Each checkpoint runs as before: tests first, ECC reviewers, gate, records.

## 7. Decisions for the owner

1. **Connector approach:** start with the service-account share (A)? Or is per-user Picker (B) needed from day one?
2. **General documents:** should anyone be able to upload any document (contracts, SOPs), or only the typed packs plus chat?
3. **Chat model:** Gemini Flash-Lite (cheapest) or a stronger model for answers? Answers are judged by the C11 eval either way.
4. **The public demo:** should visitors be able to chat? It would need the model key on the demo and a cost cap.
5. **Order:** C10 → C13 as above, or the connector earlier?

## 8. Decided (2026-10-03)

1. Drive connector: the shared folder with our service account first (option A).
2. Any document can be uploaded for chat: a `general` type alongside the checked packs.
3. Chat answers with Gemini Flash-Lite; the C11 answer eval decides if it is good enough.
4. Build in order: C10 status and general documents, C11 chat, C12 knowledge bases, C13 Drive.
