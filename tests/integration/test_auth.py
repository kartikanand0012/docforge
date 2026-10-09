"""Every route needs a credential; the tenant comes from it; other tenants' data is invisible."""

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from docforge.api.app import create_app
from docforge.auth import Authenticator
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry
from docforge.db.session import SessionFactory
from docforge.db.tenancy import tenant_scope
from docforge.extraction.pipeline import INVOICE_SPEC
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
from docforge.review.service import ReviewService
from worlds import World

pytestmark = [pytest.mark.integration, pytest.mark.no_ambient_tenant]

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
REPO = Path(__file__).resolve().parents[2]
PDF = (REPO / "tests" / "fixtures" / "synthetic" / "pair_001" / "invoice.pdf").read_bytes()


class Stack:
    def __init__(self, world: World, sessions: SessionFactory, other_tenant: uuid.UUID) -> None:
        self.world = world
        self.review = ReviewService(
            sessions, world.store, {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC}
        )
        self.auth = Authenticator(sessions, failed_logins_per_window=10)
        self.client = TestClient(
            create_app(None, service=world.service, review=self.review, authenticator=self.auth)
        )
        self.other_tenant = other_tenant
        self.review.add_reviewer(
            DEFAULT_TENANT_ID, name="Asha Rao", email="asha@example.com", pin="482913"
        )
        self.review.add_reviewer(other_tenant, name="Eve", email="eve@example.com", pin="999999")

    def key(self, role: str, tenant: uuid.UUID = DEFAULT_TENANT_ID) -> dict[str, str]:
        token = self.auth.create_api_key(tenant, name=f"{role} key", role=role)
        return {"Authorization": f"Bearer {token}"}

    def login(
        self, email: str = "asha@example.com", pin: str = "482913", tenant: str = "default"
    ) -> dict[str, str]:
        response = self.client.post(
            "/v1/sessions", json={"tenant": tenant, "email": email, "pin": pin}
        )
        assert response.status_code == 201, response.text
        return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture
def stack(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
    other_tenant: uuid.UUID,
) -> Iterator[Stack]:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    world.build()
    yield Stack(world, sessions, other_tenant)


def routes(document_id: uuid.UUID) -> list[tuple[str, str]]:
    return [
        ("GET", f"/v1/documents/{document_id}"),
        ("GET", f"/v1/documents/{document_id}/extraction"),
        ("GET", f"/v1/documents/{document_id}/assessment"),
        ("GET", f"/v1/documents/{document_id}/audit"),
        ("POST", f"/v1/documents/{document_id}/reprocess"),
        ("GET", f"/v1/documents/{document_id}/review"),
        ("GET", f"/v1/documents/{document_id}/pages/1"),
        ("GET", "/v1/review/queue"),
        ("GET", "/v1/audit/verification"),
    ]


def test_without_a_credential_every_route_is_refused(stack: Stack) -> None:
    document_id = stack.world.process("invoice")

    for method, path in [*routes(document_id), ("POST", "/v1/documents")]:
        response = stack.client.request(method, path)
        assert response.status_code == 401, (method, path)
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header", ["Bearer nonsense", "Bearer dfk_000000000000_" + "a" * 43, "Basic YWJj", "dfk_x"]
)
def test_a_bad_or_unknown_credential_is_refused(stack: Stack, header: str) -> None:
    assert (
        stack.client.get("/v1/review/queue", headers={"Authorization": header}).status_code == 401
    )


def test_another_tenants_documents_do_not_exist_for_it(stack: Stack) -> None:
    document_id = stack.world.process("invoice")
    theirs = stack.key("admin", stack.other_tenant)

    for method, path in routes(document_id):
        if path.startswith(f"/v1/documents/{document_id}"):
            assert stack.client.request(method, path, headers=theirs).status_code == 404, path
    assert stack.client.get("/v1/review/queue", headers=theirs).json() == []
    chain = stack.client.get("/v1/audit/verification", headers=theirs).json()
    assert chain["entries"] == 1  # only its own audit log: the making of its key


