"""V2 P4: the purchase register through the database - which invoices a month holds, one row
each, the 2B check over HTTP, isolation, audit and the empty month.

Bills are pair_001's (dated 2 September 2026), approved through the review; where a test needs
a bill pair_001 is not (another state, mixed rates, 25 batches, a date), its signed record is
replaced, as the database owner, with a made-up one (`tests/profile_fixtures.py`).
"""

import io
import json
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from test_mappings import Books
from test_visible_flags import Desk

from docforge.api.app import create_app
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry
from docforge.db.session import SessionFactory
from docforge.extraction.schema import InvoiceExtraction
from docforge.purchase_register import HEADERS, PurchaseRegisterService
from fakes import signed_in
from profile_fixtures import BUYER, GUJARAT_SELLER, MAHARASHTRA_SELLER, invoice, line

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
URL = "/v1/exports/purchase-register"
PAIR_001_MONTH = "2026-09"


class Month:
    def __init__(self, books: Books, owner_engine: Engine) -> None:
        self.books, self.owner = books, owner_engine
        self.registers = PurchaseRegisterService(books.sessions)

    def signed(self, record: InvoiceExtraction) -> uuid.UUID:
        """An approved bill whose signed record is `record`."""
        bill = self.books.approved_bill()
        with self.owner.begin() as conn:  # signed reviews are append-only: for this test only
            conn.execute(text("ALTER TABLE reviews DISABLE TRIGGER USER"))
            conn.execute(
                text(
                    "UPDATE reviews SET data = jsonb_set(data, '{record}', CAST(:r AS jsonb)) "
                    "WHERE document_version_id IN "
                    "(SELECT id FROM document_versions WHERE document_id = :d)"
                ),
                {"r": record.model_dump_json(), "d": bill},
            )
            conn.execute(text("ALTER TABLE reviews ENABLE TRIGGER USER"))
        return bill

    def client(self, tenant_id: uuid.UUID | None = None) -> TestClient:
        desk = self.books.desk
        app = create_app(None, service=desk.last.service, review=desk.review,
                         purchase_register=self.registers)  # fmt: skip
        return TestClient(signed_in(app, role="member", tenant_id=tenant_id))

    def file(self, month: str, **form: str) -> Any:
        from openpyxl import load_workbook

        got = self.client().post(f"{URL}/file", data={"month": month, **form})
        assert got.status_code == 200, got.text
        return load_workbook(io.BytesIO(got.content))


@pytest.fixture
def month(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
    owner_engine: Engine,
) -> Month:
    desk = Desk(sessions, raw_invoice_from_label, raw_order_from_label)
    return Month(Books(sessions, desk), owner_engine)


def _bill(number: str, day: str, seller: str, *, rates: list[tuple[str, str]],
          code: str = "24") -> InvoiceExtraction:  # fmt: skip
    """A made-up bill with a line per (taxable value, GST rate), taxed by `seller`'s state."""
    intra = seller[:2] == code
    lines, taxable, tax = [], Decimal(0), Decimal(0)
    for n, (value, rate) in enumerate(rates):
        amount = Decimal(value) * Decimal(rate) / 100
        lines.append(line(product_name=f"Synthetic Item {n}", pack="1x10", hsn="30049099",
                          batch_no=f"SB{n:03d}", expiry="2028-12", qty=1, free_qty=0,
                          mrp="999.00", ptr=value, discount_pct="0", taxable_value=value,
                          gst_rate=rate, amount=str(Decimal(value) + amount)))  # fmt: skip
        taxable, tax = taxable + Decimal(value), tax + amount
    half = str(tax / 2)
    heads = {"cgst": half, "sgst": half} if intra else {"igst": str(tax)}
    return invoice(number=number, day=day, seller=seller, code=code, lines=lines,
                   totals={"taxable_value": str(taxable), "round_off": "0",
                           "grand_total": str(taxable + tax), **heads})  # fmt: skip


# AC-P4-1: three invoices, three rows, the tax split and the totals


