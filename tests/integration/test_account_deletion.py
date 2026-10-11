"""Deleting one's own account: a self-service account's person, after their password again,
erases their whole workspace - every row in every table and every stored file - and nothing
of anyone else's. Afterwards the email is free and the session is useless."""

import logging
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from test_member_isolation import Two

from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.review.service import ReviewService
from full_stack import PASSWORD, FullStack
from worlds import World

pytestmark = [pytest.mark.integration, pytest.mark.no_ambient_tenant]

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
PIN = "482913"
WRONG = "The email or password is not right."


@pytest.fixture
def two(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
    owner_engine: Engine,
) -> Two:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    world.build()
    two = Two(FullStack(sessions, world), owner_engine)
    # More of Asha's: a claim, a correction and a signature, and files beside the originals.
    doc = f"/v1/documents/{two.doc}"
    assert two.api.post(f"{doc}/claim", headers=two.asha, json={}).status_code in (200, 201)
    corrected = two.api.post(
        f"{doc}/corrections", headers=two.asha,
        json={"path": "invoice_no", "text": "INV-9", "reason": "paper copy",
              "email": "asha@example.com", "pin": PASSWORD},
    )  # fmt: skip
    assert corrected.status_code in (200, 201), corrected.text
    seen = two.api.get(f"{doc}/review", headers=two.asha).json()["record_sha256"]
    signed = two.api.post(
        f"{doc}/review", headers=two.asha,
        json={"outcome": "rejected", "meaning": "I reject this invoice", "reason": "wrong",
              "expected_record_sha256": seen, "email": "asha@example.com", "pin": PASSWORD},
    )  # fmt: skip
    assert signed.status_code in (200, 201), signed.text
    # A claim on her other document, which stays open (signing ends the first one's).
    listed = two.api.get("/v1/documents", headers=two.asha).json()["items"]
    other = next(d["id"] for d in listed if d["id"] != str(two.doc))
    claimed = two.api.post(f"/v1/documents/{other}/claim", headers=two.asha, json={})
    assert claimed.status_code in (200, 201), claimed.text
    store = world.store
    store.put(f"renditions/{two.asha_ws}/{'a' * 64}.pdf", b"%PDF-", "application/pdf")
    store.put(f"audit-anchors/{two.asha_ws}/latest.json", b"{}", "application/json")
    # And Ben has his own, which must all stay.
    two.ben_doc = two.stack.read(two.ben_ws, "invoice")  # type: ignore[attr-defined]
    return two


def delete(two: Two, who: dict[str, str], password: str = PASSWORD) -> Any:
    return two.api.request(
        "DELETE", "/v1/accounts/current", headers=who, json={"password": password}
    )


def tenant_tables(owner_engine: Engine) -> list[str]:
    with owner_engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'tenant_id' ORDER BY 1"
                )
            ).scalars()
        )


def rows_of(owner_engine: Engine, tenant: uuid.UUID) -> dict[str, int]:
    """Every row that belongs to `tenant`, per table, and its workspace row."""
    counts: dict[str, int] = {}
    with owner_engine.connect() as conn:
        for table in tenant_tables(owner_engine):
            counts[table] = conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"),  # noqa: S608
                {"t": tenant},
            ).scalar_one()
        counts["tenants"] = conn.execute(
            text("SELECT count(*) FROM tenants WHERE id = :t"), {"t": tenant}
        ).scalar_one()
        counts["rate_windows"] = conn.execute(
            text("SELECT count(*) FROM rate_windows WHERE strpos(key, :t) > 0"), {"t": str(tenant)}
        ).scalar_one()
    return counts


def keys_of(two: Two, tenant: uuid.UUID) -> list[str]:
    stored = two.stack.world.store.keys()  # a store's keys, not a dict's
    return [key for key in stored if str(tenant) in key]


def test_deleting_an_account_erases_its_whole_workspace_and_nothing_else(
    two: Two, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    before = rows_of(owner_engine, two.asha_ws)
    # Something in every kind of thing an account's person can make.
    for table in ("documents", "document_versions", "extractions", "parse_outputs", "chunks",
                  "conversations", "messages", "collections", "collection_documents",
                  "review_claims", "corrections", "reviews", "sessions", "audit_log",
                  "accounts", "reviewers", "assessments", "model_runs", "tenants"):  # fmt: skip
        assert before[table] > 0, table
    assert len(keys_of(two, two.asha_ws)) >= 4
    bens_rows, bens_keys = rows_of(owner_engine, two.ben_ws), keys_of(two, two.ben_ws)

    with caplog.at_level(logging.INFO, logger="docforge.auth"):
        response = delete(two, two.asha)

    assert response.status_code == 204, response.text
    after = rows_of(owner_engine, two.asha_ws)
    assert {table: n for table, n in after.items() if n} == {}
    assert keys_of(two, two.asha_ws) == []
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM erasures")).scalar_one() == 0
    # Ben's workspace, and the organisation's, are as they were.
    assert rows_of(owner_engine, two.ben_ws) == bens_rows
    assert keys_of(two, two.ben_ws) == bens_keys
    assert two.api.get("/v1/documents", headers=two.ben).json()["items"]
    with owner_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM tenants WHERE id = :t"), {"t": DEFAULT_TENANT_ID}
            ).scalar_one()
            == 1
        )
    # The deletion is logged with the workspace's id and nothing that names the person.
    logged = [r.getMessage() for r in caplog.records if "account.deleted" in r.getMessage()]
    assert logged == [f"account.deleted tenant={two.asha_ws}"]


