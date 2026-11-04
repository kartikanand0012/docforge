# V2 P6: Bulk upload, and one PDF holding several invoices split

Branch `v2-p6-bulk-split` (stacked on P5's `v2-p5-email-intake`; migration 0047).

## Decisions (made before coding)

### Part A: bulk upload

- **Dropped files go one request each into a batch.** `POST /v1/batches` says how many files
  are coming (1 to 50), then the browser sends each to `POST /v1/batches/{id}/files`, four at
  a time. Each request is held to the upload's own per-file limit and gets its own outcome; no
  request carries 50 files. A batch keeps what arrived; files the browser never sent (the page
  closed while sending) are counted as "not sent". Once a file has arrived, leaving the page
  stops nothing: the reading is the worker's, as for any upload. While files are still being
  sent, the page warns before closing.
- **A ZIP is sent whole** to `POST /v1/batches/zip` (up to 500 MB, `MAX_ZIP_BYTES`; the
  request-size middleware allows that on this route only) and unpacked on the server
  (`zipsafe.py`). Every check is made before the first document is created, so a refused ZIP
  leaves nothing behind:
  - nothing is ever written to disk from a ZIP; an entry's name is only reduced to a base name
    to show (`safe_filename`), never used as a path. Traversal is impossible by construction;
    a ZIP naming an entry outside itself (`../`, `/`, `C:\`, a UNC path) or holding a link is
    still refused whole, because it was made to attack;
  - at most 200 files (folders and `__MACOSX/`, `._*`, `.DS_Store`, `Thumbs.db`, `desktop.ini`
    skipped) and 1,000 entries of any kind; at most 500 MB unpacked, by the declared sizes and
    again by the bytes actually unpacked in bounded chunks; an entry unpacking to more than
    200 times its packed size above 1 MB is a bomb; an entry is never read past its declared
    size;
  - one bad entry refuses only itself, in words: a ZIP inside the ZIP (not opened), a
    password-protected entry, one over the per-file limit, one damaged.
- **Each file is an upload:** `upload_checks.check_file` (the upload's checks and words), then
  `DocumentService.ingest` (the upload's entry point, caps and dedupe). The same bytes already
  in the workspace are "already received" and read once.
- **The Batch view** counts live from the documents: total = received + already received +
  refused; processed = total - waiting; ready; failed (refused, or its reading failed); split
  into invoices. A retry reads again only the documents whose reading failed.
- **The 50-file limit** is the brief's. More than 50 dropped at once are refused with words
  pointing to a ZIP (up to 200), and nothing is sent. *The pilot's "63 bills in one action" is
  therefore a ZIP, or two drops.* Files are sent one by one, so raising the limit costs the
  server nothing; it is one constant (`batches.MAX_FILES`) if the owner wants it.

### Part B: splitting

- **Where:** in the worker, for an invoice PDF of two pages or more that was not itself cut
  from another (`split/reader.py`, `Splitter`). It is parsed once and the parse is examined;
  with one invoice (or none found) the same parse goes on to the pipeline's own extraction. A
  normal multipage invoice is therefore read exactly as before: the same parse, the same model
  calls, the same record (AC-P6-7, tested with the splitter on and off). The extraction prompt
  and schema are unchanged.
- **Evidence** (`split/detect.py`), from the parser's blocks page by page, never "every N
  pages". A page starts an invoice when its upper part prints an invoice number ("Invoice
  No.", "Inv. Number", "Bill No" ..., then a value with a digit that is not a date) and the page
  prints a GSTIN. Then:
  - the same number again in a header (same seller) continues the invoice: certain;
  - "Page n of m" consistent with the invoice before continues it: certain;
  - any other page without a header (a continuation, a covering letter, a blank page), an
    invoice number printed lower on a page (a reference to another bill), or a number seen
    before from the same seller is placed where it most likely belongs and **marked in doubt,
    with the reason in words**.
  Two or more invoices with every page certain: the invoices are made at once. Any page in
  doubt: **Split Review**, and nothing is made until a person confirms.
- **The parent** keeps its original (downloadable, audited). Its status is `split`, or
  `split_review` while waiting. It has no extraction and is never in the review queue, an
  export or the register. It is not read again, and is not deleted while its invoices stand.
- **The children** are PDFs cut from the parent's pages (`split/pages.py`, PDFium). Each is a
  document of its own (`parent_document_id`, `parent_pages`), named
  "`<file> - invoice 2 of 3 (pages 3-4).pdf`", and is queued and read as any upload, with its
  own checks. Pages that are already a document are that document ("already received").
- **Page reconciliation** (`detect.reconcile`): every page of the parent is in exactly one
  invoice or in "not an invoice page"; parent pages = invoice pages + non-invoice pages. It is
  enforced when proposed, when confirmed (a grouping that drops or repeats a page is refused
  in words) and again when the invoices are recorded, and shown on the Split Review screen and
  in every view.
- **Split Review** (`GET /v1/splits`, `GET/POST /v1/documents/{id}/split`) shows page
  thumbnails (`/v1/documents/{id}/pages/{n}?size=thumbnail`) grouped as proposed, with pages in
  doubt marked. A reviewer moves a page to the previous or next invoice, starts an invoice at
  a page, or marks it "not an invoice page", then confirms. Audited as `split.confirmed`, with
  the reviewer as actor, their name, and how many pages they moved.
- **When detection cannot run:** a file longer than one invoice may be (20 pages) is taken up
  to 100 pages (`MAX_BUNDLE_PAGES`). If the parser stops on it (time or memory), or detection
  itself fails, it goes to Split Review with every page proposed as one invoice and the reason
  "could not detect automatically", never to an error. A long file that proves to be one
  invoice fails with the page limit's own message, as it would have at upload.
- **Free workspaces:** the invoices a file makes count against its documents and readings like
  uploads. When they would not fit, the file waits in Split Review with the reason, and a
  confirmation is refused (429) in words until there is room.
- **Email intake** takes such PDFs as an upload does; its reply now says a PDF holding several
  bills is divided (P5's note updated).

### Audit (counts only, never a filename or a value from a bill)

`batch.created` (kind, files, received, already received, refused), `batch.retried`
(documents), `split.proposed` (pages, invoices, non-invoice pages, pages in doubt),
`split.made` and `split.confirmed` (pages, invoices, invoice pages, non-invoice pages, already
received; confirmed adds the person and the pages moved). Each child's `document.received`
names its parent and its page count.

## Built

- Backend: `zipsafe.py`, `batches.py` (`BatchService`), `api/batches.py`, `split/detect.py`,
  `split/pages.py`, `split/reader.py`, `api/splits.py`; `DocumentService` divides, confirms
  and shows divisions; `pages_allowed` for uploads, intake and batches; page thumbnails;
  migration 0047 (`documents.parent_document_id`/`parent_pages`, the `split` statuses,
  `document_splits`, `upload_batches`, `upload_batch_items`, row-level security, erased with
  the account); settings `SPLIT_INVOICES` (on), `MAX_BUNDLE_PAGES` (100), `MAX_ZIP_BYTES`.
- Web (`docforge-web`, same branch): the upload page sends two or more files as a batch and a
  ZIP whole, and links the Batch view (`/batches/[id]`); Split review (`/splits`,
  `/splits/[id]`); the document page says "Split into N invoices" with links, "Group its
  pages", or which pages of which file an invoice is.

## Measured

Local Postgres 16, in-memory object store, laptop CPU.

| What | Result |
| --- | --- |
| ZIP of 200 one-page invoices: upload, unpack, check and queue all 200 | 3.7 s (brief: under 60 s) |
| A 60-page file examined in the worker and its proposal recorded (text parse) | 0.4 s |
| **Real parser (Docling, CPU)**: 3 synthetic invoices in one PDF | parsed 10.3 s; 3 invoices, certain, every number right |
| Docling: a 2-page invoice whose second page has no header, then another | parsed 7.5 s; 2 invoices, page 2 put to a person with the reason |
| Docling: one 2-page invoice | parsed 6.3 s; not split |
| Docling: 60 pages (the 20 fixtures three times) | parsed 72.1 s; detection 6 ms; every number right; the repeats put to a person |

The parse is the parser's existing budget (900 s by default); detection itself takes
milliseconds.

## Tests

- `tests/unit/test_zipsafe.py` (18): traversal names of every kind, links, bombs by ratio and by
  declared total, a lying size never read past, 200 and 201 files, nested, encrypted, damaged.
- `tests/unit/test_split_detect.py` (16): the committed synthetic invoices read for their real
  text - 3 invoices certain; a bare continuation page in doubt; a covering letter; a header on
  every page; one invoice (pairs 101-103, 001, and an 8-page invoice) not split; "Page n of m";
  a reference low on a page; the same number from another seller; reconciliation in words.
- `tests/integration/test_split.py` (18): AC-P6-3 (3 children each extracting its own number,
  seller and total; parent split, no extraction, original kept, not in the queue), AC-P6-4
  (Split Review, zero children), AC-P6-5 (a reviewer moves the letter page out and confirms;
  audited with identity; children from the grouping), a page moved to the next invoice,
  refused groupings, confirmed once, reviewers only, AC-P6-6 asserted in every division,
  AC-P6-7 (pair_101 and an 8-page invoice read identically with the splitter on and off), the
  parser stopping on a 25-page file, 60 pages, thumbnails, workspaces apart.
- `tests/integration/test_bulk_upload.py` (15): AC-P6-1 (10 + 1 corrupt + 1 duplicate = 12),
  AC-P6-2 (traversal names and a bomb: refused, no document, nothing stored, nothing written),
  nested ZIP, the ZIP size limit, dropped files each held to the per-file limit, files not
  sent, 63 at once pointed to a ZIP, retry of failed readings only, listing, workspaces apart,
  roles, 200 invoices.
- Account deletion erases batches and divisions; every route about a document is still 404
  from another workspace (the split route included).

## Left for later

- A real combined PDF from the pilot through Docling. The synthetic layouts are ours; a
  supplier's PDF may print its invoice number in a way the label pattern misses, and its pages
  would then go to a person rather than be divided.
- Scanned image bundles (TIFF stacks) and splitting by WhatsApp message: out of scope.
- More than 50 files dropped at once (one constant), and a folder dropped as a folder.
- The parent's own `document.processed` webhook: a divided file sends none; its invoices do.
