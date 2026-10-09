"""Review claims: while one reviewer has a document open, nobody else changes it.

A claim is a lease of a few minutes, renewed while the reviewer's screen is open. Another
reviewer's live claim blocks corrections, signing and reading the document again; an expired
one blocks nothing, and claims are optional, so a caller that never claims works as before.
"""

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry
from docforge.db.session import SessionFactory
from docforge.documents import DocumentNotFound
from docforge.extraction.pipeline import INVOICE_SPEC
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
from docforge.review.service import (
    CLAIM_FOR,
    AlreadySigned,
    ClaimedByOther,
    RecordChanged,
    ReviewService,
    TakeOverRefused,
)
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
ASHA, ASHA_PIN = "asha@example.com", "482913"
RAVI, RAVI_PIN = "ravi@example.com", "736251"
MEERA, MEERA_PIN = "meera@example.com", "915372"
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
    return ReviewService(
        sessions, world.store, {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC}
    )


@pytest.fixture
def people(review: ReviewService) -> dict[str, uuid.UUID]:
    return {
        "asha": review.add_reviewer(DEFAULT_TENANT_ID, name="Asha Rao", email=ASHA, pin=ASHA_PIN),
        "ravi": review.add_reviewer(DEFAULT_TENANT_ID, name="Ravi Iyer", email=RAVI, pin=RAVI_PIN),
        "meera": review.add_reviewer(
            DEFAULT_TENANT_ID, name="Meera Shah", email=MEERA, pin=MEERA_PIN, role="admin"
        ),
    }


def expire(sessions: SessionFactory) -> None:
    with sessions.begin() as session:
        session.execute(text("UPDATE review_claims SET expires_at = now() - interval '1 second'"))


def actions(sessions: SessionFactory) -> list[tuple[str, dict[str, Any]]]:
    with sessions() as session:
        return [
            (entry.action, entry.details)
            for entry in session.scalars(
                select(AuditEntry).where(AuditEntry.action.like("review.%")).order_by(AuditEntry.id)
            )
        ]


def correct(review: ReviewService, document_id: uuid.UUID, email: str, pin: str, **kw: Any) -> Any:
    return review.correct(
        DEFAULT_TENANT_ID,
        document_id,
        path="invoice_no",
        text=kw.get("text", "INV-1"),
        reason="checked against the paper copy",
        email=email,
        pin=pin,
        expected_record_sha256=kw.get("seen"),
    )


def sign(review: ReviewService, document_id: uuid.UUID, email: str, pin: str) -> Any:
    seen = review.detail(DEFAULT_TENANT_ID, document_id).record_sha256
    return review.sign(
        DEFAULT_TENANT_ID,
        document_id,
        outcome="approved",
        meaning=MEANING,
        reason="checked",
        override_reason="ordered by phone",
        expected_record_sha256=seen,
        email=email,
        pin=pin,
    )


def test_a_first_claim_is_the_callers_for_a_few_minutes_and_is_audited(
    world: World, review: ReviewService, people: dict[str, uuid.UUID], sessions: SessionFactory
) -> None:
    invoice_id = world.process("invoice")

    claim = review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])

    assert claim.reviewer_name == "Asha Rao" and claim.mine
    assert claim.expires_at - claim.claimed_at == CLAIM_FOR
    assert actions(sessions) == [("review.claimed", {"version_no": 1})]


def test_claiming_again_renews_without_a_new_start_or_audit_entry(
    world: World, review: ReviewService, people: dict[str, uuid.UUID], sessions: SessionFactory
) -> None:
    invoice_id = world.process("invoice")
    first = review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])

    renewed = review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])

    assert renewed.claimed_at == first.claimed_at
    assert renewed.expires_at > first.expires_at
    assert [action for action, _ in actions(sessions)] == ["review.claimed"]


def test_someone_elses_live_claim_is_shown_and_left_alone(
    world: World, review: ReviewService, people: dict[str, uuid.UUID], sessions: SessionFactory
) -> None:
    invoice_id = world.process("invoice")
    held = review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])

    seen = review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["ravi"])

    assert (seen.reviewer_name, seen.mine) == ("Asha Rao", False)
    assert (seen.claimed_at, seen.expires_at) == (held.claimed_at, held.expires_at)
    assert len(actions(sessions)) == 1


def test_an_expired_claim_is_ignored_and_can_be_claimed_by_another(
    world: World, review: ReviewService, people: dict[str, uuid.UUID], sessions: SessionFactory
) -> None:
    invoice_id = world.process("invoice")
    review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])
    expire(sessions)

    assert review.detail(DEFAULT_TENANT_ID, invoice_id, viewer=people["ravi"]).claim is None
    taken = review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["ravi"])

    assert (taken.reviewer_name, taken.mine) == ("Ravi Iyer", True)
    assert [action for action, _ in actions(sessions)] == ["review.claimed", "review.claimed"]


