"""V2 P6 part B: one PDF holding several invoices becomes one document per invoice, and a
grouping the splitter is not sure of waits for a person - never guessed.

The bundles are made of the committed synthetic invoices (ground truth in their labels) and
read for their real text (`split_fixtures`); every test of a division asserts that the
parent's pages are all accounted for (AC-P6-6).
"""

import json
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from docforge.api.app import create_app
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry, Document, DocumentVersion, Extraction
from docforge.db.session import SessionFactory
from docforge.db.tenancy import tenant_scope
from docforge.documents import (
    DocumentNotFound,
    DocumentService,
    SplitDecided,
    SplitFile,
    SplitNotFound,
    SplitView,
)
from docforge.evals.long_document import long_invoice
from docforge.extraction.pipeline import INVOICE_SPEC, InvoicePipeline
from docforge.parsing.base import ParsedDocument, ParserLimitExceeded
from docforge.review.service import ReviewService
from docforge.split.detect import COULD_NOT_DETECT
from docforge.split.pages import cut
from docforge.split.reader import Splitter
from docforge.storage import MemoryObjectStore
from fakes import ScriptedProvider, signed_in
from split_fixtures import BundleParser, ByNumberProvider, Fixture, bundle, fixture, letter

pytestmark = pytest.mark.integration

Raw = Callable[[dict[str, Any]], dict[str, Any]]
REVIEWER = uuid.UUID(int=77)


class Bench:
    """A document service whose parser reads real text and knows the fixture invoices."""

    def __init__(
        self, sessions: SessionFactory, known: list[Fixture], *, split: bool = True,
        parser: Any = None, provider: Any = None,
    ) -> None:  # fmt: skip
        self.sessions = sessions
        self.store = MemoryObjectStore()
        self.queued: list[uuid.UUID] = []
        self.parser = parser or BundleParser(known)
        self.provider = provider or ByNumberProvider(known)
        pipeline = InvoicePipeline(self.parser, self.provider)
        self.service = DocumentService(
            sessions, self.store, {"invoice": pipeline},
            lambda session, version: self.queued.append(version.id),
            splitter=Splitter(pipeline) if split else None,
        )  # fmt: skip

    def upload(self, pdf: bytes, name: str = "bundle.pdf") -> uuid.UUID:
        result = self.service.ingest(
            tenant_id=DEFAULT_TENANT_ID, doc_type="invoice", filename=name, data=pdf,
            actor="api:upload",
        )  # fmt: skip
        return result.document.id

    def run(self) -> list[str]:
        """Every queued reading, the invoices made along the way included."""
        outcomes = []
        while self.queued:
            outcomes.append(str(self.service.process(self.queued.pop(0))))
        return outcomes

    def document(self, document_id: uuid.UUID) -> Document:
        with self.sessions() as session:
            return session.get_one(Document, document_id)

    def children(self, parent: uuid.UUID) -> list[Document]:
        with self.sessions() as session:
            return list(session.scalars(
                select(Document).where(Document.parent_document_id == parent)
                .order_by(Document.parent_pages)
            ))  # fmt: skip

    def extraction(self, document_id: uuid.UUID) -> dict[str, Any]:
        with self.sessions() as session:
            data: dict[str, Any] = session.scalars(
                select(Extraction.data)
                .join(DocumentVersion, DocumentVersion.id == Extraction.document_version_id)
                .where(DocumentVersion.document_id == document_id)
            ).one()
            return data

    def audit(self, document_id: uuid.UUID, action: str) -> list[AuditEntry]:
        trail = self.service.audit_trail(DEFAULT_TENANT_ID, document_id)
        return [entry for entry in trail if entry.action == action]

    def client(self, **who: Any) -> TestClient:
        review = ReviewService(self.sessions, self.store, {"invoice": INVOICE_SPEC})
        return TestClient(signed_in(create_app(None, service=self.service, review=review), **who))