def test_a_month_of_three_invoices_is_three_rows_split_and_totalled(month: Month) -> None:
    month.signed(_bill("SYN/1", "2026-11-03", GUJARAT_SELLER, rates=[("1000.00", "12")]))
    month.signed(_bill("SYN/2", "2026-11-04", MAHARASHTRA_SELLER, rates=[("500.00", "18")]))
    month.signed(_bill("SYN/3", "2026-11-05", GUJARAT_SELLER,
                       rates=[("100.00", "5"), ("200.00", "12"), ("300.00", "18")]))  # fmt: skip

    sheet = month.file("2026-11")["Purchase Register"]
    values = [[c.value for c in r] for r in sheet.iter_rows()]

    assert tuple(values[0]) == HEADERS
    assert len(values) == 5  # header, three invoices, totals
    body, totals = values[1:4], values[4]
    assert [r[2] for r in body] == ["SYN/1", "SYN/2", "SYN/3"]
    assert [tuple(r[6:9]) for r in body] == [(60, 60, 0), (0, 0, 90), (41.5, 41.5, 0)]
    assert [r[4] for r in body] == ["24", "24", "24"]
    for column in (5, 6, 7, 8, 9, 10):
        assert Decimal(str(totals[column])) == sum(Decimal(str(r[column])) for r in body)


# AC-P4-2: 25 lines and 25 batches are still one row


def test_an_invoice_of_twenty_five_batches_is_one_row_at_its_grand_total(month: Month) -> None:
    bill = _bill("SYN/25", "2026-11-10", GUJARAT_SELLER,
                 rates=[(f"{100 + n}.00", "12") for n in range(25)])  # fmt: skip
    month.signed(bill)

    register = month.registers.build(DEFAULT_TENANT_ID, "2026-11")

    (row,) = register.rows
    assert row.invoice_total == bill.totals.grand_total.value
    assert register.totals.invoice_total == bill.totals.grand_total.value


# AC-P4-4: a month is the invoice's own date


def test_the_first_of_november_is_november_and_the_thirty_first_of_october_is_not(
    month: Month,
) -> None:
    month.signed(_bill("SYN/NOV", "2026-11-01", GUJARAT_SELLER, rates=[("10.00", "12")]))
    month.signed(_bill("SYN/OCT", "2026-10-31", GUJARAT_SELLER, rates=[("10.00", "12")]))

    def numbers(name: str) -> list[str | None]:
        return [r.invoice_no for r in month.registers.build(DEFAULT_TENANT_ID, name).rows]

    assert numbers("2026-11") == ["SYN/NOV"]
    assert numbers("2026-10") == ["SYN/OCT"]


# AC-P4-5: the 2B check over HTTP


def test_the_2b_check_marks_matched_differing_missing_and_extra(month: Month) -> None:
    month.signed(_bill("SYN/A", "2026-11-03", GUJARAT_SELLER, rates=[("1000.00", "12")]))
    month.signed(_bill("SYN/B", "2026-11-04", MAHARASHTRA_SELLER, rates=[("2000.00", "12")]))
    month.signed(_bill("SYN/C", "2026-11-05", GUJARAT_SELLER, rates=[("300.00", "5")]))
    twob = {"data": {"gstin": BUYER, "docdata": {"b2b": [
        {"ctin": GUJARAT_SELLER, "inv": [
            {"inum": "syn/a", "dt": "03-11-2026", "val": 1120.40,
             "items": [{"txval": 1000, "cgst": 60, "sgst": 60, "igst": 0}]}]},
        {"ctin": MAHARASHTRA_SELLER, "inv": [
            {"inum": "SYN/B", "dt": "04-11-2026", "val": 2490,
             "items": [{"txval": 2250, "igst": 240}]}]},
        {"ctin": "29AAGCB1286Q1ZT", "inv": [
            {"inum": "X/9", "dt": "07-11-2026", "val": 100, "items": [{"txval": 100}]}]},
    ]}}}  # fmt: skip

    got = month.client().post(
        f"{URL}/2b-check", data={"month": "2026-11"},
        files={"file": ("2b.json", json.dumps(twob).encode())},
    )  # fmt: skip

    assert got.status_code == 200, got.text
    found = got.json()
    assert [r["status"] for r in found["rows"]] == ["matched", "amount_differs", "docforge_only"]
    assert (found["matched"], found["amount_differs"], found["docforge_only"],
            found["twob_only"]) == (1, 1, 1, 1)  # fmt: skip
    differences = {k: Decimal(v) for k, v in found["rows"][1]["differences"].items()}
    assert differences == {"taxable_value": Decimal("-250"), "invoice_total": Decimal("-250")}
    assert [i["invoice_no"] for i in found["twob_only_invoices"]] == ["X/9"]
    assert "Not a reconciliation with the GST portal" in found["note"]


