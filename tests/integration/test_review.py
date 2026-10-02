"""Review by a person: the queue, corrections, and signed approval or rejection."""

import io
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry, Reviewer
from docforge.db.models import Correction as CorrectionRow
from docforge.db.session import SessionFactory
from docforge.extraction.pipeline import INVOICE_SPEC
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
from docforge.review.service import (
    AlreadySigned,
    ApprovalBlocked,
    NotAuthenticated,
    ReviewerLocked,
    ReviewService,
)
from docforge.review.signing import verify_pin
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
EMAIL, PIN = "asha@example.com", "482913"
MEANING = "I approve this invoice for payment"


@pytest.fixture
def world(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> World:
    return World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")


@pytest.fixture
def review(world: World, sessions: SessionFactory) -> ReviewService:
    service = ReviewService(
        sessions,
        world.store,
        {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC},
    )
    service.add_reviewer(DEFAULT_TENANT_ID, name="Asha Rao", email=EMAIL, pin=PIN)
    return service


def actions(sessions: SessionFactory) -> list[str]:
    with sessions() as session:
        return list(session.scalars(select(AuditEntry.action).order_by(AuditEntry.id)))


def correct(
    review: ReviewService, document_id: uuid.UUID, path: str, value: str | None, **kw: str
) -> Any:
    return review.correct(
        DEFAULT_TENANT_ID,
        document_id,
        path=path,
        text=value,
        reason=kw.get("reason", "checked against the paper copy"),
        email=kw.get("email", EMAIL),
        pin=kw.get("pin", PIN),
    )


def sign(
    review: ReviewService, document_id: uuid.UUID, outcome: str = "approved", **kw: Any
) -> Any:
    return review.sign(
        DEFAULT_TENANT_ID,
        document_id,
        outcome=outcome,
        meaning=kw.get("meaning", MEANING),
        reason=kw.get("reason", "matches the order"),
        override_reason=kw.get("override_reason"),
        email=kw.get("email", EMAIL),
        pin=kw.get("pin", PIN),
    )


def test_a_reviewer_is_stored_with_a_hashed_pin(
    review: ReviewService, sessions: SessionFactory
) -> None:
    with sessions() as session:
        reviewer = session.scalars(select(Reviewer)).one()

    assert (reviewer.name, reviewer.email) == ("Asha Rao", EMAIL)
    assert PIN not in reviewer.pin_hash and verify_pin(PIN, reviewer.pin_hash)
    with pytest.raises(ValueError, match="already"):
        review.add_reviewer(DEFAULT_TENANT_ID, name="Other", email=EMAIL.upper(), pin="111111")


def test_a_pin_must_be_six_digits_or_more(review: ReviewService) -> None:
    with pytest.raises(ValueError, match="PIN"):
        review.add_reviewer(DEFAULT_TENANT_ID, name="B", email="b@example.com", pin="1234")


def test_documents_that_were_accepted_are_not_in_the_queue(
    world: World, review: ReviewService
) -> None:
    world.process("purchase_order")
    world.process("invoice")  # matches its order and passes its checks

    assert review.queue(DEFAULT_TENANT_ID) == []


def test_an_invoice_with_no_order_waits_in_the_queue_with_its_reasons(
    world: World, review: ReviewService
) -> None:
    invoice_id = world.process("invoice")

    (item,) = review.queue(DEFAULT_TENANT_ID)

    assert item.document_id == invoice_id
    assert item.doc_type == "invoice"
    assert item.match_status == "no_counterpart"


def test_the_review_detail_shows_every_field_with_its_boxes_and_what_can_be_corrected(
    world: World, review: ReviewService
) -> None:
    invoice_id = world.process("invoice")

    detail = review.detail(DEFAULT_TENANT_ID, invoice_id)

    batch = next(f for f in detail.assessment.fields if f.path == "lines[0].batch_no")
    assert batch.status == "verified" and batch.boxes
    assert "lines[0].batch_no" in detail.editable_paths
    assert "place_of_supply_code" not in detail.editable_paths  # read from place_of_supply
    assert detail.corrections == ()
    assert detail.review is None
    assert detail.page_count == 1


def test_a_misread_value_is_corrected_and_the_checks_pass_again(
    world: World, review: ReviewService, sessions: SessionFactory
) -> None:
    world.reprint_invoice("lines[0].qty", "25")  # read as 25; printed 20
    world.process("purchase_order")
    invoice_id = world.process("invoice")
    before = review.detail(DEFAULT_TENANT_ID, invoice_id)

    after = correct(review, invoice_id, "lines[0].qty", "20")

    assert before.decision == "review"
    assert after.decision == "accept"
    assert after.match_status == "match"
    assert [(c.path, c.old_text, c.new_text) for c in after.corrections] == [
        ("lines[0].qty", "25", "20")
    ]
    assert after.corrections[0].reviewer_name == "Asha Rao"
    assert actions(sessions)[-1] == "review.corrected"


def test_the_audit_entry_for_a_correction_holds_no_values_or_reasons(
    world: World, review: ReviewService, sessions: SessionFactory
) -> None:
    invoice_id = world.process("invoice")
    correct(review, invoice_id, "invoice_no", "SECRET-VALUE", reason="private reason text")

    with sessions() as session:
        entry = session.scalars(select(AuditEntry).order_by(AuditEntry.id.desc())).first()

    assert entry is not None
    assert "SECRET-VALUE" not in str(entry.details) and "private reason" not in str(entry.details)
    assert entry.details["path"] == "invoice_no"


def test_a_wrong_pin_changes_nothing_and_five_lock_the_reviewer(
    world: World, review: ReviewService, sessions: SessionFactory
) -> None:
    invoice_id = world.process("invoice")
    before = actions(sessions)

    for _ in range(5):
        with pytest.raises(NotAuthenticated):
            correct(review, invoice_id, "invoice_no", "X", pin="000000")
    with pytest.raises(ReviewerLocked):
        correct(review, invoice_id, "invoice_no", "X")  # even the right PIN, for now

    assert actions(sessions) == before
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(CorrectionRow)) == 0


