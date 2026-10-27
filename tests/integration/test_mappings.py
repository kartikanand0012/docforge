"""V2 P1: the workspace's mapping master - how its Tally / Marg books name each supplier and
item - and the export runs that use it.

An export never guesses: a supplier or item with no mapping stops the run with a task list
and no file. A suggestion is only ever offered; a mapping is made by a person's own call. A
past export's file is kept as it was made, so a later edit changes only later exports.
"""

import csv
import io
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from test_visible_flags import Desk

from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry
from docforge.db.session import SessionFactory
from docforge.export_runs import ExportRunService, NoFile, RunRangeInvalid
from docforge.mappings.keys import item_key
from docforge.mappings.service import (
    MappingExists,
    MappingInvalid,
    MappingNotFound,
    MappingService,
)

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
SELLER = "24AABFN7754K1Z0"  # pair_001's seller, Navjivan Medical Agencies
LEDGER = "Navjivan Medical Agencies (Ahmedabad)"
PRODUCTS = [  # pair_001's lines: printed name, pack
    ("Amoxicillin Capsules IP 250mg", "10x10"),
    ("Ciprofloxacin Tablets IP 500mg", "10x10"),
    ("Atorvastatin Tablets IP 10mg", "10x10"),
    ("Salbutamol Inhaler 100mcg", "1x1"),
    ("Insulin Injection IP 40IU/ml", "1x10ml"),
    ("Cetirizine Tablets IP 10mg", "10x10"),
    ("Pantoprazole Tablets IP 40mg", "10x10"),
    ("Ibuprofen Tablets IP 400mg", "10x10"),
    ("Azithromycin Tablets IP 500mg", "1x3"),
    ("ORS Powder WHO Formula 21g", "1x25"),
]
ASHA = {"actor": "user:asha", "name": "Asha Rao"}


def _today() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


class Books:
    """pair_001's bills, approved, and the workspace's mapping master and export runs."""

    def __init__(self, sessions: SessionFactory, desk: Desk) -> None:
        self.sessions, self.desk = sessions, desk
        self.mappings = MappingService(sessions)
        self.numbers = 0

    def runs(self) -> ExportRunService:
        return ExportRunService(self.sessions, self.desk.last.store, self.mappings)

    def approved_bill(self, **printed: str) -> uuid.UUID:
        """A new bill of pair_001 (its own invoice number, so it is no duplicate), approved."""
        self.numbers += 1
        printed.setdefault("invoice_no", f"NVM/26-27/9{self.numbers:04d}")
        bill = self.desk.receive(f"bill-{self.numbers}.pdf", None, **printed)
        blockers = self.desk.review.detail(DEFAULT_TENANT_ID, bill).blockers
        self.desk.sign(bill, override_reason="checked on paper" if blockers else None)
        return bill

    def map_supplier(self, **fields: Any) -> Any:
        fields = {"seller_gstin": SELLER, "printed_name": "Navjivan Medical Agencies",
                  "tally_ledger": LEDGER, **fields}  # fmt: skip
        return self.mappings.create_supplier(DEFAULT_TENANT_ID, **fields, **ASHA)

    def map_items(self, products: list[tuple[str, str]] = PRODUCTS, **fields: Any) -> list[Any]:
        fields.setdefault("tally_item", None)
        return [
            self.mappings.create_item(
                DEFAULT_TENANT_ID,
                product_name=name,
                pack=pack,
                **{**fields, "tally_item": fields["tally_item"] or name.upper()},
                **ASHA,
            )
            for name, pack in products
        ]

    def export(self, target: str = "tally") -> Any:
        today = _today()
        return self.runs().create(
            DEFAULT_TENANT_ID, target=target, start=today - timedelta(days=1), end=today, **ASHA
        )

    def rows(self, run: Any) -> list[dict[str, str]]:
        _, data = self.runs().file(DEFAULT_TENANT_ID, run.id, actor="user:asha")
        return list(csv.DictReader(io.StringIO(data.decode())))

    def audit(self, prefix: str) -> list[AuditEntry]:
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(AuditEntry)
                    .where(AuditEntry.action.startswith(prefix))
                    .order_by(AuditEntry.id)
                )
            )