def reconciles(view: SplitView) -> None:
    """AC-P6-6: parent pages = child pages + non-invoice pages, each page in one place."""
    made = view.status == "split"
    groups = view.groups if made else view.proposed
    outside = view.non_invoice if made else view.proposed_non_invoice
    placed = sorted([p for g in groups for p in g.pages] + list(outside))
    assert placed == list(range(1, view.page_count + 1))
    assert view.page_count == sum(len(g.pages) for g in groups) + len(outside)


@pytest.fixture
def known(raw_invoice_from_label: Raw) -> list[Fixture]:
    names = ("pair_001", "pair_002", "pair_003", "pair_101")
    return [fixture(name, raw_invoice_from_label) for name in names]


# AC-P6-3: three invoices, three documents, each read correctly; the parent marked split


def test_a_pdf_of_three_invoices_becomes_three_documents_each_read_correctly(
    sessions: SessionFactory, known: list[Fixture]
) -> None:
    bench = Bench(sessions, known)
    three = known[:3]
    parent = bench.upload(bundle(*(f.pdf for f in three)), "october-bills.pdf")

    assert bench.run() == ["split", "succeeded", "succeeded", "succeeded"]

    document = bench.document(parent)
    assert (document.status, document.stage, document.page_count) == ("split", "split", 3)
    view = bench.service.split_view(DEFAULT_TENANT_ID, parent)
    assert view.status == "split" and view.automatic and view.reason is None
    reconciles(view)
    children = bench.children(parent)
    assert [c.parent_pages for c in children] == [[1], [2], [3]]
    assert [c.filename for c in children] == [
        f"october-bills - invoice {n} of 3 (page {n}).pdf" for n in (1, 2, 3)
    ]
    for child, truth in zip(children, three, strict=True):
        record = bench.extraction(child.id)
        assert record["invoice_no"]["value"] == truth.invoice_no
        assert record["seller"]["name"]["value"] == truth.seller
        assert record["totals"]["grand_total"]["value"] == truth.total
        assert (child.status, child.source) == ("extracted", "upload")
    # The parent is no bill: no extraction of its own; its original stays downloadable.
    with bench.sessions() as session:
        own = session.scalars(
            select(Extraction.id)
            .join(DocumentVersion, DocumentVersion.id == Extraction.document_version_id)
            .where(DocumentVersion.document_id == parent)
        ).first()
    assert own is None
    assert bench.store.exists(document.storage_key)
    (made,) = bench.audit(parent, "split.made")
    assert made.details == {"pages": 3, "invoices": 3, "invoice_pages": 3,
                            "non_invoice_pages": 0, "already_received": 0}  # fmt: skip
    (received,) = bench.audit(children[0].id, "document.received")
    assert received.details["parent_document_id"] == str(parent)


def test_the_parent_is_not_in_the_review_queue_but_its_invoices_are(
    sessions: SessionFactory, known: list[Fixture]
) -> None:
    bench = Bench(sessions, known)
    parent = bench.upload(bundle(*(f.pdf for f in known[:3])))
    bench.run()

    queue = bench.client().get("/v1/review/queue")

    assert queue.status_code == 200, queue.text
    ids = {item["document_id"] for item in queue.json()}
    assert str(parent) not in ids
    assert {str(c.id) for c in bench.children(parent)} <= ids


# AC-P6-4: an uncertain boundary waits for a person; nothing is made until then


def test_an_unsure_boundary_makes_a_split_review_task_and_no_documents(
    sessions: SessionFactory, known: list[Fixture]
) -> None:
    bench = Bench(sessions, known)
    long, other = known[3], known[0]
    parent = bench.upload(bundle(long.pdf, other.pdf))  # 101's second page has no header

    assert bench.run() == ["split"]

    assert bench.document(parent).status == "split_review"
    assert bench.children(parent) == []
    assert bench.queued == []
    view = bench.service.split_view(DEFAULT_TENANT_ID, parent)
    assert view.status == "review" and not view.automatic
    assert [g.pages for g in view.proposed] == [(1, 2), (3,)]
    assert [g.invoice_no for g in view.proposed] == [long.invoice_no, other.invoice_no]
    assert view.pages_in_doubt == (2,)
    assert "page 2 has no invoice number" in (view.reason or "")
    assert view.groups == ()
    reconciles(view)
    waiting = bench.client().get("/v1/splits").json()
    assert [w["document_id"] for w in waiting] == [str(parent)]
    assert waiting[0]["counts"] == {"pages": 3, "invoices": 2, "invoice_pages": 3,
                                    "non_invoice_pages": 0}  # fmt: skip
    (proposed,) = bench.audit(parent, "split.proposed")
    assert proposed.details == {"pages": 3, "invoices": 2, "non_invoice_pages": 0,
                                "pages_in_doubt": 1}  # fmt: skip