def test_an_unknown_reviewer_is_refused_like_a_wrong_pin(
    world: World, review: ReviewService
) -> None:
    invoice_id = world.process("invoice")

    with pytest.raises(NotAuthenticated):
        correct(review, invoice_id, "invoice_no", "X", email="nobody@example.com")


def test_a_correction_to_a_path_that_is_not_a_field_is_refused(
    world: World, review: ReviewService
) -> None:
    invoice_id = world.process("invoice")

    with pytest.raises(ValueError, match="not a field"):
        correct(review, invoice_id, "lines[0].colour", "red")


def test_approval_signs_the_record_and_produces_a_payment_approval_draft(
    world: World, review: ReviewService, sessions: SessionFactory
) -> None:
    world.process("purchase_order")
    invoice_id = world.process("invoice")

    signed = sign(review, invoice_id)

    assert signed.outcome == "approved"
    assert signed.reviewer_name == "Asha Rao"
    assert signed.meaning == MEANING
    assert len(signed.record_sha256) == 64
    assert signed.draft is not None and signed.draft["type"] == "payment_approval_draft"
    assert signed.draft["record_sha256"] == signed.record_sha256
    detail = review.detail(DEFAULT_TENANT_ID, invoice_id)
    assert detail.review is not None and detail.signature_valid
    assert actions(sessions)[-1] == "review.signed"
    assert invoice_id not in [item.document_id for item in review.queue(DEFAULT_TENANT_ID)]


def test_a_document_that_still_fails_its_checks_needs_an_override_reason_to_approve(
    world: World, review: ReviewService
) -> None:
    invoice_id = world.process("invoice")  # no order on file

    with pytest.raises(ApprovalBlocked, match="no purchase order"):
        sign(review, invoice_id)
    signed = sign(review, invoice_id, override_reason="order placed by phone; PO to follow")

    assert signed.override_reason == "order placed by phone; PO to follow"


