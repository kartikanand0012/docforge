"""Deleting a document: it is kept (the audit trail still names it) but vanishes everywhere a
caller could reach it - lists, the queue, every read by id, search, questions and their
citations, knowledge bases, exports, agents' tools and matching - and the same file can be
uploaded again as a new document."""

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from test_chat import Model, RawFromLabel, quoting
from test_mcp import call

from docforge.api.app import create_app
from docforge.auth import Authenticator
from docforge.chat.service import ChatService
from docforge.collections import CollectionService
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.extraction.pipeline import INVOICE_SPEC
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
from docforge.limits import LocalLimits
from docforge.mcp_server.tools import AgentTools
from docforge.parsing.cache import CachingParser
from docforge.review.service import ReviewService
from docforge.search.embeddings import FakeEmbedder
from docforge.search.service import Mode, SearchService
from fakes import signed_in
from worlds import World

pytestmark = pytest.mark.integration

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "recorded" / "parsed"


class Stack:
    def __init__(self, sessions: SessionFactory, world: World) -> None:
        self.sessions, self.world = sessions, world
        self.search = SearchService(sessions, FakeEmbedder())
        self.model = Model()
        self.chat = ChatService(sessions, self.search, self.model, daily_limit=100)
        self.collections = CollectionService(sessions)
        self.order_id = world.process("purchase_order")
        self.invoice_id = world.process("invoice")
        for document_id in (self.order_id, self.invoice_id):
            self.search.index_document(DEFAULT_TENANT_ID, document_id)
        assert world.service is not None
        self.documents = world.service
        self.review = ReviewService(
            sessions, world.store, {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC}
        )
        self.tools = AgentTools(
            sessions, search=self.search, chat=self.chat, collections=self.collections,
            documents=self.documents, limits=LocalLimits(),
        )  # fmt: skip
        app = create_app(
            None, service=self.documents, review=self.review, search=self.search, chat=self.chat,
            collections=self.collections,
        )  # fmt: skip
        self.api = TestClient(signed_in(app, role="admin"))
        self.batch = world.invoice_raw["lines"][0]["batch_no"]["text"]

    def delete(self, document_id: uuid.UUID) -> int:
        return int(self.api.delete(f"/v1/documents/{document_id}").status_code)


