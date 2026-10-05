"""Knowledge bases: named collections of an organisation's documents, to search and ask
within. A document can be in several. Each organisation sees only its own."""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from docforge.chat.service import ChatService, ConversationNotFound, ScopeConflict, ScopeGone
from docforge.collections import (
    CollectionNameTaken,
    CollectionNotFound,
    CollectionService,
)
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.parsing.cache import CachingParser
from docforge.search.embeddings import FakeEmbedder
from docforge.search.service import SearchService
from worlds import World

pytestmark = pytest.mark.integration

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "recorded" / "parsed"
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


class Setup:
    def __init__(self, sessions: SessionFactory, world: World) -> None:
        from test_chat import Model  # the stand-in model of the chat tests

        self.world = world
        self.collections = CollectionService(sessions)
        self.search = SearchService(sessions, FakeEmbedder())
        self.model = Model()
        self.chat = ChatService(sessions, self.search, self.model)
        self.invoice_id = world.process("invoice")
        self.order_id = world.process("purchase_order")
        for document_id in (self.invoice_id, self.order_id):
            self.search.index_document(DEFAULT_TENANT_ID, document_id)

    def create(self, name: str = "Invoices", **kw: Any) -> Any:
        return self.collections.create(DEFAULT_TENANT_ID, name, actor="admin:a", **kw)


