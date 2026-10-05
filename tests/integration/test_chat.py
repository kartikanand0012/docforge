"""Chat with documents: answers from retrieved passages, every citation checked against the
passage it names, "not in these documents" when they do not say, per tenant and per person."""

import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from docforge.chat.service import (
    ChatService,
    ConversationNotFound,
    QuestionLimitReached,
)
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.llm.base import LLMError, LLMRequest, LLMResponse
from docforge.parsing.cache import CachingParser
from docforge.search.embeddings import FakeEmbedder
from docforge.search.service import SearchService
from worlds import World

pytestmark = pytest.mark.integration

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "recorded" / "parsed"
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
_PASSAGE = re.compile(r'<passage n="(\d+)"[^>]*>\n(.*?)\n</passage>', re.S)


class Model:
    """Answers by quoting the passage that holds `target`; the reply can be set per test."""

    name = "fake"
    model = "fake-chat-1"

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []
        self.reply: Callable[[dict[int, str]], dict[str, Any] | str] = lambda passages: {
            "answer": "Not stated.", "citations": [], "unanswerable": True,
        }  # fmt: skip

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        passages = {int(n): text for n, text in _PASSAGE.findall(request.prompt)}
        reply = self.reply(passages)
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return LLMResponse(
            text=text, provider="fake", model=self.model, input_tokens=1000,
            output_tokens=100, latency_ms=5.0,
        )  # fmt: skip


def quoting(
    target: str, answer: str, *, also: str | None = None
) -> Callable[[dict[int, str]], dict[str, Any]]:
    def reply(passages: dict[int, str]) -> dict[str, Any]:
        holding = [n for n, text in passages.items() if target in text]
        # A printed table row (cells joined by "|") rather than the generated summary.
        n = next((n for n in holding if "|" in passages[n]), holding[0])
        citations = [{"passage": n, "quote": target}]
        if also is not None:
            citations.append({"passage": n, "quote": also})
        return {"answer": answer, "citations": citations, "unanswerable": False}

    return reply


class Setup:
    def __init__(
        self, world: World, search: SearchService, model: Model, chat: ChatService
    ) -> None:
        self.world, self.search, self.model, self.chat = world, search, model, chat
        self.invoice_id = world.process("invoice")
        self.order_id = world.process("purchase_order")
        for document_id in (self.invoice_id, self.order_id):
            search.index_document(DEFAULT_TENANT_ID, document_id)
        self.batch = world.invoice_raw["lines"][0]["batch_no"]["text"]
        self.invoice_no = world.invoice_raw["invoice_no"]["text"]

    def ask(self, question: str, **kwargs: Any) -> Any:
        return self.chat.ask(DEFAULT_TENANT_ID, "reviewer:a", question, **kwargs)