# AC-P6-5: a person moves a page and confirms; the invoices come from their grouping


def test_a_reviewer_moves_a_page_and_confirms_and_is_audited(
    sessions: SessionFactory, known: list[Fixture]
) -> None:
    bench = Bench(sessions, known)
    first, second = known[0], known[1]
    parent = bench.upload(bundle(first.pdf, letter(), second.pdf))
    bench.run()
    proposed = bench.service.split_view(DEFAULT_TENANT_ID, parent)
    assert [g.pages for g in proposed.proposed] == [(1, 2), (3,)]  # the letter with invoice 1
    reconciles(proposed)

    # The letter (page 2) moved out of invoice 1, to "not an invoice page".
    answer = bench.client(reviewer_id=REVIEWER).post(
        f"/v1/documents/{parent}/split", json={"groups": [[1], [3]], "non_invoice": [2]}
    )

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["status"] == "split" and not body["automatic"]
    assert body["counts"] == {"pages": 3, "invoices": 2, "invoice_pages": 2,
                              "non_invoice_pages": 1}  # fmt: skip
    assert body["decided_person"] == "test caller"
    children = bench.children(parent)
    assert [c.parent_pages for c in children] == [[1], [3]]
    assert bench.run() == ["succeeded", "succeeded"]
    for child, truth in zip(children, (first, second), strict=True):
        assert bench.extraction(child.id)["invoice_no"]["value"] == truth.invoice_no
    (confirmed,) = bench.audit(parent, "split.confirmed")
    assert confirmed.actor == f"reviewer:{REVIEWER}"
    assert confirmed.details["person"] == "test caller"
    assert confirmed.details["pages_moved"] == 1
    reconciles(bench.service.split_view(DEFAULT_TENANT_ID, parent))
    assert bench.document(parent).status == "split"


def test_a_page_moved_to_the_next_invoice(sessions: SessionFactory, known: list[Fixture]) -> None:
    bench = Bench(sessions, known)
    parent = bench.upload(bundle(known[3].pdf, known[0].pdf))
    bench.run()

    view = bench.service.confirm_split(
        DEFAULT_TENANT_ID, parent, [[1], [2, 3]], [], actor=f"reviewer:{REVIEWER}", person="Asha"
    )

    assert [g.pages for g in view.groups] == [(1,), (2, 3)]
    assert [c.parent_pages for c in bench.children(parent)] == [[1], [2, 3]]
    reconciles(view)


def test_a_grouping_that_drops_or_repeats_a_page_is_refused_in_words(
    sessions: SessionFactory, known: list[Fixture]
) -> None:
    bench = Bench(sessions, known)
    parent = bench.upload(bundle(known[3].pdf, known[0].pdf))
    bench.run()
    client = bench.client(reviewer_id=REVIEWER)
    route = f"/v1/documents/{parent}/split"

    dropped = client.post(route, json={"groups": [[1, 2]]})
    twice = client.post(route, json={"groups": [[1, 2], [2, 3]]})
    none = client.post(route, json={"groups": [], "non_invoice": [1, 2, 3]})

    assert dropped.status_code == 422 and "Page 3 is in no invoice" in dropped.json()["detail"]
    assert twice.status_code == 422 and "Page 2 is placed twice" in twice.json()["detail"]
    assert none.status_code == 422 and "at least one page" in none.json()["detail"]
    assert bench.children(parent) == []
    assert bench.document(parent).status == "split_review"


