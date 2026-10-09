"""The platform administrator (the owner): every workspace's figures and activity, and a
read-only look into one workspace - and nobody else gets any of it."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from test_accounts import filled, routes

from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.review.__main__ import make_platform_admin
from docforge.review.service import ReviewService
from full_stack import FullStack
from worlds import World

pytestmark = [pytest.mark.integration, pytest.mark.no_ambient_tenant]

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
HEADER = "X-DocForge-Workspace"
PIN = "482913"


class Platform:
    def __init__(self, sessions: SessionFactory, world: World, owner_engine: Engine) -> None:
        self.stack = FullStack(sessions, world)
        self.api = self.stack.client
        self.owner_engine = owner_engine
        review = ReviewService(sessions, world.store, {})
        review.add_reviewer(
            DEFAULT_TENANT_ID, name="Owner", email="owner@example.com", pin=PIN, role="admin"
        )
        review.add_reviewer(DEFAULT_TENANT_ID, name="Org admin", email="oa@example.com", pin=PIN,
                            role="admin")  # fmt: skip
        make_platform_admin(owner_engine.url, "default", "Owner@Example.com")
        self.admin = self.stack.sign_in("owner@example.com", PIN, "default", field="pin")
        self.org_admin = self.stack.sign_in("oa@example.com", PIN, "default", field="pin")
        self.asha = self.stack.sign_up("Asha", "asha@example.com")
        self.ben = self.stack.sign_up("Ben", "ben@example.com")
        self.asha_ws = FullStack.workspace_of(owner_engine, "asha@example.com")
        self.ben_ws = FullStack.workspace_of(owner_engine, "ben@example.com")
        self.asha_doc = self.stack.read(self.asha_ws, "invoice")
        self.ben_doc = self.stack.read(self.ben_ws, "purchase_order")

    def looking_at(self, workspace: uuid.UUID, who: dict[str, str] | None = None) -> dict[str, str]:
        return {**(who or self.admin), HEADER: str(workspace)}


@pytest.fixture
def platform(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
    owner_engine: Engine,
) -> Platform:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    world.build()
    return Platform(sessions, world, owner_engine)


# Making one


def test_the_owner_is_made_a_platform_administrator_once_and_says_so(
    platform: Platform,
) -> None:
    make_platform_admin(platform.owner_engine.url, "default", "owner@example.com")  # again

    me = platform.api.get("/v1/sessions/current", headers=platform.admin).json()
    them = platform.api.get("/v1/sessions/current", headers=platform.org_admin).json()

    assert me["platform_admin"] is True and them["platform_admin"] is False


@pytest.mark.parametrize(("tenant", "email"), [("nowhere", "owner@example.com"),
                                                ("default", "nobody@example.com")])  # fmt: skip
def test_an_unknown_organisation_or_person_is_not_made_one(
    platform: Platform, tenant: str, email: str
) -> None:
    with pytest.raises(ValueError, match="no "):
        make_platform_admin(platform.owner_engine.url, tenant, email)


# The overview


def test_the_overview_counts_every_workspace(platform: Platform) -> None:
    response = platform.api.get("/v1/platform/overview", headers=platform.admin)

    assert response.status_code == 200, response.text
    body = response.json()
    totals = body["totals"]
    assert totals["workspaces"] == 3 and totals["accounts"] == 2
    assert totals["documents"] == 2 and totals["documents_last_7_days"] == 2
    assert totals["signups_last_7_days"] == 2 and totals["signed"] == 0
    assert totals["pages"] >= 2 and totals["questions"] == 0
    assert set(totals) == {
        "workspaces", "accounts", "documents", "pages", "signed", "questions",
        "model_cost_usd", "documents_last_7_days", "signups_last_7_days",
    }  # fmt: skip
    rows = {row["tenant_id"]: row for row in body["workspaces"]}
    asha = rows[str(platform.asha_ws)]
    assert (asha["organisation"], asha["kind"], asha["owner_name"], asha["owner_email"]) == (
        "Asha's workspace", "personal", "Asha", "asha@example.com",
    )  # fmt: skip
    assert asha["documents"] == 1 and asha["sign_ins"] == 1
    org = rows[str(DEFAULT_TENANT_ID)]
    assert (org["kind"], org["owner_email"]) == ("organisation", "owner@example.com")
    assert set(asha) == {
        "tenant_id", "organisation", "kind", "owner_name", "owner_email", "created_at",
        "last_active_at", "documents", "pages", "signed", "questions", "model_cost_usd",
        "sign_ins",
    }  # fmt: skip
    active = [row["last_active_at"] for row in body["workspaces"]]
    assert active == sorted(active, reverse=True)
    # Figures only: never a document's name or contents.
    assert "invoice.pdf" not in response.text and "purchase_order.pdf" not in response.text


def test_a_deleted_document_leaves_the_overview(platform: Platform) -> None:
    platform.api.delete(f"/v1/documents/{platform.asha_doc}", headers=platform.asha)

    body = platform.api.get("/v1/platform/overview", headers=platform.admin).json()

    assert body["totals"]["documents"] == 1
    asha = next(r for r in body["workspaces"] if r["tenant_id"] == str(platform.asha_ws))
    assert asha["documents"] == 0


# Activity


def test_activity_across_every_workspace_newest_first_with_labels_only(
    platform: Platform,
) -> None:
    response = platform.api.get("/v1/platform/activity", headers=platform.admin)

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert {i["tenant_id"] for i in items} >= {str(platform.asha_ws), str(platform.ben_ws)}
    ids = [i["id"] for i in items]
    assert ids == sorted(ids, reverse=True)
    made = next(i for i in items if i["action"] == "account.created"
                and i["tenant_id"] == str(platform.asha_ws))  # fmt: skip
    assert made["action_label"] == "Account made" and made["actor_name"] == "Asha"
    assert made["organisation"] == "Asha's workspace"
    assert all(
        set(i) == {"id", "occurred_at", "tenant_id", "organisation", "actor_name", "action",
                   "action_label", "target_label"}
        for i in items
    )  # fmt: skip
    assert "invoice.pdf" not in response.text and "sha256" not in response.text


def test_activity_pages_back_and_narrows_to_one_workspace(platform: Platform) -> None:
    first = platform.api.get(
        "/v1/platform/activity", headers=platform.admin, params={"limit": 2}
    ).json()
    assert len(first["items"]) == 2 and first["next_before"] == first["items"][-1]["id"]
    second = platform.api.get(
        "/v1/platform/activity", headers=platform.admin,
        params={"limit": 2, "before": first["next_before"]},
    ).json()  # fmt: skip
    assert all(i["id"] < first["next_before"] for i in second["items"])

    only = platform.api.get(
        "/v1/platform/activity", headers=platform.admin,
        params={"tenant_id": str(platform.ben_ws), "limit": 200},
    ).json()  # fmt: skip
    assert only["items"] and {i["tenant_id"] for i in only["items"]} == {str(platform.ben_ws)}


@pytest.mark.parametrize("path", ["/v1/platform/overview", "/v1/platform/activity"])
def test_nobody_else_sees_the_platform(platform: Platform, path: str) -> None:
    key = {"Authorization": f"Bearer {platform.stack.key(DEFAULT_TENANT_ID, 'admin')}"}
    for who in (platform.asha, platform.org_admin, key):
        assert platform.api.get(path, headers=who).status_code == 403
    assert platform.api.get(path).status_code == 401


def test_the_platform_functions_are_for_the_application_alone(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT p.proname, p.prosecdef, p.proconfig, "
                "has_function_privilege('public', p.oid, 'EXECUTE') "
                "FROM pg_proc p WHERE p.proname LIKE 'docforge\\_platform\\_%'"
            )
        ).all()
    assert len(rows) >= 3
    for name, definer, config, public in rows:
        assert definer and not public, name
        assert config == ["search_path=pg_catalog, public, pg_temp"], name


def test_the_platform_functions_give_nothing_unless_the_service_checked(engine: Engine) -> None:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM docforge_platform_workspaces()"))
        assert rows.scalar_one() == 0


# Looking into a workspace


def test_the_administrator_reads_a_workspace_it_looks_into(platform: Platform) -> None:
    looking = platform.looking_at(platform.asha_ws)

    listed = platform.api.get("/v1/documents", headers=looking)
    assert listed.status_code == 200
    assert [d["id"] for d in listed.json()["items"]] == [str(platform.asha_doc)]
    for path in ("", "/extraction", "/assessment", "/timeline", "/audit", "/pages/1"):
        got = platform.api.get(f"/v1/documents/{platform.asha_doc}{path}", headers=looking)
        assert got.status_code == 200, path
    # Ben's document is not in Asha's workspace.
    assert platform.api.get(f"/v1/documents/{platform.ben_doc}", headers=looking).status_code == 404
    me = platform.api.get("/v1/sessions/current", headers=looking).json()
    assert me == {
        "kind": "session", "name": "Owner", "email": "owner@example.com", "role": "observer",
        "organisation": "Asha's workspace", "credential": "pin", "platform_admin": True,
        "workspace": "personal",
    }  # fmt: skip


def test_looking_in_changes_nothing(platform: Platform) -> None:
    looking = platform.looking_at(platform.asha_ws)
    doc = platform.asha_doc
    attempts = [
        ("POST", "/v1/documents"), ("DELETE", f"/v1/documents/{doc}"),
        ("POST", f"/v1/documents/{doc}/reprocess"), ("POST", f"/v1/documents/{doc}/claim"),
        ("DELETE", f"/v1/documents/{doc}/claim"), ("POST", f"/v1/documents/{doc}/corrections"),
        ("POST", f"/v1/documents/{doc}/review"), ("POST", "/v1/chat"), ("POST", "/v1/chat/stream"),
        ("POST", "/v1/collections"), ("PATCH", f"/v1/collections/{uuid.uuid4()}"),
        ("DELETE", "/v1/sessions/current"), ("POST", "/v1/api-keys"), ("POST", "/v1/webhooks"),
        ("POST", "/v1/mcp"), ("POST", "/v1/sessions"), ("POST", "/v1/accounts"),
    ]  # fmt: skip
    for method, path in attempts:
        response = platform.api.request(method, path, headers=looking, json={})
        assert response.status_code == 403, (method, path, response.status_code)
    # A person's review screen and the administration are not read-only views.
    for path in ("/v1/review/queue", f"/v1/documents/{doc}/review", "/v1/audit"):
        assert platform.api.get(path, headers=looking).status_code == 403, path
    # The administrator's own session still works without the header.
    assert platform.api.get("/v1/documents", headers=platform.admin).status_code == 200


def test_every_change_is_refused_while_looking_in(platform: Platform) -> None:
    looking = platform.looking_at(platform.asha_ws)
    every = routes(platform.stack.app) | {("POST", "/v1/mcp")}
    for method, path in sorted(every):
        if method in ("GET", "HEAD"):
            continue
        response = platform.api.request(method, filled(path), headers=looking, json={})
        assert response.status_code == 403, (method, path)


def test_looking_in_is_audited_in_that_workspace_once_an_hour(
    platform: Platform, owner_engine: Engine
) -> None:
    looking = platform.looking_at(platform.asha_ws)
    platform.api.get("/v1/documents", headers=looking)
    platform.api.get(f"/v1/documents/{platform.asha_doc}", headers=looking)

    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT tenant_id, actor FROM audit_log WHERE action = 'platform.workspace_viewed'"
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].tenant_id == platform.asha_ws and rows[0].actor.startswith("reviewer:")


def test_the_header_from_anyone_else_is_refused_not_ignored(platform: Platform) -> None:
    key = {"Authorization": f"Bearer {platform.stack.key(DEFAULT_TENANT_ID, 'admin')}"}
    for who in (platform.asha, platform.ben, platform.org_admin, key):
        looking = platform.looking_at(platform.asha_ws, who)
        assert platform.api.get("/v1/documents", headers=looking).status_code == 403
        assert platform.api.get("/v1/sessions/current", headers=looking).status_code == 403
    # Not even into its own workspace.
    own = platform.looking_at(platform.asha_ws, platform.asha)
    assert platform.api.get(f"/v1/documents/{platform.asha_doc}", headers=own).status_code == 403
    # Nor to the agents' server, which reads keys on its own.
    looking = platform.looking_at(platform.asha_ws)
    assert platform.api.get("/v1/mcp", headers=looking).status_code == 403
    # Without a credential the header changes nothing about the 401.
    bare = {HEADER: str(platform.asha_ws)}
    assert platform.api.get("/v1/documents", headers=bare).status_code in (401, 403)


def test_an_unknown_workspace_is_not_found(platform: Platform) -> None:
    unknown = platform.looking_at(uuid.uuid4())
    malformed = {**platform.admin, HEADER: "not-a-uuid"}

    assert platform.api.get("/v1/documents", headers=unknown).status_code == 404
    assert platform.api.get("/v1/documents", headers=malformed).status_code == 404


def test_the_administrator_does_not_read_a_members_conversations(platform: Platform) -> None:
    asked = platform.api.post("/v1/chat", headers=platform.asha, json={"question": "Total?"})
    conversation = asked.json()["conversation_id"]
    looking = platform.looking_at(platform.asha_ws)

    assert platform.api.get("/v1/conversations", headers=looking).json() == []
    assert platform.api.get(f"/v1/conversations/{conversation}", headers=looking).status_code == 404