# AC-P4-6: approved only, the empty month, isolation, audit


def test_only_approved_invoices_are_in_the_register(month: Month) -> None:
    approved = month.books.approved_bill()
    month.books.desk.receive("waiting.pdf", None, invoice_no="NVM/WAIT/1")

    register = month.registers.build(DEFAULT_TENANT_ID, PAIR_001_MONTH)

    assert [r.document_id for r in register.rows] == [approved]


def test_a_month_with_no_approved_invoice_is_refused_in_words_not_an_empty_file(
    month: Month,
) -> None:
    got = month.client().post(f"{URL}/file", data={"month": "2026-11"})

    assert got.status_code == 422
    assert got.json()["code"] == "register_empty"
    assert got.json()["detail"].startswith("No approved invoice is dated in November 2026")


def test_another_workspace_sees_none_of_the_register(month: Month, other_tenant: uuid.UUID) -> None:
    month.signed(_bill("SYN/1", "2026-11-03", GUJARAT_SELLER, rates=[("10.00", "12")]))
    theirs = month.client(tenant_id=other_tenant)

    assert theirs.get(URL, params={"month": "2026-11"}).json()["invoices"] == 0
    refused = theirs.post(f"{URL}/file", data={"month": "2026-11"})
    assert refused.json()["code"] == "register_empty"


def test_an_export_is_audited_without_names_from_the_bills(month: Month) -> None:
    month.signed(_bill("SYN/1", "2026-11-03", GUJARAT_SELLER, rates=[("10.00", "12")]))
    month.file("2026-11")

    with month.books.sessions() as session:
        (entry,) = session.scalars(
            select(AuditEntry).where(AuditEntry.action == "register.exported")
        ).all()
    assert entry.details["month"] == "2026-11" and entry.details["invoices"] == 1
    assert "SYN/1" not in str(entry.details) and "Synthetic" not in str(entry.details)


# The preview, the supplier filter, names and notes


def test_the_preview_shows_what_the_file_will_hold(month: Month) -> None:
    month.signed(_bill("SYN/1", "2026-11-03", GUJARAT_SELLER, rates=[("1000.00", "12")]))
    month.signed(_bill("SYN/2", "2026-11-04", MAHARASHTRA_SELLER, rates=[("500.00", "18")]))
    month.books.map_supplier(tally_ledger="Navjivan (Tally)")
    client = month.client()

    preview = client.get(URL, params={"month": "2026-11"}).json()
    one = client.get(URL, params={"month": "2026-11", "supplier_gstin": MAHARASHTRA_SELLER})
    sheet = month.file("2026-11")["Purchase Register"]

    assert preview["invoices"] == 2
    assert Decimal(preview["totals"]["invoice_total"]) == Decimal("1710.00")
    assert Decimal(str(sheet.cell(row=4, column=11).value)) == Decimal("1710.00")
    names = [r["supplier_name"] for r in preview["rows"]]
    assert names == ["Navjivan (Tally)", "Synthetic Seller"]
    assert one.json()["invoices"] == 1 and one.json()["rows"][0]["invoice_no"] == "SYN/2"
    assert "Credit and debit notes are not included" in preview["excluded"]


def test_a_repeated_bill_is_noted_as_a_possible_duplicate(month: Month) -> None:
    first = month.books.approved_bill(invoice_no="NVM/26-27/DUP")
    again = month.books.approved_bill(invoice_no="NVM/26-27/DUP")

    rows = month.registers.build(DEFAULT_TENANT_ID, PAIR_001_MONTH).rows

    notes = {r.document_id: r.note for r in rows}
    assert notes == {first: "", again: "Possible duplicate"}  # the later one received
