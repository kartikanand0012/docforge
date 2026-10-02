"""The document endpoints over a real database, with a stand-in parser, model and store."""

import hashlib
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from docforge.api.app import create_app
from docforge.db.models import DocumentVersion
from docforge.db.session import SessionFactory
from docforge.documents import DocumentService
from docforge.extraction.pipeline import InvoicePipeline
from docforge.storage import MemoryObjectStore, StorageUnavailable
from fakes import FakeParser, ScriptedProvider

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PDF = (FIXTURES / "pair_001" / "invoice.pdf").read_bytes()
LABEL = json.loads((FIXTURES / "pair_001" / "label.json").read_text(encoding="utf-8"))

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


class Api:
    def __init__(self, sessions: SessionFactory, replies: list[str]) -> None:
        self.queued: list[uuid.UUID] = []
        self.service = DocumentService(
            sessions,
            MemoryObjectStore(),
            {"invoice": InvoicePipeline(FakeParser(), ScriptedProvider(replies))},
            self._enqueue,
        )
        self.client = TestClient(create_app(None, service=self.service, max_pages=20))

    def _enqueue(self, session: Session, version: DocumentVersion) -> None:
        self.queued.append(version.id)

    def upload(self, data: bytes = PDF, **form: str) -> Any:
        files = {"file": ("invoice.pdf", data, "application/pdf")}
        return self.client.post("/v1/documents", files=files, data=form)

    def run_worker(self) -> None:
        """What the worker would do with the queued jobs."""
        while self.queued:
            self.service.process(self.queued.pop(0))


@pytest.fixture
def api(sessions: SessionFactory, raw_invoice_from_label: RawFromLabel) -> Api:
    reply = json.dumps(raw_invoice_from_label(LABEL))
    return Api(sessions, [reply, reply])


