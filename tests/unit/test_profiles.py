"""V2 P3: the import-profile engine. A layout is data; the engine must make, from the same
bills, the file the books accepted, cell for cell (the golden-file test is the definition of
"the format is right"). Every bill and file here is synthetic: no Marg layout is assumed.
"""

import csv
import io
from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from docforge.profiles.compare import SampleUnreadable, compare
from docforge.profiles.render import Booked, format_value, render, table
from docforge.profiles.sample import read_sample
from docforge.profiles.spec import SAMPLE_NAME, Column, Profile, sample_profile
from profile_fixtures import GOLDEN, booked


def _with(profile: Profile, **changes: object) -> Profile:
    return Profile.model_validate({**profile.model_dump(), **changes})


def _csv_rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode())))


# AC-P3-1: the golden file


def test_the_engine_reproduces_the_sample_file_cell_for_cell() -> None:
    profile = sample_profile()

    result = compare(profile, table(profile, booked()), GOLDEN.read_bytes())

    assert result.diffs == ()
    assert result.exact and result.rows_expected == result.rows_produced == 3
    assert result.cells == result.equal == 21 * 4
    assert render(profile, booked()) == GOLDEN.read_bytes()  # byte for byte, CRLF and all


def test_a_cell_that_differs_is_named_by_row_and_column() -> None:
    profile = sample_profile()
    accepted = GOLDEN.read_bytes().replace(b"SB101", b"SB102").replace(b"05-10-2026", b"5/10/26")

    result = compare(profile, table(profile, booked()), accepted)

    assert not result.exact
    found = {(d.row, d.column, d.expected, d.produced) for d in result.diffs}
    assert (2, "Batch", "SB102", "SB101") in found
    assert (2, "Bill Date", "5/10/26", "05-10-2026") in found
    assert (3, "Bill Date", "5/10/26", "05-10-2026") in found


def test_a_row_the_engine_does_not_make_is_a_difference() -> None:
    profile = sample_profile()

    result = compare(profile, table(profile, booked()[:1]), GOLDEN.read_bytes())

    assert not result.exact
    assert (result.rows_expected, result.rows_produced) == (3, 2)


def test_the_sample_profile_says_it_is_made_up() -> None:
    assert "SAMPLE" in SAMPLE_NAME and "not a Marg format" in SAMPLE_NAME


# AC-P3-3 / free quantity: its own column or its own row, never merged, never dropped


def test_batch_expiry_and_free_quantity_are_on_every_line_that_has_them() -> None:
    rows = _csv_rows(render(sample_profile(), booked()))
    header, body = rows[0], rows[1:]
    batch, expiry, qty, free = (header.index(n) for n in ("Batch", "Expiry", "Qty", "Free"))

    assert [r[batch] for r in body] == ["SB101", "SC202", "SA303"]
    assert [r[expiry] for r in body] == ["12/28", "02/27", "04/28"]
    assert [(r[qty], r[free]) for r in body] == [("10", "2"), ("5", "0"), ("20", "4")]


def test_free_quantity_on_its_own_row_is_never_added_to_the_billed_quantity() -> None:
    columns = [c for c in sample_profile().columns if c.header != "Free"]
    columns.append(Column(header="Kind", source="computed", field="row_kind",
                          format={"codes": {"billed": "B", "free": "F"}}))  # fmt: skip
    profile = _with(sample_profile(), free_qty="row", columns=columns)

    rows = _csv_rows(render(profile, booked()))
    header, body = rows[0], rows[1:]
    names = ("Item", "Batch", "Qty", "Rate", "Amount", "Kind")
    seen = [tuple(r[header.index(n)] for n in names) for r in body]

    assert seen == [
        ("PARACETAMOL 500 TAB SYN", "SB101", "10", "18.00", "201.60", "B"),
        ("PARACETAMOL 500 TAB SYN", "SB101", "2", "0.00", "0.00", "F"),
        ("COUGH SYRUP 100ML SYN", "SC202", "5", "80.00", "378.00", "B"),
        ("AMOXY 250 CAP SYN", "SA303", "20", "42.50", "952.00", "B"),
        ("AMOXY 250 CAP SYN", "SA303", "4", "0.00", "0.00", "F"),
    ]
    tax = [r[header.index("CGST")] for r in body]
    assert tax[1] == "0.00" and tax[4] == "0.00"  # a free row bears no tax


