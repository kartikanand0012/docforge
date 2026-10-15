"""V2 1.1 part 5: what an invoice's signed QR said, on the review and assessment responses -
its outcome, who signed it, what it signed and which of its checks hold - never the QR itself."""

import json
from collections.abc import Callable
from decimal import Decimal
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
from einvoice_fakes import Scanner, altered, qr_for, scanned
from fakes import signed_in
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
    return World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")


@pytest.fixture
def api_for(world: World, sessions: SessionFactory) -> Callable[[Scanner | None], TestClient]:
    """The API over the world, its invoices' QRs found by `scanner` (None: checking is off)."""

    def make(scanner: Scanner | None) -> TestClient:
        if scanner is not None:
            scanned(world, scanner)
        world.build()
        review = ReviewService(
            sessions,
            world.store,
            {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC},
        )
        reviewer_id = review.add_reviewer(DEFAULT_TENANT_ID, name="Asha Rao", **CREDENTIALS)
        app = create_app(
            None,
            service=world.service,
            review=review,
            evals_dir=REPO / "evals" / "baselines",
            cors_origins=("http://localhost:3000",),
        )
        return TestClient(signed_in(app, reviewer_id=reviewer_id))

    return make


def invoice(world: World) -> dict[str, Any]:
    return json.loads(world.label_text)["invoice"]


def outcomes(einvoice: dict[str, Any]) -> dict[str, str]:
    return {rule["rule_id"]: rule["outcome"] for rule in einvoice["rules"]}


def test_a_verified_qr_shows_who_signed_it_what_it_signed_and_its_checks(
    world: World, api_for: Callable[[Scanner | None], TestClient]
) -> None:
    api = api_for(Scanner(qr_for(world)))
    invoice_id = world.process("invoice")

    einvoice = api.get(f"/v1/documents/{invoice_id}/review").json()["einvoice"]

    assert einvoice["status"] == "verified"
    assert (einvoice["irp"], einvoice["key_name"]) == ("TEST", "TEST-1")
    assert einvoice["key_confirmed"] is False
    assert (einvoice["page"], einvoice["altered_beside"]) == (1, False)
    expected = invoice(world)
    signed = einvoice["signed"]
    assert signed["seller_gstin"] == expected["seller"]["gstin"]
    assert signed["buyer_gstin"] == expected["buyer"]["gstin"]
    assert signed["invoice_no"] == expected["invoice_no"]
    assert signed["invoice_date"] == expected["invoice_date"]
    assert Decimal(signed["total"]) == Decimal(str(expected["totals"]["grand_total"]))
    assert signed["document_type"] == "INV"
    assert signed["line_count"] == len(expected["lines"])
    assert signed["irn"] == "b" * 64
    assert outcomes(einvoice)["einvoice.signature"] == "passed"
    assert outcomes(einvoice)["einvoice.invoice_no"] == "passed"
    assert all(rule["rule_id"].startswith("einvoice.") for rule in einvoice["rules"])
    assert {"rule_id", "outcome", "severity", "message"} <= set(einvoice["rules"][0])


def test_the_assessment_response_carries_the_same_check(
    world: World, api_for: Callable[[Scanner | None], TestClient]
) -> None:
    api = api_for(Scanner(qr_for(world)))
    invoice_id = world.process("invoice")

    einvoice = api.get(f"/v1/documents/{invoice_id}/assessment").json()["einvoice"]

    assert einvoice["status"] == "verified"
    assert einvoice["signed"]["invoice_no"] == invoice(world)["invoice_no"]
    assert outcomes(einvoice)["einvoice.signature"] == "passed"


def test_neither_response_ever_carries_the_qr_itself(
    world: World, api_for: Callable[[Scanner | None], TestClient]
) -> None:
    qr = qr_for(world)
    api = api_for(Scanner(qr))
    invoice_id = world.process("invoice")
    signature = qr.rsplit(".", 1)[1]

    for path in ("review", "assessment"):
        body = api.get(f"/v1/documents/{invoice_id}/{path}").text
        assert qr not in body
        assert signature not in body


