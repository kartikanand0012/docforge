"""Two self-service accounts, Asha and Ben, each in a private workspace: nothing Ben can call
tells him anything about Asha's documents - not that they exist, not their contents, not
their names - and nothing he can call changes them."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from test_accounts import filled, routes
from test_chat import quoting

from docforge.chat.service import DocumentNotFound as ChatDocumentNotFound
from docforge.collections import CollectionNotFound
from docforge.db.session import SessionFactory
from docforge.documents import DocumentNotFound
from full_stack import PASSWORD, FullStack
from worlds import World

pytestmark = [pytest.mark.integration, pytest.mark.no_ambient_tenant]

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


class Two:
    def __init__(self, stack: FullStack, owner_engine: Engine) -> None:
        self.stack, self.api = stack, stack.client
        self.asha = stack.sign_up("Asha", "asha@example.com")
        self.ben = stack.sign_up("Ben", "ben@example.com")
        self.asha_ws = FullStack.workspace_of(owner_engine, "asha@example.com")
        self.ben_ws = FullStack.workspace_of(owner_engine, "ben@example.com")
        stack.read(self.asha_ws, "purchase_order")
        self.doc = stack.read(self.asha_ws, "invoice")
        raw = stack.world.invoice_raw
        self.batch = raw["lines"][0]["batch_no"]["text"]
        self.invoice_no = raw["invoice_no"]["text"]
        stack.model.reply = quoting(self.batch, f"Batch {self.batch} is on this invoice.")
        asked = self.api.post(
            "/v1/chat", headers=self.asha,
            json={"question": f"Which invoice billed batch {self.batch}?"},
        )  # fmt: skip
        assert asked.status_code == 200 and asked.json()["citations"], asked.text
        self.conversation = asked.json()["conversation_id"]
        kb = self.api.post("/v1/collections", headers=self.asha, json={"name": "Asha's"})
        self.kb = kb.json()["id"]
        added = self.api.post(
            f"/v1/collections/{self.kb}/documents", headers=self.asha,
            json={"document_ids": [str(self.doc)]},
        )  # fmt: skip
        assert added.status_code == 200, added.text
        stack.model.reply = lambda passages: {"statements": [], "unanswerable": True}

    def secrets(self) -> list[str]:
        """What must never reach Ben: Asha's document's id, values, and names."""
        return [str(self.doc), self.batch, self.invoice_no, str(self.asha_ws), "Asha"]

    def clean(self, response: Any) -> bool:
        return not any(secret in response.text for secret in self.secrets())


@pytest.fixture
def two(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
    owner_engine: Engine,
) -> Two:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    world.build()
    return Two(FullStack(sessions, world), owner_engine)


def test_ben_lists_nothing_of_ashas(two: Two) -> None:
    for path in ("/v1/documents", "/v1/review/queue", "/v1/collections", "/v1/conversations",
                 "/v1/exports/documents.json", "/v1/exports/documents.csv", "/v1/stats",
                 "/v1/sessions/current", "/v1/audit/verification"):  # fmt: skip
        response = two.api.get(path, headers=two.ben)
        assert response.status_code == 200, path
        assert two.clean(response), path
    assert two.api.get("/v1/documents", headers=two.ben).json()["items"] == []
    assert two.api.get("/v1/stats", headers=two.ben).json()["documents"] == 0


def test_every_route_about_ashas_document_is_not_found_for_ben(two: Two) -> None:
    about_a_document = [
        (method, path) for method, path in routes(two.stack.app) if "{document_id}" in path
    ]
    assert len(about_a_document) >= 14

    secret = {"email": "ben@example.com", "pin": PASSWORD}
    bodies: dict[tuple[str, str], dict[str, Any]] = {
        ("POST", "/corrections"): {"path": "invoice_no", "text": "X", "reason": "r", **secret},
        ("POST", "/review"): {
            "outcome": "approved", "meaning": "m", "reason": "r",
            "expected_record_sha256": "0" * 64, **secret,
        },
        ("POST", "/claim"): {},
    }  # fmt: skip
    for method, path in about_a_document:
        url = path.replace("{document_id}", str(two.doc)).replace("{page}", "1")
        url = url.replace("{collection_id}", two.kb)
        body = next(
            (b for (m, end), b in bodies.items() if m == method and path.endswith(end)), None
        )
        response = two.api.request(method, url, headers=two.ben, json=body)
        assert response.status_code == 404, (method, path, response.status_code, response.text)
        assert two.clean(response), (method, path)
    # And it is all still there for Asha.
    assert two.api.get(f"/v1/documents/{two.doc}", headers=two.asha).status_code == 200
    review = two.api.get(f"/v1/documents/{two.doc}/review", headers=two.asha).json()
    assert review["corrections"] == [] and review["review"] is None


def test_ben_cannot_search_or_ask_his_way_to_it(two: Two) -> None:
    found = two.api.get("/v1/search", headers=two.ben, params={"q": two.batch})
    assert found.status_code == 200 and found.json()["results"] == []
    assert found.json()["query"] == two.batch  # his own words back, and nothing else

    two.stack.model.requests.clear()
    asked = two.api.post("/v1/chat", headers=two.ben, json={"question": f"Batch {two.batch}?"})
    assert asked.status_code == 200 and asked.json()["citations"] == []
    assert not any("invoice.pdf" in r.prompt for r in two.stack.model.requests)

    by_id = {"question": "Total?", "document_id": str(two.doc)}
    in_kb = {"question": "Total?", "collection_id": two.kb}
    follow_up = {"question": "And?", "conversation_id": two.conversation}
    for body in (by_id, in_kb, follow_up):
        response = two.api.post("/v1/chat", headers=two.ben, json=body)
        assert response.status_code == 404, body
        streamed = two.api.post("/v1/chat/stream", headers=two.ben, json=body)
        assert "Not found." in streamed.text and two.clean(streamed)


def test_ben_cannot_reach_ashas_conversation_or_knowledge_base(two: Two) -> None:
    conversation = f"/v1/conversations/{two.conversation}"
    assert two.api.get(conversation, headers=two.ben).status_code == 404
    assert two.api.delete(conversation, headers=two.ben).status_code == 404
    kb = f"/v1/collections/{two.kb}"
    assert two.api.get(f"{kb}/documents", headers=two.ben).status_code == 404
    assert two.api.patch(kb, headers=two.ben, json={"name": "Mine"}).status_code == 404
    assert two.api.delete(kb, headers=two.ben).status_code == 404
    assert two.api.delete(f"{kb}/documents/{two.doc}", headers=two.ben).status_code == 404
    # His own knowledge base cannot take her document.
    mine = two.api.post("/v1/collections", headers=two.ben, json={"name": "Ben's"}).json()["id"]
    taken = two.api.post(
        f"/v1/collections/{mine}/documents", headers=two.ben,
        json={"document_ids": [str(two.doc)]},
    )  # fmt: skip
    assert taken.status_code == 404
    # Asha's conversation and knowledge base are untouched.
    assert two.api.get(conversation, headers=two.asha).status_code == 200
    assert len(two.api.get(f"{kb}/documents", headers=two.asha).json()) == 1


def test_the_same_file_uploaded_by_ben_is_his_own_new_document(two: Two) -> None:
    response = two.api.post(
        "/v1/documents", headers=two.ben,
        files={"file": ("mine.pdf", two.stack.world.invoice_pdf, "application/pdf")},
        data={"doc_type": "invoice"},
    )  # fmt: skip

    # Not "already known": that would tell him someone else holds this file.
    assert response.status_code == 202 and response.json()["created"] is True
    assert response.json()["document"]["id"] != str(two.doc)


def test_the_services_agents_use_find_nothing_of_ashas_for_ben(two: Two) -> None:
    stack = two.stack
    with pytest.raises(DocumentNotFound):
        stack.documents.detail(two.ben_ws, two.doc)
    assert stack.documents.latest_extraction is not None
    with pytest.raises(DocumentNotFound):
        stack.documents.latest_extraction(two.ben_ws, two.doc)
    assert stack.documents.list_documents(two.ben_ws) == []
    assert list(stack.search.search(two.ben_ws, two.batch)) == []
    with pytest.raises(CollectionNotFound):
        stack.collections.documents(two.ben_ws, uuid.UUID(two.kb))
    with pytest.raises(ChatDocumentNotFound):
        stack.chat.ask(two.ben_ws, "reviewer:x", "Total?", document_id=two.doc)


def test_bens_password_does_not_open_ashas_account(two: Two) -> None:
    ben_secret = {"email": "asha@example.com", "password": PASSWORD + "!"}
    assert two.api.post("/v1/sessions", json=ben_secret).status_code == 401
    # Naming her workspace changes nothing either.
    with_tenant = {**ben_secret, "tenant": "default"}
    assert two.api.post("/v1/sessions", json=with_tenant).status_code == 401


def test_ben_is_refused_every_administrators_and_platform_route(two: Two) -> None:
    from test_accounts import ADMIN_ROUTES

    for method, path in sorted(ADMIN_ROUTES):
        response = two.api.request(method, filled(path), headers=two.ben, json={})
        assert response.status_code == 403, (method, path)
        assert two.clean(response)
    looking = {**two.ben, "X-DocForge-Workspace": str(two.asha_ws)}
    for method, path in sorted(routes(two.stack.app)):
        response = two.api.request(method, filled(path), headers=looking, json={})
        assert response.status_code in (403, 401), (method, path, response.status_code)
        assert two.clean(response), (method, path)
