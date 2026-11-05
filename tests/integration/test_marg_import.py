"""V2 P3: the Marg purchase import - a profile's layout over the mapping master, after the
pre-flight every import format shares.

No Marg layout is assumed: these runs use the built-in SAMPLE profile (made up) or layouts
made in the test. What is tested is the engine's promise: no profile, no export; every bill
reconciled and every medicine line batched before a file; the version a run used kept, and
its file the same bytes for good; one workspace's profiles never another's.
"""

import csv
import io
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.exc import ProgrammingError
from test_api_mappings import Site
from test_mappings import PRODUCTS, Books, _today
from test_visible_flags import Desk

from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry, ExportProfileVersion
from docforge.db.session import SessionFactory
from docforge.export_runs import NoFile
from docforge.profiles.render import render
from docforge.profiles.service import NO_PROFILE, NoActiveProfile, ProfileNotFound
from docforge.profiles.spec import Profile, sample_profile

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
ASHA = {"actor": "user:asha", "person": "Asha Rao"}
PROFILES = "/v1/settings/export-profiles"
RUNS = "/v1/exports/runs"
PARTY = "NAVJIVAN MEDICAL (MARG)"


@pytest.fixture
def books(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> Books:
    return Books(sessions, Desk(sessions, raw_invoice_from_label, raw_order_from_label))


@pytest.fixture
def site(books: Books) -> Site:
    return Site(books)


def _mapped(books: Books) -> None:
    books.map_supplier(marg_party=PARTY)
    for name, pack in PRODUCTS:
        books.mappings.create_item(
            DEFAULT_TENANT_ID, product_name=name, pack=pack, marg_item=f"M-{name.upper()}",
            actor="user:asha", name="Asha Rao",
        )  # fmt: skip


def _profile(books: Books, spec: Profile | None = None, name: str = "Ours") -> Any:
    return books.runs().profiles.create(
        DEFAULT_TENANT_ID, name=name, spec=spec or sample_profile(), basis="sample", **ASHA
    )


def _marg(books: Books) -> Any:
    today = _today()
    return books.runs().create(
        DEFAULT_TENANT_ID, target="marg", start=today - timedelta(days=1), end=today,
        actor="user:asha", name="Asha Rao", file_format="marg_import",
    )  # fmt: skip


def _rows(books: Books, run: Any) -> list[dict[str, str]]:
    _, data = books.runs().file(DEFAULT_TENANT_ID, run.id, actor="user:asha")
    return list(csv.DictReader(io.StringIO(data.decode())))


def _spec() -> dict[str, Any]:
    return sample_profile().model_dump(mode="json")


# AC-P3-2: no profile, no export


def test_with_no_active_profile_a_marg_import_is_refused_saying_what_is_needed(
    books: Books,
) -> None:
    _mapped(books)
    books.approved_bill()

    with pytest.raises(NoActiveProfile) as refused:
        _marg(books)

    assert str(refused.value) == NO_PROFILE
    assert "We need one purchase import file your Marg accepted" in NO_PROFILE
    assert "Settings > Marg profile" in NO_PROFILE
    assert books.runs().list(DEFAULT_TENANT_ID) == []  # no run, no file


def test_over_http_the_refusal_is_a_409_the_screen_can_act_on(site: Site) -> None:
    client = site.client()
    body = {"target": "marg", "format": "marg_import", "from": str(_today()),
            "to": str(_today())}  # fmt: skip

    refused = client.post(RUNS, json=body)

    assert refused.status_code == 409
    assert refused.json() == {"detail": NO_PROFILE, "code": "no_active_profile"}
    wrong = client.post(RUNS, json={**body, "target": "tally"})
    assert wrong.status_code == 422 and "for Marg" in wrong.json()["detail"]


# A run in the workspace's layout


def test_a_marg_import_books_every_line_with_batch_expiry_and_free_quantity(
    books: Books,
) -> None:
    _mapped(books)
    books.approved_bill()
    _profile(books)

    run = _marg(books)

    assert run.status == "ready" and run.format == "marg_import", run.tasks
    assert run.profile is not None and run.profile.version_no == 1
    assert run.filename is not None and run.filename.startswith("SAMPLE-docforge-marg-import-")
    rows = _rows(books, run)
    assert len(rows) == len(PRODUCTS)
    assert {r["Supplier"] for r in rows} == {PARTY}
    assert all(r["Item"].startswith("M-") for r in rows)
    assert all(r["Batch"] and r["Expiry"] and r["Free"] != "" for r in rows)
    assert all(len(r["Expiry"]) == 5 and r["Expiry"][2] == "/" for r in rows)  # MM/YY


# AC-P3-3 / AC-P3-5: pre-flight refuses a bill by name, and makes no file


def test_a_line_without_its_batch_stops_the_run_naming_the_bill_and_line(books: Books) -> None:
    _mapped(books)
    books.numbers += 1
    bill = books.desk.receive("no-batch.pdf", invoice_no="NVM/26-27/90999")
    books.desk.correct(bill, "lines[2].batch_no", "")
    books.desk.sign(bill, override_reason="checked on paper")
    _profile(books)

    run = _marg(books)

    assert run.status == "needs_mapping" and run.filename is None
    assert run.tasks is not None
    found = [(p.document_id, p.line, p.problem) for p in run.tasks.problems]
    assert [(d, n) for d, n, _ in found] == [(bill, 3)]
    assert found[0][2] == (
        "Invoice NVM/26-27/90999, line 3 has no batch number: correct it on the bill, then "
        "export again."
    )
    with pytest.raises(NoFile):
        books.runs().file(DEFAULT_TENANT_ID, run.id, actor="user:asha")


def test_a_bill_whose_totals_do_not_reconcile_is_refused_by_name(books: Books) -> None:
    _mapped(books)
    good = books.approved_bill()
    bad = books.approved_bill(totals__grand_total="99,999.00")
    _profile(books)

    run = _marg(books)

    assert run.status == "needs_mapping" and run.tasks is not None
    named = {p.document_id for p in run.tasks.problems}
    assert named == {bad} and good not in named
    assert any("Grand total 99999.00" in p.problem for p in run.tasks.problems)


def test_the_mapped_csv_of_p1_is_not_held_to_the_import_pre_flight(books: Books) -> None:
    books.map_supplier(marg_party=PARTY)
    books.map_items(marg_item="X")
    books.approved_bill(totals__grand_total="99,999.00")

    run = books.export("marg")

    assert run.status == "ready" and run.format == "mapped_csv" and run.profile is None


# AC-P3-4: versions, and the file a run made


def test_an_edit_is_a_new_version_and_an_old_run_keeps_its_version_and_bytes(
    books: Books,
) -> None:
    _mapped(books)
    books.approved_bill()
    made = _profile(books)
    first = _marg(books)
    _, before = books.runs().file(DEFAULT_TENANT_ID, first.id, actor="user:asha")

    columns = [c.model_copy(update={"header": "Batch No"}) if c.header == "Batch" else c
               for c in sample_profile().columns]  # fmt: skip
    spec = sample_profile().model_copy(update={"columns": tuple(columns)})
    edited = books.runs().profiles.save_version(
        DEFAULT_TENANT_ID, made.id, spec=spec, basis="manual", **ASHA
    )
    second = _marg(books)

    assert edited.latest.version_no == 2 and edited.id == made.id
    again = books.runs().get(DEFAULT_TENANT_ID, first.id)
    assert again.profile is not None and again.profile.version_no == 1
    assert second.profile is not None and second.profile.version_no == 2
    _, after = books.runs().file(DEFAULT_TENANT_ID, first.id, actor="user:asha")
    assert after == before and again.sha256 == first.sha256
    assert "Batch No" in _rows(books, second)[0] and "Batch" in _rows(books, first)[0]
    versions = books.runs().profiles.versions(DEFAULT_TENANT_ID, made.id)
    assert [v.version_no for v in versions] == [2, 1]
    assert versions[1].spec == sample_profile()  # version 1 as it was saved


def test_the_version_a_run_used_makes_the_same_file_again(books: Books) -> None:
    _mapped(books)
    books.approved_bill()
    _profile(books)
    run = _marg(books)
    assert run.profile is not None
    _, stored = books.runs().file(DEFAULT_TENANT_ID, run.id, actor="user:asha")

    version = books.runs().profiles.version(DEFAULT_TENANT_ID, run.profile.id)
    again = _marg(books)  # the same bills, through the same version
    _, made_again = books.runs().file(DEFAULT_TENANT_ID, again.id, actor="user:asha")

    assert version.spec == sample_profile()
    assert made_again == stored


def test_a_profile_version_is_never_changed_in_the_database(books: Books) -> None:
    made = _profile(books)

    with pytest.raises(ProgrammingError), books.sessions.begin() as session:
        session.execute(
            update(ExportProfileVersion)
            .where(ExportProfileVersion.id == made.latest.id)
            .values(basis="upload")
        )


def test_one_profile_is_active_at_a_time(books: Books) -> None:
    first = _profile(books, name="First")
    second = _profile(books, name="Second")
    profiles = books.runs().profiles

    assert (first.active, second.active) == (True, False)  # the first is active by itself
    profiles.activate(DEFAULT_TENANT_ID, second.id, **ASHA)
    listed = profiles.list(DEFAULT_TENANT_ID)
    assert [(p.name, p.active) for p in listed] == [("Second", True), ("First", False)]
    assert profiles.active_version(DEFAULT_TENANT_ID).profile_id == second.id


# AC-P3-6: the re-export warning, audit and isolation, as for every export


def test_a_second_run_over_the_same_bills_warns_naming_the_first(books: Books) -> None:
    _mapped(books)
    books.approved_bill()
    books.approved_bill()
    _profile(books)
    first = _marg(books)

    second = _marg(books)

    assert first.overlaps == ()
    assert [(o.run_id, o.invoices, o.format) for o in second.overlaps] == [
        (first.id, 2, "marg_import")
    ]
    assert second.status == "ready"  # a warning, not a refusal


def test_a_run_that_made_no_file_is_not_counted_as_an_export(books: Books) -> None:
    _mapped(books)
    books.approved_bill(totals__grand_total="99,999.00")
    _profile(books)
    _marg(books)

    again = _marg(books)

    assert again.overlaps == ()


def test_every_profile_change_and_run_is_audited_without_names(books: Books) -> None:
    _mapped(books)
    books.approved_bill()
    made = _profile(books)
    books.runs().profiles.save_version(
        DEFAULT_TENANT_ID, made.id, spec=sample_profile(), basis="manual", **ASHA
    )
    _marg(books)

    found = {e.action: e.details for e in books.audit("export")}

    assert found["export_profile.created"] == {"target": "marg", "basis": "sample", "columns": 21}
    assert found["export_profile.activated"] == {"version": 1}
    assert found["export_profile.versioned"] == {"version": 2, "basis": "manual", "columns": 21}
    created = found["export.created"]
    assert (created["format"], created["profile_version"], created["status"]) == (
        "marg_import", 2, "ready"
    )  # fmt: skip
    text = str(list(found.values()))
    assert PARTY not in text and "AMOXICILLIN" not in text and "M-" not in text


def test_one_workspace_never_reads_or_uses_anothers_profile(
    site: Site, other_tenant: uuid.UUID
) -> None:
    mine = site.client()
    made = mine.post(PROFILES, json={"name": "Ours", "basis": "sample", "spec": _spec()})
    assert made.status_code == 201
    pid = made.json()["id"]

    theirs = site.client(tenant_id=other_tenant)

    assert theirs.get(PROFILES).json() == []
    assert theirs.get(f"{PROFILES}/{pid}").status_code == 404
    assert (
        theirs.put(f"{PROFILES}/{pid}", json={"basis": "manual", "spec": _spec()}).status_code
        == 404
    )
    assert theirs.post(f"{PROFILES}/{pid}/activate").status_code == 404
    assert theirs.get(f"{PROFILES}/{pid}/versions").status_code == 404
    body = {"target": "marg", "format": "marg_import", "from": str(_today()),
            "to": str(_today())}  # fmt: skip
    assert theirs.post(RUNS, json=body).json()["code"] == "no_active_profile"
    with pytest.raises(ProfileNotFound):
        site.books.runs().profiles.get(other_tenant, uuid.UUID(pid))


# Settings > Marg profile over HTTP


def test_a_profile_is_made_from_a_read_sample_edited_and_made_active(site: Site) -> None:
    client = site.client()
    sample = sample_profile()
    header_only = render(sample, [])  # the SAMPLE layout's header row
    read = client.post(f"{PROFILES}/read-sample", files={"file": ("ours.csv", header_only)})
    assert read.status_code == 200, read.text
    layout = read.json()
    assert layout["header_row"] is True and layout["file_type"] == "csv"
    assert [c["header"] for c in layout["columns"]] == [c.header for c in sample.columns]

    template = client.get(f"{PROFILES}/template").json()
    assert template["basis"] == "sample" and "SAMPLE" in template["name"]
    made = client.post(PROFILES, json={"name": "Ours", "basis": "upload",
                                       "spec": template["spec"]})  # fmt: skip
    assert made.status_code == 201 and made.json()["active"] is True
    assert made.json()["latest"]["sample"] is False
    pid = made.json()["id"]

    short = {**template["spec"], "columns": template["spec"]["columns"][:2]}
    bad = client.put(f"{PROFILES}/{pid}", json={"basis": "manual", "spec": short})
    assert bad.status_code == 422
    assert "must carry the item as the books name it" in bad.json()["detail"]
    good = client.put(f"{PROFILES}/{pid}", json={"basis": "manual", "spec": template["spec"],
                                                 "name": "Ours v2"})  # fmt: skip
    assert good.status_code == 200 and good.json()["latest"]["version_no"] == 2
    assert [v["version_no"] for v in client.get(f"{PROFILES}/{pid}/versions").json()] == [2, 1]
    taken = client.post(PROFILES, json={"name": "Ours v2", "basis": "manual",
                                        "spec": template["spec"]})  # fmt: skip
    assert taken.status_code == 409
    fields = client.get(f"{PROFILES}/fields").json()
    assert {"source": "line", "field": "free_qty", "kind": "int",
            "label": "Free (scheme) quantity", "per_line_only": True} in fields  # fmt: skip


def test_only_those_who_review_change_profiles_and_everyone_reads_them(site: Site) -> None:
    observer = site.client(role="observer")
    made = observer.post(PROFILES, json={"name": "X", "basis": "manual", "spec": _spec()})
    read = observer.post(f"{PROFILES}/read-sample", files={"file": ("a.csv", b"a\n1\n")})

    assert observer.get(PROFILES).status_code == 200
    assert made.status_code == 403 and read.status_code == 403


def test_a_sample_that_cannot_be_read_is_a_plain_422(site: Site) -> None:
    refused = site.client().post(
        f"{PROFILES}/read-sample", files={"file": ("a.xlsx", b"PK\x03\x04broken")}
    )

    assert refused.status_code == 422
    assert refused.json()["detail"] == "The file is not a workbook Excel can open."


def test_the_check_compares_the_profile_with_the_file_marg_accepted(site: Site) -> None:
    books = site.books
    _mapped(books)
    first = books.approved_bill()
    second = books.approved_bill()
    client = site.client()
    made = client.post(PROFILES, json={"name": "Ours", "basis": "sample", "spec": _spec()})
    pid = made.json()["id"]
    run = _marg(books)
    _, accepted = books.runs().file(DEFAULT_TENANT_ID, run.id, actor="user:asha")
    ids = f"{first},{second}"

    def check(data: bytes, document_ids: str = ids) -> Any:
        return client.post(
            f"{PROFILES}/{pid}/check", data={"document_ids": document_ids},
            files={"file": ("accepted.csv", data)},
        )  # fmt: skip

    exact = check(accepted)
    differs = check(accepted.replace(b"M-", b"MARG-", 1))
    unknown = check(accepted, str(uuid.uuid4()))

    assert exact.status_code == 200, exact.text
    assert exact.json()["exact"] is True and exact.json()["diffs"] == []
    assert exact.json()["cells"] == exact.json()["equal"] == 21 * (1 + 2 * len(PRODUCTS))
    result = differs.json()
    assert result["exact"] is False
    assert [(d["row"], d["column"]) for d in result["diffs"]] == [(2, "Item")]
    assert result["diffs"][0]["expected"].startswith("MARG-")
    assert unknown.status_code == 422 and "not approved invoices" in unknown.json()["detail"]
    with books.sessions() as session:
        checked = session.scalars(
            select(AuditEntry)
            .where(AuditEntry.action == "export_profile.checked")
            .order_by(AuditEntry.id)
        ).all()
    assert [e.details["equal"] == e.details["cells"] for e in checked] == [True, False]


def test_an_excel_profile_makes_an_excel_file(site: Site) -> None:
    books = site.books
    _mapped(books)
    books.approved_bill()
    client = site.client()
    client.post(PROFILES, json={"name": "Excel", "basis": "manual",
                                "spec": {**_spec(), "file_type": "xlsx"}})  # fmt: skip
    body = {"target": "marg", "format": "marg_import", "from": str(_today() - timedelta(days=1)),
            "to": str(_today())}  # fmt: skip

    run = client.post(RUNS, json=body).json()
    got = client.get(f"{RUNS}/{run['id']}/file")

    assert run["status"] == "ready" and run["profile"]["sample"] is False
    assert run["file"]["filename"].endswith(".xlsx")
    assert got.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert got.content.startswith(b"PK")


def _client_run(client: TestClient, target: str) -> Any:
    today = _today()
    body = {"target": target, "from": str(today - timedelta(days=1)), "to": str(today)}
    return client.post(RUNS, json=body).json()


def test_the_run_lists_its_format_profile_and_earlier_runs_over_http(site: Site) -> None:
    books = site.books
    books.map_supplier(marg_party=PARTY)
    books.map_items(marg_item="X")
    books.approved_bill()
    client = site.client()

    first = _client_run(client, "marg")
    second = _client_run(client, "marg")

    assert (first["format"], first["profile"], first["overlaps"]) == ("mapped_csv", None, [])
    assert [(o["run_id"], o["invoices"]) for o in second["overlaps"]] == [(first["id"], 1)]
    assert _client_run(client, "tally")["overlaps"] == []  # other books, no warning


def test_a_value_excel_cannot_hold_stops_an_excel_run_by_bill_and_line(books: Books) -> None:
    _mapped(books)
    books.numbers += 1
    bill = books.desk.receive("ff.pdf", invoice_no="NVM/26-27/90998")
    books.desk.correct(bill, "lines[1].batch_no", "B\x0112")
    books.desk.sign(bill, override_reason="checked on paper")
    xlsx = sample_profile().model_copy(update={"file_type": "xlsx"})
    _profile(books, spec=xlsx)

    run = _marg(books)

    assert run.status == "needs_mapping" and run.filename is None
    assert run.tasks is not None
    assert [(p.document_id, p.line) for p in run.tasks.problems] == [(bill, 2)]
    assert "the Batch value has a character" in run.tasks.problems[0].problem
