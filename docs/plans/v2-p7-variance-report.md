# V2 P7: PO Variance Report

Branch `v2-p7-variance-report` (stacked on P6's `v2-p6-bulk-split`; migration 0048).

## What the order match covered before P7 (verified in `trust/match.py`)

| Compared | How | Tolerance |
| --- | --- | --- |
| Order number, supplier GSTIN (or name when either side prints none), buyer GSTIN | exact, as corrected | none |
| PO date not after the invoice date | dates | none |
| Lines paired by product name (case and spacing aside); several PO lines of one product: the one the bill line agrees with most | - | - |
| Quantity | billed `qty` vs PO `qty` | exact |
| Rate | billed `ptr` vs PO `rate` | exact (`Decimal` equality) |
| Pack | when both print one | exact (error) |
| HSN | when both print one, spaces aside | warning only |
| Free quantity | billed `free_qty` vs the PO's scheme (`10+1`) applied to the billed quantity; an unreadable scheme is skipped | exact |
| Bill line with no PO line / PO line not billed / PO line with no readable product | listed | - |
| PO total vs the bill's grand total or taxable value | warning | 1 paisa |

**Compared nowhere:** a line's **taxable amount**. P7 adds it (below). Nothing was priced in
rupees, and nothing had a tolerance. The review's discrepancies and match status are
unchanged by P7 (still exact): the tolerance applies to the variance only, so no check is
weakened.

## Decisions

- **One pairing.** `match.pair_lines` is now the single place lines are paired;
  `match_invoice_to_order` uses it (same discrepancies, same order: all match tests and the 20
  synthetic pairs unchanged) and so does the variance, so the panel and the review always speak
  of the same lines.
- **Money impact** (`trust/variance.py`), all `Decimal`, rounded to the paisa half up, each with
  its arithmetic in words ("(105.00 − 100.00) × 200 billed = ₹1,000.00"):
  - Rate: (billed rate − PO rate) × billed qty, less the bill's own line discount when it
    prints one (shown: "…, less 2% discount").
  - Qty: (billed − ordered) × PO rate.
  - Free Qty: (scheme quantity due on the billed qty − free qty billed) × the bill's PTR (the
    PO rate, said so, when the PTR was not read). Its own row, never merged into Qty.
  - Taxable Amount: the bill's line taxable value against the PO's terms for what was billed
    (billed qty × PO rate, less the bill's discount), less the rate difference already priced,
    so no rupee is counted twice. A bill that prints no line taxable value is compared on rate
    and quantity alone.
  - Sign: positive is the bill costing more than the PO agreed (or scheme goods short);
    negative, less. Net = the sum; "charged above PO" = the positive impacts only.
- **Not compared, never guessed:** a value not read on either side is "not compared" with the
  reason, and carries no money.
- **One-sided lines** are listed apart ("Not in PO" with the billed value, "Not billed" with PO
  qty × rate) and never enter the net. A second bill line of a product already paired (another
  batch) says so.
- **A different pack** is noted on the line ("check before acting on it"); rates of different
  packs are not comparable, and the match already flags it.
- **Tolerances** per workspace (`workspace_settings`, migration 0048): rate (₹ a unit), qty,
  free qty (units), taxable amount (₹). Rate, qty and free qty exact by default, as the brief
  says; the taxable amount within ₹1 by default (the brief sets no default for it; ₹1 is the
  purchase register's 2B tolerance, and a line's taxable value is rounded to the paisa from
  qty × rate less a percentage). Inside a tolerance a difference is shown as matched, with a
  note, and carries no money. Changed by an administrator (or a personal workspace's person),
  each change audited with old and new (`settings.changed`).
- **The panel** is computed on read, like duplicates: the review read already holds the bill
  and its PO as corrected for the match, so the variance costs one lookup (the tolerances, by
  primary key). It shows on either side of the pair.
- **The period report** (`variance_report.py`): invoices *dated* in the month (as the purchase
  register). Checked = newest version approved, values from the signed record, paired with a
  PO (the match's newest pair whose PO is still a live document's newest version). The PO's
  values: its signed record, else its reading with its corrections applied, normalised (the
  review's own way, without loading the parse: normalising only uses it for citations). Every
  other invoice dated in the month is **not checked**, with its reason: no purchase order
  linked, not approved yet, rejected, its PO not readable. Counted on screen and in the file,
  never hidden.
- **Supplier summary** = the sums of its invoices' rows (so the totals always equal the rows):
  invoices, lines checked, lines matched, variances, not compared, one-sided lines, charged
  above PO, net. Ordered by most charged above the PO.
- **The file** (openpyxl write-only): Summary (by supplier, totals row, the not-checked count),
  Invoices, Variances (each field's status, values, money and working), Not in PO - Not billed,
  Not checked, About (tolerances, scope, the sign). Money and quantities are numbers; every text
  is a text cell (never a formula). Audited `variance.exported` by counts and the file's SHA-256,
  never a value from a bill.
- **It informs a person.** No debit note, no claim, no mail to a supplier (out of scope).

## API

- `GET /v1/documents/{id}/review` gains `variance` (null without a linked PO).
- `GET /v1/exports/po-variance?month=&supplier_gstin=`: the month's preview (totals, suppliers,
  checked invoices, not checked with reasons).
- `POST /v1/exports/po-variance/file` (form `month`, `supplier_gstin`): the Excel file; 422
  `variance_empty` when no invoice is dated in the month.
- `GET/PATCH /v1/settings` gain `variance_tolerances` (`rate`, `qty`, `free_qty`, `amount`);
  a PATCH may change the window, the tolerances, or both, and must change something.

## Web (`docforge-web`, same branch)

- Document screen, Order tab: the Variance panel under Order Match (header with invoice total,
  PO total for matched lines, net in ₹ and %; differing lines only, each with its working;
  "42 lines matched" folded; Not in PO and Not billed sections).
- Exports: "PO Variance Report (Excel)" by month and supplier: the month's number, by supplier,
  "Not checked: N invoices" with each reason and a link, and the download.
- Settings: PO variance tolerances beside the near-expiry window.

## Measured (local Postgres 16, laptop)

| What | Result | Target |
| --- | --- | --- |
| 3,000 invoices in a month, **each with its own unsigned PO** (worked out from its reading): preview | 4.4 s | - |
| The same month's Excel file (0.6 MB) | 5.5 s | under 30 s |
| Review read of a bill at 10,000 invoices, with the variance / without | 211 ms / 213 ms (median of 7, alternated) | adds under 200 ms |

The first cut worked each PO out through the review's full reassessment (loading and
validating its parse) and took 22.5 s for 3,000; reading the values the review's own way
without the parse brought it to 5.5 s. (With one PO shared by all 3,000: 3.7 s.)

## Tests

- `tests/unit/test_variance.py` (39): AC-P7-1 (105 vs 100 × 200 = exactly ₹1,000.00, Decimal,
  the working shown), AC-P7-2 (scheme 10+1 on 100, 0 billed: Free Qty, ₹1,000.00 at PTR, not
  Qty), AC-P7-3 (all 20 synthetic pairs: no differing line, matched count = lines; ₹0.40 inside
  a ₹1 tolerance matched with its note), AC-P7-4 (one-sided lines apart, not in the net), the
  taxable amount never counting the rate twice, the discount, a shortfall at the PO rate,
  quantities within tolerance, values not read, a different pack, a second batch, the
  percentage, India's grouping.
- `tests/integration/test_variance_report.py` (22): the panel over HTTP (differences, both
  sections, a matching bill, no PO, the PO's own view), the tolerance setting (applied, audited,
  bounds refused), AC-P7-4/6 in the file (numbers as numbers, sections, supplier totals = the
  sum of the invoice rows), formula-like text kept as text, AC-P7-5 (no PO and not approved
  listed with reasons, counted), a PO corrected by a person read as corrected, the supplier
  filter, the empty month, AC-P7-7 (another workspace sees none of it, audit by counts only).
- `tests/integration/test_variance_scale.py`: the measurements above, asserted.
- The existing match tests (`test_trust_match.py`, the seeded synthetic pairs, the evals) pass
  unchanged on the shared pairing.

## Left for later

- **The mind-change test needs the pilot's own bills**: one real variance on the owner's bills,
  in rupees. Nothing here is verified on a real PO yet; the synthetic pairs are ours.
- A product split over several batch lines against one PO line is paired as the match pairs it
  (the first batch takes the PO line; the others are "Not in PO", noted as another batch).
  Summing batches into one PO line would change the match too; not done here.
- The PO's printed line amount is not compared (it may be printed with or without tax).
- A tolerance as a percentage rather than an amount.