@pytest.fixture
def setup(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> Setup:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    world.invoice_parsed = CachingParser(RECORDED).parse(world.invoice_pdf)
    return Setup(sessions, world)


def test_a_knowledge_base_is_created_named_and_listed_with_its_size(setup: Setup) -> None:
    kb = setup.create("Invoices", description="Supplier invoices for 2026")
    setup.collections.add(DEFAULT_TENANT_ID, kb.id, [setup.invoice_id])

    (listed,) = setup.collections.list(DEFAULT_TENANT_ID)
    assert (listed.name, listed.description, listed.documents) == (
        "Invoices", "Supplier invoices for 2026", 1,
    )  # fmt: skip


def test_names_are_unique_in_an_organisation_whatever_their_case(setup: Setup) -> None:
    setup.create("Invoices")
    with pytest.raises(CollectionNameTaken):
        setup.create(" invoices ")


def test_documents_are_added_once_and_removed(setup: Setup) -> None:
    kb = setup.create()
    setup.collections.add(DEFAULT_TENANT_ID, kb.id, [setup.invoice_id, setup.order_id])
    setup.collections.add(DEFAULT_TENANT_ID, kb.id, [setup.invoice_id])  # again: nothing new

    setup.collections.remove(DEFAULT_TENANT_ID, kb.id, [setup.order_id])

    members = setup.collections.documents(DEFAULT_TENANT_ID, kb.id)
    assert [d.id for d in members] == [setup.invoice_id]


def test_a_document_not_in_the_organisation_is_not_added(
    setup: Setup, other_tenant: uuid.UUID
) -> None:
    kb = setup.create()
    from docforge.collections import DocumentsNotFound

    with pytest.raises(DocumentsNotFound):
        setup.collections.add(DEFAULT_TENANT_ID, kb.id, [setup.invoice_id, uuid.uuid4()])
    assert setup.collections.documents(DEFAULT_TENANT_ID, kb.id) == []  # all or nothing


def test_another_organisation_cannot_see_or_change_it(
    setup: Setup, other_tenant: uuid.UUID
) -> None:
    kb = setup.create()

    assert setup.collections.list(other_tenant) == []
    with pytest.raises(CollectionNotFound):
        setup.collections.documents(other_tenant, kb.id)
    with pytest.raises(CollectionNotFound):
        setup.collections.rename(other_tenant, kb.id, "Mine now")
    with pytest.raises(CollectionNotFound):
        setup.collections.delete(other_tenant, kb.id)


def test_search_within_a_knowledge_base_finds_only_its_documents(setup: Setup) -> None:
    kb = setup.create()
    setup.collections.add(DEFAULT_TENANT_ID, kb.id, [setup.order_id])

    hits = setup.search.search(
        DEFAULT_TENANT_ID, "invoice order", mode="keyword", collection_id=kb.id
    )

    assert hits and {h.document_id for h in hits} == {setup.order_id}


def test_a_question_within_a_knowledge_base_reads_only_its_documents(setup: Setup) -> None:
    kb = setup.create()
    setup.collections.add(DEFAULT_TENANT_ID, kb.id, [setup.order_id])

    first = setup.chat.ask(
        DEFAULT_TENANT_ID, "reviewer:a", "What was ordered?", collection_id=kb.id
    )
    setup.chat.ask(
        DEFAULT_TENANT_ID, "reviewer:a", "And when?", conversation_id=first.conversation_id
    )

    for request in setup.model.requests:
        assert 'document="purchase_order.pdf"' in request.prompt
        assert 'document="invoice.pdf"' not in request.prompt


def test_a_question_has_one_scope(setup: Setup) -> None:
    kb = setup.create()
    with pytest.raises(ScopeConflict):
        setup.chat.ask(
            DEFAULT_TENANT_ID,
            "reviewer:a",
            "Total?",
            collection_id=kb.id,
            document_id=setup.invoice_id,
        )


def test_a_follow_up_after_its_knowledge_base_is_deleted_is_refused_not_widened(
    setup: Setup,
) -> None:
    kb = setup.create()
    setup.collections.add(DEFAULT_TENANT_ID, kb.id, [setup.order_id])
    first = setup.chat.ask(
        DEFAULT_TENANT_ID, "reviewer:a", "What was ordered?", collection_id=kb.id
    )

    setup.collections.delete(DEFAULT_TENANT_ID, kb.id)

    with pytest.raises(ScopeGone):
        setup.chat.ask(
            DEFAULT_TENANT_ID, "reviewer:a", "More?", conversation_id=first.conversation_id
        )
    # The conversation itself is still there to read.
    assert setup.chat.conversation(DEFAULT_TENANT_ID, "reviewer:a", first.conversation_id)


def test_a_knowledge_base_of_another_organisation_cannot_be_asked(
    setup: Setup, other_tenant: uuid.UUID
) -> None:
    kb = setup.create()
    from docforge.chat.service import DocumentNotFound

    with pytest.raises((DocumentNotFound, CollectionNotFound, ConversationNotFound)):
        setup.chat.ask(other_tenant, "reviewer:x", "Total?", collection_id=kb.id)


# --- review findings (C12) ---------------------------------------------------------------


def test_resending_a_deleted_knowledge_base_is_gone_not_a_conflict(setup: Setup) -> None:
    kb = setup.create()
    setup.collections.add(DEFAULT_TENANT_ID, kb.id, [setup.order_id])
    first = setup.chat.ask(
        DEFAULT_TENANT_ID, "reviewer:a", "What was ordered?", collection_id=kb.id
    )
    setup.collections.delete(DEFAULT_TENANT_ID, kb.id)

    with pytest.raises(ScopeGone):
        setup.chat.ask(
            DEFAULT_TENANT_ID, "reviewer:a", "More?",
            conversation_id=first.conversation_id, collection_id=kb.id,
        )  # fmt: skip


def test_a_progress_listener_that_fails_does_not_fail_the_question(setup: Setup) -> None:
    def broken(stage: str, details: dict[str, Any]) -> None:
        raise RuntimeError("the listener went away")

    answer = setup.chat.ask(DEFAULT_TENANT_ID, "reviewer:a", "Bank account?", progress=broken)

    assert answer.status == "not_found"
    (message,) = setup.chat.conversation(DEFAULT_TENANT_ID, "reviewer:a", answer.conversation_id)
    assert message.status == "not_found"


def test_more_documents_than_one_call_takes_are_refused_not_cut(setup: Setup) -> None:
    kb = setup.create()
    with pytest.raises(ValueError, match="500"):
        setup.collections.add(DEFAULT_TENANT_ID, kb.id, [uuid.uuid4() for _ in range(501)])


def test_renaming_to_a_name_taken_in_another_case_is_refused(setup: Setup) -> None:
    setup.create("Invoices")
    other = setup.create("Orders")
    with pytest.raises(CollectionNameTaken):
        setup.collections.rename(DEFAULT_TENANT_ID, other.id, "INVOICES")
