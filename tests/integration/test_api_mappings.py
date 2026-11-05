"""V2 P1 over HTTP: the mapping master under /v1/settings/mappings and export runs under
/v1/exports/runs - who may change them, the plain errors, and one workspace never reaching
another's mappings (AC-P1-5)."""

import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from test_mappings import LEDGER, PRODUCTS, SELLER, Books
from test_visible_flags import Desk

from docforge.api.app import create_app
from docforge.db.session import SessionFactory
from fakes import signed_in

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
REPO = Path(__file__).resolve().parents[2]
SUPPLIERS = "/v1/settings/mappings/suppliers"
ITEMS = "/v1/settings/mappings/items"
IMPORT = "/v1/settings/mappings/import"
SUGGESTIONS = "/v1/settings/mappings/suggestions"


def _today() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


class Site:
    def __init__(self, books: Books) -> None:
        self.books = books

    def client(self, role: str = "member", tenant_id: uuid.UUID | None = None) -> TestClient:
        desk = self.books.desk
        app = create_app(
            None, service=desk.last.service, review=desk.review,
            evals_dir=REPO / "evals" / "baselines", mappings=self.books.mappings,
            export_runs=self.books.runs(),
        )  # fmt: skip
        return TestClient(signed_in(app, role=role, tenant_id=tenant_id))

    def supplier(self, client: TestClient, **fields: Any) -> Any:
        body = {"seller_gstin": SELLER, "printed_name": "Navjivan Medical Agencies",
                "tally_ledger": LEDGER, **fields}  # fmt: skip
        return client.post(SUPPLIERS, json=body)

    def run(self, client: TestClient, target: str = "tally") -> Any:
        today = _today()
        body = {"target": target, "from": str(today - timedelta(days=1)), "to": str(today)}
        return client.post("/v1/exports/runs", json=body)


