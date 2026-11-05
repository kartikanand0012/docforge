# V2 P4: Purchase register (Excel) and the 2B check

Branch `v2-p4-purchase-register` (stacked on P3's `v2-p3-marg-profiles`). No migration.

## What it is

One row per approved invoice of a month, in the shape a CA sets beside the GSTR-2B download:

`Supplier GSTIN | Supplier Name | Invoice Number | Invoice Date | Place of Supply (code) |
Taxable Value | CGST | SGST | IGST | Total Tax | Invoice Total | Reverse Charge |
ITC Eligibility Note`, then a totals row (taxable, CGST, SGST, IGST, total tax, invoice
total). Sheet "Purchase Register"; sheet "About" (month, workspace, generated at and by,
invoice count, supplier filter, and "Generated from invoices approved in DocForge. Verify
against GSTR-2B before filing."); with a 2B file attached, sheet "2B check".

- **Which invoices:** live invoices whose newest version was approved, **dated** in the month
  (the bill's invoice date, a calendar date: never shifted by a timezone; 1 Nov is November,
  31 Oct is October). Credit and debit notes are not read today, and the screen and the file
  say so.
- **Values:** the bill as signed (its corrections): GSTIN, the books' name from the mapping
  master (Tally ledger, else Marg party) or else the printed name, place of supply's 2-digit
  code, totals as printed. However many lines or batches, one row.
- **Cells:** money as numbers (`#,##0.00`), dates as dates (`dd-mmm-yyyy`), GSTIN, invoice
  number, code and names as text cells - leading zeros kept, never a formula (`=`, `+`, `-`,
  `@` stay text). Streamed with openpyxl's write-only mode.
- **ITC Eligibility Note:** data flags from checks DocForge already runs - "GSTIN format
  issue", "Possible duplicate" (the review's own duplicate rule, `review.flags`), "E-invoice
  QR not verified" (a QR whose signature failed, was malformed, had an unknown key, or could
  not be read), "Place of supply missing", "Amounts missing". The About sheet and the screen
  say these are for the CA to decide on, not a judgement of eligibility and not tax advice.
- **Reverse Charge:** DocForge does not read it from bills, so the column is blank and the
  About sheet says why (writing "N" would be a claim the bill never made).
- **Empty month:** refused in words (`422`, `register_empty`), never an empty spreadsheet.

## The 2B check

The user uploads the month's GSTR-2B Excel or JSON (at most 10 MB and 20,000 rows; larger is
refused naming the limit). Each register row is matched by supplier GSTIN and invoice number
(compared as duplicates are: case and spaces aside) and is **Matched** (every amount -
taxable, CGST, SGST, IGST, invoice total - within 1 rupee, in Decimal), **Amount differs**
(each amount beyond 1 rupee, DocForge less 2B) or **In DocForge only**; 2B invoices with no
row are **In 2B only**. A comparison, not a reconciliation with the portal; nothing is fetched
from or filed on the portal.

**The 2B layout is not from GSTN** (which publishes no schema): the Excel "B2B" sheet (a
two-row header - "GSTIN of supplier", "Invoice details" over "Invoice number" / "Invoice Date"
/ "Invoice Value", "Taxable Value", "Tax Amount" over "Integrated Tax" / "Central Tax" /
"State/UT Tax" / "Cess"; a row per invoice and rate) and the JSON (`data.docdata.b2b[].inv[]`
with `items[]`) are as two independent open-source readers of the portal's downloads describe
them (arjukjin-cyber/itc-rescue PR #27, pranavv05/verax PR #15). The reader finds columns by
their header words, not their places. **To do: run one real 2B download (Excel and JSON)
from a pilot's portal through it** and add it, with values replaced, as a fixture.

## API

| Route | Who |
| --- | --- |
| `GET /v1/exports/purchase-register?month=YYYY-MM&supplier_gstin=` (rows, count, totals) | readers |
| `POST /v1/exports/purchase-register/file` (multipart `month`, `supplier_gstin`, `gstr2b`) | readers |
| `POST /v1/exports/purchase-register/2b-check` (multipart `file`, `month`, `supplier_gstin`) | readers |

Audit: `register.exported` (month, invoices, supplier filter, with 2B, SHA-256),
`register.compared_2b` (month and the four counts). Never a value from a bill.

## Measured (local Postgres 16; pair_001's bill cloned)

| What | Time | Target |
| --- | --- | --- |
| 10,000 invoices in a month: the file (0.4 MB) | 3.8 s | under 60 s |
| The same month's preview | 3.2 s | - |
| 2B JSON of 20,000 invoices: read and compared with 10,000 rows | 0.17 s | no timeout |
| 2B Excel of 20,000 rows: read | 1.2 s | no timeout |

## Tests

- `tests/unit/test_register_workbook.py`: AC-P4-1's shape (columns in order, totals row = the
  rows' sums), AC-P4-3 (cell types: numbers, dates, text with leading zeros), AC-P4-7 (`= + -
  @` names stay text), the About sheet, AC-P4-5 (matched / 250 rupees differ / missing / extra;
  exactly a rupee matches, a paisa more does not), the 2B check sheet, months.
- `tests/unit/test_gstr2b.py`: Excel with a two-row header and a row per rate added up, a
  number Excel held as a number, JSON, refusals in words, the 20,000-row limit.
- `tests/integration/test_purchase_register.py`: AC-P4-1 (three invoices: within a state,
  across states, mixed rates), AC-P4-2 (25 batches, one row at the grand total), AC-P4-4 (1 Nov
  / 31 Oct), AC-P4-5 over HTTP, AC-P4-6 (approved only, empty month refused, another workspace
  sees nothing, audit without names), the preview equals the file, supplier filter, mapped
  names, the duplicate note.

## Left for later

- A real GSTR-2B download through the reader (above).
- Credit and debit notes (B2B-CDNR), and amendments (B2BA), once DocForge reads them.
- Reverse charge, once read from bills.