def test_a_rejection_needs_only_a_reason_and_produces_no_draft(
    world: World, review: ReviewService
) -> None:
    invoice_id = world.process("invoice")

    signed = sign(
        review, invoice_id, outcome="rejected", meaning="I reject this invoice", reason="duplicate"
    )

    assert (signed.outcome, signed.draft) == ("rejected", None)


def test_a_signed_document_cannot_be_corrected_or_signed_again(
    world: World, review: ReviewService
) -> None:
    world.process("purchase_order")
    invoice_id = world.process("invoice")
    sign(review, invoice_id)

    with pytest.raises(AlreadySigned):
        correct(review, invoice_id, "invoice_no", "X")
    with pytest.raises(AlreadySigned):
        sign(review, invoice_id, outcome="rejected", reason="changed my mind")


def test_a_signature_no_longer_matches_if_the_signed_record_is_altered(
    world: World, review: ReviewService, engine: Engine
) -> None:
    """Reviews are append-only; this goes round that, as an owner of the database could."""
    world.process("purchase_order")
    invoice_id = world.process("invoice")
    sign(review, invoice_id)

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE reviews DISABLE TRIGGER reviews_append_only"))
        conn.execute(
            text(
                "UPDATE reviews SET data = "
                "jsonb_set(data, '{record,invoice_no,value}', '\"FORGED\"')"
            )
        )
        conn.execute(text("ALTER TABLE reviews ENABLE ALWAYS TRIGGER reviews_append_only"))

    assert review.detail(DEFAULT_TENANT_ID, invoice_id).signature_valid is False


@pytest.mark.parametrize("table", ["corrections", "reviews"])
def test_corrections_and_reviews_cannot_be_changed(
    world: World, review: ReviewService, engine: Engine, table: str
) -> None:
    world.process("purchase_order")
    invoice_id = world.process("invoice")
    correct(review, invoice_id, "invoice_no", world.invoice_raw["invoice_no"]["text"])
    sign(review, invoice_id)

    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608 - fixed names


def test_another_tenant_cannot_see_or_review_the_document(
    world: World, review: ReviewService, other_tenant: uuid.UUID
) -> None:
    invoice_id = world.process("invoice")
    review.add_reviewer(other_tenant, name="Eve", email="eve@example.com", pin="999999")

    assert review.queue(other_tenant) == []
    with pytest.raises(LookupError):
        review.detail(other_tenant, invoice_id)
    with pytest.raises(LookupError):
        review.correct(
            other_tenant,
            invoice_id,
            path="invoice_no",
            text="X",
            reason="r",
            email="eve@example.com",
            pin="999999",
        )


def test_a_page_of_the_original_is_served_as_an_image(world: World, review: ReviewService) -> None:
    invoice_id = world.process("invoice")

    png = review.page_image(DEFAULT_TENANT_ID, invoice_id, 1)

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(LookupError):
        review.page_image(DEFAULT_TENANT_ID, invoice_id, 2)


def test_the_command_line_adds_a_reviewer_reading_the_pin_from_standard_input(
    sessions: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    engine: Engine,
) -> None:
    from docforge.config import get_settings
    from docforge.review.__main__ import main

    monkeypatch.setenv("DATABASE_URL", engine.url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    monkeypatch.setattr("sys.stdin", io.StringIO("135790\n"))
    try:
        assert main(["add-reviewer", "--name", "Ravi", "--email", "Ravi@Example.com"]) == 0
        monkeypatch.setattr("sys.stdin", io.StringIO("12\n"))
        assert main(["add-reviewer", "--name", "R", "--email", "r2@example.com"]) == 1
    finally:
        get_settings.cache_clear()

    assert "Added reviewer" in capsys.readouterr().out
    with sessions() as session:
        stored = session.scalars(select(Reviewer).where(Reviewer.email == "ravi@example.com")).one()
    assert verify_pin("135790", stored.pin_hash)
