# V2 P5: Invoices by email

Branch `v2-p5-email-intake` (stacked on P4's `v2-p4-purchase-register`; migration 0046).

Each workspace gets a forwarding address. A bill forwarded to it is read exactly as an upload
is; the sender gets a plain reply saying what happened to each attachment. No WhatsApp.

## Decisions (made before coding)

### Transport

- **One provider-agnostic way in:** `POST /v1/intake/inbound` takes a parsed email as JSON
  (`from`, `to[]`, `subject`, `message_id`, `attachments[]` of `filename`, `content_type`,
  `content` base64, `inline`). Everything past it works on one internal object,
  `intake.model.InboundEmail`; no business code imports a provider.
- **One provider adapter: Postmark inbound** (`POST /v1/intake/inbound/postmark`). It turns
  Postmark's inbound webhook JSON (`FromFull`, `ToFull` / `OriginalRecipient`, `Subject`,
  `MessageID`, `Attachments[]` of `Name`, `Content`, `ContentType`, `ContentID`) into an
  `InboundEmail`, nothing more. Chosen because the deployment sends no mail today, Postmark does
  inbound parsing and outbound sending on one account, and its inbound webhook needs no SDK.
  Postmark does not sign webhooks; its documented protection is HTTP Basic credentials in the
  webhook URL, so both routes require HTTP Basic with `INTAKE_WEBHOOK_PASSWORD` (compared in
  constant time) and are refused when it is unset.