def test_a_per_line_profile_must_carry_the_item_batch_expiry_and_free_quantity() -> None:
    for left_out in ("Item", "Batch", "Expiry", "Free"):
        columns = [c for c in sample_profile().columns if c.header != left_out]
        with pytest.raises(ValidationError, match="must carry"):
            _with(sample_profile(), columns=columns)


# the tax split, formats and file types


def test_the_tax_is_split_by_the_sellers_state_against_the_place_of_supply() -> None:
    rows = _csv_rows(render(sample_profile(), booked()))
    header = rows[0]
    heads = [tuple(r[header.index(h)] for h in ("CGST", "SGST", "IGST")) for r in rows[1:]]

    assert heads == [
        ("10.80", "10.80", "0.00"),
        ("9.00", "9.00", "0.00"),
        ("0.00", "0.00", "102.00"),  # a Maharashtra seller supplying Gujarat
    ]


def test_dates_numbers_codes_and_case_follow_the_profile() -> None:
    profile = _with(
        sample_profile(),
        columns=[
            *sample_profile().columns,
            Column(header="Exp Day", source="line", field="expiry",
                   format={"date": "DD-MMM-YYYY", "month_day": "last"}),
            Column(header="Type", source="computed", field="supply_type",
                   format={"codes": {"intra": "L", "inter": "I"}}),
            Column(header="Party Upper", source="invoice", field="seller_name",
                   format={"upper": True}),
            Column(header="Rate 0dp", source="line", field="ptr", format={"decimals": 0}),
            Column(header="Book", source="constant", value="PURCHASE"),
            Column(header="Note", source="blank"),
        ],
    )  # fmt: skip

    rows = _csv_rows(render(profile, booked()))
    header, first, third = rows[0], rows[1], rows[3]
    value = {name: first[header.index(name)] for name in header}

    assert value["Exp Day"] == "31-Dec-2028"
    assert (value["Type"], third[header.index("Type")]) == ("L", "I")
    assert value["Party Upper"] == "SYNTHETIC SELLER"
    assert value["Rate 0dp"] == "18" and third[header.index("Rate 0dp")] == "43"  # 42.50 half-up
    assert (value["Book"], value["Note"]) == ("PURCHASE", "")


def test_delimiter_quoting_header_and_line_ends_follow_the_profile() -> None:
    profile = _with(sample_profile(), delimiter=";", quoting="all", header_row=False, line_end="lf")

    data = render(profile, booked())

    assert b"\r\n" not in data
    assert data.decode().splitlines()[0].startswith('"SYN/0001";"05-10-2026";')
    assert compare(profile, table(profile, booked()), data).exact


def test_a_cell_that_would_be_a_spreadsheet_formula_is_written_as_text() -> None:
    from openpyxl import load_workbook

    entries = booked()
    first = entries[0]
    hostile = first.items[0].__class__(**{**vars(first.items[0]), "marg_item": "=HYPERLINK(1)"})
    entries[0] = Booked(first.bill, first.party, (hostile, first.items[1]))

    rows = _csv_rows(render(sample_profile(), entries))
    assert rows[1][rows[0].index("Item")] == "'=HYPERLINK(1)"

    xlsx = render(_with(sample_profile(), file_type="xlsx"), entries)
    sheet = load_workbook(io.BytesIO(xlsx)).active
    assert sheet is not None
    cell = sheet.cell(row=2, column=5)
    assert (cell.value, cell.data_type) == ("=HYPERLINK(1)", "s")  # text, never a formula


def test_an_xlsx_file_is_the_same_bytes_every_time_and_reads_back_exactly() -> None:
    from openpyxl import load_workbook

    def typed(column: Column) -> Column:
        if column.header in ("Qty", "Rate", "Amount"):
            return column.model_copy(update={"cell": "number"})
        if column.header == "Bill Date":
            return column.model_copy(update={"cell": "date"})
        return column

    profile = _with(
        sample_profile(), file_type="xlsx", columns=[typed(c) for c in sample_profile().columns]
    )

    first = render(profile, booked())

    assert first == render(profile, booked())
    assert first.startswith(b"PK")
    assert compare(profile, table(profile, booked()), first).exact
    sheet = load_workbook(io.BytesIO(first)).active
    assert sheet is not None
    assert sheet.cell(row=2, column=2).value == datetime(2026, 10, 5)
    assert Decimal(str(sheet.cell(row=4, column=13).value)) == Decimal("42.5")