def test_an_upload_belongs_to_the_tenant_of_the_key(stack: Stack, sessions: SessionFactory) -> None:
    theirs = stack.key("integrator", stack.other_tenant)

    response = stack.client.post(
        "/v1/documents", files={"file": ("a.pdf", PDF, "application/pdf")}, headers=theirs
    )

    assert response.status_code == 202
    document_id = response.json()["document"]["id"]
    assert (
        stack.client.get(f"/v1/documents/{document_id}", headers=stack.key("admin")).status_code
        == 404
    )
    assert stack.client.get(f"/v1/documents/{document_id}", headers=theirs).status_code == 200
    with tenant_scope(stack.other_tenant), sessions() as session:
        entry = session.scalars(select(AuditEntry).order_by(AuditEntry.id.desc())).first()
    assert entry is not None and entry.actor.startswith("key:")


def test_roles_limit_what_a_credential_can_do(stack: Stack) -> None:
    document_id = stack.world.process("invoice")
    integrator, reviewer = stack.key("integrator"), stack.login()

    assert stack.client.get("/v1/review/queue", headers=integrator).status_code == 403
    assert (
        stack.client.get(f"/v1/documents/{document_id}/review", headers=reviewer).status_code == 200
    )
    upload = stack.client.post(
        "/v1/documents", files={"file": ("a.pdf", PDF, "application/pdf")}, headers=reviewer
    )
    assert upload.status_code == 403


def test_only_a_signed_in_person_corrects_or_signs_and_only_as_themselves(stack: Stack) -> None:
    document_id = stack.world.process("invoice")
    body = {
        "path": "invoice_no",
        "text": "X",
        "reason": "r",
        "email": "asha@example.com",
        "pin": "482913",
    }

    by_key = stack.client.post(
        f"/v1/documents/{document_id}/corrections", json=body, headers=stack.key("admin")
    )
    stack.review.add_reviewer(
        DEFAULT_TENANT_ID, name="Ravi", email="ravi@example.com", pin="135790"
    )
    as_other = stack.client.post(
        f"/v1/documents/{document_id}/corrections",
        json=body,
        headers=stack.login("ravi@example.com", "135790"),
    )
    as_self = stack.client.post(
        f"/v1/documents/{document_id}/corrections", json=body, headers=stack.login()
    )

    assert by_key.status_code == 403  # a system cannot sign for a person
    assert as_other.status_code == 403
    assert as_self.status_code == 200


def test_a_session_from_a_wrong_pin_is_refused_and_logins_are_rate_limited(stack: Stack) -> None:
    for _ in range(10):
        response = stack.client.post(
            "/v1/sessions",
            json={"tenant": "default", "email": "nobody@example.com", "pin": "000000"},
        )
        assert response.status_code == 401
    limited = stack.client.post(
        "/v1/sessions", json={"tenant": "default", "email": "asha@example.com", "pin": "482913"}
    )

    assert limited.status_code == 429
    assert "retry-after" in limited.headers


def test_a_revoked_key_and_an_ended_session_stop_working(stack: Stack, engine: Engine) -> None:
    key = stack.key("integrator")
    session = stack.login()
    assert stack.client.get("/v1/audit/verification", headers=key).status_code == 200

    stack.auth.revoke_api_key(DEFAULT_TENANT_ID, key["Authorization"].split()[1])
    assert stack.client.delete("/v1/sessions/current", headers=session).status_code == 204

    assert stack.client.get("/v1/audit/verification", headers=key).status_code == 401
    assert (
        stack.client.get(
            "/v1/documents/" + str(uuid.uuid4()) + "/review", headers=session
        ).status_code
        == 401
    )


def test_an_expired_session_is_refused(stack: Stack, owner_engine: Engine) -> None:
    session = stack.login()
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE sessions SET created_at = now() - interval '9 hours', "
                "expires_at = now() - interval '1 hour'"
            )
        )

    assert stack.client.get("/v1/review/queue", headers=session).status_code == 401


def test_tokens_are_stored_only_as_hashes(stack: Stack, owner_engine: Engine) -> None:
    key = stack.key("admin")["Authorization"].split()[1]
    session = stack.login()["Authorization"].split()[1]

    with owner_engine.connect() as conn:
        stored = " ".join(str(row) for row in conn.execute(text("SELECT * FROM api_keys")))
        stored += " ".join(str(row) for row in conn.execute(text("SELECT * FROM sessions")))
    # The secret part may itself contain "_": split twice at most, or a short fragment of it
    # could turn up in the stored rows by chance.
    assert key.split("_", 2)[2] not in stored
    assert session.split("_", 2)[2] not in stored


