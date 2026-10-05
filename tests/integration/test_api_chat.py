"""The chat endpoints: ask a question, read a conversation back, list one's conversations."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docforge.api.app import create_app
from docforge.chat.service import ChatService
from docforge.db.session import SessionFactory
from docforge.llm.base import LLMError, LLMRequest, LLMResponse
from docforge.search.embeddings import FakeEmbedder
from docforge.search.service import SearchHit, SearchHits, SearchService
from fakes import signed_in

pytestmark = pytest.mark.integration


class Unanswering:
    name = "fake"
    model = "fake-chat-1"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def generate(self, request: LLMRequest) -> LLMResponse:
        if self.fail:
            raise LLMError("provider down")
        return LLMResponse(
            text='{"answer": "No.", "citations": [], "unanswerable": true}',
            provider="fake", model=self.model, input_tokens=10, output_tokens=5, latency_ms=1.0,
        )  # fmt: skip


class OnePassage:
    """A search that always finds one passage, so the model is always asked."""

    def search(self, tenant_id: uuid.UUID, query: str, **kwargs: Any) -> SearchHits:
        hit = SearchHit(
            document_id=uuid.uuid4(), doc_type="general", filename="sop.docx", kind="text",
            page=1, text="Anything above 8 °C is rejected.", score=1.0, boxes=(),
        )  # fmt: skip
        return SearchHits([hit])


def client(
    sessions: SessionFactory,
    *,
    fail: bool = False,
    limit: int = 100,
    search: Any = None,
    **who: Any,
) -> TestClient:
    found = search or SearchService(sessions, FakeEmbedder())
    chat = ChatService(sessions, found, Unanswering(fail), daily_limit=limit)
    return TestClient(signed_in(create_app(None, chat=chat), **who))


def test_a_question_is_answered_and_its_conversation_read_back(sessions: SessionFactory) -> None:
    api = client(sessions, role="reviewer", reviewer_id=uuid.uuid4())

    answer = api.post("/v1/chat", json={"question": "What is the total?"})

    assert answer.status_code == 200
    body = answer.json()
    assert body["status"] == "not_found" and body["citations"] == []
    conversation = api.get(f"/v1/conversations/{body['conversation_id']}").json()
    assert [m["question"] for m in conversation["messages"]] == ["What is the total?"]
    listed = api.get("/v1/conversations").json()
    assert [c["id"] for c in listed] == [body["conversation_id"]]


@pytest.mark.parametrize("question", ["", " ", "x" * 2001])
def test_an_empty_or_overlong_question_is_refused(sessions: SessionFactory, question: str) -> None:
    assert client(sessions).post("/v1/chat", json={"question": question}).status_code == 422


def test_a_question_about_a_document_that_does_not_exist_is_not_found(
    sessions: SessionFactory,
) -> None:
    response = client(sessions).post(
        "/v1/chat", json={"question": "What is it?", "document_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404


def test_someone_elses_conversation_is_not_found(sessions: SessionFactory) -> None:
    mine = client(sessions, role="reviewer", reviewer_id=uuid.uuid4())
    theirs = client(sessions, role="reviewer", reviewer_id=uuid.uuid4())
    conversation_id = mine.post("/v1/chat", json={"question": "Total?"}).json()["conversation_id"]

    assert theirs.get(f"/v1/conversations/{conversation_id}").status_code == 404
    follow_up = {"question": "More?", "conversation_id": conversation_id}
    assert theirs.post("/v1/chat", json=follow_up).status_code == 404


def test_the_daily_limit_is_429_and_a_failed_model_is_503(sessions: SessionFactory) -> None:
    limited = client(sessions, limit=1)
    assert limited.post("/v1/chat", json={"question": "One?"}).status_code == 200
    assert limited.post("/v1/chat", json={"question": "Two?"}).status_code == 429

    failing = client(sessions, fail=True, search=OnePassage())
    response = failing.post("/v1/chat", json={"question": "Anything?"})
    assert response.status_code == 503
    assert response.json()["detail"] == "The model could not answer just now. Try again shortly."


def test_chat_is_absent_without_a_chat_service(sessions: SessionFactory) -> None:
    api = TestClient(signed_in(create_app(None)))
    assert api.post("/v1/chat", json={"question": "Hi?"}).status_code == 404


def test_a_conversation_can_be_deleted_by_its_owner(sessions: SessionFactory) -> None:
    api = client(sessions, role="reviewer", reviewer_id=uuid.uuid4())
    conversation_id = api.post("/v1/chat", json={"question": "Total?"}).json()["conversation_id"]

    assert api.delete(f"/v1/conversations/{conversation_id}").status_code == 204
    assert api.get(f"/v1/conversations/{conversation_id}").status_code == 404


def test_a_follow_up_naming_another_document_is_refused(sessions: SessionFactory) -> None:
    api = client(sessions, role="reviewer", reviewer_id=uuid.uuid4())
    conversation_id = api.post("/v1/chat", json={"question": "Total?"}).json()["conversation_id"]

    follow_up = {"question": "More?", "conversation_id": conversation_id, "document_id": str(uuid.uuid4())}
    assert api.post("/v1/chat", json=follow_up).status_code == 422