@pytest.fixture
def site(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> Site:
    return Site(Books(sessions, Desk(sessions, raw_invoice_from_label, raw_order_from_label)))


def test_a_supplier_mapping_is_made_listed_edited_and_removed(site: Site) -> None:
    api = site.client()

    made = site.supplier(api, purchase_ledger="Purchase 12%")
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["seller_gstin"] == SELLER and body["tally_ledger"] == LEDGER
    assert body["source"] == "typed" and body["created_by"] == "test caller"

    listed = api.get(SUPPLIERS).json()
    assert listed["total"] == 1 and [m["id"] for m in listed["items"]] == [body["id"]]

    edited = api.patch(f"{SUPPLIERS}/{body['id']}", json={"marg_party": "NAVJIVAN"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["marg_party"] == "NAVJIVAN"
    assert edited.json()["tally_ledger"] == LEDGER  # untouched

    assert api.delete(f"{SUPPLIERS}/{body['id']}").status_code == 204
    assert api.get(SUPPLIERS).json()["items"] == []
    assert api.delete(f"{SUPPLIERS}/{body['id']}").status_code == 404


def test_a_second_mapping_for_a_gstin_is_a_409_naming_the_first(site: Site) -> None:
    api = site.client()
    first = site.supplier(api).json()

    again = site.supplier(api, seller_gstin=SELLER.lower(), tally_ledger="Another")

    assert again.status_code == 409
    assert again.json()["existing_id"] == first["id"]
    assert LEDGER in again.json()["detail"] and SELLER in again.json()["detail"]


def test_an_item_mapping_is_made_and_a_bad_one_is_refused_in_words(site: Site) -> None:
    api = site.client()
    name, pack = PRODUCTS[0]

    made = api.post(ITEMS, json={"product_name": name, "pack": pack, "tally_item": "AMOXY 250"})
    assert made.status_code == 201, made.text
    assert made.json()["item_key"]

    bad = api.post(ITEMS, json={"product_name": "X", "preferred_hsn": "12", "tally_item": "X"})
    assert bad.status_code == 422
    assert "HSN" in bad.json()["detail"]
    nameless = api.post(ITEMS, json={"product_name": "Dolo 650"})
    assert nameless.status_code == 422
    assert "Tally stock item or the Marg item" in nameless.json()["detail"]


def test_only_those_who_review_may_change_mappings_and_everyone_may_read(site: Site) -> None:
    reader = site.client(role="reader")

    assert site.supplier(reader).status_code == 403
    imported = reader.post(IMPORT, files={"file": ("m.csv", b"name,type\nA,item\n")})
    assert imported.status_code == 403
    assert reader.get(SUPPLIERS).status_code == 200
    assert reader.get(ITEMS).status_code == 200
    for role in ("reviewer", "admin"):
        assert site.client(role=role).get(ITEMS).status_code == 200


def test_a_master_import_reports_bad_rows_by_line(site: Site) -> None:
    data = f"name,gstin,type\n{LEDGER},{SELLER},supplier\n,,item\nAMOXY,,item\n".encode()

    response = site.client().post(IMPORT, files={"file": ("masters.csv", data, "text/csv")})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "rows": 3, "imported": 2, "already_known": 0,
        "rejected": [{"line": 3, "reason": "The name is empty."}],
    }  # fmt: skip
    unreadable = site.client().post(IMPORT, files={"file": ("m.csv", b"ledger\nABC\n")})
    assert unreadable.status_code == 422
    assert "name and type" in unreadable.json()["detail"]


def test_an_export_run_stops_with_tasks_and_suggestions_until_each_is_confirmed(
    site: Site,
) -> None:
    api = site.client()
    api.post(IMPORT, files={"file": ("m.csv", f"name,gstin,type\n{LEDGER},{SELLER},supplier\n")})
    site.books.map_items()
    site.books.approved_bill()

    run = site.run(api)
    assert run.status_code == 201, run.text
    assert run.json()["status"] == "needs_mapping" and run.json()["file"] is None
    (task,) = run.json()["tasks"]["suppliers"]
    assert task["seller_gstin"] == SELLER
    run_id = run.json()["id"]
    assert api.get(f"/v1/exports/runs/{run_id}/file").status_code == 409

    suggested = api.get(SUGGESTIONS, params={"export_run_id": run_id})
    assert suggested.status_code == 200, suggested.text
    (supplier,) = suggested.json()["suppliers"]
    assert supplier["suggestions"][0] == {
        "name": LEDGER, "gstin": SELLER, "source": "master", "score": 1.0, "why": "same GSTIN",
    }  # fmt: skip
    assert api.get(SUPPLIERS).json()["items"] == []  # offered, not made

    confirmed = site.supplier(api, source="suggestion")
    assert confirmed.status_code == 201 and confirmed.json()["source"] == "suggestion"

    again = site.run(api)
    assert again.json()["status"] == "ready", again.text
    file = again.json()["file"]
    downloaded = api.get(f"/v1/exports/runs/{again.json()['id']}/file")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("text/csv")
    assert file["filename"] in downloaded.headers["content-disposition"]
    assert len(downloaded.content) == file["size_bytes"]
    listed = api.get("/v1/exports/runs").json()
    assert [r["id"] for r in listed] == [again.json()["id"], run_id]


def test_suggestions_for_a_document_list_what_it_still_needs(site: Site) -> None:
    api = site.client()
    site.books.map_items(PRODUCTS[:9])
    bill = site.books.desk.receive("bill.pdf")

    suggested = api.get(SUGGESTIONS, params={"document_id": str(bill)})

    assert suggested.status_code == 200, suggested.text
    assert [s["seller_gstin"] for s in suggested.json()["suppliers"]] == [SELLER]
    assert [i["product_name"] for i in suggested.json()["items"]] == [PRODUCTS[9][0]]
    assert api.get(SUGGESTIONS).status_code == 422


def test_an_export_range_too_long_is_a_plain_422(site: Site) -> None:
    body = {"target": "tally", "from": "2025-01-01", "to": "2026-06-30"}

    response = site.client().post("/v1/exports/runs", json=body)

    assert response.status_code == 422
    assert "366 days" in response.json()["detail"]


# AC-P1-5: one workspace never reaches another's mappings


def test_a_workspace_cannot_read_suggest_from_or_change_anothers_mappings(
    site: Site, other_tenant: uuid.UUID
) -> None:
    mine = site.client()
    made = site.supplier(mine).json()
    item = mine.post(ITEMS, json={"product_name": "Dolo 650 Tab", "tally_item": "DOLO"}).json()
    secret = b"name,type\nSECRET LEDGER NAME,supplier\nSECRET ITEM,item\n"
    mine.post(IMPORT, files={"file": ("m.csv", secret)})
    bill = site.books.desk.receive("bill.pdf")
    site.books.approved_bill()
    my_run = site.run(mine).json()

    theirs = site.client(tenant_id=other_tenant)

    assert theirs.get(SUPPLIERS).json() == {"items": [], "total": 0, "next_after": None}
    assert theirs.get(ITEMS).json()["items"] == []
    assert theirs.patch(f"{SUPPLIERS}/{made['id']}", json={"tally_ledger": "X"}).status_code == 404
    assert theirs.delete(f"{SUPPLIERS}/{made['id']}").status_code == 404
    assert theirs.patch(f"{ITEMS}/{item['id']}", json={"tally_item": "X"}).status_code == 404
    assert theirs.delete(f"{ITEMS}/{item['id']}").status_code == 404
    assert theirs.get(SUGGESTIONS, params={"document_id": str(bill)}).status_code == 404
    assert theirs.get(SUGGESTIONS, params={"export_run_id": my_run["id"]}).status_code == 404
    assert theirs.get(f"/v1/exports/runs/{my_run['id']}").status_code == 404
    assert theirs.get(f"/v1/exports/runs/{my_run['id']}/file").status_code == 404
    assert theirs.get("/v1/exports/runs").json() == []

    # Their own mapping for the same GSTIN is theirs alone, and their suggestions never draw
    # on our master names.
    assert site.supplier(theirs, tally_ledger="Their Ledger").status_code == 201
    tasks = site.books.mappings.tasks_for_items(
        other_tenant, [("SECRET ITEM", None, None)], target="tally"
    )
    offered = site.books.mappings.suggestions(other_tenant, tasks, target="tally")
    assert all(not task.suggestions for task in offered.items)
    # And ours are as we left them.
    assert mine.get(SUPPLIERS).json()["items"][0]["tally_ledger"] == LEDGER
