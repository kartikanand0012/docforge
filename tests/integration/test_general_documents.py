"""General documents and other file formats: a Word file, slides or a photo is stored as
uploaded, turned into a PDF once in the worker, then read and indexed like any PDF, so
page images, citations and search work the same."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docforge.api.app import create_app
from docforge.conversion import ConversionError
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.documents import DocumentService, UnsupportedFormat
from docforge.extraction.general import GeneralPipeline
from docforge.formats import Format
from docforge.review.service import ReviewService
from docforge.search.embeddings import FakeEmbedder
from docforge.search.service import SearchService
from docforge.storage import MemoryObjectStore, rendition_key
from fakes import FakeParser, signed_in
from office_files import docx_bytes, image_bytes

pytestmark = pytest.mark.integration

PNG = image_bytes("PNG")
DOCX = docx_bytes("Goods are counted on arrival.")
CONVERTED = image_bytes("PDF")  # what the stand-in converter makes of anything


class Converter:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def to_pdf(self, data: bytes, fmt: Format) -> bytes:
        self.calls.append(fmt)
        if self.fail:
            raise ConversionError("LibreOffice could not read the file")
        return data if fmt == "pdf" else CONVERTED


class Service:
    def __init__(self, sessions: SessionFactory, converter: Converter | None = None) -> None:
        self.queued: list[uuid.UUID] = []
        self.indexed: list[uuid.UUID] = []
        self.store = MemoryObjectStore()
        self.converter = converter or Converter()
        self.parser = FakeParser()
        self.service = DocumentService(
            sessions,
            self.store,
            {"general": GeneralPipeline(self.parser)},
            lambda session, version: self.queued.append(version.id),
            converter=self.converter,
            index=lambda session, version: self.indexed.append(version.id),
        )

    def ingest(self, data: bytes, filename: str = "sop.docx") -> Any:
        return self.service.ingest(
            tenant_id=DEFAULT_TENANT_ID, doc_type="general", filename=filename, data=data,
            actor="api:upload",
        )  # fmt: skip

    def run(self) -> list[str]:
        return [self.service.process(self.queued.pop(0)) for _ in list(self.queued)]

    def stages(self, document_id: uuid.UUID) -> list[str]:
        return [s.stage for s in self.service.timeline(DEFAULT_TENANT_ID, document_id)]


@pytest.fixture
def svc(sessions: SessionFactory) -> Service:
    return Service(sessions)


def test_a_docx_is_stored_as_uploaded_with_its_media_type(svc: Service) -> None:
    result = svc.ingest(DOCX)

    document = result.document
    assert document.media_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert document.storage_key.endswith(".docx")
    assert svc.store.get(document.storage_key) == DOCX


def test_a_docx_is_converted_read_and_queued_for_indexing(svc: Service) -> None:
    document_id = svc.ingest(DOCX).document.id

    assert svc.run() == ["succeeded"]

    assert svc.converter.calls == ["docx"]
    assert svc.stages(document_id) == ["stored", "converting", "parsing", "indexing"]
    detail = svc.service.detail(DEFAULT_TENANT_ID, document_id)
    assert detail.document.stage == "indexing" and detail.document.page_count == 1
    assert svc.indexed == [detail.versions[-1].id]
    assert svc.store.get(rendition_key(DEFAULT_TENANT_ID, detail.document.sha256)) == CONVERTED
    assert svc.parser.calls == 1


def test_a_pdf_is_not_converted(svc: Service) -> None:
    document_id = svc.ingest(CONVERTED, "manual.pdf").document.id

    svc.run()

    assert svc.converter.calls == []
    assert "converting" not in svc.stages(document_id)


def test_reprocessing_reuses_the_stored_pdf(svc: Service) -> None:
    document_id = svc.ingest(PNG, "photo.png").document.id
    svc.run()

    svc.service.reprocess(tenant_id=DEFAULT_TENANT_ID, document_id=document_id, actor="t")
    svc.run()

    assert svc.converter.calls == ["png"]


def test_a_file_that_cannot_be_converted_fails_with_the_reason(sessions: SessionFactory) -> None:
    svc = Service(sessions, Converter(fail=True))
    document_id = svc.ingest(DOCX).document.id

    assert svc.run() == ["failed"]

    last = svc.service.timeline(DEFAULT_TENANT_ID, document_id)[-1]
    assert last.stage == "failed"
    assert last.detail == (
        "The file could not be converted to PDF: LibreOffice could not read the file."
    )


def test_a_format_the_service_does_not_know_is_refused(svc: Service) -> None:
    with pytest.raises(UnsupportedFormat):
        svc.ingest(b"just some text")


def test_the_page_image_of_a_converted_file_comes_from_its_pdf(
    sessions: SessionFactory, svc: Service
) -> None:
    document_id = svc.ingest(PNG, "photo.png").document.id
    svc.run()
    review = ReviewService(sessions, svc.store, {})

    png = review.page_image(DEFAULT_TENANT_ID, document_id, 1)

    assert png.startswith(b"\x89PNG")


def test_a_general_document_is_found_by_search(sessions: SessionFactory, svc: Service) -> None:
    document_id = svc.ingest(DOCX).document.id
    svc.run()
    search = SearchService(sessions, FakeEmbedder())

    assert search.index_document(DEFAULT_TENANT_ID, document_id) > 0
    (top, *_) = search.search(DEFAULT_TENANT_ID, "text 2", mode="keyword")

    assert top.document_id == document_id


def test_the_api_takes_word_files_and_images_and_says_which_formats_it_takes(
    svc: Service,
) -> None:
    client = TestClient(signed_in(create_app(None, service=svc.service, max_pages=20)))
    docx_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    def upload(name: str, data: bytes, media_type: str) -> Any:
        files = {"file": (name, data, media_type)}
        return client.post("/v1/documents", files=files, data={"doc_type": "general"})

    docx = upload("sop.docx", DOCX, docx_type)
    png = upload("p.png", PNG, "image/png")
    text = upload("a.txt", b"hello", "text/plain")

    assert docx.status_code == 202 and docx.json()["document"]["media_type"] == docx_type
    assert png.status_code == 202 and png.json()["document"]["media_type"] == "image/png"
    assert text.status_code == 415
    assert text.json()["detail"] == (
        "This file type is not accepted. Accepted: PDF, Word (DOCX), PowerPoint (PPTX), "
        "Excel (XLSX), PNG, JPEG and TIFF."
    )