@pytest.fixture
def stack(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> Stack:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    world.invoice_parsed = CachingParser(RECORDED).parse(world.invoice_pdf)
    return Stack(sessions, world)


def ids(items: list[dict[str, Any]]) -> set[str]:
    return {item["id"] for item in items}


def test_a_deleted_document_is_gone_from_the_list_and_every_read_by_id(stack: Stack) -> None:
    invoice = stack.invoice_id
    assert stack.api.get(f"/v1/documents/{invoice}/review").status_code == 200

    assert stack.delete(invoice) == 204

    assert str(invoice) not in ids(stack.api.get("/v1/documents").json()["items"])
    assert str(stack.order_id) in ids(stack.api.get("/v1/documents").json()["items"])
    for path in (
        "", "/extraction", "/assessment", "/timeline", "/events", "/audit", "/review",
        "/pages/1",
    ):  # fmt: skip
        assert stack.api.get(f"/v1/documents/{invoice}{path}").status_code == 404, path
    assert stack.api.post(f"/v1/documents/{invoice}/reprocess").status_code == 404
    assert all(item["document_id"] != str(invoice) for item in stack.api.get(
        "/v1/review/queue"
    ).json())  # fmt: skip


def test_deleting_twice_or_an_unknown_document_is_not_found(stack: Stack) -> None:
    assert stack.delete(stack.invoice_id) == 204
    assert stack.delete(stack.invoice_id) == 404
    assert stack.delete(uuid.uuid4()) == 404


def test_another_organisation_cannot_delete_a_document(
    stack: Stack, other_tenant: uuid.UUID
) -> None:
    app = create_app(None, service=stack.documents)
    theirs = TestClient(signed_in(app, tenant_id=other_tenant, role="admin"))

    assert theirs.delete(f"/v1/documents/{stack.invoice_id}").status_code == 404
    assert stack.api.get(f"/v1/documents/{stack.invoice_id}").status_code == 200


def test_only_a_caller_that_may_upload_may_delete(stack: Stack) -> None:
    app = create_app(None, service=stack.documents)
    reviewer = TestClient(signed_in(app, role="reviewer", reviewer_id=uuid.uuid4()))
    reader = TestClient(signed_in(create_app(None, service=stack.documents), role="reader"))

    assert reviewer.delete(f"/v1/documents/{stack.invoice_id}").status_code == 403
    assert reader.delete(f"/v1/documents/{stack.invoice_id}").status_code == 403


def test_deleting_is_audited_without_the_filename(stack: Stack, owner_engine: Engine) -> None:
    assert stack.delete(stack.invoice_id) == 204

    with owner_engine.connect() as conn:
        actor, details = conn.execute(
            text(
                "SELECT actor, details FROM audit_log WHERE action = 'document.deleted' "
                "AND target_id = :id"
            ),
            {"id": str(stack.invoice_id)},
        ).one()
        deleted_at = conn.execute(
            text("SELECT deleted_at FROM documents WHERE id = :id"), {"id": stack.invoice_id}
        ).scalar_one()
    assert actor.startswith("key:") and details == {}
    assert deleted_at is not None  # kept, not erased: its history still refers to it


def test_search_by_words_and_by_meaning_no_longer_finds_it(stack: Stack) -> None:
    modes: tuple[Mode, ...] = ("keyword", "vector", "hybrid")
    for mode in modes:
        found = stack.search.search(DEFAULT_TENANT_ID, stack.batch, mode=mode)
        assert stack.invoice_id in {hit.document_id for hit in found}, mode

    stack.delete(stack.invoice_id)

    for mode in modes:
        found = stack.search.search(DEFAULT_TENANT_ID, stack.batch, mode=mode)
        assert stack.invoice_id not in {hit.document_id for hit in found}, mode
    response = stack.api.get("/v1/search", params={"q": stack.batch})
    assert response.status_code == 200
    assert all(r["document_id"] != str(stack.invoice_id) for r in response.json()["results"])


def test_questions_neither_read_nor_cite_it_afterwards(stack: Stack) -> None:
    stack.model.reply = quoting(stack.batch, f"Batch {stack.batch} is on this invoice.")
    question = {"question": f"Which invoice billed batch {stack.batch}?"}
    asked = stack.api.post("/v1/chat", json=question)
    assert asked.status_code == 200 and asked.json()["citations"], asked.json()
    scoped = stack.api.post(
        "/v1/chat", json={"question": "Its total?", "document_id": str(stack.invoice_id)}
    ).json()

    stack.delete(stack.invoice_id)

    stack.model.requests.clear()
    stack.model.reply = lambda passages: {"statements": [], "unanswerable": True}
    again = stack.api.post("/v1/chat", json=question)
    assert again.status_code == 200
    assert all(c["document_id"] != str(stack.invoice_id) for c in again.json()["citations"])
    assert not any("invoice.pdf" in r.prompt for r in stack.model.requests)  # no passage of it
    # Asked about it by id, or in a conversation about it: gone.
    by_id = {"question": "Total?", "document_id": str(stack.invoice_id)}
    assert stack.api.post("/v1/chat", json=by_id).status_code == 404
    follow_up = {"question": "And the date?", "conversation_id": scoped["conversation_id"]}
    assert stack.api.post("/v1/chat", json=follow_up).status_code == 409
    # Earlier answers read back without its quotes.
    earlier = stack.api.get(f"/v1/conversations/{asked.json()['conversation_id']}").json()
    assert all(
        c["document_id"] != str(stack.invoice_id)
        for message in earlier["messages"]
        for c in message["citations"]
    )


def test_knowledge_bases_no_longer_list_or_count_it(stack: Stack) -> None:
    kb = stack.collections.create(DEFAULT_TENANT_ID, "Invoices", actor="key:x")
    stack.collections.add(DEFAULT_TENANT_ID, kb.id, [stack.invoice_id, stack.order_id], actor="x")

    stack.delete(stack.invoice_id)

    members = stack.api.get(f"/v1/collections/{kb.id}/documents").json()
    assert [m["id"] for m in members] == [str(stack.order_id)]
    (listed,) = stack.api.get("/v1/collections").json()
    assert listed["documents"] == 1
    adding = {"document_ids": [str(stack.invoice_id)]}
    assert stack.api.post(f"/v1/collections/{kb.id}/documents", json=adding).status_code == 404


def test_signed_records_of_a_deleted_document_leave_the_exports(
    stack: Stack, sessions: SessionFactory
) -> None:
    stack.review.add_reviewer(DEFAULT_TENANT_ID, name="Asha", email="a@example.com", pin="482913")
    detail = stack.review.detail(DEFAULT_TENANT_ID, stack.order_id)
    stack.review.sign(
        DEFAULT_TENANT_ID, stack.order_id, outcome="approved",
        meaning=detail.meanings["approved"], reason="ok", override_reason="checked by hand",
        expected_record_sha256=detail.record_sha256, email="a@example.com", pin="482913",
    )  # fmt: skip
    assert len(stack.api.get("/v1/exports/documents.json").json()) == 1

    stack.delete(stack.order_id)

    assert stack.api.get("/v1/exports/documents.json").json() == []
    assert stack.api.get("/v1/exports/documents.csv").text.count("\n") <= 1


def test_agents_tools_do_not_find_it(stack: Stack) -> None:
    auth = Authenticator(stack.sessions)
    reader = auth.create_api_key(DEFAULT_TENANT_ID, name="agent", role="reader")
    app = create_app(
        None, service=stack.documents, search=stack.search, chat=stack.chat,
        collections=stack.collections, authenticator=auth, agents=stack.tools,
    )  # fmt: skip
    stack.delete(stack.invoice_id)

    with TestClient(app) as api:
        failed, listed = call(api, reader, "list_documents")
        assert not failed and str(stack.invoice_id) not in {d["id"] for d in listed["items"]}
        failed, _ = call(api, reader, "get_document", document_id=str(stack.invoice_id))
        assert failed
        failed, _ = call(api, reader, "get_page_text", document_id=str(stack.invoice_id), page=1)
        assert failed
        failed, found = call(api, reader, "search_documents", query=stack.batch)
        assert not failed
        assert str(stack.invoice_id) not in {r["document_id"] for r in found["results"]}


def test_a_deleted_order_is_no_longer_a_match_for_its_invoice(stack: Stack) -> None:
    before = stack.api.get(f"/v1/documents/{stack.invoice_id}/assessment").json()
    assert before["match_status"] != "no_counterpart"

    stack.delete(stack.order_id)

    after = stack.api.get(f"/v1/documents/{stack.invoice_id}/assessment").json()
    assert after["match_status"] == "no_counterpart" and after["match"] is None
    review = stack.api.get(f"/v1/documents/{stack.invoice_id}/review").json()
    assert review["match_status"] == "no_counterpart"


def test_a_new_invoice_is_not_matched_to_a_deleted_order(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> None:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    order_id = world.process("purchase_order")
    assert world.service is not None
    world.service.delete(DEFAULT_TENANT_ID, order_id, actor="key:x")

    invoice_id = world.process("invoice")

    assert world.assessment(invoice_id).match is None


def test_the_same_file_can_be_uploaded_again_as_a_new_document(stack: Stack) -> None:
    stack.delete(stack.invoice_id)

    again = stack.api.post(
        "/v1/documents",
        files={"file": ("invoice.pdf", stack.world.invoice_pdf, "application/pdf")},
        data={"doc_type": "invoice"},
    )

    assert again.status_code == 202, again.text
    assert again.json()["created"] is True
    assert again.json()["document"]["id"] != str(stack.invoice_id)


def test_the_application_cannot_read_or_undelete_a_deleted_document_itself(
    stack: Stack, engine: Engine
) -> None:
    stack.delete(stack.invoice_id)

    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('docforge.tenant_id', :t, true)"), {"t": str(DEFAULT_TENANT_ID)}
        )
        seen = conn.execute(
            text("SELECT count(*) FROM documents WHERE id = :id"), {"id": stack.invoice_id}
        ).scalar_one()
        changed = conn.execute(
            text("UPDATE documents SET deleted_at = NULL WHERE id = :id"), {"id": stack.invoice_id}
        ).rowcount
    assert (seen, changed) == (0, 0)


# Erasure: the stored files go, the hidden row stays


def stored(stack: Stack, document_id: uuid.UUID, owner_engine: Engine) -> tuple[str, str]:
    with owner_engine.connect() as conn:
        row = conn.execute(
            text("SELECT storage_key, sha256 FROM documents WHERE id = :d"), {"d": document_id}
        ).one()
    return row.storage_key, row.sha256


def test_a_deleted_documents_file_and_the_pdfs_made_from_it_are_erased(
    stack: Stack, owner_engine: Engine
) -> None:
    key, sha = stored(stack, stack.invoice_id, owner_engine)
    store = stack.world.store
    renditions = [f"renditions/{DEFAULT_TENANT_ID}/{sha}.pdf",
                  f"renditions/{DEFAULT_TENANT_ID}/{sha}/0123456789abcdef.pdf"]  # fmt: skip
    for rendition in renditions:
        store.put(rendition, b"%PDF-", "application/pdf")
    order_key, _ = stored(stack, stack.order_id, owner_engine)

    assert stack.delete(stack.invoice_id) == 204

    assert not store.exists(key)
    assert not any(store.exists(r) for r in renditions)
    assert store.exists(order_key)  # the other document's file stays
    with owner_engine.connect() as conn:
        row = conn.execute(
            text("SELECT deleted_at FROM documents WHERE id = :d"), {"d": stack.invoice_id}
        ).one()
        left = conn.execute(text("SELECT count(*) FROM erasures")).scalar_one()
    assert row.deleted_at is not None and left == 0


def test_the_same_file_uploaded_again_after_deleting_is_stored_again(
    stack: Stack, owner_engine: Engine
) -> None:
    stack.delete(stack.invoice_id)

    again = stack.world.process("invoice")

    key, _ = stored(stack, again, owner_engine)
    assert again != stack.invoice_id and stack.world.store.exists(key)


def test_a_file_a_live_document_still_holds_is_not_erased(
    stack: Stack, owner_engine: Engine
) -> None:
    from docforge.erasure import Erasure

    key, sha = stored(stack, stack.invoice_id, owner_engine)
    # As if the erasure of an earlier deletion of these bytes ran after their new upload.
    stack.documents.eraser.erase(Erasure(0, "document", DEFAULT_TENANT_ID, sha, key))

    assert stack.world.store.exists(key)


def test_with_storage_down_the_deletion_stands_and_the_erasure_is_finished_later(
    stack: Stack, owner_engine: Engine, owner_sessions: SessionFactory
) -> None:
    from docforge.erasure import finish_all
    from docforge.storage import MemoryObjectStore, StorageUnavailable

    key, _ = stored(stack, stack.invoice_id, owner_engine)
    real = stack.world.store

    class Down(MemoryObjectStore):
        def delete(self, key: str) -> None:
            raise StorageUnavailable("connection refused")

    stack.documents.eraser._store = Down()

    assert stack.delete(stack.invoice_id) == 204
    assert real.exists(key)
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM erasures")).scalar_one() == 1

    assert finish_all(owner_sessions, real) == (1, 0)

    assert not real.exists(key)
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM erasures")).scalar_one() == 0


def test_the_application_cannot_read_the_erasures_still_to_do(engine: Engine) -> None:
    with engine.connect() as conn, pytest.raises(Exception, match="permission denied"):
        conn.execute(text("SELECT count(*) FROM erasures"))


# Deleting a document whose reading is still waiting or under way


def versions_of(owner_engine: Engine, document_id: uuid.UUID) -> list[tuple[str, str | None]]:
    with owner_engine.connect() as conn:
        return [
            (row.status, row.error)
            for row in conn.execute(
                text("SELECT status, error FROM document_versions WHERE document_id = :d"),
                {"d": document_id},
            )
        ]


def pending(stack: Stack) -> int:
    from docforge.db.tenancy import tenant_scope
    from docforge.documents import DocumentService

    with tenant_scope(DEFAULT_TENANT_ID), stack.sessions() as session:
        return DocumentService._pending(session, DEFAULT_TENANT_ID)


@pytest.mark.parametrize("status", ["queued", "running"])
def test_deleting_a_document_ends_its_reading_in_flight_and_frees_its_place(
    stack: Stack, owner_engine: Engine, status: str
) -> None:
    received = stack.documents.ingest(
        tenant_id=DEFAULT_TENANT_ID, doc_type="invoice", filename="new.pdf",
        data=stack.world.invoice_pdf + b"\n% another copy", actor="api:upload",
    )  # fmt: skip
    document_id = received.document.id
    if status == "running":
        with owner_engine.begin() as conn:
            conn.execute(
                text("UPDATE document_versions SET status = 'running' WHERE document_id = :d"),
                {"d": document_id},
            )
    assert pending(stack) == 1

    assert stack.delete(document_id) == 204

    assert versions_of(owner_engine, document_id) == [("failed", "Deleted")]
    assert pending(stack) == 0
    # The worker's delivery of it, when it comes, finds nothing to do.
    assert received.version is not None
    with pytest.raises(Exception, match="no document"):
        stack.documents.process(received.version.id)


def test_a_deleted_documents_reading_never_counts_towards_the_queue_limit(
    stack: Stack, owner_engine: Engine
) -> None:
    received = stack.documents.ingest(
        tenant_id=DEFAULT_TENANT_ID, doc_type="invoice", filename="new.pdf",
        data=stack.world.invoice_pdf + b"\n% a third copy", actor="api:upload",
    )  # fmt: skip
    # As a deletion before versions in flight were ended left it: hidden, still queued.
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE documents SET deleted_at = now() WHERE id = :d"),
            {"d": received.document.id},
        )

    assert pending(stack) == 0