@pytest.fixture
def setup(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> Setup:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    world.invoice_parsed = CachingParser(RECORDED).parse(world.invoice_pdf)
    search = SearchService(sessions, FakeEmbedder())
    model = Model()
    return Setup(world, search, model, ChatService(sessions, search, model, daily_limit=5))


def test_an_answer_cites_the_passage_and_page_it_comes_from(setup: Setup) -> None:
    setup.model.reply = quoting(setup.batch, f"Batch {setup.batch} is on this invoice.")

    answer = setup.ask(f"Which invoice billed batch {setup.batch}?")

    assert answer.status == "supported"
    assert answer.text == f"Batch {setup.batch} is on this invoice."
    (citation,) = answer.citations
    assert citation.document_id == setup.invoice_id
    assert citation.quote == setup.batch and citation.page == 1
    assert citation.boxes and all(box["page"] == 1 for box in citation.boxes)
    assert all(box["page_width"] > 0 and box["page_height"] > 0 for box in citation.boxes)


def test_when_the_documents_do_not_say_it_says_so(setup: Setup) -> None:
    answer = setup.ask("What is the supplier's bank account number?")

    assert answer.status == "not_found"
    assert answer.text == "The documents do not say."
    assert answer.citations == ()


def test_an_answer_whose_quotes_are_not_in_the_passages_is_withheld(setup: Setup) -> None:
    setup.model.reply = lambda passages: {
        "answer": "The total is 99,999.00.",
        "citations": [{"passage": 1, "quote": "Grand total 99,999.00 approved"}],
        "unanswerable": False,
    }

    answer = setup.ask("What is the grand total?")

    assert answer.status == "unsupported"
    assert "99,999" not in answer.text
    assert answer.citations == ()


def test_a_citation_naming_a_passage_that_was_not_given_is_not_a_citation(setup: Setup) -> None:
    setup.model.reply = lambda passages: {
        "answer": "Yes.", "citations": [{"passage": 99, "quote": setup.invoice_no}],
        "unanswerable": False,
    }  # fmt: skip

    assert setup.ask("Is there an invoice?").status == "unsupported"


def test_an_answer_with_one_good_and_one_made_up_quote_says_it_is_partly_supported(
    setup: Setup,
) -> None:
    setup.model.reply = quoting(
        setup.invoice_no, "It is that one.", also="approved by the director"
    )

    answer = setup.ask(f"Which invoice billed batch {setup.batch}?")

    assert answer.status == "partly_supported"
    assert [c.quote for c in answer.citations] == [setup.invoice_no]
    assert answer.dropped_citations == 1


def test_a_question_about_one_document_only_reads_that_document(setup: Setup) -> None:
    setup.model.reply = quoting(setup.invoice_no, "Yes.")
    setup.ask("What is the invoice number?", document_id=setup.invoice_id)

    prompt = setup.model.requests[-1].prompt
    assert 'document="invoice.pdf"' in prompt and 'document="purchase_order.pdf"' not in prompt


def test_a_follow_up_is_asked_with_the_conversation_so_far(setup: Setup) -> None:
    setup.model.reply = quoting(setup.invoice_no, f"It is {setup.invoice_no}.")
    first = setup.ask(f"Which invoice billed batch {setup.batch}?")

    second = setup.ask("And who issued it?", conversation_id=first.conversation_id)

    assert second.conversation_id == first.conversation_id
    prompt = setup.model.requests[-1].prompt
    assert "<conversation>" in prompt and f"Which invoice billed batch {setup.batch}?" in prompt
    messages = setup.chat.conversation(DEFAULT_TENANT_ID, "reviewer:a", first.conversation_id)
    assert [m.question for m in messages] == [
        f"Which invoice billed batch {setup.batch}?", "And who issued it?",
    ]  # fmt: skip


def test_a_conversation_is_only_its_owners(setup: Setup, other_tenant: uuid.UUID) -> None:
    setup.model.reply = quoting(setup.invoice_no, "Yes.")
    first = setup.ask("What is the invoice number?")

    with pytest.raises(ConversationNotFound):
        setup.chat.conversation(DEFAULT_TENANT_ID, "reviewer:b", first.conversation_id)
    with pytest.raises(ConversationNotFound):
        setup.chat.conversation(other_tenant, "reviewer:a", first.conversation_id)
    with pytest.raises(ConversationNotFound):
        setup.chat.ask(other_tenant, "reviewer:a", "More?", conversation_id=first.conversation_id)


def test_another_organisation_gets_nothing_from_these_documents(
    setup: Setup, other_tenant: uuid.UUID
) -> None:
    answer = setup.chat.ask(
        other_tenant, "reviewer:x", f"Which invoice billed batch {setup.batch}?"
    )

    assert answer.status == "not_found"
    assert setup.model.requests == []  # nothing to read, so no model call


def test_the_cost_and_model_of_each_answer_are_recorded(setup: Setup) -> None:
    setup.model.reply = quoting(setup.invoice_no, "Yes.")
    answer = setup.ask("What is the invoice number?")

    (message,) = setup.chat.conversation(DEFAULT_TENANT_ID, "reviewer:a", answer.conversation_id)
    assert message.model == "fake-chat-1"
    assert (message.input_tokens, message.output_tokens) == (1000, 100)
    assert message.status == "supported"


def test_an_organisation_has_a_daily_number_of_questions(setup: Setup) -> None:
    for _ in range(5):
        setup.ask("What is the supplier's bank account number?")

    with pytest.raises(QuestionLimitReached):
        setup.ask("One more?")


def test_a_malformed_reply_is_asked_again_once_then_fails(setup: Setup) -> None:
    replies = iter(["not json", json.dumps({"answer": "x", "citations": [], "unanswerable": True})])
    setup.model.reply = lambda passages: next(replies)
    assert setup.ask("What is the invoice number?").status == "not_found"

    setup.model.reply = lambda passages: "still not json"
    with pytest.raises(LLMError):
        setup.ask("What is the invoice number?")


# --- review findings (C11) ---------------------------------------------------------------


def test_questions_that_fail_still_count_toward_the_daily_limit(setup: Setup) -> None:
    def down(passages: dict[int, str]) -> str:
        raise LLMError("provider down")

    setup.model.reply = down
    for _ in range(5):
        with pytest.raises(LLMError):
            setup.ask("What is the invoice number?")

    with pytest.raises(QuestionLimitReached):
        setup.ask("And now?")


def test_a_malformed_answer_keeps_the_tokens_it_cost(setup: Setup) -> None:
    setup.model.reply = lambda passages: "not json"
    with pytest.raises(LLMError):
        setup.ask("What is the invoice number?")

    (conversation,) = setup.chat.conversations(DEFAULT_TENANT_ID, "reviewer:a")
    (message,) = setup.chat.conversation(DEFAULT_TENANT_ID, "reviewer:a", conversation.id)
    assert message.status == "error"
    assert (message.input_tokens, message.output_tokens) == (2000, 200)  # both calls


def test_the_limit_holds_when_questions_arrive_together(setup: Setup) -> None:
    from concurrent.futures import ThreadPoolExecutor

    def ask(_: int) -> str:
        try:
            setup.ask("What is the supplier's bank account number?")
            return "answered"
        except QuestionLimitReached:
            return "refused"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(ask, range(8)))

    assert outcomes.count("answered") == 5 and outcomes.count("refused") == 3


