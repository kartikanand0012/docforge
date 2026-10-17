"""V2 3.4: stock with less than the workspace's window left when it is billed is flagged on
the line (`line.shelf_life`, a warning and never a reason for review). The window is a
workspace setting: 6 months unless changed, 0 for off, read when a bill is read and again on
every review read."""

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
from docforge.review.service import ReviewService
from docforge.workspace_settings import WorkspaceSettingsService
from fakes import signed_in
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
FOUR_MONTHS_LEFT = "01/27"  # pair_001 is billed on 2 September 2026


def shelf_life(rules: list[dict[str, Any]] | tuple[Any, ...]) -> list[tuple[str, str]]:
    """Each line.shelf_life result as (path, outcome)."""
    found = []
    for rule in rules:
        rule_id, paths, outcome = (
            (rule["rule_id"], rule["paths"], rule["outcome"])
            if isinstance(rule, dict)
            else (rule.rule_id, rule.paths, rule.outcome)
        )
        if rule_id == "line.shelf_life":
            found.append((paths[0], outcome))
    return found


def flagged(rules: list[dict[str, Any]] | tuple[Any, ...]) -> list[str]:
    return [path for path, outcome in shelf_life(rules) if outcome == "failed"]


class Bill:
    """pair_001's invoice with its first line four months from expiry."""

    def __init__(
        self, sessions: SessionFactory, invoice: RawFromLabel, order: RawFromLabel
    ) -> None:
        self.sessions = sessions
        self.world = World(sessions, invoice, order, "pair_001")
        self.world.reprint_invoice("lines[0].expiry", FOUR_MONTHS_LEFT)
        self.service = self.world.build()
        self.settings = WorkspaceSettingsService(sessions)
        self.review = ReviewService(
            sessions, self.world.store,
            {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC},
        )  # fmt: skip

    def read(self) -> uuid.UUID:
        return self.world.process_pdf("invoice", self.world.invoice_pdf, "bill.pdf")

    def stored(self, document_id: uuid.UUID) -> dict[str, Any]:
        """The assessment stored when the bill was read."""
        detail = self.service.assessment(DEFAULT_TENANT_ID, document_id)
        assert detail is not None
        data: dict[str, Any] = detail.record.data
        return data

    def window(self, months: int) -> None:
        self.settings.update(
            DEFAULT_TENANT_ID, near_expiry_months=months, actor="cli", name="Asha Rao"
        )