def test_upload_is_accepted_and_returns_before_processing(api: Api) -> None:
    response = api.upload()

    assert response.status_code == 202
    body = response.json()
    assert body["created"] is True
    assert body["document"]["status"] == "received"
    assert body["document"]["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert body["document"]["doc_type"] == "invoice"
    assert body["version"] == {
        "version_no": 1,
        "status": "queued",
        "attempts": 0,
        "error": None,
        "parser_version": None,
        "schema_version": None,
        "prompt_version": None,
        "model_id": None,
        "started_at": None,
        "finished_at": None,
    }
    assert response.headers["location"] == f"/v1/documents/{body['document']['id']}"
    assert len(api.queued) == 1


def test_uploading_the_same_file_again_returns_the_same_document(api: Api) -> None:
    first = api.upload().json()

    second = api.upload()

    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["document"]["id"] == first["document"]["id"]
    assert second.json()["version"] is None
    assert len(api.queued) == 1


def test_status_shows_progress_through_to_extracted(api: Api) -> None:
    document_id = api.upload().json()["document"]["id"]

    before = api.client.get(f"/v1/documents/{document_id}").json()
    api.run_worker()
    after = api.client.get(f"/v1/documents/{document_id}").json()

    assert before["document"]["status"] == "received"
    assert after["document"]["status"] == "extracted"
    assert after["document"]["page_count"] == 1
    (version,) = after["versions"]
    assert (version["status"], version["attempts"], version["model_id"]) == (
        "succeeded",
        1,
        "fake-1",
    )
    assert version["finished_at"] is not None


def test_extraction_is_not_found_until_it_exists(api: Api) -> None:
    document_id = api.upload().json()["document"]["id"]

    response = api.client.get(f"/v1/documents/{document_id}/extraction")

    assert response.status_code == 404
    assert response.json()["detail"] == "This document has no extraction yet (status: received)."


def test_extraction_is_returned_with_its_hash_and_model_runs(api: Api) -> None:
    document_id = api.upload().json()["document"]["id"]
    api.run_worker()

    response = api.client.get(f"/v1/documents/{document_id}/extraction")

    assert response.status_code == 200
    body = response.json()
    assert body["document_id"] == document_id
    assert body["version"]["version_no"] == 1
    assert body["extraction"]["invoice_no"]["value"] == LABEL["invoice"]["invoice_no"]
    assert len(body["sha256"]) == 64
    assert body["model_runs"] == [
        {
            "call_no": 1,
            "provider": "fake",
            "model": "fake-1",
            "prompt_version": "invoice-v1",
            "input_tokens": 100,
            "output_tokens": 50,
            "thinking_tokens": None,
            "latency_ms": 1.0,
        }
    ]


def test_reprocess_queues_a_new_version(api: Api) -> None:
    document_id = api.upload().json()["document"]["id"]
    api.run_worker()

    response = api.client.post(f"/v1/documents/{document_id}/reprocess")

    assert response.status_code == 202
    assert (response.json()["version_no"], response.json()["status"]) == (2, "queued")
    api.run_worker()
    latest = api.client.get(f"/v1/documents/{document_id}/extraction").json()
    assert latest["version"]["version_no"] == 2


def test_audit_trail_lists_every_step_and_the_chain_verifies(api: Api) -> None:
    document_id = api.upload().json()["document"]["id"]
    api.upload()
    api.run_worker()

    trail = api.client.get(f"/v1/documents/{document_id}/audit").json()
    verification = api.client.get("/v1/audit/verification").json()

    assert [entry["action"] for entry in trail] == [
        "document.received",
        "processing.started",
        "extraction.created",
        "assessment.created",
    ]
    assert trail[0]["actor"] == "api:anonymous"
    assert trail[0]["prev_hash"] is None
    assert trail[1]["prev_hash"] == trail[0]["hash"]
    assert verification == {"consistent": True, "entries": 4, "first_bad_id": None, "reason": None}


@pytest.mark.parametrize(
    "path",
    ["", "/extraction", "/audit"],
)
def test_an_unknown_document_is_not_found(api: Api, path: str) -> None:
    response = api.client.get(f"/v1/documents/{uuid.uuid4()}{path}")

    assert response.status_code == 404
    assert response.json()["detail"] == "No such document."


def test_reprocessing_an_unknown_document_is_not_found(api: Api) -> None:
    assert api.client.post(f"/v1/documents/{uuid.uuid4()}/reprocess").status_code == 404


def test_a_malformed_document_id_is_a_validation_error(api: Api) -> None:
    assert api.client.get("/v1/documents/not-a-uuid").status_code == 422


def test_an_unknown_document_type_is_refused(api: Api) -> None:
    response = api.upload(doc_type="passport")

    assert response.status_code == 422
    assert response.json()["detail"] == "Unknown document type. Supported: invoice."
    assert api.queued == []


@pytest.mark.parametrize(
    ("data", "status"),
    [(b"PK\x03\x04 a zip", 415), (b"%PDF-1.4\nnot really a pdf", 422)],
)
def test_bad_uploads_are_refused_before_anything_is_stored(
    api: Api, data: bytes, status: int
) -> None:
    response = api.upload(data)

    assert response.status_code == status
    assert api.queued == []
    assert api.client.get("/v1/audit/verification").json()["entries"] == 0


def test_the_synchronous_endpoint_is_absent_without_a_pipeline(api: Api) -> None:
    files = {"file": ("invoice.pdf", PDF, "application/pdf")}

    assert api.client.post("/v1/extractions", files=files).status_code == 404


def test_reprocess_is_refused_while_the_document_is_still_being_processed(api: Api) -> None:
    document_id = api.upload().json()["document"]["id"]

    response = api.client.post(f"/v1/documents/{document_id}/reprocess")

    assert response.status_code == 409
    assert response.json()["detail"] == "This document is still being processed."
    assert len(api.queued) == 1


def test_an_upload_while_storage_is_down_is_service_unavailable(api: Api) -> None:
    class DownStore(MemoryObjectStore):
        def exists(self, key: str) -> bool:
            raise StorageUnavailable("connection refused")

    api.service._store = DownStore()

    response = api.upload()

    assert response.status_code == 503
    assert response.json()["detail"] == "Storage is unavailable. Try again later."
    assert api.queued == []


def test_a_database_outage_is_service_unavailable(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(*args: object, **kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(api.service, "detail", down)
    client = TestClient(api.client.app, raise_server_exceptions=False)

    response = client.get(f"/v1/documents/{uuid.uuid4()}")

    assert response.status_code == 503
    assert response.json()["detail"] == "The database is unavailable. Try again later."