def test_a_grouping_is_confirmed_once(sessions: SessionFactory, known: list[Fixture]) -> None:
    bench = Bench(sessions, known)
    parent = bench.upload(bundle(known[3].pdf, known[0].pdf))
    bench.run()
    bench.service.confirm_split(DEFAULT_TENANT_ID, parent, [[1, 2], [3]], [], actor="a", person="A")

    with pytest.raises(SplitDecided):
        bench.service.confirm_split(
            DEFAULT_TENANT_ID, parent, [[1], [2, 3]], [], actor="b", person="B"
        )
    assert len(bench.children(parent)) == 2


def test_only_reviewers_group_pages(sessions: SessionFactory, known: list[Fixture]) -> None:
    bench = Bench(sessions, known)
    parent = bench.upload(bundle(known[3].pdf, known[0].pdf))
    bench.run()

    answer = bench.client(role="reader").post(
        f"/v1/documents/{parent}/split", json={"groups": [[1, 2], [3]]}
    )

    assert answer.status_code == 403
    assert bench.children(parent) == []


# A divided file is no bill: not read again, and kept while its invoices are


def test_a_divided_file_is_not_read_again_nor_deleted_while_its_invoices_stand(
    sessions: SessionFactory, known: list[Fixture]
) -> None:
    bench = Bench(sessions, known)
    parent = bench.upload(bundle(*(f.pdf for f in known[:2])))
    bench.run()
    client = bench.client()

    again = client.post(f"/v1/documents/{parent}/reprocess")
    delete = client.delete(f"/v1/documents/{parent}")

    assert again.status_code == 409 and "read those again" in again.json()["detail"]
    assert delete.status_code == 409 and "Delete those first" in delete.json()["detail"]
    for child in bench.children(parent):
        assert client.delete(f"/v1/documents/{child.id}").status_code == 204
    assert client.delete(f"/v1/documents/{parent}").status_code == 204


def test_a_file_waiting_for_grouping_is_not_read_again_but_can_be_deleted(
    sessions: SessionFactory, known: list[Fixture]
) -> None:
    bench = Bench(sessions, known)
    parent = bench.upload(bundle(known[3].pdf, known[0].pdf))
    bench.run()

    with pytest.raises(SplitFile, match="Group this file's pages"):
        bench.service.reprocess(tenant_id=DEFAULT_TENANT_ID, document_id=parent, actor="x")
    bench.service.delete(DEFAULT_TENANT_ID, parent, actor="api:upload")

    assert bench.service.splits(DEFAULT_TENANT_ID, status="review") == []


# AC-P6-7: one long invoice is read exactly as before


@pytest.mark.parametrize("which", ["pair_101", "eight_pages"])
def test_one_multipage_invoice_is_not_split_and_reads_as_before(
    sessions: SessionFactory, known: list[Fixture], which: str
) -> None:
    long = known[3]
    pdf = long.pdf if which == "pair_101" else cut(long_invoice(8), list(range(1, 9)))
    results = {}
    for split in (False, True):
        replies = [json.dumps(long.raw)] * 20
        bench = Bench(sessions, known, split=split, provider=ScriptedProvider(replies))
        document_id = bench.upload(pdf, "long.pdf")
        assert bench.run() == ["succeeded"]
        results[split] = (
            bench.extraction(document_id), len(bench.provider.requests), bench.parser.calls,
            bench.document(document_id).status, bench.children(document_id),
        )  # fmt: skip
        # Deleted, so the same bytes are a new document for the next run.
        bench.service.delete(DEFAULT_TENANT_ID, document_id, actor="test")

    # The same extraction, the same model calls, one parse: exactly as before.
    assert results[True] == results[False]
    _, requests, parses, status, children = results[True]
    assert (parses, status, children) == (1, "extracted", [])
    assert requests >= 1