def test_after_deleting_the_session_is_useless_and_the_email_is_free(two: Two) -> None:
    assert delete(two, two.asha).status_code == 204

    assert two.api.get("/v1/sessions/current", headers=two.asha).status_code == 401
    assert two.api.get("/v1/documents", headers=two.asha).status_code == 401
    signed_in = two.api.post(
        "/v1/sessions", json={"email": "asha@example.com", "password": PASSWORD}
    )
    assert signed_in.status_code == 401
    again = two.stack.sign_up("Asha", "asha@example.com")
    assert two.api.get("/v1/documents", headers=again).json()["items"] == []


def test_a_wrong_password_deletes_nothing_and_counts_as_a_failed_sign_in(
    two: Two, owner_engine: Engine
) -> None:
    before = rows_of(owner_engine, two.asha_ws)

    for _ in range(5):
        wrong = delete(two, two.asha, "not my password at all")
        assert (wrong.status_code, wrong.json()["detail"]) == (401, WRONG)

    # Locked for this address now, as after five wrong passwords at sign-in.
    assert delete(two, two.asha).status_code == 401
    assert rows_of(owner_engine, two.asha_ws)["documents"] == before["documents"]
    assert rows_of(owner_engine, two.asha_ws)["tenants"] == 1
    with owner_engine.connect() as conn:
        failed = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE tenant_id = :t "
                 "AND action = 'reviewer.pin_failed'"), {"t": two.asha_ws},
        ).scalar_one()  # fmt: skip
    assert failed == 5


def test_too_many_failures_from_an_address_are_refused_with_429(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
    owner_engine: Engine,
) -> None:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    world.build()
    stack = FullStack(sessions, world)
    asha = stack.sign_up("Asha", "asha@example.com")
    for _ in range(1000):
        stack.auth.limiter.failed("testclient")

    response = stack.client.request(
        "DELETE", "/v1/accounts/current", headers=asha, json={"password": PASSWORD}
    )

    assert response.status_code == 429
    assert FullStack.workspace_of(owner_engine, "asha@example.com")


def test_only_an_accounts_own_person_may_delete_and_only_a_personal_workspace(
    two: Two, owner_engine: Engine
) -> None:
    review = ReviewService(two.stack.sessions, two.stack.world.store, {})
    review.add_reviewer(DEFAULT_TENANT_ID, name="Org admin", email="oa@example.com", pin=PIN,
                        role="admin")  # fmt: skip
    review.add_reviewer(DEFAULT_TENANT_ID, name="Org reviewer", email="or@example.com", pin=PIN)
    admin = two.stack.sign_in("oa@example.com", PIN, "default", field="pin")
    reviewer = two.stack.sign_in("or@example.com", PIN, "default", field="pin")
    key = {"Authorization": f"Bearer {two.stack.key(DEFAULT_TENANT_ID, 'admin')}"}

    for who, secret in ((admin, PIN), (reviewer, PIN), (key, PIN)):
        assert delete(two, who, secret).status_code == 403
    # Nor into someone else's workspace, as a platform administrator looking in.
    looking = {**two.asha, "X-DocForge-Workspace": str(two.ben_ws)}
    assert delete(two, looking).status_code == 403
    assert delete(two, {}).status_code == 401
    assert rows_of(owner_engine, DEFAULT_TENANT_ID)["tenants"] == 1


def test_the_database_deletes_only_a_personal_workspace_and_only_in_scope(
    two: Two, engine: Engine, owner_engine: Engine
) -> None:
    for scope in (None, DEFAULT_TENANT_ID):
        with engine.connect() as conn:
            if scope is not None:
                conn.execute(
                    text("SELECT set_config('docforge.tenant_id', :t, true)"), {"t": str(scope)}
                )
            with pytest.raises(Exception, match="only a personal workspace"):
                conn.execute(text("SELECT docforge_delete_account()"))
    assert rows_of(owner_engine, DEFAULT_TENANT_ID)["tenants"] == 1


def test_the_append_only_tables_still_refuse_deletion_outside_an_erasure(
    two: Two, owner_engine: Engine
) -> None:
    # Even the owner, and even with the setting made by hand: only an erasure deletes.
    with owner_engine.connect() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute(text("DELETE FROM audit_log WHERE tenant_id = :t"), {"t": two.asha_ws})
    with owner_engine.connect() as conn, pytest.raises(Exception, match="not deleted"):
        conn.execute(text("DELETE FROM reviewers WHERE tenant_id = :t"), {"t": two.asha_ws})
    with owner_engine.connect() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute(
            text("SELECT set_config('docforge.erasing', :t, true)"), {"t": str(two.ben_ws)}
        )
        conn.execute(text("DELETE FROM audit_log WHERE tenant_id = :t"), {"t": two.asha_ws})
