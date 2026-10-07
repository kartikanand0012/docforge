"""The audit log as administrators read it: filtered, paged, with names, and exported.

The log keeps ids only; names (a reviewer's, a key's, a document's filename) are looked up when
read, so they stay erasable. Only the details an action allows are shown. Reading never changes
the log; exporting it is itself logged.
"""

import csv
import io
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docforge import audit
from docforge.api.app import create_app
from docforge.audit_log import AuditLogService, Filters
from docforge.auth import Authenticator
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.limits import LocalLimits
from docforge.review.service import ReviewService
from docforge.storage import MemoryObjectStore
from fakes import signed_in
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


class Setup:
    def __init__(self, sessions: SessionFactory, owner: SessionFactory, world: World) -> None:
        self.sessions, self.owner, self.world = sessions, owner, world
        # document.received, processing.started ... appended by the worker
        self.invoice_id = world.process("invoice")
        people = ReviewService(sessions, MemoryObjectStore(), {})
        self.reviewer_id = people.add_reviewer(
            DEFAULT_TENANT_ID, name="Asha Rao", email="asha@example.com", pin="482913"
        )
        # api_key.created, by the command line
        self.key = Authenticator(sessions).create_api_key(
            DEFAULT_TENANT_ID, name="ERP sync", role="integrator"
        )
        self.log = AuditLogService(sessions)

    def append(self, action: str, actor: str, **details: Any) -> None:
        with self.owner.begin() as session:
            audit.append(
                session,
                tenant_id=DEFAULT_TENANT_ID,
                actor=actor,
                action=action,
                target_type="document",
                target_id=str(self.invoice_id),
                details=details,
            )