def test_without_the_splitter_every_file_is_read_whole(
    sessions: SessionFactory, known: list[Fixture]
) -> None:
    replies = [json.dumps(known[0].raw)] * 5
    bench = Bench(sessions, known, split=False, provider=ScriptedProvider(replies))
    parent = bench.upload(bundle(*(f.pdf for f in known[:3])))

    assert bench.run() == ["succeeded"]
    assert bench.children(parent) == []
    with pytest.raises(SplitNotFound):
        bench.service.split_view(DEFAULT_TENANT_ID, parent)


# Scale and the fallback: never an error, a person instead


class StoppedParser(BundleParser):
    def parse(self, pdf: bytes) -> ParsedDocument:
        raise ParserLimitExceeded("it took too long")


def test_a_long_file_the_parser_cannot_read_goes_to_a_person_not_to_an_error(
    sessions: SessionFactory, known: list[Fixture]
) -> None:
    bench = Bench(sessions, known, parser=StoppedParser(known))
    pdf = bundle(*(f.pdf for f in known[:3]), pages=[(i % 3, 1) for i in range(25)])
    parent = bench.upload(pdf)  # 25 pages: longer than one invoice may be

    assert bench.run() == ["split"]

    view = bench.service.split_view(DEFAULT_TENANT_ID, parent)
    assert view.status == "review" and view.reason == COULD_NOT_DETECT
    assert view.page_count == 25 and [g.pages for g in view.proposed] == [tuple(range(1, 26))]
    reconciles(view)
    assert bench.children(parent) == []


def test_a_60_page_file_is_examined_well_within_the_budget(
    sessions: SessionFactory, known: list[Fixture]
) -> None:
    bench = Bench(sessions, known)
    pdf = bundle(*(f.pdf for f in known[:3]), pages=[(i % 3, 1) for i in range(60)])
    parent = bench.upload(pdf)

    started = time.monotonic()
    outcome = bench.service.process(bench.queued.pop(0))
    seconds = time.monotonic() - started

    assert outcome == "split"
    view = bench.service.split_view(DEFAULT_TENANT_ID, parent)
    # The same three invoices repeat: a number seen before is put to a person.
    assert view.status == "review" and view.page_count == 60 and len(view.proposed) == 60
    reconciles(view)
    assert seconds < 30, seconds


def test_a_long_file_holding_one_invoice_fails_with_the_page_limit(
    sessions: SessionFactory, known: list[Fixture]
) -> None:
    bench = Bench(sessions, known)
    document_id = bench.upload(cut(long_invoice(22), list(range(1, 23))))

    assert bench.run() == ["failed"]

    with bench.sessions() as session:
        error = session.scalars(
            select(DocumentVersion.error).where(DocumentVersion.document_id == document_id)
        ).one()
    assert error == "The document has 22 pages; the limit is 20."


def test_a_thumbnail_is_served_for_each_page_of_a_file_in_review(
    sessions: SessionFactory, known: list[Fixture]
) -> None:
    bench = Bench(sessions, known)
    parent = bench.upload(bundle(known[3].pdf, known[0].pdf))
    bench.run()
    client = bench.client()

    small = client.get(f"/v1/documents/{parent}/pages/3?size=thumbnail")
    large = client.get(f"/v1/documents/{parent}/pages/3")

    assert small.status_code == 200 and small.headers["content-type"] == "image/png"
    assert len(small.content) < len(large.content)


# AC-P6-8: each workspace sees only its own


def test_a_workspace_sees_only_its_own_divided_files(
    sessions: SessionFactory, known: list[Fixture], other_tenant: uuid.UUID
) -> None:
    bench = Bench(sessions, known)
    parent = bench.upload(bundle(known[3].pdf, known[0].pdf))
    bench.run()

    with tenant_scope(other_tenant):
        assert bench.service.splits(other_tenant, status=None) == []
        with pytest.raises(DocumentNotFound):
            bench.service.split_view(other_tenant, parent)
        with pytest.raises(DocumentNotFound):
            bench.service.confirm_split(
                other_tenant, parent, [[1, 2], [3]], [], actor="x", person="X"
            )
    assert bench.children(parent) == []
