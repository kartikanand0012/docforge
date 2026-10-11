"""C0, C1, C6 on the running stack: who may do what, one document end to end, and nothing
crosses from one organisation to another."""

from pathlib import Path

import httpx

from system.stack import BASE_URL, Organisation

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
INVOICE = FIXTURES / "synthetic" / "pair_001" / "invoice.pdf"


def test_without_a_credential_nothing_is_answered(org: Organisation) -> None:
    with httpx.Client(base_url=BASE_URL, verify=org.tls()) as anonymous:
        for path in ("/v1/documents", "/v1/review/queue", "/v1/search?q=invoice", "/v1/audit"):
            assert anonymous.get(path).status_code == 401, path


def test_a_recorded_invoice_is_extracted_end_to_end_once(org: Organisation) -> None:
    first = org.upload(INVOICE)
    again = org.upload(INVOICE)

    assert first.status_code == 202 and first.json()["created"] is True
    assert again.status_code == 200 and again.json()["created"] is False
    document_id = first.json()["document"]["id"]
    assert again.json()["document"]["id"] == document_id

    document = org.wait(document_id)
    assert document["status"] == "extracted"
    extraction = org.client("integrator").get(f"/v1/documents/{document_id}/extraction")
    assert extraction.status_code == 200 and extraction.json()


def test_each_role_may_do_only_its_own_work(org: Organisation) -> None:
    for role in ("reviewer", "reader"):
        assert org.upload(INVOICE, role=role).status_code == 403, role
    for role in ("integrator", "reviewer", "reader"):
        assert org.client(role).get("/v1/audit").status_code == 403, role
        assert org.client(role).get("/v1/webhooks").status_code == 403, role
    assert org.client("admin").get("/v1/audit").status_code == 200


def test_another_organisations_documents_are_not_found(
    org: Organisation, other_org: Organisation
) -> None:
    document_id = org.upload(INVOICE).json()["document"]["id"]
    org.wait(document_id)
    stranger = other_org.client("admin")

    for path in ("", "/extraction", "/review", "/timeline", "/pages/1", "/audit"):
        assert stranger.get(f"/v1/documents/{document_id}{path}").status_code == 404, path
    listed = stranger.get("/v1/documents").json()["items"]
    assert document_id not in {item["id"] for item in listed}
    hits = stranger.get("/v1/search", params={"q": "invoice", "mode": "keyword"}).json()
    assert document_id not in {hit["document_id"] for hit in hits["results"]}
