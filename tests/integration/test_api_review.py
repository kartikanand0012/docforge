"""The review API: what the review screen calls."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docforge.api.app import create_app
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.extraction.pipeline import INVOICE_SPEC
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
from docforge.review.service import ReviewService
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
REPO = Path(__file__).resolve().parents[2]
CREDENTIALS = {"email": "asha@example.com", "pin": "482913"}


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
def api(world: World, sessions: SessionFactory) -> TestClient:
    review = ReviewService(
        sessions, world.store, {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC}
    )
    review.add_reviewer(DEFAULT_TENANT_ID, name="Asha Rao", **CREDENTIALS)
    app = create_app(
        None,
        service=world.service,
        review=review,
        evals_dir=REPO / "evals" / "baselines",
        cors_origins=("http://localhost:3000",),
    )
    return TestClient(app)


def test_the_queue_lists_documents_waiting_for_a_person(world: World, api: TestClient) -> None:
    invoice_id = world.process("invoice")

    response = api.get("/v1/review/queue")

    assert response.status_code == 200
    (item,) = response.json()
    assert item["document_id"] == str(invoice_id)
    assert item["match_status"] == "no_counterpart"
    assert item["reasons"]


def test_the_review_detail_has_fields_boxes_and_what_blocks_approval(
    world: World, api: TestClient
) -> None:
    invoice_id = world.process("invoice")

    body = api.get(f"/v1/documents/{invoice_id}/review").json()

    assert body["decision"] == "review"
    assert body["blockers"] == ["there is no purchase order on file to compare it with"]
    field = next(f for f in body["assessment"]["fields"] if f["path"] == "invoice_no")
    assert field["boxes"][0]["page"] == 1
    assert "invoice_no" in body["editable_paths"]
    assert body["record"]["invoice_no"]["raw"] == world.invoice_raw["invoice_no"]["text"]
    assert body["page_count"] == 1
    (page,) = body["pages"]
    assert page["number"] == 1
    assert (page["width"], page["height"]) == pytest.approx((841.89, 595.28), abs=0.01)
    assert body["review"] is None


def test_a_correction_with_a_wrong_pin_is_refused_without_saying_why(
    world: World, api: TestClient
) -> None:
    invoice_id = world.process("invoice")
    payload = {"path": "invoice_no", "text": "X", "reason": "r", "email": "asha@example.com"}

    wrong = api.post(f"/v1/documents/{invoice_id}/corrections", json={**payload, "pin": "000000"})
    unknown = api.post(
        f"/v1/documents/{invoice_id}/corrections",
        json={**payload, "email": "x@example.com", "pin": "482913"},
    )

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    for _ in range(4):
        api.post(f"/v1/documents/{invoice_id}/corrections", json={**payload, "pin": "000000"})
    locked = api.post(f"/v1/documents/{invoice_id}/corrections", json={**payload, "pin": "482913"})
    # A locked account answers like a wrong PIN, so the reply does not say the email exists.
    assert locked.status_code == 401
    assert locked.json() == wrong.json()


def test_a_correction_returns_the_reassessed_record(world: World, api: TestClient) -> None:
    world.reprint_invoice("lines[0].qty", "25")
    world.process("purchase_order")
    invoice_id = world.process("invoice")

    response = api.post(
        f"/v1/documents/{invoice_id}/corrections",
        json={"path": "lines[0].qty", "text": "20", "reason": "paper copy", **CREDENTIALS},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "accept"
    assert body["corrections"][0]["new_text"] == "20"
    assert "482913" not in response.text


def test_a_bad_path_is_a_validation_error(world: World, api: TestClient) -> None:
    invoice_id = world.process("invoice")

    response = api.post(
        f"/v1/documents/{invoice_id}/corrections",
        json={"path": "lines[0].colour", "text": "red", "reason": "r", **CREDENTIALS},
    )

    assert response.status_code == 422


def test_signing_blocked_then_overridden_produces_the_draft_once(
    world: World, api: TestClient
) -> None:
    invoice_id = world.process("invoice")
    seen = api.get(f"/v1/documents/{invoice_id}/review").json()["record_sha256"]
    payload = {
        "expected_record_sha256": seen,
        "outcome": "approved",
        "meaning": "I approve this invoice for payment",
        "reason": "checked",
        **CREDENTIALS,
    }

    blocked = api.post(f"/v1/documents/{invoice_id}/review", json=payload)
    signed = api.post(
        f"/v1/documents/{invoice_id}/review",
        json={**payload, "override_reason": "order placed by phone"},
    )
    again = api.post(
        f"/v1/documents/{invoice_id}/review",
        json={**payload, "override_reason": "order placed by phone"},
    )

    assert blocked.status_code == 409 and "override" in blocked.json()["detail"]
    assert signed.status_code == 201
    assert signed.json()["draft"]["type"] == "payment_approval_draft"
    assert again.status_code == 409
    detail = api.get(f"/v1/documents/{invoice_id}/review").json()
    assert detail["signature_valid"] is True
    assert detail["review"]["record_sha256"] == signed.json()["record_sha256"]


def test_an_unknown_outcome_is_a_validation_error(world: World, api: TestClient) -> None:
    invoice_id = world.process("invoice")

    response = api.post(
        f"/v1/documents/{invoice_id}/review",
        json={
            "outcome": "maybe",
            "meaning": "m",
            "reason": "r",
            "expected_record_sha256": "0" * 64,
            **CREDENTIALS,
        },
    )

    assert response.status_code == 422


def test_a_page_image_is_served_and_a_missing_page_is_not_found(
    world: World, api: TestClient
) -> None:
    invoice_id = world.process("invoice")

    page = api.get(f"/v1/documents/{invoice_id}/pages/1")
    missing = api.get(f"/v1/documents/{invoice_id}/pages/2")

    assert page.status_code == 200
    assert page.headers["content-type"] == "image/png"
    assert page.content.startswith(b"\x89PNG")
    assert missing.status_code == 404


def test_an_unknown_document_is_not_found(api: TestClient) -> None:
    unknown = "00000000-0000-0000-0000-000000000001"

    assert api.get(f"/v1/documents/{unknown}/review").status_code == 404
    assert api.get(f"/v1/documents/{unknown}/pages/1").status_code == 404


def test_the_evals_endpoint_summarises_the_committed_reports(api: TestClient) -> None:
    body = api.get("/v1/evals").json()

    extraction = json.loads((REPO / "evals" / "baselines" / "invoice.json").read_text())
    assert body["extraction"]["fields_correct"] == extraction["summary"]["fields"]["correct"]
    assert body["extraction"]["fields_total"] == extraction["summary"]["fields"]["total"]
    assert body["trust"]["seeded_cases_caught"] == 9
    assert set(body["scans"]) == {"clean", "scan_good", "scan_poor"}
    assert body["scans"]["scan_poor"]["silent_errors_after_order_match"] == 0
    assert body["multipage"]["documents"] == 3
    assert body["cost"]["per_document_usd"] is None  # no prices configured: none invented


def test_the_web_app_origin_is_allowed_and_others_are_not(api: TestClient) -> None:
    allowed = api.options(
        "/v1/review/queue",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
    )
    other = api.options(
        "/v1/review/queue",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )

    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert "access-control-allow-origin" not in other.headers


def test_signing_a_record_that_changed_since_it_was_shown_is_a_conflict(
    world: World, api: TestClient
) -> None:
    invoice_id = world.process("invoice")

    response = api.post(
        f"/v1/documents/{invoice_id}/review",
        json={
            "expected_record_sha256": "0" * 64,
            "outcome": "rejected",
            "meaning": "I reject this invoice",
            "reason": "duplicate",
            **CREDENTIALS,
        },
    )

    assert response.status_code == 409
    assert "changed" in response.json()["detail"]


def test_an_internal_lookup_error_is_not_reported_as_a_missing_document(
    world: World, api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    invoice_id = world.process("invoice")

    def broken(*args: object, **kwargs: object) -> None:
        raise KeyError("internal")

    monkeypatch.setattr(ReviewService, "detail", broken)
    response = TestClient(api.app, raise_server_exceptions=False).get(
        f"/v1/documents/{invoice_id}/review"
    )

    assert response.status_code == 500
