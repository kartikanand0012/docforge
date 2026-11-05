"""V2 P0 over HTTP: flag counts on the queue, near-expiry lines on a bill, the workspace's
summary of what was caught, and an invoice linked to its order by hand."""

import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from docforge.api.app import create_app
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.extraction.pipeline import INVOICE_SPEC
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
from docforge.review.service import ReviewService
from fakes import signed_in
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
REPO = Path(__file__).resolve().parents[2]
MISREAD_ORDER_NO = "PO-JPH-12OO1"


def _today() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


class Site:
    def __init__(self, sessions: SessionFactory, invoice: RawFromLabel, order: RawFromLabel):
        self.world = World(sessions, invoice, order, "pair_001")
        self.world.reprint_invoice("lines[0].expiry", "01/27")  # four months left
        self.world.reprint_invoice("po_no", MISREAD_ORDER_NO)
        self.world.build()
        self.review = ReviewService(
            sessions, self.world.store,
            {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC},
        )  # fmt: skip
        self.reviewer_id = self.review.add_reviewer(
            DEFAULT_TENANT_ID, name="Asha Rao", email="asha@example.com", pin="482913"
        )

    def app(self) -> FastAPI:
        return create_app(
            None, service=self.world.service, review=self.review,
            evals_dir=REPO / "evals" / "baselines", cors_origins=("http://localhost:3000",),
        )  # fmt: skip

    def as_reviewer(self) -> TestClient:
        return TestClient(signed_in(self.app(), reviewer_id=self.reviewer_id))

    def as_key(self, role: str = "admin", tenant_id: uuid.UUID | None = None) -> TestClient:
        return TestClient(signed_in(self.app(), role=role, tenant_id=tenant_id))


@pytest.fixture
def site(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> Site:
    return Site(sessions, raw_invoice_from_label, raw_order_from_label)


def test_the_queue_row_counts_its_flags(site: Site) -> None:
    site.world.process("invoice")

    (item,) = site.as_reviewer().get("/v1/review/queue").json()

    assert item["flags"] == {"duplicates": 0, "near_expiry_lines": 1, "schedule_h1_lines": 0}


def test_the_bill_lists_its_near_expiry_lines(site: Site) -> None:
    invoice_id = site.world.process("invoice")

    body = site.as_reviewer().get(f"/v1/documents/{invoice_id}/review").json()

    assert body["near_expiry"] == [
        {
            "path": "lines[0]",
            "product_name": "Amoxicillin Capsules IP 250mg",
            "batch_no": "XGX944068",
            "expiry": "2027-01",
            "months_left": 4,
        }
    ]
    assert body["near_expiry_months"] == 6


def test_the_summary_counts_the_last_30_days_by_default(site: Site) -> None:
    site.world.process("invoice")

    response = site.as_key(role="reader").get("/v1/flags/summary")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body == {
        "from": (_today() - timedelta(days=29)).isoformat(),
        "to": _today().isoformat(),
        "invoices": 1,
        "duplicates": 0,
        "near_expiry_lines": 1,
        "near_expiry_bills": 1,
        "schedule_h1_lines": 0,
        "schedule_h1_bills": 0,
        "capped": False,
    }


def test_the_summary_takes_a_range_and_refuses_a_bad_one(site: Site) -> None:
    site.world.process("invoice")
    client = site.as_key()
    yesterday = (_today() - timedelta(days=1)).isoformat()

    before = client.get("/v1/flags/summary", params={"from": "2026-01-01", "to": yesterday})
    backwards = client.get("/v1/flags/summary", params={"from": "2026-10-10", "to": "2026-10-01"})
    too_long = client.get("/v1/flags/summary", params={"from": "2024-01-01", "to": "2026-01-01"})

    assert before.json()["invoices"] == 0
    for refused in (backwards, too_long):
        assert refused.status_code == 422
        assert refused.json()["detail"].startswith("That date range cannot be counted")


def test_the_summary_of_another_workspace_counts_nothing_of_this_one(
    site: Site, other_tenant: uuid.UUID
) -> None:
    site.world.process("invoice")

    body = site.as_key(tenant_id=other_tenant).get("/v1/flags/summary").json()

    assert (body["invoices"], body["near_expiry_lines"]) == (0, 0)


def test_an_invoice_is_linked_to_an_order_by_hand(site: Site) -> None:
    order_id = site.world.process("purchase_order")
    invoice_id = site.world.process("invoice")
    client = site.as_reviewer()

    offered = client.get(f"/v1/documents/{invoice_id}/order-candidates").json()
    linked = client.post(
        f"/v1/documents/{invoice_id}/order-link",
        json={"order_document_id": str(order_id), "reason": "number misprinted"},
    )

    assert [(c["document_id"], c["same_supplier"]) for c in offered] == [(str(order_id), True)]
    assert linked.status_code == 200
    assert linked.json()["counterpart_document_id"] == str(order_id)
    trail = client.get(f"/v1/documents/{invoice_id}/audit").json()
    assert "match.linked" in [entry["action"] for entry in trail]


def test_a_link_is_refused_in_plain_words(site: Site) -> None:
    order_id = site.world.process("purchase_order")
    invoice_id = site.world.process("invoice")
    client = site.as_reviewer()

    no_reason = client.post(
        f"/v1/documents/{invoice_id}/order-link",
        json={"order_document_id": str(order_id), "reason": "   "},
    )
    backwards = client.post(
        f"/v1/documents/{order_id}/order-link",
        json={"order_document_id": str(invoice_id), "reason": "same"},
    )
    unknown = client.post(
        f"/v1/documents/{invoice_id}/order-link",
        json={"order_document_id": str(uuid.uuid4()), "reason": "same"},
    )
    by_key = site.as_key().post(
        f"/v1/documents/{invoice_id}/order-link",
        json={"order_document_id": str(order_id), "reason": "same"},
    )

    assert (no_reason.status_code, no_reason.json()["detail"]) == (
        422,
        "Say why this is the right order before linking it.",
    )
    assert backwards.status_code == 409
    assert backwards.json()["detail"].startswith("This cannot be linked: only an invoice")
    assert unknown.status_code == 404
    assert by_key.status_code == 403