@pytest.fixture
def books(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> Books:
    return Books(sessions, Desk(sessions, raw_invoice_from_label, raw_order_from_label))


# AC-P1-1: one mapping per GSTIN, whatever name the bill prints


def test_every_later_bill_from_a_mapped_gstin_uses_its_ledger_whatever_name_it_prints(
    books: Books,
) -> None:
    books.map_supplier()
    books.map_items()
    books.approved_bill()
    books.approved_bill(seller__name="NAVJIVAN MEDICAL AGENCIES.")
    books.approved_bill(seller__name="Navjivan Med. Agencies (Ahmedabad Branch)")

    run = books.export()

    assert run.status == "ready" and run.tasks is None
    rows = books.rows(run)
    assert {row["party"] for row in rows} == {LEDGER}
    assert {row["supplier_gstin"] for row in rows} == {SELLER}
    assert run.invoices == 3 and run.lines == 30


def test_a_mapping_is_found_by_the_gstin_however_it_was_typed(books: Books) -> None:
    made = books.map_supplier(seller_gstin=" 24aabfn7754k1z0 ")

    assert made.seller_gstin == SELLER


# AC-P1-2: an unmapped supplier or item stops the run with a task list and no file


def test_a_run_with_unmapped_parties_lists_exactly_them_and_makes_no_file(books: Books) -> None:
    books.map_items(PRODUCTS[:8])  # two items unmapped
    books.approved_bill()
    books.approved_bill(seller__name="NAVJIVAN MEDICAL AGENCIES")

    run = books.export()

    assert run.status == "needs_mapping"
    assert run.filename is None and run.sha256 is None
    assert run.tasks is not None
    (supplier,) = run.tasks.suppliers
    assert supplier.seller_gstin == SELLER
    assert set(supplier.printed_names) == {"Navjivan Medical Agencies", "NAVJIVAN MEDICAL AGENCIES"}
    assert supplier.bills == 2
    assert {(i.product_name, i.pack) for i in run.tasks.items} == set(PRODUCTS[8:])
    assert run.tasks.problems == ()
    with pytest.raises(NoFile):
        books.runs().file(DEFAULT_TENANT_ID, run.id, actor="user:asha")


def test_once_each_task_is_mapped_the_same_run_again_comes_out_clean(books: Books) -> None:
    books.map_items(PRODUCTS[:9])
    books.approved_bill()
    first = books.export()
    assert first.status == "needs_mapping"

    books.map_supplier()
    books.map_items(PRODUCTS[9:])
    again = books.export()

    assert again.status == "ready" and again.tasks is None
    assert len(books.rows(again)) == 10


def test_a_supplier_mapped_for_tally_only_is_a_task_for_a_marg_export(books: Books) -> None:
    mapping = books.map_supplier()
    books.map_items(marg_item="MARG ITEM")
    books.approved_bill()

    run = books.export("marg")

    assert run.status == "needs_mapping" and run.tasks is not None
    (supplier,) = run.tasks.suppliers
    assert supplier.mapping_id == mapping.id  # mapped already: fill in its Marg name
    assert run.tasks.items == ()


def test_a_bill_without_a_seller_gstin_is_a_problem_to_correct_not_a_guess(books: Books) -> None:
    books.map_supplier()
    books.map_items()
    bill = books.approved_bill(seller__gstin="")

    run = books.export()

    assert run.status == "needs_mapping" and run.tasks is not None
    (problem,) = run.tasks.problems
    assert problem.document_id == bill
    assert "no seller GSTIN" in problem.problem


def test_only_approved_bills_in_the_range_are_exported(books: Books) -> None:
    books.map_supplier()
    books.map_items()
    books.approved_bill()
    books.desk.receive("waiting.pdf", invoice_no="NVM/WAIT/1")  # not signed

    run = books.export()

    assert run.invoices == 1
    later = books.runs().create(
        DEFAULT_TENANT_ID, target="tally", start=_today() + timedelta(days=1),
        end=_today() + timedelta(days=2), **ASHA,
    )  # fmt: skip
    assert later.status == "ready" and later.invoices == 0
    _, data = books.runs().file(DEFAULT_TENANT_ID, later.id, actor="user:asha")
    assert data.decode().splitlines()[0].startswith("voucher_date,")  # the header still


def test_a_range_too_long_or_backwards_is_refused(books: Books) -> None:
    today = _today()
    for start, end in ((today, today - timedelta(days=1)), (today - timedelta(days=400), today)):
        with pytest.raises(RunRangeInvalid):
            books.runs().create(DEFAULT_TENANT_ID, target="tally", start=start, end=end, **ASHA)


def test_each_run_is_audited_without_names_from_the_bills(books: Books) -> None:
    books.approved_bill()
    books.export()
    books.map_supplier()
    books.map_items()
    ready = books.export()
    books.rows(ready)

    entries = books.audit("export.")
    assert [e.action for e in entries] == ["export.created", "export.created", "export.downloaded"]
    assert entries[0].details["status"] == "needs_mapping"
    assert entries[0].details["unmapped_suppliers"] == 1
    assert entries[1].details["sha256"] == ready.sha256
    for entry in entries:
        assert "Navjivan" not in str(entry.details) and SELLER not in str(entry.details)


# The file itself


def test_the_file_books_each_line_under_the_mapped_names_and_preferred_hsn(books: Books) -> None:
    books.map_supplier(purchase_ledger="Purchase GST 12%", tax_treatment="Regular")
    books.map_items(PRODUCTS[1:])
    books.mappings.create_item(
        DEFAULT_TENANT_ID, product_name=PRODUCTS[0][0], pack=PRODUCTS[0][1],
        tally_item="AMOXY 250 CAP", preferred_hsn="30041090", **ASHA,
    )  # fmt: skip
    books.approved_bill()

    rows = books.rows(books.export())

    first = rows[0]
    assert first["item"] == "AMOXY 250 CAP"
    assert first["hsn"] == "30041090"  # the workspace's own, not the printed 30041030
    assert first["printed_product_name"] == PRODUCTS[0][0]
    assert first["purchase_ledger"] == "Purchase GST 12%"
    assert first["tax_treatment"] == "Regular"
    assert first["supplier_invoice_no"].startswith("NVM/26-27/9")
    assert first["voucher_date"] == "2026-09-02"  # pair_001's bill date, as YYYY-MM-DD
    assert rows[1]["hsn"] == "30042019"  # no preference: as printed


# AC-P1-3: a suggestion is never a mapping until a person confirms it


def test_suggestions_alone_never_make_a_mapping(books: Books) -> None:
    books.mappings.import_masters(
        DEFAULT_TENANT_ID,
        f"name,gstin,type\n{LEDGER},{SELLER},supplier\nAMOXY 250 CAP,,item\n".encode(),
        **ASHA,
    )
    books.approved_bill()
    run = books.export()

    suggested = books.mappings.suggestions(DEFAULT_TENANT_ID, run.tasks, target="tally")

    (supplier,) = suggested.suppliers
    assert (supplier.suggestions[0].name, supplier.suggestions[0].why) == (LEDGER, "same GSTIN")
    amoxy = next(i for i in suggested.items if i.product_name == PRODUCTS[0][0])
    assert amoxy.suggestions[0].name == "AMOXY 250 CAP"
    assert books.mappings.list_suppliers(DEFAULT_TENANT_ID).items == ()
    assert books.mappings.list_items(DEFAULT_TENANT_ID).items == ()
    assert [e.action for e in books.audit("mapping.")] == ["mapping.imported"]

    confirmed = books.map_supplier(source="suggestion")
    assert confirmed.source == "suggestion"
    assert confirmed.created_by == "Asha Rao"


def test_a_mapping_already_made_is_offered_for_a_similar_item(books: Books) -> None:
    books.map_items(PRODUCTS[:1], tally_item="AMOXY 250 CAP")
    tasks = books.mappings.tasks_for_items(
        DEFAULT_TENANT_ID, [("Amoxicillin Caps 250 mg", "1x10", None)], target="tally"
    )

    suggested = books.mappings.suggestions(DEFAULT_TENANT_ID, tasks, target="tally")

    (item,) = suggested.items
    assert item.suggestions[0].name == "AMOXY 250 CAP"
    assert item.suggestions[0].source == "mapping"


# AC-P1-4: a master import keeps the good rows and names the bad ones


def test_a_thousand_row_import_with_five_bad_rows_imports_the_rest_and_is_audited(
    books: Books,
) -> None:
    bad = {7, 150, 333, 640, 999}  # data rows: line in the file is one more (the header)
    rows = ["name,gstin,type"]
    for n in range(1, 1001):
        if n == 7:
            rows.append(",,item")
        elif n == 150:
            rows.append("Bad Kind,,vendor")
        elif n == 333:
            rows.append("Bad GSTIN,24AABFN7754K1Z9,supplier")
        elif n == 640:
            rows.append(f"{'X' * 201},,item")
        elif n == 999:
            rows.append(f"An Item,{SELLER},item")
        elif n % 2:
            rows.append(f"Supplier {n},,supplier")
        else:
            rows.append(f"Item {n} Tab,,item")
    data = ("\n".join(rows) + "\n").encode()

    report = books.mappings.import_masters(DEFAULT_TENANT_ID, data, **ASHA)

    assert (report.rows, report.imported, report.already_known) == (1000, 995, 0)
    assert [r.line for r in report.rejected] == sorted(n + 1 for n in bad)
    assert all(r.reason for r in report.rejected)
    (entry,) = books.audit("mapping.imported")
    assert entry.actor == "user:asha"
    assert entry.details == {"rows": 1000, "imported": 995, "already_known": 0, "rejected": 5}

    again = books.mappings.import_masters(DEFAULT_TENANT_ID, data, **ASHA)
    assert (again.imported, again.already_known) == (0, 995)  # no name twice


# AC-P1-6: an edit changes later exports only


def test_editing_a_mapping_changes_later_exports_and_a_past_file_stays_byte_for_byte(
    books: Books,
) -> None:
    supplier = books.map_supplier()
    books.map_items()
    books.approved_bill()
    first = books.export()
    _, original = books.runs().file(DEFAULT_TENANT_ID, first.id, actor="user:asha")

    books.mappings.update_supplier(
        DEFAULT_TENANT_ID, supplier.id, {"tally_ledger": "NAVJIVAN (NEW NAME)"}, **ASHA
    )
    books.mappings.delete_item(
        DEFAULT_TENANT_ID, books.mappings.list_items(DEFAULT_TENANT_ID).items[0].id,
        actor="user:asha",
    )  # fmt: skip

    _, again = books.runs().file(DEFAULT_TENANT_ID, first.id, actor="user:asha")
    assert again == original
    assert books.runs().get(DEFAULT_TENANT_ID, first.id).sha256 == first.sha256
    later = books.export()
    assert later.status == "needs_mapping"  # the removed item
    books.map_items(PRODUCTS[:1])
    assert {r["party"] for r in books.rows(books.export())} == {"NAVJIVAN (NEW NAME)"}


# AC-P1-7: one mapping per GSTIN, and a plain error naming the one there is


def test_a_second_mapping_for_a_gstin_is_refused_naming_the_first(books: Books) -> None:
    first = books.map_supplier()

    with pytest.raises(MappingExists) as raised:
        books.map_supplier(seller_gstin=SELLER.lower(), tally_ledger="Another Ledger")

    assert raised.value.existing.id == first.id
    assert LEDGER in str(raised.value)
    assert SELLER in str(raised.value)


def test_a_second_mapping_for_an_item_is_refused_naming_the_first(books: Books) -> None:
    (first,) = books.map_items(PRODUCTS[:1])

    with pytest.raises(MappingExists) as raised:
        books.mappings.create_item(
            DEFAULT_TENANT_ID, product_name="AMOXICILLIN CAPSULES IP 250 MG", pack="10 x 10",
            tally_item="Other", **ASHA,
        )  # fmt: skip

    assert raised.value.existing.id == first.id
    assert first.item_key == item_key(*PRODUCTS[0])


# Making, editing and removing


@pytest.mark.parametrize(
    ("fields", "says"),
    [
        ({"seller_gstin": "24AABFN7754K1Z9"}, "not a valid GSTIN"),
        ({"tally_ledger": None}, "Tally ledger or the Marg party"),
        ({"tally_ledger": "   "}, "Tally ledger or the Marg party"),
        ({"printed_name": ""}, "printed name"),
        ({"tally_ledger": "X" * 201}, "200 characters"),
    ],
)
def test_a_supplier_mapping_that_cannot_be_booked_is_refused_in_words(
    books: Books, fields: dict[str, Any], says: str
) -> None:
    with pytest.raises(MappingInvalid, match=says):
        books.map_supplier(**fields)


def test_a_preferred_hsn_is_four_six_or_eight_digits(books: Books) -> None:
    with pytest.raises(MappingInvalid, match="HSN"):
        books.map_items(PRODUCTS[:1], preferred_hsn="3004A")


def test_each_change_is_audited_with_who_and_no_names_from_the_bills(books: Books) -> None:
    made = books.map_supplier()
    books.mappings.update_supplier(
        DEFAULT_TENANT_ID, made.id, {"marg_party": "NAVJIVAN MED"}, **ASHA
    )
    books.mappings.delete_supplier(DEFAULT_TENANT_ID, made.id, actor="user:asha")

    entries = books.audit("mapping.")
    assert [e.action for e in entries] == ["mapping.created", "mapping.updated", "mapping.deleted"]
    assert {e.actor for e in entries} == {"user:asha"}
    assert {e.target_id for e in entries} == {str(made.id)}
    assert entries[1].details == {"kind": "supplier", "fields": ["marg_party"]}
    for entry in entries:
        shown = str(entry.details)
        assert "Navjivan" not in shown and "NAVJIVAN" not in shown and SELLER not in shown


def test_an_edit_records_who_and_when_and_keeps_who_made_it(books: Books) -> None:
    made = books.map_supplier()

    changed = books.mappings.update_supplier(
        DEFAULT_TENANT_ID, made.id, {"tally_ledger": "Navjivan"}, actor="user:ben", name="Ben"
    )

    assert (changed.created_by, changed.updated_by) == ("Asha Rao", "Ben")
    assert changed.updated_at is not None and changed.updated_at >= changed.created_at
    with pytest.raises(MappingInvalid):
        books.mappings.update_supplier(
            DEFAULT_TENANT_ID, made.id, {"tally_ledger": None}, **ASHA
        )  # it would book under no name at all


def test_removing_or_editing_a_mapping_not_there_is_not_found(books: Books) -> None:
    missing = uuid.uuid4()
    with pytest.raises(MappingNotFound):
        books.mappings.delete_supplier(DEFAULT_TENANT_ID, missing, actor="user:asha")
    with pytest.raises(MappingNotFound):
        books.mappings.update_item(DEFAULT_TENANT_ID, missing, {"tally_item": "X"}, **ASHA)


def test_the_list_pages_through_in_name_order_and_finds_by_part_of_a_name(books: Books) -> None:
    books.map_items()

    first = books.mappings.list_items(DEFAULT_TENANT_ID, limit=4)
    second = books.mappings.list_items(DEFAULT_TENANT_ID, limit=4, after=first.next_after)

    assert first.total == 10 and len(first.items) == 4
    names = [i.product_name for i in (*first.items, *second.items)]
    assert names == sorted(names, key=str.casefold)
    found = books.mappings.list_items(DEFAULT_TENANT_ID, q="tablets ip 500")
    assert {i.product_name for i in found.items} == {
        "Ciprofloxacin Tablets IP 500mg",
        "Azithromycin Tablets IP 500mg",
    }


# Suggestions are worked out from an index kept while the workspace's names stand


def test_a_name_imported_or_mapped_since_is_offered_the_next_time(books: Books) -> None:
    tasks = books.mappings.tasks_for_items(
        DEFAULT_TENANT_ID, [("Dolo 650 Tablets", "15s", None)], target="tally"
    )
    assert (
        books.mappings.suggestions(DEFAULT_TENANT_ID, tasks, target="tally").items[0].suggestions
        == ()
    )

    books.mappings.import_masters(DEFAULT_TENANT_ID, b"name,type\nDOLO 650 TAB 15S,item\n", **ASHA)
    (item,) = books.mappings.suggestions(DEFAULT_TENANT_ID, tasks, target="tally").items
    assert [s.name for s in item.suggestions] == ["DOLO 650 TAB 15S"]

    books.map_items([("Dolo 650 Tablet", "15 s")], tally_item="DOLO-650 (15)")
    (item,) = books.mappings.suggestions(DEFAULT_TENANT_ID, tasks, target="tally").items
    assert {s.name for s in item.suggestions} == {"DOLO 650 TAB 15S", "DOLO-650 (15)"}


def test_one_workspaces_kept_index_is_never_offered_to_another(
    books: Books, other_tenant: uuid.UUID
) -> None:
    books.mappings.import_masters(DEFAULT_TENANT_ID, b"name,type\nDOLO 650 TAB,item\n", **ASHA)
    products = [("Dolo 650 Tab", None, None)]
    mine = books.mappings.tasks_for_items(DEFAULT_TENANT_ID, products, target="tally")
    assert books.mappings.suggestions(DEFAULT_TENANT_ID, mine, target="tally").items[0].suggestions

    theirs = books.mappings.tasks_for_items(other_tenant, products, target="tally")
    offered = books.mappings.suggestions(other_tenant, theirs, target="tally")

    assert offered.items[0].suggestions == ()