@pytest.fixture
def setup(
    sessions: SessionFactory,
    owner_sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> Setup:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    return Setup(sessions, owner_sessions, world)


def test_entries_come_newest_first_with_names_and_only_allowed_details(setup: Setup) -> None:
    reviewer = f"reviewer:{setup.reviewer_id}"
    setup.append(
        "review.signed", reviewer, outcome="approved", version_no=1,
        record_sha256="ab" * 32, overridden=False, unexpected="x",
    )  # fmt: skip

    page = setup.log.entries(DEFAULT_TENANT_ID, Filters())

    newest = page.items[0]
    assert newest.action == "review.signed" and newest.action_label == "Review signed"
    assert newest.actor_name == "Asha Rao"
    assert newest.target_label is not None and newest.target_label.endswith(".pdf")
    assert newest.details == {
        "outcome": "approved", "version_no": 1, "record_sha256": "ab" * 32, "overridden": False,
    }  # fmt: skip
    assert newest.hidden_details == 1
    names = {item.actor: item.actor_name for item in page.items}
    assert names["system:worker"] == "DocForge (processing)"
    assert names["cli"] == "Command line"
    ids = [item.id for item in page.items]
    assert ids == sorted(ids, reverse=True)


def test_filters_narrow_alone_and_together(setup: Setup) -> None:
    reviewer = f"reviewer:{setup.reviewer_id}"
    setup.append("review.corrected", reviewer, path="invoice_no", version_no=1, changed=True)
    log, tenant = setup.log, DEFAULT_TENANT_ID

    corrected = log.entries(tenant, Filters(action="review.corrected")).items
    assert [e.action for e in corrected] == ["review.corrected"]
    worker = log.entries(tenant, Filters(actor="system:worker")).items
    assert worker and all(e.actor == "system:worker" for e in worker)
    keys = log.entries(tenant, Filters(target_type="api_key")).items
    assert [e.action for e in keys] == ["api_key.created"]
    both = log.entries(tenant, Filters(action="review.corrected", actor="system:worker")).items
    assert both == []
    now = datetime.now(UTC)
    assert log.entries(tenant, Filters(since=now + timedelta(days=1))).items == []
    assert log.entries(tenant, Filters(until=now - timedelta(days=1))).items == []
    window = Filters(since=now - timedelta(days=1), until=now + timedelta(days=1))
    assert len(log.entries(tenant, window).items) == len(log.entries(tenant, Filters()).items)


def test_pages_never_repeat_or_skip_while_entries_arrive(setup: Setup) -> None:
    log, tenant = setup.log, DEFAULT_TENANT_ID
    everything = [e.id for e in log.entries(tenant, Filters(), limit=200).items]
    first = log.entries(tenant, Filters(), limit=3)
    # A new entry between pages, newer than every one already listed.
    setup.append("review.corrected", f"reviewer:{setup.reviewer_id}", path="x", version_no=1)
    seen = [e.id for e in first.items]
    before = first.next_before
    while before is not None:
        page = log.entries(tenant, Filters(), before=before, limit=3)
        seen += [e.id for e in page.items]
        before = page.next_before
    assert seen == everything


def test_another_organisation_never_sees_these_entries(
    setup: Setup, other_tenant: uuid.UUID
) -> None:
    mine = setup.log.entries(DEFAULT_TENANT_ID, Filters()).items
    theirs = setup.log.entries(other_tenant, Filters(), before=mine[0].id + 1).items
    assert theirs == []


def test_the_filter_lists_offer_registered_actions_and_known_people_keys_and_system(
    setup: Setup,
) -> None:
    choices = setup.log.filters(DEFAULT_TENANT_ID)
    assert ("review.signed", "Review signed") in choices.actions
    actors = dict(choices.actors)
    assert actors[f"reviewer:{setup.reviewer_id}"] == "Asha Rao"
    assert any(name.startswith("ERP sync") for name in actors.values())
    assert actors["system:worker"] == "DocForge (processing)"


def test_an_export_is_the_filtered_view_formula_safe_and_itself_logged(setup: Setup) -> None:
    reviewer = f"reviewer:{setup.reviewer_id}"
    setup.append("review.corrected", reviewer, path="=HYPERLINK(1)", version_no=1, changed=True)
    body, truncated = setup.log.export(
        DEFAULT_TENANT_ID, Filters(action="review.corrected"), actor="key:1"
    )
    rows = list(csv.DictReader(io.StringIO(body)))
    assert not truncated and len(rows) == 1
    assert list(rows[0]) == [
        "id", "occurred_at", "actor", "actor_name", "action", "target_type", "target_id",
        "details", "prev_hash", "hash",
    ]  # fmt: skip
    assert rows[0]["details"].startswith("'")  # a formula is not a formula in a spreadsheet
    assert rows[0]["actor_name"] == "Asha Rao"
    exported = setup.log.entries(DEFAULT_TENANT_ID, Filters(action="audit.exported")).items
    assert [e.details for e in exported] == [{"rows": 1, "truncated": False, "filtered": True}]
    with setup.sessions() as session:
        assert audit.verify_chain(session, DEFAULT_TENANT_ID).consistent


def test_an_export_beyond_its_limit_says_it_was_cut(setup: Setup) -> None:
    body, truncated = AuditLogService(setup.sessions, export_limit=2).export(
        DEFAULT_TENANT_ID, Filters(), actor="key:1"
    )
    assert truncated and len(list(csv.DictReader(io.StringIO(body)))) == 2


# --- the API -----------------------------------------------------------------------------------


def api(setup: Setup, role: str = "admin", limits: LocalLimits | None = None) -> TestClient:
    app = create_app(
        None, service=setup.world.service, audit_log=setup.log, limits=limits or LocalLimits()
    )
    return TestClient(signed_in(app, role=role))


def test_only_administrators_read_the_log(setup: Setup) -> None:
    for role in ("reviewer", "integrator", "reader"):
        client = api(setup, role)
        assert client.get("/v1/audit").status_code == 403, role
        assert client.get("/v1/audit/filters").status_code == 403, role
        assert client.get("/v1/audit/export.csv").status_code == 403, role
    page = api(setup).get("/v1/audit", params={"action": "processing.started", "limit": 1})
    assert page.status_code == 200
    body = page.json()
    assert [item["action"] for item in body["items"]] == ["processing.started"]
    assert "next_before" in body


@pytest.mark.parametrize(
    "params", [{"before": "x"}, {"from": "yesterday"}, {"limit": 0}, {"limit": 201}]
)
def test_bad_filters_are_refused(setup: Setup, params: dict[str, Any]) -> None:
    assert api(setup).get("/v1/audit", params=params).status_code == 422


def test_the_export_downloads_with_its_name_and_says_when_cut(setup: Setup) -> None:
    response = api(setup).get("/v1/audit/export.csv", params={"action": "processing.started"})
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-docforge-truncated"] == "false"
    assert response.text.startswith("id,occurred_at,actor,")


def test_checking_the_chain_is_limited_per_organisation(setup: Setup) -> None:
    client = api(setup, limits=LocalLimits())
    codes = [client.get("/v1/audit/verification").status_code for _ in range(7)]
    assert codes[:6] == [200] * 6 and codes[6] == 429