@pytest.fixture
def bill(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> Bill:
    return Bill(sessions, raw_invoice_from_label, raw_order_from_label)


def test_a_bill_read_flags_stock_with_less_than_six_months_left(bill: Bill) -> None:
    document = bill.read()

    stored = bill.stored(document)
    assert flagged(stored["rules"]) == ["lines[0].expiry"]
    # A flag, not a reason for review.
    assert not any("shelf_life" in reason for reason in stored["reasons"])
    detail = bill.review.detail(DEFAULT_TENANT_ID, document)
    assert flagged(detail.assessment.rules) == ["lines[0].expiry"]
    assert detail.near_expiry_months == 6
    assert not any("shelf_life" in blocker for blocker in detail.blockers)


def test_the_window_set_before_the_bill_is_read_is_the_one_it_is_read_with(bill: Bill) -> None:
    bill.window(3)

    document = bill.read()

    assert flagged(bill.stored(document)["rules"]) == []
    assert shelf_life(bill.stored(document)["rules"])  # still checked, and passed


def test_a_bill_in_review_follows_the_window_as_it_is_now(bill: Bill) -> None:
    document = bill.read()

    bill.window(3)
    detail = bill.review.detail(DEFAULT_TENANT_ID, document)
    assert flagged(detail.assessment.rules) == []
    assert detail.near_expiry_months == 3

    bill.window(13)  # lines 4 and 6 expire 2027-09, twelve months after the bill
    detail = bill.review.detail(DEFAULT_TENANT_ID, document)
    assert flagged(detail.assessment.rules) == [
        "lines[0].expiry",
        "lines[4].expiry",
        "lines[6].expiry",
    ]

    bill.window(0)
    detail = bill.review.detail(DEFAULT_TENANT_ID, document)
    assert shelf_life(detail.assessment.rules) == []
    assert detail.near_expiry_months == 0
    # The bill as read keeps the window it was read with.
    assert flagged(bill.stored(document)["rules"]) == ["lines[0].expiry"]


def test_another_workspaces_window_is_not_this_ones(
    bill: Bill, sessions: SessionFactory, other_tenant: uuid.UUID
) -> None:
    with tenant_scope(other_tenant):
        bill.settings.update(other_tenant, near_expiry_months=0, actor="cli", name="Ben")

    document = bill.read()

    assert flagged(bill.stored(document)["rules"]) == ["lines[0].expiry"]
    assert bill.settings.get(DEFAULT_TENANT_ID).near_expiry_months == 6
    assert bill.settings.get(other_tenant).near_expiry_months == 0


# --- the API -------------------------------------------------------------------------------


def client(bill: Bill, role: str = "admin") -> TestClient:
    app = create_app(None, service=bill.service, review=bill.review, settings=bill.settings)
    return TestClient(signed_in(app, role=role))


def test_the_settings_are_the_defaults_until_changed(bill: Bill) -> None:
    response = client(bill, "reviewer").get("/v1/settings")

    assert response.status_code == 200
    assert response.json() == {"near_expiry_months": 6, "updated_by": None, "updated_at": None}


@pytest.mark.parametrize("role", ["admin", "member"])
def test_an_administrator_or_a_personal_workspaces_person_changes_them(
    bill: Bill, sessions: SessionFactory, role: str
) -> None:
    response = client(bill, role).patch("/v1/settings", json={"near_expiry_months": 9})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["near_expiry_months"] == 9
    assert body["updated_by"] == "test caller"
    assert body["updated_at"] is not None
    assert client(bill, "reader").get("/v1/settings").json()["near_expiry_months"] == 9
    with sessions() as session:
        entry = session.scalars(
            select(AuditEntry).where(AuditEntry.action == "settings.changed")
        ).one()
    assert entry.target_type == "workspace"
    assert entry.target_id == str(DEFAULT_TENANT_ID)
    assert entry.details == {"setting": "near_expiry_months", "old": 6, "new": 9}

    again = client(bill, role).patch("/v1/settings", json={"near_expiry_months": 4})
    assert again.json()["near_expiry_months"] == 4
    with sessions() as session:
        details = [
            e.details
            for e in session.scalars(
                select(AuditEntry)
                .where(AuditEntry.action == "settings.changed")
                .order_by(AuditEntry.id)
            )
        ]
    assert details[-1] == {"setting": "near_expiry_months", "old": 9, "new": 4}


@pytest.mark.parametrize("role", ["reviewer", "integrator", "reader", "observer"])
def test_no_one_else_changes_them(bill: Bill, role: str) -> None:
    response = client(bill, role).patch("/v1/settings", json={"near_expiry_months": 9})

    assert response.status_code == 403
    assert bill.settings.get(DEFAULT_TENANT_ID).near_expiry_months == 6


REFUSED: list[dict[str, Any]] = [
    {"near_expiry_months": -1},
    {"near_expiry_months": 37},
    {"near_expiry_months": "6"},
    {"near_expiry_months": 6, "other": 1},
    {},
]


@pytest.mark.parametrize("body", REFUSED)
def test_a_window_outside_0_to_36_months_is_refused(bill: Bill, body: dict[str, Any]) -> None:
    response = client(bill).patch("/v1/settings", json=body)

    assert response.status_code == 422
    assert bill.settings.get(DEFAULT_TENANT_ID).updated_by is None


def test_the_review_response_says_which_window_its_checks_used(bill: Bill) -> None:
    document = bill.read()
    api = client(bill)

    assert api.get(f"/v1/documents/{document}/review").json()["near_expiry_months"] == 6
    api.patch("/v1/settings", json={"near_expiry_months": 0})
    review = api.get(f"/v1/documents/{document}/review").json()

    assert review["near_expiry_months"] == 0
    assert shelf_life(review["assessment"]["rules"]) == []