def test_an_xlsx_file_from_a_spreadsheet_compares_by_value() -> None:
    from openpyxl import Workbook

    numbers = [c.model_copy(update={"cell": "number"}) if c.header in ("Qty", "Rate") else c
               for c in sample_profile().columns]  # fmt: skip
    profile = _with(sample_profile(), file_type="xlsx", columns=numbers)
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Sheet1"
    for row in _csv_rows(GOLDEN.read_bytes()):
        sheet.append(row)
    sheet.cell(row=4, column=10).value = 20  # a number cell where the profile writes "20"
    sheet.cell(row=4, column=13).value = 42.5  # 42.5 for "42.50"
    buffer = io.BytesIO()
    workbook.save(buffer)

    assert compare(profile, table(profile, booked()), buffer.getvalue()).exact


def test_a_per_invoice_profile_is_one_row_per_bill_and_takes_no_line_fields() -> None:
    profile = Profile(
        layout="per_invoice",
        columns=(
            Column(header="Bill", source="invoice", field="invoice_no"),
            Column(header="Party", source="mapping", field="party"),
            Column(header="Lines", source="computed", field="line_count"),
            Column(header="Tax", source="computed", field="tax_total", format={"decimals": 2}),
        ),
    )

    rows = _csv_rows(render(profile, booked()))

    assert rows == [
        ["Bill", "Party", "Lines", "Tax"],
        ["SYN/0001", "SYNTHETIC SUPPLIER (AHD)", "2", "39.60"],
        ["SYN/0002", "SYNTHETIC PHARMA (MUMBAI)", "1", "102.00"],
    ]
    with pytest.raises(ValidationError, match="does not have"):
        Profile(
            layout="per_invoice", columns=(Column(header="B", source="line", field="batch_no"),)
        )


@pytest.mark.parametrize(
    ("columns", "message"),
    [
        ([Column(header="X", source="constant")], "constant with no text"),
        ([Column(header="X", source="invoice", field="nothing")], "does not have"),
        ([Column(header="X", source="invoice", field="invoice_no"),
          Column(header="x ", source="invoice", field="po_no")], "same header"),
        ([Column(header="X", source="invoice", field="invoice_no", cell="date")], "not a date"),
    ],
)  # fmt: skip
def test_a_profile_that_cannot_be_made_is_refused_in_words(
    columns: list[Column], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Profile(layout="per_invoice", columns=tuple(columns))


def test_a_date_pattern_with_other_letters_is_refused() -> None:
    with pytest.raises(ValidationError, match="DD, MM, MMM, YY, YYYY"):
        Column(header="D", source="invoice", field="invoice_date", format={"date": "dd/mm/yy"})


def test_a_profile_round_trips_through_json() -> None:
    profile = sample_profile()
    assert Profile.model_validate_json(profile.model_dump_json()) == profile


def test_month_values_are_written_on_the_first_or_last_day() -> None:
    column = Column(header="E", source="line", field="expiry", format={"date": "DD/MM/YYYY"})
    last = Column(header="E", source="line", field="expiry",
                  format={"date": "DD/MM/YYYY", "month_day": "last"})  # fmt: skip

    assert format_value("2028-02", column).text == "01/02/2028"
    assert format_value("2028-02", last).text == "29/02/2028"
    assert format_value("2028-02", last).value == date(2028, 2, 29)


# reading a sample to start a profile from


def test_reading_the_sample_offers_a_source_and_format_for_each_known_header() -> None:
    layout = read_sample(GOLDEN.read_bytes())

    assert (layout.file_type, layout.delimiter, layout.header_row, layout.rows) == (
        "csv", ",", True, 3
    )  # fmt: skip
    offers = {c.header: c.offer for c in layout.columns}
    assert offers["Batch"] is not None and offers["Batch"].field == "batch_no"
    assert offers["Expiry"] is not None
    assert (offers["Expiry"].field, offers["Expiry"].format.date) == ("expiry", "MM/YY")
    assert offers["Bill Date"] is not None and offers["Bill Date"].format.date == "DD-MM-YYYY"
    assert offers["Free"] is not None and offers["Free"].field == "free_qty"
    assert offers["Rate"] is not None and offers["Rate"].format.decimals == 2
    assert offers["Item"] is not None and offers["Item"].source == "mapping"
    batch = next(c for c in layout.columns if c.header == "Batch")
    assert batch.examples == ("SB101", "SC202", "SA303")


def test_a_header_it_does_not_know_is_left_for_a_person_to_choose() -> None:
    layout = read_sample(b"Bill No;Godown;Batch\r\nA1;Main;B1\r\n")

    assert layout.delimiter == ";"
    assert [c.offer is None for c in layout.columns] == [False, True, False]


def test_a_file_without_a_header_row_is_read_as_such() -> None:
    layout = read_sample(b"A1,05-10-2026,10\nA2,06-10-2026,20\n")

    assert not layout.header_row and layout.rows == 2
    assert all(c.header == "" and c.offer is None for c in layout.columns)


def test_an_excel_sample_is_read_with_its_dates_and_numbers() -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Purchase"
    sheet.append(["Bill No", "Bill Date", "Qty", "Rate"])
    sheet.append(["A1", datetime(2026, 10, 5), 10, 18.5])
    buffer = io.BytesIO()
    workbook.save(buffer)

    layout = read_sample(buffer.getvalue())

    assert (layout.file_type, layout.sheet_name) == ("xlsx", "Purchase")
    offers = {c.header: c.offer for c in layout.columns}
    assert offers["Bill Date"] is not None and offers["Bill Date"].cell == "date"
    assert offers["Qty"] is not None and offers["Qty"].cell == "number"
    assert next(c for c in layout.columns if c.header == "Bill Date").examples == ("2026-10-05",)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "empty"),
        (b"\xff\xfe\x00b\x00a", "not UTF-8"),
        (b"PK\x03\x04not a workbook", "not a workbook"),
        (b"x" * (2 * 1024 * 1024 + 1), "larger than 2 MB"),
    ],
)
def test_a_sample_that_cannot_be_read_is_refused_in_words(data: bytes, message: str) -> None:
    with pytest.raises(SampleUnreadable, match=message):
        read_sample(data)