def test_one_person_cannot_use_up_the_organisations_day(
    sessions: SessionFactory, setup: Setup
) -> None:
    chat = ChatService(
        sessions, setup.search, setup.model, daily_limit=10, daily_limit_per_person=2
    )
    for _ in range(2):
        chat.ask(DEFAULT_TENANT_ID, "reviewer:a", "Bank account?")
    with pytest.raises(QuestionLimitReached):
        chat.ask(DEFAULT_TENANT_ID, "reviewer:a", "Bank account?")

    assert chat.ask(DEFAULT_TENANT_ID, "reviewer:b", "Bank account?").status == "not_found"


def test_answers_that_were_not_answers_are_not_given_as_history(setup: Setup) -> None:
    first = setup.ask("What is the supplier's bank account number?")  # not found
    setup.ask("And the account holder?", conversation_id=first.conversation_id)

    prompt = setup.model.requests[-1].prompt
    assert "The documents do not say." not in prompt
    assert "What is the supplier's bank account number?" in prompt


def test_a_follow_up_cannot_switch_to_another_document(setup: Setup) -> None:
    from docforge.chat.service import ScopeConflict

    setup.model.reply = quoting(setup.invoice_no, "Yes.")
    first = setup.ask("What is the invoice number?", document_id=setup.invoice_id)

    with pytest.raises(ScopeConflict):
        setup.ask(
            "And this one?", conversation_id=first.conversation_id, document_id=setup.order_id
        )


