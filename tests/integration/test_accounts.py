"""Self-service accounts: each sign-up is one private workspace with one member in it, who
signs in with an email and a password; sign-up is off unless switched on, and limited."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from docforge.api.app import MemberCaps, create_app
from docforge.auth import Authenticator
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.review.service import ReviewService
from full_stack import PASSWORD, FullStack
from worlds import World

pytestmark = [pytest.mark.integration, pytest.mark.no_ambient_tenant]

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
WRONG = "The email or password is not right."


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
def stack(sessions: SessionFactory, world: World) -> FullStack:
    return FullStack(sessions, world)


def sign_up(api: TestClient, **body: Any) -> Any:
    sent = {"name": "Asha Rao", "email": "asha@example.com", "password": PASSWORD, **body}
    return api.post("/v1/accounts", json=sent)


def test_signing_up_makes_a_private_workspace_and_signs_in(
    stack: FullStack, owner_engine: Engine
) -> None:
    response = sign_up(stack.client, email="  Asha@Example.COM ")

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"token", "expires_in_seconds"} and body["token"].startswith("dfs_")
    me = stack.client.get(
        "/v1/sessions/current", headers={"Authorization": f"Bearer {body['token']}"}
    ).json()
    assert me == {
        "kind": "session", "name": "Asha Rao", "email": "asha@example.com", "role": "member",
        "organisation": "Asha Rao's workspace", "credential": "password",
        "platform_admin": False, "workspace": "personal",
    }  # fmt: skip
    with owner_engine.connect() as conn:
        tenant = conn.execute(
            text(
                "SELECT t.name, t.kind, t.display_name FROM tenants t "
                "JOIN accounts a ON a.tenant_id = t.id WHERE a.email = 'asha@example.com'"
            )
        ).one()
        reviewer = conn.execute(
            text("SELECT role, credential, platform_admin, pin_hash FROM reviewers "
                 "WHERE email = 'asha@example.com'")
        ).one()  # fmt: skip
        audited = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'account.created'")
        ).scalar_one()
    assert tenant.name.startswith("u-") and len(tenant.name) == 14
    assert (tenant.kind, tenant.display_name) == ("personal", "Asha Rao's workspace")
    assert (reviewer.role, reviewer.credential, reviewer.platform_admin) == (
        "member", "password", False,
    )  # fmt: skip
    assert PASSWORD not in reviewer.pin_hash and reviewer.pin_hash.startswith("scrypt$")
    assert audited == 1


def test_every_account_is_its_own_workspace(stack: FullStack, owner_engine: Engine) -> None:
    stack.sign_up("Asha", "asha@example.com")
    stack.sign_up("Ben", "ben@example.com")

    a = FullStack.workspace_of(owner_engine, "asha@example.com")
    b = FullStack.workspace_of(owner_engine, "ben@example.com")
    assert a != b and DEFAULT_TENANT_ID not in (a, b)


def test_an_email_has_one_account_whatever_its_case(stack: FullStack) -> None:
    stack.sign_up("Asha", "asha@example.com")

    again = sign_up(stack.client, email="ASHA@example.com ", name="Someone else")

    assert again.status_code == 409
    assert again.json()["detail"] == "An account with this email already exists."


@pytest.mark.parametrize(
    "body",
    [
        {"password": "short1234"},  # nine characters
        {"password": " " * 12},
        {"password": "x" * 129},
        {"email": "not-an-email"},
        {"email": "a@b"},
        {"name": ""},
        {"name": "   "},
        {"name": "n" * 121},
        {"tenant": "default"},  # nothing else is accepted
    ],
)
def test_an_unacceptable_sign_up_is_refused_and_makes_nothing(
    stack: FullStack, owner_engine: Engine, body: dict[str, Any]
) -> None:
    assert sign_up(stack.client, **body).status_code == 422
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM accounts")).scalar_one() == 0


def test_sign_up_is_off_unless_switched_on(sessions: SessionFactory, world: World) -> None:
    off = FullStack(sessions, world, signup=False)

    assert sign_up(off.client).status_code == 404


def test_one_address_makes_five_accounts_an_hour_at_most(stack: FullStack) -> None:
    for n in range(5):
        assert sign_up(stack.client, email=f"p{n}@example.com").status_code == 201

    sixth = sign_up(stack.client, email="p6@example.com")

    assert sixth.status_code == 429 and int(sixth.headers["Retry-After"]) > 0


def test_new_accounts_pause_once_the_days_cap_is_reached(
    sessions: SessionFactory, world: World
) -> None:
    capped = FullStack(sessions, world, signups_per_day=2)
    capped.sign_up("A", "a@example.com")
    capped.sign_up("B", "b@example.com")

    third = sign_up(capped.client, email="c@example.com")

    assert third.status_code == 429 and "Retry-After" in third.headers


def test_a_member_signs_in_with_email_and_password_alone(stack: FullStack) -> None:
    stack.sign_up("Asha", "asha@example.com")

    headers = stack.sign_in(" ASHA@example.com", PASSWORD)

    assert stack.client.get("/v1/sessions/current", headers=headers).json()["role"] == "member"
    # The password may also travel in the field a PIN uses.
    stack.sign_in("asha@example.com", PASSWORD, field="pin")


def test_an_unknown_email_and_a_wrong_password_get_the_same_answer(stack: FullStack) -> None:
    stack.sign_up("Asha", "asha@example.com")

    unknown = stack.client.post(
        "/v1/sessions", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    wrong = stack.client.post(
        "/v1/sessions", json={"email": "asha@example.com", "password": "wrong password!"}
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json() == {"detail": WRONG}


def test_a_sign_in_needs_exactly_one_secret(stack: FullStack) -> None:
    neither = stack.client.post("/v1/sessions", json={"email": "a@example.com"})
    both = stack.client.post(
        "/v1/sessions", json={"email": "a@example.com", "pin": "123456", "password": PASSWORD}
    )
    assert neither.status_code == both.status_code == 422


def test_five_wrong_passwords_lock_the_account(stack: FullStack) -> None:
    stack.sign_up("Asha", "asha@example.com")
    for _ in range(5):
        stack.client.post(
            "/v1/sessions", json={"email": "asha@example.com", "password": "wrong password!"}
        )

    locked = stack.client.post(
        "/v1/sessions", json={"email": "asha@example.com", "password": PASSWORD}
    )

    assert locked.status_code == 401 and locked.json() == {"detail": WRONG}


def test_failed_sign_ins_without_an_organisation_are_limited_per_address(
    sessions: SessionFactory, world: World
) -> None:
    auth = Authenticator(sessions, failed_logins_per_window=3)
    api = TestClient(create_app(None, authenticator=auth, signup_enabled=True))
    for n in range(3):
        api.post("/v1/sessions", json={"email": f"x{n}@example.com", "password": PASSWORD})

    limited = api.post("/v1/sessions", json={"email": "y@example.com", "password": PASSWORD})

    assert limited.status_code == 429


def test_an_organisations_reviewer_still_signs_in_with_its_name_and_a_pin(
    stack: FullStack, sessions: SessionFactory
) -> None:
    review = ReviewService(sessions, stack.world.store, {})
    review.add_reviewer(DEFAULT_TENANT_ID, name="Ravi", email="ravi@example.com", pin="482913")

    headers = stack.sign_in("ravi@example.com", "482913", "default", field="pin")
    me = stack.client.get("/v1/sessions/current", headers=headers).json()

    assert (me["credential"], me["workspace"], me["organisation"]) == (
        "pin", "organisation", "default",
    )  # fmt: skip
    # Without the organisation's name only self-service accounts are found.
    alone = stack.client.post("/v1/sessions", json={"email": "ravi@example.com", "pin": "482913"})
    assert alone.status_code == 401


def test_an_account_and_a_reviewer_may_share_an_email_without_meeting(
    stack: FullStack, sessions: SessionFactory, owner_engine: Engine
) -> None:
    review = ReviewService(sessions, stack.world.store, {})
    review.add_reviewer(DEFAULT_TENANT_ID, name="Ravi", email="ravi@example.com", pin="482913")
    stack.sign_up("Ravi at home", "ravi@example.com")

    member = stack.sign_in("ravi@example.com", PASSWORD)
    reviewer = stack.sign_in("ravi@example.com", "482913", "default", field="pin")

    assert stack.client.get("/v1/sessions/current", headers=member).json()["role"] == "member"
    assert stack.client.get("/v1/sessions/current", headers=reviewer).json()["role"] == "reviewer"


def test_the_application_cannot_read_the_accounts_table(engine: Engine) -> None:
    with engine.connect() as conn:
        rights = conn.execute(
            text(
                "SELECT has_table_privilege(current_user, 'accounts', 'SELECT'), "
                "has_table_privilege(current_user, 'accounts', 'INSERT'), "
                "has_table_privilege(current_user, 'accounts', 'UPDATE')"
            )
        ).one()
        found = conn.execute(text("SELECT docforge_account_tenant('nobody@example.com')"))
        assert found.scalar_one() is None
    assert tuple(rights) == (False, False, False)


def test_the_application_cannot_make_anyone_a_platform_administrator(engine: Engine) -> None:
    with engine.connect() as conn:
        update = conn.execute(
            text(
                "SELECT has_column_privilege(current_user, 'reviewers', 'platform_admin', 'UPDATE')"
            )
        ).scalar_one()
        insert = conn.execute(
            text(
                "SELECT has_column_privilege(current_user, 'reviewers', 'platform_admin', 'INSERT')"
            )
        ).scalar_one()
    assert (update, insert) == (False, False)


# What a member may do


def member_stack(sessions: SessionFactory, world: World, **caps: int) -> FullStack:
    return FullStack(sessions, world, caps=MemberCaps(**caps))


def upload(stack: FullStack, headers: dict[str, str], pdf: bytes, name: str) -> Any:
    return stack.client.post(
        "/v1/documents", headers=headers, files={"file": (name, pdf, "application/pdf")},
        data={"doc_type": "invoice"},
    )  # fmt: skip


def test_a_member_uploads_reads_and_deletes_its_own_documents(stack: FullStack) -> None:
    me = stack.sign_up("Asha", "asha@example.com")

    uploaded = upload(stack, me, stack.world.invoice_pdf, "invoice.pdf")

    assert uploaded.status_code == 202, uploaded.text
    document_id = uploaded.json()["document"]["id"]
    listed = stack.client.get("/v1/documents", headers=me).json()["items"]
    assert [d["id"] for d in listed] == [document_id]
    assert stack.client.get(f"/v1/documents/{document_id}", headers=me).status_code == 200
    assert stack.client.delete(f"/v1/documents/{document_id}", headers=me).status_code == 204


def test_a_free_workspace_holds_a_few_documents(sessions: SessionFactory, world: World) -> None:
    stack = member_stack(sessions, world, max_documents=1, uploads_per_day=10)
    me = stack.sign_up("Asha", "asha@example.com")
    first = upload(stack, me, stack.world.invoice_pdf, "invoice.pdf").json()["document"]["id"]

    full = upload(stack, me, stack.world.order_pdf, "order.pdf")

    assert full.status_code == 429
    assert full.json()["detail"] == (
        "This free workspace holds up to 1 documents. Delete one to upload another."
    )
    # The same file again is the document it already holds, not another one.
    assert upload(stack, me, stack.world.invoice_pdf, "invoice.pdf").status_code == 200
    stack.client.delete(f"/v1/documents/{first}", headers=me)
    assert upload(stack, me, stack.world.order_pdf, "order.pdf").status_code == 202


def test_a_free_workspace_takes_a_few_uploads_a_day_even_after_deleting(
    sessions: SessionFactory, world: World
) -> None:
    stack = member_stack(sessions, world, max_documents=10, uploads_per_day=1)
    me = stack.sign_up("Asha", "asha@example.com")
    first = upload(stack, me, stack.world.invoice_pdf, "invoice.pdf").json()["document"]["id"]
    stack.client.delete(f"/v1/documents/{first}", headers=me)

    again = upload(stack, me, stack.world.order_pdf, "order.pdf")

    assert again.status_code == 429
    assert again.json()["detail"] == (
        "This free workspace takes up to 1 uploads a day. Try again tomorrow."
    )
    assert int(again.headers["Retry-After"]) > 0


def test_a_free_workspace_asks_a_few_questions_a_day(
    sessions: SessionFactory, world: World
) -> None:
    stack = member_stack(sessions, world, questions_per_day=1)
    me = stack.sign_up("Asha", "asha@example.com")
    assert stack.client.post("/v1/chat", headers=me, json={"question": "Hi?"}).status_code == 200

    second = stack.client.post("/v1/chat", headers=me, json={"question": "Again?"})
    streamed = stack.client.post("/v1/chat/stream", headers=me, json={"question": "Again?"})

    assert second.status_code == 429
    assert second.json()["detail"] == (
        "This free workspace has 1 questions a day. Try again tomorrow."
    )
    assert "This free workspace has 1 questions a day" in streamed.text


def test_an_organisation_is_not_held_to_a_free_workspaces_caps(
    sessions: SessionFactory, world: World
) -> None:
    stack = member_stack(sessions, world, max_documents=1, uploads_per_day=1)
    admin = {"Authorization": f"Bearer {stack.key(DEFAULT_TENANT_ID, 'admin')}"}

    assert upload(stack, admin, stack.world.invoice_pdf, "invoice.pdf").status_code == 202
    assert upload(stack, admin, stack.world.order_pdf, "order.pdf").status_code == 202


def test_a_member_corrects_and_signs_with_its_password(
    stack: FullStack, owner_engine: Engine
) -> None:
    long_password = "p" * 100  # longer than any PIN
    stack.sign_up("Asha", "asha@example.com", long_password)
    me = stack.sign_in("asha@example.com", long_password)
    tenant = FullStack.workspace_of(owner_engine, "asha@example.com")
    stack.read(tenant, "purchase_order")
    invoice = stack.read(tenant, "invoice")
    detail = stack.client.get(f"/v1/documents/{invoice}/review", headers=me).json()
    secret = {"email": "asha@example.com", "pin": long_password}

    path = detail["editable_paths"][0]
    corrected = stack.client.post(
        f"/v1/documents/{invoice}/corrections", headers=me,
        json={"path": path, "text": None, "reason": "checked", **secret},
    )  # fmt: skip
    assert corrected.status_code == 200, corrected.text
    record = corrected.json()["record_sha256"]
    signed = stack.client.post(
        f"/v1/documents/{invoice}/review", headers=me,
        json={"outcome": "rejected", "meaning": detail["meanings"]["rejected"],
              "reason": "test", "expected_record_sha256": record, **secret},
    )  # fmt: skip
    assert signed.status_code == 201, signed.text
    wrong = stack.client.post(
        f"/v1/documents/{invoice}/corrections", headers=me,
        json={"path": path, "text": None, "reason": "x", "email": "asha@example.com",
              "pin": "not the password"},
    )  # fmt: skip
    assert wrong.status_code in (403, 409)


def test_a_member_cannot_use_the_stateless_preview(stack: FullStack) -> None:
    me = stack.sign_up("Asha", "asha@example.com")
    app = create_app(object(), authenticator=stack.auth)  # type: ignore[arg-type]

    response = TestClient(app).post(
        "/v1/extractions", headers=me,
        files={"file": ("invoice.pdf", stack.world.invoice_pdf, "application/pdf")},
    )  # fmt: skip

    assert response.status_code == 403


# Every administrator's route refuses a member

ADMIN_ROUTES = {
    ("GET", "/v1/api-keys"),
    ("POST", "/v1/api-keys"),
    ("DELETE", "/v1/api-keys/{prefix}"),
    ("GET", "/v1/agent-calls"),
    ("GET", "/v1/audit"),
    ("GET", "/v1/audit/filters"),
    ("GET", "/v1/audit/export.csv"),
    ("GET", "/v1/questions/unanswered"),
    ("POST", "/v1/webhooks"),
    ("GET", "/v1/webhooks"),
    ("GET", "/v1/webhooks/events"),
    ("PATCH", "/v1/webhooks/{webhook_id}"),
    ("DELETE", "/v1/webhooks/{webhook_id}"),
    ("POST", "/v1/webhooks/{webhook_id}/secret"),
    ("GET", "/v1/webhooks/{webhook_id}/deliveries"),
    ("POST", "/v1/webhooks/{webhook_id}/test"),
    ("POST", "/v1/webhooks/{webhook_id}/deliveries/{delivery_id}/resend"),
    ("GET", "/v1/platform/overview"),
    ("GET", "/v1/platform/activity"),
}


def routes(app: Any) -> set[tuple[str, str]]:
    """Every /v1 operation the app serves, from its OpenAPI description."""
    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/v1/")
        for method in operations
    }


def filled(path: str) -> str:
    for name in ("document_id", "webhook_id", "delivery_id", "conversation_id",
                 "collection_id"):  # fmt: skip
        path = path.replace("{" + name + "}", str(uuid.uuid4()))
    return path.replace("{prefix}", "0123456789ab").replace("{page}", "1")


def test_a_member_is_refused_every_administrators_route_and_only_those(stack: FullStack) -> None:
    me = stack.sign_up("Asha", "asha@example.com")
    every = routes(stack.app)
    assert every >= ADMIN_ROUTES, ADMIN_ROUTES - every

    refused = {
        (method, path)
        for method, path in every
        if (method, path) != ("DELETE", "/v1/sessions/current")  # would sign it out
        and stack.client.request(method, filled(path), headers=me, json={}).status_code == 403
    }

    assert refused == ADMIN_ROUTES


def test_a_member_cannot_make_an_api_key_for_an_agent(stack: FullStack) -> None:
    me = stack.sign_up("Asha", "asha@example.com")

    made = stack.client.post("/v1/api-keys", headers=me, json={"name": "mine"})

    assert made.status_code == 403
