# ruff: noqa: RUF001 - the arithmetic is printed with real minus and multiplication signs
"""V2 P7: the PO variance through the database - the document's panel (its review read), the
month's report and Excel file, the tolerance setting, isolation and audit.

Bills are pair_001's (dated 2 September 2026), each received with its purchase order on file,
so the worker pairs them. A bill is made to differ by correcting it as a reviewer would:
line 2's rate up by 3.00 on 200 units (taxable value with it), line 3's 20 scheme units not
billed, and line 1 renamed to a product the PO does not hold.
"""

import io
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_mappings import Books
from test_visible_flags import Desk

from docforge.api.app import create_app
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry
from docforge.db.session import SessionFactory
from docforge.variance_report import (
    INVOICE_HEADERS,
    NO_ORDER,
    NOT_APPROVED,
    SUMMARY_HEADERS,
    VarianceReportService,
)
from docforge.workspace_settings import WorkspaceSettingsService
from fakes import signed_in

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
URL = "/v1/exports/po-variance"
MONTH = "2026-09"
INJECTION = '=HYPERLINK("http://x","y")'


class Ledger:
    def __init__(self, books: Books) -> None:
        self.books, self.desk = books, books.desk
        self.reports = VarianceReportService(books.sessions)
        self.numbers = 0

    def order(self) -> uuid.UUID:
        return self.desk.receive_order()

    def bill(self, *, differ: bool = True, sign: bool = True, **printed: str) -> uuid.UUID:
        """A new bill of pair_001; made to differ from its PO unless `differ` is false."""
        self.numbers += 1
        printed.setdefault("invoice_no", f"NVM/26-27/8{self.numbers:04d}")
        bill = self.desk.receive(f"bill-{self.numbers}.pdf", None, **printed)
        if differ:
            for path, value in (
                ("lines[1].ptr", "65.47"),
                ("lines[1].taxable_value", "13094.00"),
                ("lines[2].free_qty", "0"),
                ("lines[0].product_name", "Zinc Tablets 20mg"),
            ):
                self.desk.correct(bill, path, value)
        if sign:
            blockers = self.desk.review.detail(DEFAULT_TENANT_ID, bill).blockers
            self.desk.sign(bill, override_reason="checked on paper" if blockers else None)
        return bill

    def client(self, tenant_id: uuid.UUID | None = None, role: str = "member") -> TestClient:
        app = create_app(
            None, service=self.desk.last.service, review=self.desk.review,
            po_variance=self.reports, settings=WorkspaceSettingsService(self.books.sessions),
        )  # fmt: skip
        return TestClient(signed_in(app, role=role, tenant_id=tenant_id))

    def file(self, month: str = MONTH, **form: str) -> Any:
        from openpyxl import load_workbook

        got = self.client().post(f"{URL}/file", data={"month": month, **form})
        assert got.status_code == 200, got.text
        return load_workbook(io.BytesIO(got.content))


