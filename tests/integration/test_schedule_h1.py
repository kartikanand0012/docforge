"""V2 3.6: the lines of a purchase bill a chemist must enter in the Schedule H1 register - a
molecule on the schedule named in the product, or a product the workspace marked as H1 -
shown on the bill and never holding it."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from docforge.api.app import create_app
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry
from docforge.db.session import SessionFactory
from docforge.db.tenancy import tenant_scope
from docforge.extraction.pipeline import INVOICE_SPEC
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
from docforge.h1_products import H1ProductService
from docforge.review.service import ReviewService
from fakes import signed_in
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
EMAIL, PIN = "asha@example.com", "482913"


class Bill:
    """pair_001's invoice (billed 2 September 2026) with three products of interest."""

    def __init__(
        self, sessions: SessionFactory, invoice: RawFromLabel, order: RawFromLabel
    ) -> None:
        self.sessions = sessions
        self.world = World(sessions, invoice, order, "pair_001")
        self.world.reprint_invoice("lines[0].product_name", "LEVOFLOXACIN 500 TAB")
        self.world.reprint_invoice("lines[1].product_name", "ALPRAX 0.5 TAB")
        self.world.reprint_invoice("lines[2].product_name", "Pregabalin 75 Cap")
        self.service = self.world.build()
        self.products = H1ProductService(sessions)
        self.review = ReviewService(
            sessions, self.world.store,
            {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC},
        )  # fmt: skip

    def read(self) -> uuid.UUID:
        return self.world.process_pdf("invoice", self.world.invoice_pdf, "bill.pdf")

    def h1(self, document_id: uuid.UUID) -> list[tuple[str, str | None, bool]]:
        """(path, molecule, marked) of each H1 line."""
        detail = self.review.detail(DEFAULT_TENANT_ID, document_id)
        return [(h.path, h.molecule, h.product_id is not None) for h in detail.schedule_h1]

    def mark(self, name: str, tenant: uuid.UUID = DEFAULT_TENANT_ID) -> uuid.UUID:
        product, _ = self.products.mark(tenant, name, actor="cli", name="Asha Rao")
        return product.id