def test_an_altered_qr_says_its_signature_failed_and_shows_nothing_it_claims(
    world: World, api_for: Callable[[Scanner | None], TestClient]
) -> None:
    api = api_for(Scanner(altered(qr_for(world))))
    invoice_id = world.process("invoice")

    body = api.get(f"/v1/documents/{invoice_id}/review").json()

    einvoice = body["einvoice"]
    assert einvoice["status"] == "bad_signature"
    assert einvoice["signed"] is None
    assert einvoice["reason"]
    assert outcomes(einvoice) == {"einvoice.signature": "failed"}
    assert body["decision"] == "review"


def test_a_copied_qr_shows_which_signed_field_disagrees(
    world: World, api_for: Callable[[Scanner | None], TestClient]
) -> None:
    api = api_for(Scanner(qr_for(world, DocNo="OTHER/1")))
    invoice_id = world.process("invoice")

    einvoice = api.get(f"/v1/documents/{invoice_id}/review").json()["einvoice"]

    assert einvoice["status"] == "verified"
    assert outcomes(einvoice)["einvoice.invoice_no"] == "failed"
    assert outcomes(einvoice)["einvoice.seller_gstin"] == "passed"


def test_its_checks_follow_a_reviewers_correction(
    world: World, api_for: Callable[[Scanner | None], TestClient]
) -> None:
    api = api_for(Scanner(qr_for(world, DocNo="OTHER/1")))
    invoice_id = world.process("invoice")

    response = api.post(
        f"/v1/documents/{invoice_id}/corrections",
        json={"path": "invoice_no", "text": "OTHER/1", "reason": "paper copy", **CREDENTIALS},
    )

    assert response.status_code == 200
    assert outcomes(response.json()["einvoice"])["einvoice.invoice_no"] == "passed"
    # The assessment response is the reading's own, as first read.
    stored = api.get(f"/v1/documents/{invoice_id}/assessment").json()["einvoice"]
    assert outcomes(stored)["einvoice.invoice_no"] == "failed"


def test_an_invoice_without_a_qr_says_none_was_found(
    world: World, api_for: Callable[[Scanner | None], TestClient]
) -> None:
    api = api_for(Scanner())
    invoice_id = world.process("invoice")

    einvoice = api.get(f"/v1/documents/{invoice_id}/review").json()["einvoice"]

    assert einvoice["status"] == "none"
    assert (einvoice["signed"], einvoice["irp"], einvoice["rules"]) == (None, None, [])


def test_a_scan_that_failed_says_so(
    world: World, api_for: Callable[[Scanner | None], TestClient]
) -> None:
    api = api_for(Scanner(error=RuntimeError("decoder crashed")))
    invoice_id = world.process("invoice")

    einvoice = api.get(f"/v1/documents/{invoice_id}/assessment").json()["einvoice"]

    assert einvoice["status"] == "unreadable"
    assert einvoice["rules"] == []


def test_with_checking_off_or_for_an_order_there_is_no_einvoice(
    world: World, api_for: Callable[[Scanner | None], TestClient]
) -> None:
    api = api_for(None)
    invoice_id = world.process("invoice")
    order_id = world.process("purchase_order")

    for document_id in (invoice_id, order_id):
        for path in ("review", "assessment"):
            body = api.get(f"/v1/documents/{document_id}/{path}").json()
            assert body["einvoice"] is None


def test_the_api_contract_describes_the_einvoice_field(
    api_for: Callable[[Scanner | None], TestClient],
) -> None:
    schema = api_for(None).get("/openapi.json").json()["components"]["schemas"]

    assert "einvoice" in schema["ReviewOut"]["properties"]
    assert "einvoice" in schema["AssessmentOut"]["properties"]
    assert set(schema["EinvoiceOut"]["properties"]["status"]["enum"]) == {
        "verified", "bad_signature", "malformed", "unknown_key", "none", "unreadable"
    }  # fmt: skip
    assert "qr_text" not in schema["EinvoiceOut"]["properties"]
