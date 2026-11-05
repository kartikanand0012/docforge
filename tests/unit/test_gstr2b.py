"""V2 P4: reading a GSTR-2B download (Excel or JSON) to set beside the purchase register.
The files here are made up in the layout two open-source readers of the portal's downloads
describe: a "B2B" sheet with a two-row header, one row per invoice and tax rate."""

import io
import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from docforge.gstr2b import MAX_ROWS, TwoBInvalid, read_2b

GROUPS = [
    "GSTIN of supplier",
    "Trade/Legal name",
    "Invoice details",
    None,
    None,
    None,
    "Place of supply",
    "Supply Attract Reverse Charge",
    "Rate(%)",
    "Taxable Value (₹)",
    "Tax Amount",
    None,
    None,
    None,
    "GSTR-1/IFF/GSTR-5 Period",
    "ITC Availability",
]
SUBS = [None, None, "Invoice number", "Invoice type", "Invoice Date", "Invoice Value(₹)",
        None, None, None, None, "Integrated Tax(₹)", "Central Tax(₹)", "State/UT Tax(₹)",
        "Cess(₹)", None, None]  # fmt: skip


def portal_excel(rows: list[list[Any]], *, sheet: str = "B2B") -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    readme = book.active
    assert readme is not None
    readme.title = "Read me"
    b2b = book.create_sheet(sheet)
    b2b.append(["Goods and Services Tax - GSTR-2B"])
    b2b.append(["Taxable inward supplies received from registered persons"])
    b2b.append([])
    b2b.append(GROUPS)
    b2b.append(SUBS)
    for row in rows:
        b2b.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def line(gstin: str, number: Any, day: str, value: float, rate: int, taxable: float,
         igst: float = 0, cgst: float = 0, sgst: float = 0) -> list[Any]:  # fmt: skip
    return [gstin, "Synthetic Seller", number, "Regular", day, value, "24-Gujarat", "N", rate,
            taxable, igst, cgst, sgst, 0, "Oct'26", "Yes"]  # fmt: skip


def test_an_excel_2b_adds_an_invoices_rate_rows_into_one() -> None:
    data = portal_excel([
        line("24AABFN7754K1Z0", "NVM/101", "05-10-2026", 580.0, 12, 180.0, cgst=10.8, sgst=10.8),
        line("24AABFN7754K1Z0", "NVM/101", "05-10-2026", 580.0, 5, 360.0, cgst=9.0, sgst=9.0),
        line("27AAACS1234F1ZS", "000123", "06-10-2026", 952.0, 12, 850.0, igst=102.0),
    ])  # fmt: skip

    found = {i.invoice_no: i for i in read_2b(data)}

    first = found["NVM/101"]
    assert (first.rows, first.taxable_value, first.cgst, first.sgst, first.igst) == (
        2, Decimal("540.0"), Decimal("19.8"), Decimal("19.8"), Decimal("0")
    )  # fmt: skip
    assert (first.invoice_value, first.invoice_date) == (Decimal("580.0"), date(2026, 10, 5))
    assert found["000123"].supplier_gstin == "27AAACS1234F1ZS"  # leading zeros kept
    assert found["000123"].igst == Decimal("102.0")


def test_a_number_excel_held_as_a_number_is_read_as_its_digits() -> None:
    data = portal_excel([line("27AAACS1234F1ZS", 4521, "06-10-2026", 952.0, 12, 850.0)])

    assert [i.invoice_no for i in read_2b(data)] == ["4521"]


def test_a_json_2b_is_read_from_its_b2b_invoices() -> None:
    document = {"chksum": "x", "data": {"gstin": "24AAACD5678G1ZN", "rtnprd": "102026",
        "docdata": {"b2b": [{"ctin": "24AABFN7754K1Z0", "trdnm": "Synthetic Seller", "inv": [
            {"inum": "NVM/101", "dt": "05-10-2026", "val": 580, "pos": "24", "rev": "N",
             "itcavl": "Y", "items": [
                 {"num": 1, "rt": 12, "txval": 180, "cgst": 10.8, "sgst": 10.8, "igst": 0},
                 {"num": 2, "rt": 5, "txval": 360, "cgst": 9, "sgst": 9, "igst": 0},
             ]}]}]}}}  # fmt: skip

    (invoice,) = read_2b(json.dumps(document).encode())

    assert (invoice.taxable_value, invoice.total_tax, invoice.invoice_value) == (
        Decimal("540"), Decimal("39.6"), Decimal("580")
    )  # fmt: skip
    assert invoice.invoice_date == date(2026, 10, 5)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "empty"),
        (b"not json", "neither a GSTR-2B Excel nor its JSON"),
        (b'{"data": {"docdata": {}}}', "no B2B invoices"),
        (b"PK\x03\x04broken", "not a workbook"),
    ],
)
def test_a_file_that_is_no_2b_is_refused_in_words(data: bytes, message: str) -> None:
    with pytest.raises(TwoBInvalid, match=message):
        read_2b(data)


def test_a_workbook_without_the_b2b_sheet_or_header_is_refused() -> None:
    from openpyxl import Workbook

    with pytest.raises(TwoBInvalid, match="no B2B sheet"):
        read_2b(portal_excel([], sheet="B2BA"))
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "B2B"
    sheet.append(["Supplier", "Bill", "Amount"])
    buffer = io.BytesIO()
    book.save(buffer)
    with pytest.raises(TwoBInvalid, match="no header with 'GSTIN of supplier'"):
        read_2b(buffer.getvalue())


def test_more_rows_than_the_limit_are_refused_naming_the_limit() -> None:
    supplier = {"ctin": "24AABFN7754K1Z0", "inv": [
        {"inum": f"N{n}", "dt": "05-10-2026", "val": 1, "txval": 1} for n in range(MAX_ROWS + 1)
    ]}  # fmt: skip

    with pytest.raises(TwoBInvalid, match="more than 20,000 invoice rows"):
        read_2b(json.dumps({"data": {"docdata": {"b2b": [supplier]}}}).encode())