def test_an_administrator_can_take_over_and_it_is_audited(
    world: World, review: ReviewService, people: dict[str, uuid.UUID], sessions: SessionFactory
) -> None:
    invoice_id = world.process("invoice")
    review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])

    taken = review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["meera"], take_over=True)

    assert (taken.reviewer_name, taken.mine) == ("Meera Shah", True)
    assert actions(sessions)[-1] == ("review.taken_over", {"from_reviewer": "Asha Rao"})
    # The one taken from now sees it as someone else's.
    assert not review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"]).mine


def test_a_reviewer_who_is_not_an_administrator_cannot_take_over(
    world: World, review: ReviewService, people: dict[str, uuid.UUID], sessions: SessionFactory
) -> None:
    invoice_id = world.process("invoice")
    review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])

    with pytest.raises(TakeOverRefused):
        review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["ravi"], take_over=True)

    detail = review.detail(DEFAULT_TENANT_ID, invoice_id, viewer=people["ravi"])
    assert detail.claim is not None and detail.claim.reviewer_name == "Asha Rao"
    assert [action for action, _ in actions(sessions)] == ["review.claimed"]


def test_taking_over_ones_own_or_a_free_document_is_an_ordinary_claim(
    world: World, review: ReviewService, people: dict[str, uuid.UUID]
) -> None:
    invoice_id = world.process("invoice")

    claim = review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["ravi"], take_over=True)

    assert claim.mine


def test_releasing_removes_only_the_callers_own_claim(
    world: World, review: ReviewService, people: dict[str, uuid.UUID]
) -> None:
    invoice_id = world.process("invoice")
    review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])

    review.release(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["ravi"])
    still = review.detail(DEFAULT_TENANT_ID, invoice_id, viewer=people["ravi"]).claim
    review.release(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])
    gone = review.detail(DEFAULT_TENANT_ID, invoice_id, viewer=people["ravi"]).claim

    assert still is not None and still.reviewer_name == "Asha Rao"
    assert gone is None


def test_a_signed_version_cannot_be_claimed(
    world: World, review: ReviewService, people: dict[str, uuid.UUID]
) -> None:
    invoice_id = world.process("invoice")
    sign(review, invoice_id, ASHA, ASHA_PIN)

    with pytest.raises(AlreadySigned):
        review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["ravi"])


def test_an_unknown_document_cannot_be_claimed_or_released(
    review: ReviewService, people: dict[str, uuid.UUID]
) -> None:
    unknown = uuid.uuid4()

    with pytest.raises(DocumentNotFound):
        review.claim(DEFAULT_TENANT_ID, unknown, reviewer_id=people["asha"])
    with pytest.raises(DocumentNotFound):
        review.release(DEFAULT_TENANT_ID, unknown, reviewer_id=people["asha"])


def test_another_reviewers_live_claim_blocks_corrections_and_signing(
    world: World, review: ReviewService, people: dict[str, uuid.UUID]
) -> None:
    invoice_id = world.process("invoice")
    review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])

    with pytest.raises(ClaimedByOther) as corrected:
        correct(review, invoice_id, RAVI, RAVI_PIN)
    with pytest.raises(ClaimedByOther):
        sign(review, invoice_id, RAVI, RAVI_PIN)

    assert corrected.value.name == "Asha Rao"
    assert review.detail(DEFAULT_TENANT_ID, invoice_id).corrections == ()
    assert review.detail(DEFAULT_TENANT_ID, invoice_id).review is None


def test_ones_own_claim_or_an_expired_one_does_not_block(
    world: World, review: ReviewService, people: dict[str, uuid.UUID], sessions: SessionFactory
) -> None:
    invoice_id = world.process("invoice")
    review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])

    correct(review, invoice_id, ASHA, ASHA_PIN)
    expire(sessions)
    correct(review, invoice_id, RAVI, RAVI_PIN, text="INV-2")

    assert len(review.detail(DEFAULT_TENANT_ID, invoice_id).corrections) == 2


def test_without_any_claim_nothing_is_blocked(
    world: World, review: ReviewService, people: dict[str, uuid.UUID]
) -> None:
    invoice_id = world.process("invoice")

    correct(review, invoice_id, RAVI, RAVI_PIN)
    signed = sign(review, invoice_id, RAVI, RAVI_PIN)

    assert signed.outcome == "approved"


def test_signing_releases_the_signers_claim(
    world: World, review: ReviewService, people: dict[str, uuid.UUID], engine: Engine
) -> None:
    invoice_id = world.process("invoice")
    review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])

    sign(review, invoice_id, ASHA, ASHA_PIN)

    assert count_claims(engine, DEFAULT_TENANT_ID) == 0


