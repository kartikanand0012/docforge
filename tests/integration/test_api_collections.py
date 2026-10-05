"""Knowledge base endpoints, and questions within one; the chat stream of stages."""

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_api_chat import OnePassage, Unanswering

from docforge.api.app import create_app
from docforge.chat.service import ChatService
from docforge.collections import CollectionService
from docforge.db.session import SessionFactory
from docforge.search.embeddings import FakeEmbedder
from docforge.search.service import SearchService
from fakes import signed_in

pytestmark = pytest.mark.integration


def client(sessions: SessionFactory, *, search: Any = None, **who: Any) -> TestClient:
    found = search or SearchService(sessions, FakeEmbedder())
    chat = ChatService(sessions, found, Unanswering())
    app = create_app(None, chat=chat, collections=CollectionService(sessions))
    return TestClient(signed_in(app, **who))


def test_knowledge_bases_are_created_listed_renamed_and_deleted(sessions: SessionFactory) -> None:
    api = client(sessions, role="admin")

    created = api.post("/v1/collections", json={"name": "SOPs", "description": "Procedures"})
    assert created.status_code == 201
    kb = created.json()
    assert api.post("/v1/collections", json={"name": "sops"}).status_code == 409
    assert [c["name"] for c in api.get("/v1/collections").json()] == ["SOPs"]

    renamed = api.patch(f"/v1/collections/{kb['id']}", json={"name": "Procedures"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Procedures"
    assert api.delete(f"/v1/collections/{kb['id']}").status_code == 204
    assert api.get("/v1/collections").json() == []


def test_documents_that_do_not_exist_are_refused_by_name(sessions: SessionFactory) -> None:
    api = client(sessions, role="admin")
    kb = api.post("/v1/collections", json={"name": "SOPs"}).json()

    response = api.post(
        f"/v1/collections/{kb['id']}/documents", json={"document_ids": [str(uuid.uuid4())]}
    )

    assert response.status_code == 404
    assert api.get(f"/v1/collections/{kb['id']}/documents").json() == []


def test_a_reviewer_reads_and_asks_but_does_not_change_knowledge_bases(
    sessions: SessionFactory,
) -> None:
    admin = client(sessions, role="admin")
    kb = admin.post("/v1/collections", json={"name": "SOPs"}).json()
    reviewer = client(sessions, role="reviewer", reviewer_id=uuid.uuid4())

    assert reviewer.get("/v1/collections").status_code == 200
    assert reviewer.post("/v1/collections", json={"name": "Mine"}).status_code == 403
    assert reviewer.delete(f"/v1/collections/{kb['id']}").status_code == 403
    asked = reviewer.post("/v1/chat", json={"question": "Total?", "collection_id": kb["id"]})
    assert asked.status_code == 200 and asked.json()["status"] == "not_found"


def test_an_unknown_knowledge_base_is_not_found(sessions: SessionFactory) -> None:
    api = client(sessions, role="admin")
    missing = str(uuid.uuid4())
    assert api.get(f"/v1/collections/{missing}/documents").status_code == 404
    assert (
        api.post("/v1/chat", json={"question": "Hi?", "collection_id": missing}).status_code == 404
    )


def test_the_chat_stream_says_each_stage_then_gives_the_checked_answer(
    sessions: SessionFactory,
) -> None:
    api = client(sessions, search=OnePassage(), role="admin")

    with api.stream("POST", "/v1/chat/stream", json={"question": "What is rejected?"}) as stream:
        assert stream.headers["content-type"].startswith("text/event-stream")
        body = "".join(stream.iter_text())

    events = [
        (
            block.split("\n")[0].removeprefix("event: "),
            json.loads(block.split("\n")[1].removeprefix("data: ")),
        )
        for block in body.strip().split("\n\n")
    ]
    assert [name for name, _ in events] == ["stage", "stage", "stage", "answer"]
    assert [data["stage"] for name, data in events if name == "stage"] == [
        "searching",
        "reading",
        "checking",
    ]
    assert events[1][1]["passages"] == 1
    assert events[-1][1]["status"] == "not_found"


def test_a_stream_that_cannot_be_answered_ends_with_an_error_event(
    sessions: SessionFactory,
) -> None:
    search = SearchService(sessions, FakeEmbedder())
    chat = ChatService(sessions, OnePassage(), Unanswering(fail=True))
    api = TestClient(
        signed_in(create_app(None, chat=chat, collections=CollectionService(sessions)))
    )

    with api.stream("POST", "/v1/chat/stream", json={"question": "Anything?"}) as stream:
        body = "".join(stream.iter_text())

    last = body.strip().split("\n\n")[-1]
    assert last.startswith("event: error")
    assert "The model could not answer just now" in last
    assert search is not None


def test_one_person_has_a_few_questions_in_flight_at_most(
    sessions: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from docforge.api import chat as chat_api

    monkeypatch.setattr(chat_api, "MAX_IN_FLIGHT_PER_CALLER", 0)
    api = client(sessions, role="admin")

    assert api.post("/v1/chat", json={"question": "Total?"}).status_code == 429
    assert api.post("/v1/chat/stream", json={"question": "Total?"}).status_code == 429


def test_two_api_processes_share_the_cap_on_questions_in_flight(
    sessions: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from docforge.api import chat as chat_api
    from docforge.limits import DatabaseLimits

    monkeypatch.setattr(chat_api, "MAX_IN_FLIGHT_PER_CALLER", 1)
    limits = DatabaseLimits(sessions)
    from docforge.db import DEFAULT_TENANT_ID

    caller = f"chat:{DEFAULT_TENANT_ID}:key:{uuid.UUID(int=1)}"
    held = limits.hold(caller, at_most=1, seconds=60)  # this caller's question, elsewhere
    held.__enter__()
    try:
        search = SearchService(sessions, FakeEmbedder())
        app = create_app(
            None, chat=ChatService(sessions, search, Unanswering()), limits=DatabaseLimits(sessions)
        )
        api = TestClient(signed_in(app, role="admin"))
        assert api.post("/v1/chat", json={"question": "Total?"}).status_code == 429
    finally:
        held.__exit__(None, None, None)
