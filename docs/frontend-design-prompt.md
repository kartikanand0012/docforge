# Prompt for Claude Design: the DocForge web app

Paste everything below the line into Claude Design. Attach `docs/frontend-handover.md` (and,
if it accepts files, `docs/api/openapi.json`) so it has every endpoint and field.

---

Design the web app for **DocForge**, a document-intelligence product for finance and quality
teams (Indian pharma distributors and manufacturers first). It reads invoices, purchase
orders and certificates of analysis (CoAs), extracts their fields, checks them, matches each
invoice to its order, and sends anything doubtful to a person who corrects and signs it. It
also answers questions about the documents with quotes that link to the exact place on the
page. The backend is finished; the attached handover document lists every screen, endpoint,
field, state and error. Design the frontend to sit on it without new backend work.

## Who uses it

- **Reviewers** (accounts-payable clerks, QA officers): work through a queue of documents
  that need a person, all day, mostly keyboard. They must see *why* each one needs them,
  compare a value with the page it came from, correct it, and sign with their PIN.
- **Administrators**: everything a reviewer does, plus API keys, AI-agent keys, webhooks,
  the audit log, unanswered questions and cost/quality figures.
- **Buyers evaluating the product** (often via a demo): must understand in one minute that
  every value is traceable to its source and nothing is accepted without evidence.

## What the design must make obvious

1. **Evidence first.** Every extracted value sits next to the page image with a box drawn
   around where it was read. Hovering or focusing a value highlights its box; clicking a box
   selects its value. Chat answers show their quotes, each linked to its page and box.
2. **Why a person is needed.** The queue and the document view state the reasons in words
   (a check failed, a value is uncertain, the invoice does not match its order), never only
   as a colour or an icon.
3. **Trust and control.** Signing is a deliberate act: PIN, a stated meaning ("I approve
   this invoice for payment"), the record's hash shown; a stale record is refused with a
   clear "this changed since you opened it - reload".
4. **Where each document is.** A document moves through stages (stored, converting, reading,
   extracting, checking, indexing, ready, or failed with a reason); show progress live and
   failures in plain words with what to do next.

## Screens (details and endpoints in the handover, section 5)

Sign in; review queue (home); documents list with type and stage filters; upload (drag and
drop, many files, per-file progress and outcome, including refusals such as "protected by a
password" or "older Office file"); **the document view** (the heart: page viewer with boxes,
fields with confidence and checks, corrections, order match and discrepancies, linked CoAs,
signature, timeline, audit trail, and a chat panel about this document); search; chat
(streamed stages, then the answer with quotes; follow-ups; conversations list); knowledge
bases; unanswered questions (admin); AI agents and their keys (admin); audit log with
filters, export and chain verification (admin); webhooks with deliveries, test, re-send and
secret rotation (admin); evals and cost ("measured, not claimed").

## Style

- Calm, precise, professional: a tool people trust with money and compliance, used for hours.
  Dense where it helps (tables, queues) without feeling cramped. Light and dark themes.
- One accent colour for action; status colours (ok, attention, failed) always paired with
  words or labelled icons. Numbers in tabular figures; Indian number grouping (1,00,000)
  and INR shown as the documents print them.
- Quality reference: the polish of Extend's Jevbox demo (https://jevbox.extend.ai) - a clean
  document viewer beside structured data - but with DocForge's own identity.

## Must-haves

- Accessibility: WCAG 2.2 AA; full keyboard use (queue navigation, field-to-box focus, sign);
  visible focus; status never by colour alone; live regions for streamed progress and
  answers; reduced motion respected.
- Every state designed: loading, empty, partial, error (each status code in the handover's
  conventions), offline, rate-limited (429 with "try again in N seconds"), and permission
  denied ("Only administrators can see this page"); navigation hides admin pages from
  non-admins.
- One-time secrets (API keys, webhook secrets) shown once with Copy and "I have copied it".
- Responsive: full experience at 1280px and above; review and reading usable on a tablet;
  status, queue and chat usable on a phone.
- Times in the viewer's local time; paging by "Load more" (cursors), never page numbers.

## Deliver

1. A small design system: colour, type, spacing and radius tokens (light and dark), and the
   components the screens need (buttons, inputs, PIN entry, tables, filters, badges with
   text, stage progress, page viewer with boxes, field row with confidence and checks, quote
   card, toast, dialog, empty and error states).
2. High-fidelity designs of every screen above, starting with the review queue, the
   document view and chat, each in its main states.
3. Short notes for the engineer: which endpoint feeds each part of each screen (from the
   handover), and any interaction that needs care (streaming, box highlighting, signing).

Build for **Next.js 16 and React 19** with the existing same-origin proxy (`/api/v1/...`,
HttpOnly session cookie): the browser never sees a token.
