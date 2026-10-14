"""V2 0.4 on the running stack: an invoice approved in review becomes a label, and a reading
is scored against it, through the same export and API key the owner will use."""

import json
import secrets
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from docforge.evals.real import pull, score_against_record
from docforge.extraction.schema import InvoiceExtraction
from system.stack import BASE_URL, Organisation, make_organisation

PAIRS = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
FINISHED = {"ready", "processed", "failed"}


@pytest.fixture
def labelling_org(verify: Path) -> Iterator[Organisation]:
    # Its own organisation: the other tests' uploads share one file name, which pull refuses.
    org = make_organisation(verify)
    yield org
    org.close()


def test_an_approved_invoice_becomes_a_label_and_a_reading_is_scored_against_it(
    labelling_org: Organisation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = labelling_org
    name = f"real-{secrets.token_hex(3)}.pdf"
    uploaded = org.upload(PAIRS / "pair_005" / "invoice.pdf", filename=name)
    assert uploaded.status_code in (200, 202), uploaded.text
    document_id = uploaded.json()["document"]["id"]
    assert org.wait(document_id, lambda d: d["stage"] in FINISHED)["stage"] != "failed"

    person = org.session()
    corrected = person.post(
        f"/v1/documents/{document_id}/corrections",
        json={"path": "invoice_no", "text": "INV-REAL-1", "reason": "labelling check",
              "email": org.email, "pin": org.pin},
    )  # fmt: skip
    assert corrected.status_code == 200, corrected.text
    review = corrected.json()
    signed = person.post(
        f"/v1/documents/{document_id}/review",
        json={"outcome": "approved", "meaning": review["meanings"]["approved"],
              "reason": "labelling check", "email": org.email, "pin": org.pin,
              "override_reason": "system check" if review["blockers"] else None,
              "expected_record_sha256": review["record_sha256"]},
    )  # fmt: skip
    assert signed.status_code in (200, 201), signed.text

    monkeypatch.setenv("DOCFORGE_API_KEY", org.keys["reader"])
    transport = httpx.HTTPTransport(verify=org.tls())
    assert pull(BASE_URL, tmp_path / "labels", transport=transport) == 1
    label = json.loads((tmp_path / "labels" / f"{name}.json").read_text(encoding="utf-8"))
    assert label["document_id"] == document_id
    assert label["record"]["invoice_no"]["value"] == "INV-REAL-1"

    # The stack's own reading, before the correction: only the corrected field differs.
    extraction = org.client("reader").get(f"/v1/documents/{document_id}/extraction")
    reading = InvoiceExtraction.model_validate(extraction.json()["extraction"])
    score = score_against_record(name, label["record"], reading)

    assert [field.path for field in score.fields if field.outcome != "correct"] == ["invoice_no"]
    assert score.extracted_lines == score.expected_lines > 0