def test_another_reviewers_live_claim_blocks_reading_the_document_again(
    world: World, review: ReviewService, people: dict[str, uuid.UUID], sessions: SessionFactory
) -> None:
    invoice_id = world.process("invoice")
    assert world.service is not None
    review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])

    with pytest.raises(ClaimedByOther):
        world.service.reprocess(
            tenant_id=DEFAULT_TENANT_ID,
            document_id=invoice_id,
            actor=f"reviewer:{people['meera']}",
            reviewer_id=people["meera"],
        )
    # A system (an API key) has no claim of its own: any live claim blocks it.
    with pytest.raises(ClaimedByOther):
        world.service.reprocess(tenant_id=DEFAULT_TENANT_ID, document_id=invoice_id, actor="key:x")
    # The holder may.
    version = world.service.reprocess(
        tenant_id=DEFAULT_TENANT_ID,
        document_id=invoice_id,
        actor=f"reviewer:{people['asha']}",
        reviewer_id=people["asha"],
    )

    assert version.version_no == 2


def test_an_expired_claim_does_not_block_reading_again(
    world: World, review: ReviewService, people: dict[str, uuid.UUID], sessions: SessionFactory
) -> None:
    invoice_id = world.process("invoice")
    assert world.service is not None
    review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])
    expire(sessions)

    version = world.service.reprocess(
        tenant_id=DEFAULT_TENANT_ID, document_id=invoice_id, actor="key:x"
    )

    assert version.version_no == 2


def test_the_review_detail_shows_a_live_claim_relative_to_the_viewer(
    world: World, review: ReviewService, people: dict[str, uuid.UUID]
) -> None:
    invoice_id = world.process("invoice")
    assert review.detail(DEFAULT_TENANT_ID, invoice_id, viewer=people["asha"]).claim is None
    review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])

    mine = review.detail(DEFAULT_TENANT_ID, invoice_id, viewer=people["asha"]).claim
    theirs = review.detail(DEFAULT_TENANT_ID, invoice_id, viewer=people["ravi"]).claim
    system = review.detail(DEFAULT_TENANT_ID, invoice_id).claim

    assert mine is not None and mine.mine
    assert theirs is not None and not theirs.mine and theirs.reviewer_name == "Asha Rao"
    assert system is not None and not system.mine


def test_the_queue_names_who_else_is_reviewing_each_document(
    world: World, review: ReviewService, people: dict[str, uuid.UUID], sessions: SessionFactory
) -> None:
    invoice_id = world.process("invoice")  # no order: it waits in the queue
    review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])

    (theirs,) = review.queue(DEFAULT_TENANT_ID, viewer=people["ravi"])
    (mine,) = review.queue(DEFAULT_TENANT_ID, viewer=people["asha"])
    expire(sessions)
    (expired,) = review.queue(DEFAULT_TENANT_ID, viewer=people["ravi"])

    assert theirs.claimed_by == "Asha Rao"
    assert mine.claimed_by is None
    assert expired.claimed_by is None


def test_a_correction_to_a_record_that_changed_since_it_was_seen_is_refused(
    world: World, review: ReviewService, people: dict[str, uuid.UUID]
) -> None:
    invoice_id = world.process("invoice")
    seen = review.detail(DEFAULT_TENANT_ID, invoice_id).record_sha256
    correct(review, invoice_id, RAVI, RAVI_PIN, seen=seen)  # what was seen: accepted

    with pytest.raises(RecordChanged):
        correct(review, invoice_id, ASHA, ASHA_PIN, text="INV-9", seen=seen)

    assert len(review.detail(DEFAULT_TENANT_ID, invoice_id).corrections) == 1


def count_claims(engine: Engine, tenant: uuid.UUID) -> int:
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('docforge.tenant_id', :t, true)"), {"t": str(tenant)})
        return int(conn.execute(text("SELECT count(*) FROM review_claims")).scalar_one())


def test_another_organisation_neither_sees_nor_touches_a_claim(
    world: World,
    review: ReviewService,
    people: dict[str, uuid.UUID],
    engine: Engine,
    other_tenant: uuid.UUID,
) -> None:
    invoice_id = world.process("invoice")
    review.claim(DEFAULT_TENANT_ID, invoice_id, reviewer_id=people["asha"])
    outsider = review.add_reviewer(other_tenant, name="Out Sider", email=ASHA, pin=ASHA_PIN)

    with pytest.raises(DocumentNotFound):
        review.claim(other_tenant, invoice_id, reviewer_id=outsider, take_over=True)
    with pytest.raises(DocumentNotFound):
        review.release(other_tenant, invoice_id, reviewer_id=outsider)

    assert count_claims(engine, other_tenant) == 0
    assert count_claims(engine, DEFAULT_TENANT_ID) == 1


def test_the_lease_is_five_minutes() -> None:
    assert timedelta(minutes=5) == CLAIM_FOR
