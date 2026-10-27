# Plan: V2 P0 - the checks already built, made visible; two QA bugs proved or fixed

## Why

Duplicates (3.3), near-expiry stock (3.4) and Schedule H1 (3.6) are built and tested, but a
buyer cannot pay for a check he never sees. Each must be plain on the queue and on the bill,
with a count an owner can repeat ("it caught 2 duplicates and 3 near-expiry lines on my own
bills"). No new check and no change to what any check decides.

## Owner decisions (2026-10-27)

- **Separators still count** for duplicates, as decided in 3.3: case and spaces are ignored,
  "/" and "-" are part of the number. AC-P0-2 is tested for case and spaces;
  `test_separators_still_count_as_decided_for_duplicates` pins the decision.
- **Order link: both** - a correction to a value pairing reads looks for the order again, and
  a reviewer can link an order by hand ("Link order") as the fallback.
- **Flag-only bills stay out of the queue** (decision of 2026-10-17 unchanged): queue badges
  show on rows that wait anyway; the home screen's summary and the bill's strip cover the rest.

## What

### Visible flags

- `QueueItemOut.flags`: `{duplicates, near_expiry_lines, schedule_h1_lines}` for each row.
  H1 lines are found only for the rows returned, not for every document the queue scans.
- `ReviewOut.near_expiry`: `[{path, product_name, batch_no, expiry, months_left}]` - the lines
  `line.shelf_life` flags, months counted from the bill date's month (`months_left` and
  `is_near_expiry` in `trust/invoice_rules.py` are shared by the rule and this list).
- `GET /v1/flags/summary?from&to` (any reader; India's dates, both included; by default the 30
  days to today, `from = to - 29`; at most 366 days, else 422 in plain words):
  `{from, to, invoices, duplicates, near_expiry_lines, near_expiry_bills, schedule_h1_lines,
  schedule_h1_bills, capped}`. `review/flags.py` counts invoices received in the range (live,
  newest version read), on the same rules and the same effective values as the bill: a
  duplicate repeats an earlier live invoice not rejected, by verified IRN or by seller GSTIN
  and number as last corrected; lines are read from the stored values, and a bill with
  corrections is worked out as corrected. At most 10,000 invoices per summary (`capped`).

### QA bug 1 (2026-10-09): place of supply code held "Gujarat (24)"

Proved: the code field was already the 2-digit code for the formats the parser read; the
whole string was a display fallback on the web. Still broken, now fixed in
`extraction/normalize.py`: formats the parser missed ("Gujarat [24]", "Gujarat (State Code:
24)", "State: Gujarat, Code: 24", "Gujarat, 24", "(24) Gujarat", "Gujarat 24") left the code
null and the web showed the whole text; a number that is no GST state code ("Gujarat (99)")
was taken as one. The code is now exactly a GST state code (01-38, 96, 97) or null, after
extraction and after a correction (the correction goes through the same parser). Web: a value
read from part of the printed text never shows that text.

### QA bug 2 (2026-10-09): invoice-to-PO match only on invoice-ready

Proved fixed already: the worker pairs whichever of the two is read second
(`test_an_invoice_and_its_order_are_matched_whichever_arrives_first`). Still broken, now
fixed: pairing read the values as first read, so a misread order number, once corrected,
never paired. `order_match.py` (moved out of `documents.py`):

- pairing reads both sides as last corrected (the order number as read, index
  `ix_extractions_po_no`, or as corrected, new index `ix_corrections_po_no`);
- `ReviewService.correct` looks again after a correction to `po_no`, the seller or supplier,
  or the buyer, in the correction's transaction (a failure leaves the correction stored);
- `POST /v1/documents/{id}/order-link` `{order_document_id, reason}` (signed-in reviewer;
  invoice only, unsigned; reason required) and `GET /v1/documents/{id}/order-candidates`
  (orders, the bill's seller's first, at most 50). A hand link is compared as usual (a wrong
  order shows as a mismatch), audited `match.linked` on both documents (no reason or values
  in the audit log; the reason is kept with the link).

Migration 0043: `matches.manual`; the unique pair becomes a partial index over automatic
comparisons only, so a hand link back to an order linked before is a new, newest row
(append-only as before); `ix_matches_invoice_version_id`; `ix_corrections_po_no`.
Reversible (downgrade drops hand links).

## Measured (local Postgres 16, one workspace of 10,000 invoices of 10 lines)

| What | Result | Target |
| --- | --- | --- |
| Review read of one bill, p95 of 30 (whole read) | 155-234 ms (p50 128 ms) | under 300 ms added |
| Queue badges, 200 rows | +0.7 s over the queue without them (3.5 ms a row) | - |
| Summary, 7 days (3,235 invoices) | 0.40 s | - |
| Summary, 30 days (10,000 invoices) | 0.72 s | - |

**Not met, and not caused by this change:** the queue itself takes 26 s to list 200 waiting
bills at this size, because it works out every bill it lists in full (about 130 ms each).
A follow-up should make the queue cheaper (store the decision per version, or page it).

## Tests

`tests/integration/test_visible_flags.py` (AC-P0-1 to AC-P0-7, service level),
`tests/integration/test_api_visible_flags.py` (HTTP, roles, plain errors, tenant scope),
`tests/unit/test_normalize.py` and `tests/unit/test_place_of_supply_fixtures.py` (every
synthetic fixture). `test_eval_scoring.py`'s misread-code case now uses a real state code
(27), since 99 is no longer read as a code at all.

## Web (docforge-web, branch v2-p0-visible-flags)

Queue badges, a "Flagged on this bill" strip above the values, a "Caught on your bills" card
on the home screen (7 / 30 / 90 days), "Link an order" in the Order match tab, and the place
of supply display fix.
