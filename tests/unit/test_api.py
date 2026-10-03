"""The HTTP API with a stand-in parser and model."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docforge.api.app import create_app
from docforge.config import Settings
from docforge.extraction.pipeline import InvoicePipeline
from docforge.extraction.prompt import PROMPT_VERSION
from docforge.llm.base import LLMError, LLMQuotaExhausted
from docforge.parsing.base import ParsedDocument
from docforge.parsing.isolation import IsolatedParser
from docforge.wiring import build_pipeline
from fakes import PARSED, FakeParser, ScriptedProvider, signed_in

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PDF = (FIXTURES / "pair_001" / "invoice.pdf").read_bytes()
LABEL = json.loads((FIXTURES / "pair_001" / "label.json").read_text(encoding="utf-8"))

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


def client(replies: list[str | Exception], **options: int) -> TestClient:
    max_pages = options.pop("max_pages", 20)
    pipeline = InvoicePipeline(FakeParser(), ScriptedProvider(replies), max_pages=max_pages)
    return TestClient(
        signed_in(create_app(pipeline, options.pop("max_upload_bytes", 10 * 1024 * 1024)))
    )


def upload(api: TestClient, data: bytes = PDF, query: str = "") -> Any:
    files = {"file": ("invoice.pdf", data, "application/pdf")}
    return api.post(f"/v1/extractions{query}", files=files)


@pytest.fixture
def perfect_reply(raw_invoice_from_label: RawFromLabel) -> str:
    return json.dumps(raw_invoice_from_label(LABEL))


def test_health_check() -> None:
    response = client([]).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_extraction_returns_the_record_with_its_run_details(perfect_reply: str) -> None:
    response = upload(client([perfect_reply]))

    assert response.status_code == 200
    body = response.json()
    assert body["document"] == {
        "filename": "invoice.pdf",
        "sha256": hashlib.sha256(PDF).hexdigest(),
        "size_bytes": len(PDF),
        "pages": 1,
    }
    assert body["parser"] == {"name": "fake", "version": "0"}
    assert body["model_runs"] == [
        {
            "provider": "fake",
            "model": "fake-1",
            "prompt_version": PROMPT_VERSION,
            "input_tokens": 100,
            "output_tokens": 50,
            "thinking_tokens": None,
            "latency_ms": 1.0,
        }
    ]
    extraction = body["extraction"]
    assert extraction["schema_version"] == "invoice-1"
    assert extraction["invoice_no"]["value"] == LABEL["invoice"]["invoice_no"]
    assert extraction["invoice_date"]["value"] == LABEL["invoice"]["invoice_date"]
    assert extraction["totals"]["grand_total"]["value"] == LABEL["invoice"]["totals"]["grand_total"]
    assert len(extraction["lines"]) == len(LABEL["invoice"]["lines"])


def test_blocks_are_returned_only_when_asked_for(perfect_reply: str) -> None:
    without = upload(client([perfect_reply])).json()
    with_blocks = upload(client([perfect_reply]), query="?include_blocks=true").json()

    assert without["blocks"] is None
    assert [block["id"] for block in with_blocks["blocks"]] == [b.id for b in PARSED.blocks]
    assert with_blocks["blocks"][0]["bbox"] == {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}


def test_a_file_that_is_not_a_pdf_is_rejected() -> None:
    response = upload(client([]), data=b"PK\x03\x04 this is a zip")

    assert response.status_code == 415
    assert "PDF" in response.json()["detail"]


def test_an_oversized_upload_is_rejected() -> None:
    response = upload(client([], max_upload_bytes=100))

    assert response.status_code == 413
    assert "100 bytes" in response.json()["detail"]


def test_a_corrupt_pdf_is_rejected() -> None:
    response = upload(client([]), data=b"%PDF-1.4\nnot really a pdf")

    assert response.status_code == 422
    assert response.json()["detail"] == "The file could not be read as a PDF."


def test_a_pdf_with_too_many_pages_is_rejected() -> None:
    response = upload(client([], max_pages=0))

    assert response.status_code == 413
    assert "pages" in response.json()["detail"]


def test_a_missing_file_field_is_a_validation_error() -> None:
    response = client([]).post("/v1/extractions")

    assert response.status_code == 422


def test_a_model_failure_is_a_bad_gateway_without_provider_details() -> None:
    response = upload(client([LLMError("status 500 INTERNAL: secret-ish provider text")]))

    assert response.status_code == 502
    assert response.json()["detail"] == "The model provider failed. Try again later."


def test_an_exhausted_quota_is_service_unavailable() -> None:
    response = upload(client([LLMQuotaExhausted("daily quota used up")]))

    assert response.status_code == 503
    assert response.json()["detail"] == "The model quota is used up. Try again later."


def test_a_reply_that_never_fits_the_schema_is_a_bad_gateway() -> None:
    response = upload(client(["not json", "{}"]))

    assert response.status_code == 502
    assert response.json()["detail"] == "The model reply did not fit the schema."


def test_openapi_describes_the_extraction_endpoint() -> None:
    schema = client([]).get("/openapi.json").json()

    operation = schema["paths"]["/v1/extractions"]["post"]
    assert "200" in operation["responses"]
    assert {"413", "415", "422", "502", "503"} <= set(operation["responses"])


def test_build_pipeline_needs_a_gemini_key() -> None:
    settings = Settings(_env_file=None, gemini_api_key=None)

    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        build_pipeline(settings)


def test_build_pipeline_uses_the_configured_model_and_limits() -> None:
    settings = Settings(
        _env_file=None,
        gemini_api_key="not-a-real-key",
        gemini_model="gemini-test",
        max_pages=7,
    )

    pipeline = build_pipeline(settings)

    assert pipeline.provider.model == "gemini-test"
    assert pipeline.parser.name == "docling"
    assert pipeline.max_pages == 7
    # The real parser runs in a child process; nothing is started until a document arrives.
    assert isinstance(pipeline.parser, IsolatedParser)


def test_a_pdf_without_a_text_layer_is_rejected_with_a_clear_message() -> None:
    class EmptyParser(FakeParser):
        def parse(self, pdf: bytes) -> ParsedDocument:
            return PARSED.model_copy(update={"blocks": ()})

    api = TestClient(signed_in(create_app(InvoicePipeline(EmptyParser(), ScriptedProvider([])))))

    response = upload(api)

    assert response.status_code == 422
    assert response.json()["detail"] == "No text could be read from the PDF."


def test_an_oversized_request_is_refused_from_its_declared_length() -> None:
    api = client([], max_upload_bytes=100)

    response = api.post(
        "/v1/extractions",
        content=b"x" * 10,
        headers={"content-type": "multipart/form-data; boundary=x", "content-length": "999999"},
    )

    assert response.status_code == 413


def test_the_uploaded_filename_is_reduced_to_a_safe_base_name(perfect_reply: str) -> None:
    api = client([perfect_reply])
    name = "../../etc/pass\x07wd<script>" + "a" * 400 + ".pdf"

    body = api.post("/v1/extractions", files={"file": (name, PDF, "application/pdf")}).json()

    filename = body["document"]["filename"]
    assert "/" not in filename
    assert "\x07" not in filename
    assert len(filename) <= 255


def test_an_unexpected_failure_is_a_plain_500_without_internals(perfect_reply: str) -> None:
    class BrokenParser(FakeParser):
        def parse(self, pdf: bytes) -> ParsedDocument:
            raise RuntimeError("secret internal detail /Users/someone")

    pipeline = InvoicePipeline(BrokenParser(), ScriptedProvider([perfect_reply]))
    api = TestClient(signed_in(create_app(pipeline)), raise_server_exceptions=False)

    response = upload(api)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal error."}


def test_the_replay_factory_extracts_a_recorded_document_offline() -> None:
    """The end-to-end test runs the real service on recorded parses and replies: no key."""
    from pathlib import Path

    from docforge.wiring import build_replay_pipelines

    repo = Path(__file__).resolve().parents[2]
    settings = Settings(
        _env_file=None,
        gemini_api_key=None,
        recordings_dir=str(repo / "tests" / "fixtures" / "recorded"),
    )
    pipelines = build_replay_pipelines(settings)

    pdf = (repo / "tests" / "fixtures" / "synthetic" / "pair_002" / "invoice.pdf").read_bytes()
    result = pipelines["invoice"].run(pdf)

    assert set(pipelines) == {"invoice", "purchase_order"}
    assert result.responses[0].model == settings.gemini_model