@pytest.fixture
def bill(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> Bill:
    return Bill(sessions, raw_invoice_from_label, raw_order_from_label)


def test_a_line_naming_a_scheduled_molecule_is_h1(bill: Bill) -> None:
    document = bill.read()

    detail = bill.review.detail(DEFAULT_TENANT_ID, document)

    first = detail.schedule_h1[0]
    assert (first.path, first.product_name, first.molecule) == (
        "lines[0]",
        "LEVOFLOXACIN 500 TAB",
        "Levofloxacin",
    )
    assert first.notification == "G.S.R. 588(E), 30 August 2013"
    assert first.product_id is None
    assert first.joins_on is None
    # A brand names no molecule: not H1 until the workspace says so.
    assert "lines[1]" not in [h.path for h in detail.schedule_h1]
    # Shown, never a reason to hold the bill.
    assert not any("H1" in blocker or "Schedule" in blocker for blocker in detail.blockers)


def test_a_molecule_not_yet_in_force_on_the_bills_date_says_when_it_joins(bill: Bill) -> None:
    document = bill.read()

    lines = bill.review.detail(DEFAULT_TENANT_ID, document).schedule_h1
    pregabalin = next(h for h in lines if h.path == "lines[2]")

    assert pregabalin.molecule == "Pregabalin"
    assert str(pregabalin.joins_on) == "2026-11-16"


def test_a_product_the_workspace_marked_is_h1_on_every_bill_after(bill: Bill) -> None:
    document = bill.read()
    assert bill.h1(document) == [
        ("lines[0]", "Levofloxacin", False),
        ("lines[2]", "Pregabalin", False),
    ]

    product = bill.mark("Alprax 0.5 tab.")  # another case, spacing and full stop

    assert bill.h1(document) == [
        ("lines[0]", "Levofloxacin", False),
        ("lines[1]", None, True),
        ("lines[2]", "Pregabalin", False),
    ]
    lines = bill.review.detail(DEFAULT_TENANT_ID, document).schedule_h1
    assert next(h for h in lines if h.path == "lines[1]").product_id == product

    bill.products.unmark(DEFAULT_TENANT_ID, product, actor="cli")
    assert [path for path, _, _ in bill.h1(document)] == ["lines[0]", "lines[2]"]


def test_a_marked_product_that_also_names_a_molecule_says_both(bill: Bill) -> None:
    document = bill.read()

    bill.mark("levofloxacin 500 tab")

    assert bill.h1(document)[0] == ("lines[0]", "Levofloxacin", True)


def test_another_workspaces_marks_are_never_seen(bill: Bill, other_tenant: uuid.UUID) -> None:
    with tenant_scope(other_tenant):
        bill.mark("ALPRAX 0.5 TAB", other_tenant)

    document = bill.read()

    assert "lines[1]" not in [path for path, _, _ in bill.h1(document)]
    assert bill.products.list(DEFAULT_TENANT_ID) == []
    with tenant_scope(other_tenant):
        assert [p.product_name for p in bill.products.list(other_tenant)] == ["ALPRAX 0.5 TAB"]


def test_a_correction_of_the_product_name_is_followed(bill: Bill) -> None:
    document = bill.read()
    bill.review.add_reviewer(DEFAULT_TENANT_ID, name="Asha Rao", email=EMAIL, pin=PIN)

    bill.review.correct(
        DEFAULT_TENANT_ID, document, path="lines[1].product_name", text="Tramadol 50 Cap",
        reason="the paper copy", email=EMAIL, pin=PIN,
    )  # fmt: skip

    assert ("lines[1]", "Tramadol", False) in bill.h1(document)


# --- the API -------------------------------------------------------------------------------


def client(bill: Bill, role: str = "admin") -> TestClient:
    app = create_app(None, service=bill.service, review=bill.review, h1_products=bill.products)
    return TestClient(signed_in(app, role=role))


@pytest.mark.parametrize("role", ["admin", "reviewer", "member"])
def test_a_reviewer_marks_and_unmarks_a_product(
    bill: Bill, sessions: SessionFactory, role: str
) -> None:
    api = client(bill, role)

    made = api.post("/v1/schedule-h1/products", json={"product_name": "  ALPRAX   0.5 TAB "})
    again = api.post("/v1/schedule-h1/products", json={"product_name": "alprax 0.5 tab"})

    assert made.status_code == 201, made.text
    assert made.json()["product_name"] == "ALPRAX 0.5 TAB"
    assert made.json()["marked_by"] == "test caller"
    assert again.status_code == 200
    assert again.json()["id"] == made.json()["id"]
    listed = client(bill, "reader").get("/v1/schedule-h1/products").json()
    assert [p["product_name"] for p in listed] == ["ALPRAX 0.5 TAB"]

    gone = api.delete(f"/v1/schedule-h1/products/{made.json()['id']}")
    assert gone.status_code == 204
    assert api.get("/v1/schedule-h1/products").json() == []
    assert api.delete(f"/v1/schedule-h1/products/{made.json()['id']}").status_code == 404

    with sessions() as session:
        entries = session.scalars(
            select(AuditEntry)
            .where(AuditEntry.action.in_(("h1_product.marked", "h1_product.unmarked")))
            .order_by(AuditEntry.id)
        ).all()
    assert [(e.action, e.details) for e in entries] == [
        ("h1_product.marked", {"product_name": "ALPRAX 0.5 TAB"}),
        ("h1_product.unmarked", {"product_name": "ALPRAX 0.5 TAB"}),
    ]


@pytest.mark.parametrize("role", ["reader", "integrator", "observer"])
def test_no_one_who_does_not_review_marks_or_unmarks(bill: Bill, role: str) -> None:
    product = bill.mark("ALPRAX 0.5 TAB")
    api = client(bill, role)

    assert api.post("/v1/schedule-h1/products", json={"product_name": "X 1"}).status_code == 403
    assert api.delete(f"/v1/schedule-h1/products/{product}").status_code == 403
    assert len(bill.products.list(DEFAULT_TENANT_ID)) == 1


REFUSED: list[dict[str, Any]] = [
    {"product_name": "..."},
    {"product_name": ""},
    {"product_name": "x" * 301},
    {},
]


@pytest.mark.parametrize("body", REFUSED)
def test_a_product_name_with_nothing_to_compare_is_refused(
    bill: Bill, body: dict[str, Any]
) -> None:
    response = client(bill).post("/v1/schedule-h1/products", json=body)

    assert response.status_code == 422
    assert bill.products.list(DEFAULT_TENANT_ID) == []


def test_the_review_response_lists_the_h1_lines(bill: Bill) -> None:
    document = bill.read()
    api = client(bill)
    api.post("/v1/schedule-h1/products", json={"product_name": "ALPRAX 0.5 TAB"})

    lines = api.get(f"/v1/documents/{document}/review").json()["schedule_h1"]

    found = [(h["path"], h["molecule"], h["product_id"] is not None, h["joins_on"]) for h in lines]
    assert found == [
        ("lines[0]", "Levofloxacin", False, None),
        ("lines[1]", None, True, None),
        ("lines[2]", "Pregabalin", False, "2026-11-16"),
    ]