- **Replies go out by plain SMTP** (`SMTP_URL`, Python's `smtplib`): Postmark's SMTP server in
  this deployment, or any mailbox's. Unset: no replies are sent (logged), intake still works.
- **Zero-new-vendor fallback, not built:** IMAP polling of a dedicated catch-all mailbox would
  produce the same `InboundEmail`; it is a second adapter, left for when it is wanted.
- **What the owner must set up:** a domain or subdomain for intake (e.g. `in.example.com`, MX
  to Postmark), a Postmark inbound stream whose webhook is
  `https://intake:<password>@<api host>/v1/intake/inbound/postmark`, and the settings
  `INTAKE_DOMAIN`, `INTAKE_WEBHOOK_PASSWORD`, `SMTP_URL`, `INTAKE_REPLY_FROM`.

### Address

`<workspace-slug>-<token>@<INTAKE_DOMAIN>`. The token is 128 random bits, written in lower-case
base32 (26 characters: mail systems may change the case of a local part). Only its SHA-256 is
stored, so the full address is **shown once**, when an admin makes or regenerates it; after
that the screen shows it masked. Regenerating revokes the old address at once: mail to it makes
nothing, is recorded in the workspace's Intake as "rejected: address no longer in use", and
gets no reply. The slug is only for people to read: the token alone finds the workspace,
through a `SECURITY DEFINER` lookup (row-level security otherwise keeps every workspace's rows
out of reach).

### Senders

Each workspace keeps an allowlist of whole addresses (`asha@supplier.in`) and domains
(`@supplier.in`), managed by its admins. Mail from anyone else is **held** in the Intake list for
a reviewer to accept (it then runs through the pipeline) or discard (its held copies are
deleted); it is never processed by itself and never deleted by itself. "Trust a sender once
accepted" is a workspace setting, off unless an admin turns it on.

Replies go **only to listed senders**: a held mail's sender may be forged, and replying to a
forged address sends mail to someone who never wrote (backscatter).

### Every attachment as an upload

`docforge.upload_checks.check_file` holds the upload checks - size (10 MB by default), the
format sniffed from the bytes (`formats.py`), older Office or password-protected files, a
password-protected or unreadable PDF, the page limit - with the upload's own messages. The
upload endpoint now calls it too, so the two can never drift. An accepted attachment goes
through `DocumentService.ingest`, the upload's own entry point (`source="email"`), so the
same bytes from any channel are one document ("already received").

Ignored, with the reason recorded, not failed: `winmail.dat` (Outlook's formatting), text,
HTML, calendar and contact files, and images under 30 KB (logos and signatures; a phone photo
of a bill is far larger). One PDF holding several bills is read as one document, and the reply
says each attached file is one document.

### Limits and failures

- Per sender and workspace, at most 100 documents an hour by default (a workspace setting);
  beyond it attachments are held with the reason "rate limit".
- Intake leaves 100 places of the workspace's processing queue to uploads: when the queue is
  that close to full, attachments are held with the reason "queue busy".
- Each attachment is taken on its own: one that fails cannot stop the rest of the mail, nor any
  later mail. A document whose reading fails is retried by the worker's rules and shows in the
  Intake list as failed, with a retry action (the document's own "read again").

### Who

Admins (or a personal workspace's own person, as for the workspace's other settings) see and
change the address and allowlist; those who review accept or discard held
mail and retry failed ones; everyone in the workspace sees the Intake list and the Source
filter.

### Audit

`intake.received` (sender, attachments by outcome), `intake.held` (sender, reason),
`intake.accepted`, `intake.discarded`, `intake.address_created`, `intake.token_revoked`,
`intake.sender_added`, `intake.sender_removed`. Sender addresses, never content or filenames.

## Built

- `upload_checks.py`: the upload's checks and messages, now called by the upload endpoint and
  by intake alike.
- `intake/model.py` (`InboundEmail`), `intake/postmark.py` (the one adapter),
  `intake/mailer.py` (SMTP replies, `Auto-Submitted: auto-replied`; never to `mailer-daemon`,
  `no-reply` and the like; never for mail from the intake domain itself, so no loop),
  `intake/service.py` (addresses, allowlist, delivery, holding, accept, discard, retry, the
  test), `api/intake.py`, migration 0046, `documents.source` and the `source` filter on
  `GET /v1/documents`.
- The test button puts a made-up invoice (new bytes each time) through intake as mail from
  DocForge - every step after the mail provider - so it shows in the Intake list and the
  documents; it cannot send real mail to the address, whose token is not kept.

## API

| Route | Who |
| --- | --- |
| `POST /v1/intake/inbound`, `POST /v1/intake/inbound/postmark` | the mail provider (HTTP Basic) |
| `GET/PATCH /v1/settings/intake`, `POST .../address`, `POST/DELETE .../senders`, `POST .../test` | admins, or a personal workspace's person |
| `GET /v1/intake/messages?view=all|held|failed|ignored|rejected` | readers |
| `POST /v1/intake/messages/{id}/accept`, `.../discard`, `POST /v1/intake/items/{id}/retry` | reviewers |
| `GET /v1/documents?source=email|upload` | readers |

The provider's routes take up to 50 MB (a mail's attachments in base64); every other route
keeps the upload's limit.

## Measured (local Postgres 16, in-memory object store)

| What | Result |
| --- | --- |
| 200 mails of 1 to 3 attachments from 10 senders (399 documents queued) | 10.3 s in all; 48 ms a mail at the median, 87 ms at p95 |
| An upload straight after | accepted: intake leaves the queue's last 100 places to uploads |
| One sender, 140 attachments in a few seconds (default 100 an hour) | 100 queued, 40 held "rate limit", in 2.3 s |

Intake only stores and queues; the reading is the worker's, as for uploads.

## Tests

`tests/integration/test_email_intake.py`: AC-P5-1 (one PDF, one document, source email, read
to the end), AC-P5-2 (mailed then uploaded: one document; "already received"), AC-P5-3 (held,
accepted, discarded, audited; trust-once off by default; a listed domain), AC-P5-4 (revoked
address: rejected, nothing made, no reply; the new one works; shown once), AC-P5-5 (logo and
note ignored with reasons, all three in the reply), AC-P5-6 (too large and not accepted, in
the upload's words; the rest read), AC-P5-7 (no value from the bill in the reply), AC-P5-8
(swapped tokens: each reaches only its own workspace), the rate limit, the queue reserve, a
poison attachment, a failed reading retried, the test button, the Postmark and generic routes
with and without the password, roles. `test_account_deletion.py`: every intake table and the
held copies erased with the account.

## Left for later

- The owner's set-up: the intake domain's MX, the Postmark inbound stream and its webhook
  URL, `INTAKE_DOMAIN`, `INTAKE_WEBHOOK_PASSWORD`, `SMTP_URL`, `INTAKE_REPLY_FROM`, `WEB_URL`.
  Until then intake is off (the provider's routes answer 503; the screen says so).
- The IMAP adapter, if a deployment wants no mail provider.
- One PDF holding several bills is split in P6.