@pytest.fixture
def ledger(
    sessions: SessionFactory, raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> Ledger:  # fmt: skip
    return Ledger(Books(sessions, Desk(sessions, raw_invoice_from_label, raw_order_from_label)))


def _rows(sheet: Any) -> list[list[Any]]:
    return [[cell.value for cell in row] for row in sheet.iter_rows()]


# The document's Variance panel (its review read)


def test_ac_p7_4_the_panel_shows_differences_and_both_one_sided_sections(ledger: Ledger) -> None:
    ledger.order()
    bill = ledger.bill(sign=False)

    variance = ledger.client().get(f"/v1/documents/{bill}/review").json()["variance"]

    assert (variance["lines_checked"], variance["lines_matched"]) == (9, 7)
    differing = [line for line in variance["lines"] if not line["clean"]]
    rate = differing[0]["differences"][0]
    assert (differing[0]["line"], rate["label"], rate["money_impact"]) == (2, "Rate", "600.00")
    assert rate["working"] == "(65.47 − 62.47) × 200 billed = ₹600.00"
    free = differing[1]["differences"][0]
    assert (free["label"], free["po_value"], free["billed_value"]) == ("Free Qty", 20, 0)
    assert free["money_impact"] == "1491.80"
    assert variance["net_impact"] == "2091.80"
    assert [u["product"] for u in variance["not_in_po"]] == ["Zinc Tablets 20mg"]
    assert [u["product"] for u in variance["not_billed"]] == ["Amoxicillin Capsules IP 250mg"]
    assert variance["not_billed"][0]["working"] == "20 × 59.51 = ₹1,190.20"


def test_a_bill_matching_its_order_shows_every_line_matched(ledger: Ledger) -> None:
    ledger.order()
    bill = ledger.bill(differ=False, sign=False)

    variance = ledger.client().get(f"/v1/documents/{bill}/review").json()["variance"]

    assert (variance["lines_checked"], variance["lines_matched"]) == (10, 10)
    assert variance["variances"] == 0 and variance["net_impact"] == "0.00"


def test_a_bill_with_no_order_on_file_has_no_variance(ledger: Ledger) -> None:
    bill = ledger.bill(sign=False)

    assert ledger.client().get(f"/v1/documents/{bill}/review").json()["variance"] is None


def test_the_order_shows_the_same_variance_as_its_bill(ledger: Ledger) -> None:
    order = ledger.order()
    bill = ledger.bill(sign=False)
    api = ledger.client()

    theirs = api.get(f"/v1/documents/{order}/review").json()["variance"]

    assert theirs == api.get(f"/v1/documents/{bill}/review").json()["variance"]


# The tolerance setting


def test_a_difference_inside_the_workspaces_tolerance_is_shown_matched(ledger: Ledger) -> None:
    ledger.order()
    bill = ledger.bill(sign=False)
    api = ledger.client()

    changed = api.patch("/v1/settings", json={"variance_tolerances": {"rate": "3.00"}})

    assert changed.status_code == 200, changed.text
    assert changed.json()["variance_tolerances"] == {
        "rate": "3.00", "qty": 0, "free_qty": 0, "amount": "1.00",
    }  # fmt: skip
    variance = api.get(f"/v1/documents/{bill}/review").json()["variance"]
    line = next(line for line in variance["lines"] if line["line"] == 2)
    assert line["clean"] is True
    assert line["differences"][0]["note"] == (
        "Rate differs by 3.00, inside the workspace's tolerance of 3.00."
    )
    assert variance["net_impact"] == "1491.80"  # the free goods still count
    with ledger.books.sessions() as session:
        entry = session.scalars(
            select(AuditEntry).where(AuditEntry.action == "settings.changed")
        ).one()
    assert entry.details == {"setting": "variance_rate_tolerance", "old": "0.00", "new": "3.00"}


@pytest.mark.parametrize(
    "body",
    [
        {"variance_tolerances": {"rate": "-1"}},
        {"variance_tolerances": {"rate": "0.001"}},
        {"variance_tolerances": {"qty": -1}},
        {"variance_tolerances": {"amount": "100001"}},
        {"variance_tolerances": {"other": 1}},
        {"variance_tolerances": {}},
    ],
)
def test_a_tolerance_out_of_bounds_is_refused(ledger: Ledger, body: dict[str, Any]) -> None:
    assert ledger.client().patch("/v1/settings", json=body).status_code == 422


def test_the_near_expiry_window_and_the_tolerances_change_apart(ledger: Ledger) -> None:
    api = ledger.client()
    api.patch("/v1/settings", json={"variance_tolerances": {"qty": 2}})
    api.patch("/v1/settings", json={"near_expiry_months": 9})

    found = api.get("/v1/settings").json()

    assert found["near_expiry_months"] == 9
    assert found["variance_tolerances"]["qty"] == 2


# The month's report and its file


def test_ac_p7_4_and_6_the_file_holds_numbers_its_sections_and_sums(ledger: Ledger) -> None:
    ledger.order()
    ledger.bill()
    ledger.bill()

    book = ledger.file()

    summary, invoices = _rows(book["Summary"]), _rows(book["Invoices"])
    assert tuple(summary[0]) == SUMMARY_HEADERS and tuple(invoices[0]) == INVOICE_HEADERS
    body, total = invoices[1:], summary[2]
    assert len(body) == 2 and total[0] == "Total"
    net = INVOICE_HEADERS.index("Net Variance (INR)")
    summed = SUMMARY_HEADERS.index("Net money impact (INR)")
    assert Decimal(str(total[summed])) == sum(Decimal(str(r[net])) for r in body)
    assert Decimal(str(total[summed])) == Decimal("4183.60")
    assert total[2] == 2  # invoices checked
    assert summary[1][2:9] == total[2:9]  # one supplier: its row is the total

    variances = _rows(book["Variances"])
    rate = next(r for r in variances[1:] if r[5] == "Rate")
    assert rate[7:11] == [62.47, 65.47, 3, 600]
    assert all(isinstance(v, int | float) for v in rate[7:11])
    assert rate[11] == "(65.47 − 62.47) × 200 billed = ₹600.00"
    free = next(r for r in variances[1:] if r[5] == "Free Qty")
    assert free[7:11] == [20, 0, -20, 1491.8]

    apart = _rows(book["Not in PO - Not billed"])
    assert sorted({(r[0], r[5]) for r in apart[1:]}) == [
        ("Not billed", "Amoxicillin Capsules IP 250mg"), ("Not in PO", "Zinc Tablets 20mg"),
    ]  # fmt: skip


def test_the_report_reads_the_order_as_corrected_as_the_panel_does(ledger: Ledger) -> None:
    order = ledger.order()
    bill = ledger.bill()
    ledger.desk.correct(order, "lines[1].rate", "65.47")  # the order, read again by a person

    panel = ledger.client().get(f"/v1/documents/{bill}/review").json()["variance"]
    (row,) = ledger.client().get(URL, params={"month": MONTH}).json()["invoices"]

    assert (panel["variances"], panel["net_impact"]) == (1, "1491.80")  # free goods only
    assert (row["variances"], row["net_impact"]) == (1, "1491.80")


def test_ac_p7_6_text_from_a_bill_is_never_a_formula(ledger: Ledger) -> None:
    ledger.order()
    bill = ledger.bill(sign=False)
    ledger.desk.correct(bill, "lines[0].product_name", INJECTION)
    ledger.desk.sign(bill, override_reason="checked on paper")

    apart = ledger.file()["Not in PO - Not billed"]

    cell = next(row[5] for row in apart.iter_rows(min_row=2) if row[0].value == "Not in PO")
    assert (cell.value, cell.data_type) == (INJECTION, "s")


def test_ac_p7_5_invoices_without_an_order_are_listed_not_checked(ledger: Ledger) -> None:
    ledger.order()
    ledger.bill()
    alone = ledger.bill(po_no="PO-NONE-1")
    waiting = ledger.bill(sign=False, invoice_no="NVM/26-27/WAIT")
    api = ledger.client()

    preview = api.get(URL, params={"month": MONTH}).json()

    assert preview["totals"]["invoices_checked"] == 1
    reasons = {n["document_id"]: n["reason"] for n in preview["not_checked"]}
    assert reasons == {str(alone): NO_ORDER, str(waiting): NOT_APPROVED}
    assert str(alone) not in {i["document_id"] for i in preview["invoices"]}
    book = ledger.file()
    assert _rows(book["Summary"])[-1][0] == "Not checked: 2 invoices (see 'Not checked')"
    unchecked = _rows(book["Not checked"])
    assert sorted(r[4] for r in unchecked[1:]) == sorted([NO_ORDER, NOT_APPROVED])
    assert len(_rows(book["Invoices"])) == 2  # the header and the one checked


def test_the_supplier_filter_and_a_bad_gstin(ledger: Ledger) -> None:
    ledger.order()
    ledger.bill()
    api = ledger.client()

    mine = api.get(URL, params={"month": MONTH, "supplier_gstin": "24aabfn7754k1z0"}).json()
    other = api.get(URL, params={"month": MONTH, "supplier_gstin": "27AAPFU0939F1ZV"}).json()

    assert mine["totals"]["invoices_checked"] == 1
    assert other["totals"]["invoices_checked"] == 0 and other["not_checked"] == []
    bad = api.get(URL, params={"month": MONTH, "supplier_gstin": "NOT-A-GSTIN"})
    assert bad.status_code == 422 and "15 characters" in bad.json()["detail"]


def test_an_empty_month_makes_no_file_and_says_why(ledger: Ledger) -> None:
    refused = ledger.client().post(f"{URL}/file", data={"month": "2026-01"})

    assert refused.status_code == 422
    assert refused.json()["code"] == "variance_empty"
    assert "January 2026" in refused.json()["detail"]


# AC-P7-7: isolation and audit


def test_another_workspace_sees_none_of_the_report(ledger: Ledger, other_tenant: uuid.UUID) -> None:
    ledger.order()
    bill = ledger.bill()
    theirs = ledger.client(tenant_id=other_tenant)

    preview = theirs.get(URL, params={"month": MONTH}).json()

    assert preview["totals"]["invoices_checked"] == 0 and preview["not_checked"] == []
    assert theirs.post(f"{URL}/file", data={"month": MONTH}).json()["code"] == "variance_empty"
    assert theirs.get(f"/v1/documents/{bill}/review").status_code == 404


def test_an_export_is_audited_by_counts_never_values(ledger: Ledger) -> None:
    ledger.order()
    ledger.bill()
    ledger.file()

    with ledger.books.sessions() as session:
        (entry,) = session.scalars(
            select(AuditEntry).where(AuditEntry.action == "variance.exported")
        ).all()
    assert entry.details["month"] == MONTH
    assert (entry.details["invoices_checked"], entry.details["variances"]) == (1, 2)
    assert set(entry.details) == {
        "month", "invoices_checked", "not_checked", "variances", "supplier_filter", "sha256",
    }  # fmt: skip
    assert "NVM" not in str(entry.details) and "Zinc" not in str(entry.details)


def test_a_reader_may_read_the_report(ledger: Ledger) -> None:
    ledger.order()
    ledger.bill()

    assert ledger.client(role="reader").get(URL, params={"month": MONTH}).status_code == 200