def test_the_admin_command_makes_organisations_and_keys(
    engine: Engine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from docforge.admin import main
    from docforge.config import get_settings

    monkeypatch.setenv("DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL", owner_engine.url.render_as_string(hide_password=False)
    )
    get_settings.cache_clear()
    try:
        assert main(["create-tenant", "acme"]) == 0
        assert main(["create-tenant", "acme"]) == 1  # already there
        assert (
            main(["create-key", "--tenant", "acme", "--role", "integrator", "--name", "ERP"]) == 0
        )
        token = capsys.readouterr().out.strip().splitlines()[-1]
        assert token.startswith("dfk_")
        assert main(["revoke-key", "--tenant", "acme", "--prefix", token.split("_")[1]]) == 0
        assert main(["create-key", "--tenant", "nowhere", "--role", "admin", "--name", "x"]) == 1
    finally:
        get_settings.cache_clear()


def test_an_admin_reviewer_may_upload_and_a_plain_reviewer_may_not(stack: Stack) -> None:
    stack.review.add_reviewer(
        DEFAULT_TENANT_ID, name="Head", email="head@example.com", pin="112233", role="admin"
    )
    admin = stack.login("head@example.com", "112233")

    upload = stack.client.post(
        "/v1/documents", files={"file": ("a.pdf", PDF, "application/pdf")}, headers=admin
    )

    assert upload.status_code == 202


def test_wrong_pins_from_many_addresses_lock_the_reviewer_for_those_addresses(
    stack: Stack,
) -> None:
    """The address limit can be dodged; the reviewer's own count cannot. It locks the
    addresses the wrong PINs came from, not the reviewer's own."""
    body = {"tenant": "default", "email": "asha@example.com"}
    for attempt in range(5):
        response = TestClient(stack.client.app, client=(f"203.0.113.{attempt}", 1)).post(
            "/v1/sessions", json={**body, "pin": "000000"}
        )
        assert response.status_code == 401

    locked = TestClient(stack.client.app, client=("203.0.113.0", 1)).post(
        "/v1/sessions", json={**body, "pin": "482913"}
    )
    asha = TestClient(stack.client.app, client=("198.51.100.1", 1)).post(
        "/v1/sessions", json={**body, "pin": "482913"}
    )

    assert locked.status_code == 401  # the right PIN, but this address is locked for now
    assert asha.status_code == 201


def test_a_shared_account_with_a_public_pin_is_never_locked(stack: Stack) -> None:
    """The public demo's account: its PIN is on the sign-in page, so a lock would protect
    nothing and let any visitor shut everyone else out."""
    stack.review.add_reviewer(
        DEFAULT_TENANT_ID, name="Demo", email="demo@example.com", pin="111111", shared=True
    )
    for attempt in range(6):
        stack.client.post(
            "/v1/sessions",
            json={"tenant": "default", "email": "demo@example.com", "pin": "000000"},
            headers={"X-Forwarded-For": f"203.0.113.{attempt}"},
        )

    assert stack.login("demo@example.com", "111111")


# --- for the web app: who is signed in ------------------------------------------------------


def test_a_caller_can_ask_who_they_are(stack: Stack) -> None:
    """The web app shows the person's name and shows administration only to administrators;
    sign-in returns a token only, so it asks."""
    me = stack.client.get("/v1/sessions/current", headers=stack.login())

    assert me.status_code == 200
    assert me.headers["cache-control"] == "no-store"
    assert me.json() == {
        "kind": "session", "name": "Asha Rao", "email": "asha@example.com",
        "role": "reviewer", "organisation": "default", "credential": "pin",
        "platform_admin": False, "workspace": "organisation",
    }  # fmt: skip
    key = stack.client.get("/v1/sessions/current", headers=stack.key("integrator")).json()
    assert (key["kind"], key["name"], key["email"], key["role"], key["credential"]) == (
        "api_key", "integrator key", None, "integrator", None,
    )  # fmt: skip
    assert stack.client.get("/v1/sessions/current").status_code == 401