def test_a_file_too_long_or_too_wide_is_refused_without_reading_it_all() -> None:
    from openpyxl import Workbook

    long_csv = b"Bill No\n" + b"A1\n" * 20_001
    with pytest.raises(SampleUnreadable, match="more than 20,000 rows"):
        read_sample(long_csv)
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.cell(row=1, column=1).value = "Bill No"
    sheet.cell(row=1, column=16_000).value = "far away"  # a sheet that claims to be vast
    buffer = io.BytesIO()
    workbook.save(buffer)
    with pytest.raises(SampleUnreadable, match="more than 80 columns"):
        read_sample(buffer.getvalue())


def test_a_code_or_upper_case_is_what_an_excel_cell_holds() -> None:
    coded = Column(header="GST", source="line", field="gst_rate", cell="number",
                   format={"decimals": 0, "codes": {"12": "G12"}})  # fmt: skip

    cell = format_value(Decimal("12"), coded)

    assert (cell.text, cell.value) == ("G12", None)


def test_in_an_excel_file_a_cells_kind_is_part_of_the_format() -> None:
    from openpyxl import Workbook

    profile = _with(sample_profile(), file_type="xlsx")  # every column a text cell
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Sheet1"
    for row in _csv_rows(GOLDEN.read_bytes()):
        sheet.append(row)
    sheet.cell(row=2, column=2).value = datetime(2026, 10, 5)  # Marg's file holds a date cell
    buffer = io.BytesIO()
    workbook.save(buffer)

    result = compare(profile, table(profile, booked()), buffer.getvalue())

    assert [(d.row, d.column) for d in result.diffs] == [(2, "Bill Date")]


def test_a_value_an_excel_cell_cannot_hold_is_found_before_the_file() -> None:
    from docforge.profiles.render import unwritable

    entries = booked()
    first = entries[0]
    bad = first.items[1].__class__(**{**vars(first.items[1]), "marg_item": "COUGH\x0cSYRUP"})
    entries[0] = Booked(first.bill, first.party, (first.items[0], bad))

    assert unwritable(sample_profile(), entries) == []  # CSV holds it
    (found,) = unwritable(_with(sample_profile(), file_type="xlsx"), entries)
    assert (found.line, found.header, found.booked.bill.filename) == (2, "Item", "syn-0001.pdf")


def test_a_workbook_that_unpacks_to_far_more_than_its_size_is_refused() -> None:
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/sharedStrings.xml", b"<a>" + b"x" * (60 * 1024 * 1024) + b"</a>")
    data = buffer.getvalue()

    assert len(data) < 2 * 1024 * 1024
    with pytest.raises(SampleUnreadable, match="unpacks to more than 50 MB"):
        read_sample(data)


def test_a_csv_cell_past_the_readers_limit_is_refused_in_words() -> None:
    with pytest.raises(SampleUnreadable, match="cannot be read as CSV"):
        read_sample(b'Bill No\n"' + b"x" * 200_000 + b'"\n')