def test_a_person_can_delete_their_conversation(setup: Setup) -> None:
    first = setup.ask("What is the supplier's bank account number?")

    with pytest.raises(ConversationNotFound):
        setup.chat.delete(DEFAULT_TENANT_ID, "reviewer:b", first.conversation_id)
    setup.chat.delete(DEFAULT_TENANT_ID, "reviewer:a", first.conversation_id)

    with pytest.raises(ConversationNotFound):
        setup.chat.conversation(DEFAULT_TENANT_ID, "reviewer:a", first.conversation_id)


# --- follow-ups keep their subject (hardening) --------------------------------------------


class Spy:
    """The real search, with every call it is asked to make recorded."""

    def __init__(self, inner: SearchService) -> None:
        self.inner = inner
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def search(self, tenant_id: uuid.UUID, query: str, **kwargs: Any) -> Any:
        self.calls.append((query, kwargs))
        return self.inner.search(tenant_id, query, **kwargs)


def test_a_third_follow_up_is_searched_with_the_whole_conversation(
    sessions: SessionFactory, setup: Setup
) -> None:
    spy = Spy(setup.search)
    chat = ChatService(sessions, spy, setup.model)
    setup.model.reply = quoting(setup.batch, f"Invoice {setup.invoice_no} billed it.")
    first = chat.ask(DEFAULT_TENANT_ID, "reviewer:a", f"Which invoice billed batch {setup.batch}?")
    chat.ask(
        DEFAULT_TENANT_ID, "reviewer:a", "Who issued it?", conversation_id=first.conversation_id
    )
    spy.calls.clear()

    chat.ask(
        DEFAULT_TENANT_ID,
        "reviewer:a",
        "What is its grand total?",
        conversation_id=first.conversation_id,
    )

    queries = " ".join(query for query, _ in spy.calls)
    assert setup.batch in queries  # the first question is still part of the search
    assert setup.invoice_no in queries  # and what its answer named
    # The documents earlier answers cited are searched directly.
    assert any(kwargs.get("document_id") == setup.invoice_id for _, kwargs in spy.calls)


def test_passages_from_the_documents_already_cited_come_first(
    sessions: SessionFactory, setup: Setup
) -> None:
    chat = ChatService(sessions, setup.search, setup.model)
    setup.model.reply = quoting(setup.batch, f"Invoice {setup.invoice_no} billed it.")
    first = chat.ask(DEFAULT_TENANT_ID, "reviewer:a", f"Which invoice billed batch {setup.batch}?")

    chat.ask(
        DEFAULT_TENANT_ID, "reviewer:a", "And the order?", conversation_id=first.conversation_id
    )

    prompt = setup.model.requests[-1].prompt
    first_passage = prompt.split("<passage ", 2)[1]
    assert 'document="invoice.pdf"' in first_passage


def test_a_follow_up_stays_within_its_scope_when_searching_cited_documents(
    sessions: SessionFactory, setup: Setup
) -> None:
    spy = Spy(setup.search)
    chat = ChatService(sessions, spy, setup.model)
    setup.model.reply = quoting(setup.batch, "That one.")
    first = chat.ask(
        DEFAULT_TENANT_ID, "reviewer:a", f"Which invoice billed batch {setup.batch}?",
        document_id=setup.invoice_id,
    )  # fmt: skip
    spy.calls.clear()

    chat.ask(DEFAULT_TENANT_ID, "reviewer:a", "Its total?", conversation_id=first.conversation_id)

    assert spy.calls and all(
        kwargs.get("document_id") == setup.invoice_id for _, kwargs in spy.calls
    )


def test_a_question_naming_a_document_reads_that_documents_other_passages(
    sessions: SessionFactory, setup: Setup
) -> None:
    """A line row does not print its invoice's number: a question naming the number must still
    reach the row, by searching within the document that prints it."""
    spy = Spy(setup.search)
    chat = ChatService(sessions, spy, setup.model)

    chat.ask(
        DEFAULT_TENANT_ID,
        "reviewer:a",
        f"How many units were billed on invoice {setup.invoice_no}?",
    )

    assert any(kwargs.get("document_id") == setup.invoice_id for _, kwargs in spy.calls)
