"""The hash-chained audit log on a real database."""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from docforge import audit
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry
from docforge.db.session import SessionFactory

pytestmark = pytest.mark.integration


def append(sessions: SessionFactory, n: int, tenant_id: uuid.UUID = DEFAULT_TENANT_ID) -> None:
    for index in range(n):
        with sessions.begin() as session:
            audit.append(
                session,
                tenant_id=tenant_id,
                actor="test",
                action="thing.happened",
                target_type="document",
                target_id=f"doc-{index}",
                details={"n": index},
            )


def entries(sessions: SessionFactory, tenant_id: uuid.UUID = DEFAULT_TENANT_ID) -> list[AuditEntry]:
    with sessions() as session:
        query = select(AuditEntry).where(AuditEntry.tenant_id == tenant_id).order_by(AuditEntry.id)
        return list(session.scalars(query))


def tamper(engine: Engine, statement: str, **params: object) -> None:
    """Change the log the way someone with table-owner rights could."""
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
        conn.execute(text(statement), params)
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER USER"))


def test_each_entry_links_to_the_one_before(sessions: SessionFactory) -> None:
    append(sessions, 3)

    first, second, third = entries(sessions)

    assert first.prev_hash is None
    assert second.prev_hash == first.hash
    assert third.prev_hash == second.hash


def test_an_untouched_chain_verifies(sessions: SessionFactory) -> None:
    append(sessions, 5)

    with sessions() as session:
        report = audit.verify_chain(session, DEFAULT_TENANT_ID)

    assert (report.consistent, report.entries, report.first_bad_id) == (True, 5, None)


def test_an_empty_chain_verifies(sessions: SessionFactory) -> None:
    with sessions() as session:
        report = audit.verify_chain(session, DEFAULT_TENANT_ID)

    assert (report.consistent, report.entries) == (True, 0)


def test_each_tenant_has_its_own_chain(sessions: SessionFactory, other_tenant: uuid.UUID) -> None:
    append(sessions, 2)
    append(sessions, 2, other_tenant)
    append(sessions, 1)

    assert entries(sessions, other_tenant)[0].prev_hash is None
    mine = entries(sessions)
    assert mine[2].prev_hash == mine[1].hash
    with sessions() as session:
        assert audit.verify_chain(session, DEFAULT_TENANT_ID).consistent
        assert audit.verify_chain(session, other_tenant).consistent


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE audit_log SET details = '{\"n\": 99}'::jsonb WHERE target_id = 'doc-2'",
        "UPDATE audit_log SET actor = 'someone else' WHERE target_id = 'doc-2'",
        "UPDATE audit_log SET occurred_at = occurred_at + interval '1 second' "
        "WHERE target_id = 'doc-2'",
    ],
)
def test_an_edited_entry_is_detected(sessions: SessionFactory, engine: Engine, change: str) -> None:
    append(sessions, 5)
    edited = entries(sessions)[2]

    tamper(engine, change)

    with sessions() as session:
        report = audit.verify_chain(session, DEFAULT_TENANT_ID)
    assert not report.consistent
    assert report.first_bad_id == edited.id
    assert report.reason == "hash does not match the entry's contents"


def test_a_removed_entry_is_detected_at_the_next_one(
    sessions: SessionFactory, engine: Engine
) -> None:
    append(sessions, 5)
    after_removed = entries(sessions)[3]

    tamper(engine, "DELETE FROM audit_log WHERE target_id = 'doc-2'")

    with sessions() as session:
        report = audit.verify_chain(session, DEFAULT_TENANT_ID)
    assert not report.consistent
    assert report.first_bad_id == after_removed.id
    assert report.reason == "does not link to the previous entry"


def test_a_rewritten_entry_with_a_recomputed_hash_breaks_the_link_after_it(
    sessions: SessionFactory, engine: Engine
) -> None:
    append(sessions, 4)
    target, following = entries(sessions)[1:3]
    forged = audit.entry_hash(
        prev_hash=target.prev_hash,
        tenant_id=target.tenant_id,
        occurred_at=target.occurred_at,
        actor="forger",
        action=target.action,
        target_type=target.target_type,
        target_id=target.target_id,
        details=target.details,
    )

    tamper(
        engine,
        "UPDATE audit_log SET actor = 'forger', hash = :hash WHERE id = :id",
        hash=forged,
        id=target.id,
    )

    with sessions() as session:
        report = audit.verify_chain(session, DEFAULT_TENANT_ID)
    assert not report.consistent
    assert report.first_bad_id == following.id


def test_concurrent_appends_form_one_unbroken_chain(sessions: SessionFactory) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: append(sessions, 5), range(8)))

    chain = entries(sessions)
    assert len(chain) == 40
    assert len({entry.prev_hash for entry in chain}) == 40  # no two entries share a parent
    with sessions() as session:
        assert audit.verify_chain(session, DEFAULT_TENANT_ID).consistent


def test_an_append_that_is_rolled_back_leaves_no_gap_in_the_chain(
    sessions: SessionFactory,
) -> None:
    append(sessions, 1)
    session = sessions()
    audit.append(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor="test",
        action="never.happened",
        target_type="document",
        target_id="x",
        details={},
    )
    session.rollback()
    session.close()
    append(sessions, 1)

    assert len(entries(sessions)) == 2
    with sessions() as session:
        assert audit.verify_chain(session, DEFAULT_TENANT_ID).consistent


def test_the_database_refuses_a_second_entry_with_the_same_parent(
    sessions: SessionFactory, engine: Engine
) -> None:
    """A fork of the chain is impossible even for a writer that bypasses `audit.append`."""
    append(sessions, 2)
    first = entries(sessions)[0]

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_log (tenant_id, occurred_at, actor, action, target_type, "
                "target_id, details, prev_hash, hash) VALUES (:tenant, now(), 'x', 'fork', "
                "'document', 'd', '{}'::jsonb, :prev, :hash)"
            ),
            {"tenant": DEFAULT_TENANT_ID, "prev": first.hash, "hash": "f" * 64},
        )


def test_the_database_refuses_a_second_first_entry(
    sessions: SessionFactory, engine: Engine
) -> None:
    append(sessions, 1)

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_log (tenant_id, occurred_at, actor, action, target_type, "
                "target_id, details, prev_hash, hash) VALUES (:tenant, now(), 'x', 'genesis', "
                "'document', 'd', '{}'::jsonb, NULL, :hash)"
            ),
            {"tenant": DEFAULT_TENANT_ID, "hash": "e" * 64},
        )


def test_timestamps_never_run_backwards_along_the_chain(sessions: SessionFactory) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: append(sessions, 5), range(4)))

    times = [entry.occurred_at for entry in entries(sessions)]

    assert times == sorted(times)
