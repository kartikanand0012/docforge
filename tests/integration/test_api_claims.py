"""The review-claim API: what the web app calls while a reviewer has a document open."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
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
ASHA = {"email": "asha@example.com", "pin": "482913"}
RAVI = {"email": "ravi@example.com", "pin": "736251"}
Client = Callable[..., TestClient]


@pytest.fixture
def world(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> World:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    world.build()
    return world


@pytest.fixture
def review(world: World, sessions: SessionFactory) -> ReviewService:
    return ReviewService(
        sessions, world.store, {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC}
    )


@pytest.fixture
def people(review: ReviewService) -> dict[str, uuid.UUID]:
    return {
        "asha": review.add_reviewer(
            DEFAULT_TENANT_ID, name="Asha Rao", email=ASHA["email"], pin=ASHA["pin"]
        ),
        "ravi": review.add_reviewer(
            DEFAULT_TENANT_ID, name="Ravi Iyer", email=RAVI["email"], pin=RAVI["pin"]
        ),
        "meera": review.add_reviewer(
            DEFAULT_TENANT_ID,
            name="Meera Shah",
            email="meera@example.com",
            pin="915372",
            role="admin",
        ),
    }


@pytest.fixture
def client(world: World, review: ReviewService) -> Client:
    def make(reviewer_id: uuid.UUID | None = None, role: str = "admin") -> TestClient:
        app = create_app(None, service=world.service, review=review)
        return TestClient(signed_in(app, reviewer_id=reviewer_id, role=role))

    return make


def test_a_reviewer_claims_renews_and_releases_a_document(
    world: World, client: Client, people: dict[str, uuid.UUID]
) -> None:
    invoice_id = world.process("invoice")
    asha = client(people["asha"])

    first = asha.post(f"/v1/documents/{invoice_id}/claim", json={})
    renewed = asha.post(f"/v1/documents/{invoice_id}/claim", json={"take_over": False})
    shown = asha.get(f"/v1/documents/{invoice_id}/review").json()["claim"]
    released = asha.delete(f"/v1/documents/{invoice_id}/claim")

    assert first.status_code == renewed.status_code == 200
    body = first.json()
    assert set(body) == {"reviewer_name", "claimed_at", "expires_at", "mine"}
    assert (body["reviewer_name"], body["mine"]) == ("Asha Rao", True)
    assert renewed.json()["claimed_at"] == body["claimed_at"]
    assert shown["mine"] is True
    assert released.status_code == 204
    assert asha.get(f"/v1/documents/{invoice_id}/review").json()["claim"] is None


def test_another_reviewer_sees_the_claim_and_is_refused_changes(
    world: World, client: Client, people: dict[str, uuid.UUID]
) -> None:
    invoice_id = world.process("invoice")
    asha, ravi = client(people["asha"]), client(people["ravi"])
    asha.post(f"/v1/documents/{invoice_id}/claim", json={})

    polled = ravi.post(f"/v1/documents/{invoice_id}/claim", json={})
    queue = ravi.get("/v1/review/queue").json()
    corrected = ravi.post(
        f"/v1/documents/{invoice_id}/corrections",
        json={"path": "invoice_no", "text": "X", "reason": "r", **RAVI},
    )
    released = ravi.delete(f"/v1/documents/{invoice_id}/claim")
    take_over = ravi.post(f"/v1/documents/{invoice_id}/claim", json={"take_over": True})

    assert polled.status_code == 200
    assert (polled.json()["reviewer_name"], polled.json()["mine"]) == ("Asha Rao", False)
    assert queue[0]["claimed_by"] == "Asha Rao"
    assert asha.get("/v1/review/queue").json()[0]["claimed_by"] is None
    assert corrected.status_code == 409
    assert corrected.json() == {
        "detail": "Asha Rao is reviewing this document. Wait until they finish, "
        "or ask an administrator to take over."
    }
    assert released.status_code == 204  # nothing happens to someone else's claim
    assert ravi.get(f"/v1/documents/{invoice_id}/review").json()["claim"]["mine"] is False
    assert take_over.status_code == 403
    assert take_over.json() == {"detail": "Only an administrator can take over a review."}


def test_an_administrator_takes_over(
    world: World, client: Client, people: dict[str, uuid.UUID]
) -> None:
    invoice_id = world.process("invoice")
    client(people["asha"]).post(f"/v1/documents/{invoice_id}/claim", json={})

    taken = client(people["meera"]).post(
        f"/v1/documents/{invoice_id}/claim", json={"take_over": True}
    )

    assert taken.status_code == 200
    assert (taken.json()["reviewer_name"], taken.json()["mine"]) == ("Meera Shah", True)


def test_signing_while_another_holds_the_claim_is_a_conflict_and_a_signed_one_cannot_be_claimed(
    world: World, client: Client, people: dict[str, uuid.UUID]
) -> None:
    invoice_id = world.process("invoice")
    asha, ravi = client(people["asha"]), client(people["ravi"])
    seen = asha.get(f"/v1/documents/{invoice_id}/review").json()["record_sha256"]
    payload = {
        "expected_record_sha256": seen,
        "outcome": "rejected",
        "meaning": "I reject this invoice",
        "reason": "duplicate",
    }
    asha.post(f"/v1/documents/{invoice_id}/claim", json={})

    refused = ravi.post(f"/v1/documents/{invoice_id}/review", json={**payload, **RAVI})
    signed = asha.post(f"/v1/documents/{invoice_id}/review", json={**payload, **ASHA})
    after = ravi.post(f"/v1/documents/{invoice_id}/claim", json={})

    assert refused.status_code == 409 and "Asha Rao is reviewing" in refused.json()["detail"]
    assert signed.status_code == 201
    assert after.status_code == 409
    assert after.json() == {"detail": "This version is already signed and can no longer change."}


def test_reading_again_is_refused_while_another_reviews(
    world: World, client: Client, people: dict[str, uuid.UUID]
) -> None:
    invoice_id = world.process("invoice")
    client(people["asha"]).post(f"/v1/documents/{invoice_id}/claim", json={})

    by_key = client().post(f"/v1/documents/{invoice_id}/reprocess")

    assert by_key.status_code == 409
    assert "Asha Rao is reviewing" in by_key.json()["detail"]


def test_a_stale_correction_is_a_conflict(
    world: World, client: Client, people: dict[str, uuid.UUID]
) -> None:
    invoice_id = world.process("invoice")
    asha = client(people["asha"])

    response = asha.post(
        f"/v1/documents/{invoice_id}/corrections",
        json={
            "path": "invoice_no",
            "text": "X",
            "reason": "r",
            "expected_record_sha256": "0" * 64,
            **ASHA,
        },
    )
    bad = asha.post(
        f"/v1/documents/{invoice_id}/corrections",
        json={
            "path": "invoice_no",
            "text": "X",
            "reason": "r",
            "expected_record_sha256": "x",
            **ASHA,
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "The record changed after you opened it. Look at it again before correcting."
    }
    assert bad.status_code == 422


def test_only_a_signed_in_reviewer_can_claim(world: World, client: Client) -> None:
    invoice_id = world.process("invoice")
    key = client()

    assert key.post(f"/v1/documents/{invoice_id}/claim", json={}).status_code == 403
    assert key.delete(f"/v1/documents/{invoice_id}/claim").status_code == 403


def test_an_unknown_document_cannot_be_claimed(
    client: Client, people: dict[str, uuid.UUID]
) -> None:
    unknown = "00000000-0000-0000-0000-000000000001"
    asha = client(people["asha"])

    assert asha.post(f"/v1/documents/{unknown}/claim", json={}).status_code == 404
    assert asha.delete(f"/v1/documents/{unknown}/claim").status_code == 404
