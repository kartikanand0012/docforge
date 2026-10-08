"""C5, C7, C10-C12, C14, C16 on the running stack: progress streamed, a correction and a
signature with a PIN, search, chat, knowledge bases, AI agents, audit log and webhooks."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from system.stack import BASE_URL, Organisation

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PAIRS = FIXTURES / "synthetic"
FINISHED = {"ready", "processed", "failed"}


def events(response: httpx.Response) -> list[tuple[str, dict[str, Any]]]:
    """Server-sent events, the last one even without its closing blank line."""
    out: list[tuple[str, dict[str, Any]]] = []
    name, data = "message", ""
    for line in [*response.iter_lines(), ""]:
        if line.startswith("event:"):
            name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data += line.removeprefix("data:").strip()
        elif not line and data:
            out.append((name, json.loads(data)))
            name, data = "message", ""
    return out


def processed(org: Organisation, pair: str, doc_type: str = "invoice") -> str:
    reply = org.upload(PAIRS / pair / f"{doc_type}.pdf", doc_type)
    assert reply.status_code in (200, 202), reply.text
    document_id: str = reply.json()["document"]["id"]
    org.wait(document_id, lambda d: d["stage"] in FINISHED)
    return document_id


# --- C10: progress ------------------------------------------------------------------------


def test_every_stage_of_a_document_is_streamed_to_the_end(org: Organisation) -> None:
    reply = org.upload(PAIRS / "pair_003" / "invoice.pdf")
    document_id = reply.json()["document"]["id"]

    with org.client("integrator").stream(
        "GET", f"/v1/documents/{document_id}/events", timeout=300
    ) as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        stages = [data["stage"] for name, data in events(stream) if name == "stage"]

    assert stages[-1] in FINISHED, stages
    assert stages[-1] != "failed", stages
    for expected in ("parsing", "extracting", "checking"):
        assert expected in stages, stages


# --- C5: review ---------------------------------------------------------------------------


def test_a_correction_and_a_signature_need_the_pin_and_bind_the_record(
    org: Organisation,
) -> None:
    document_id = processed(org, "pair_004")
    person = org.session()
    review = person.get(f"/v1/documents/{document_id}/review").json()
    assert "invoice_no" in review["editable_paths"]
    not_the_pin = "1" * 8 if org.pin != "1" * 8 else "2" * 8

    wrong_pin = person.post(
        f"/v1/documents/{document_id}/corrections",
        json={"path": "invoice_no", "text": "INV-SYSTEM-1", "reason": "system check",
              "email": org.email, "pin": not_the_pin},
    )  # fmt: skip
    assert wrong_pin.status_code in (401, 403), wrong_pin.text

    corrected = person.post(
        f"/v1/documents/{document_id}/corrections",
        json={"path": "invoice_no", "text": "INV-SYSTEM-1", "reason": "system check",
              "email": org.email, "pin": org.pin},
    )  # fmt: skip
    assert corrected.status_code == 200, corrected.text
    after = corrected.json()
    assert after["record"]["invoice_no"]["value"] == "INV-SYSTEM-1"

    sign = {
        "outcome": "rejected", "meaning": after["meanings"]["rejected"], "reason": "system check",
        "email": org.email, "pin": org.pin,
    }  # fmt: skip
    stale = person.post(
        f"/v1/documents/{document_id}/review",
        json={**sign, "expected_record_sha256": review["record_sha256"]},
    )
    assert stale.status_code == 409, stale.text  # the record changed since it was shown

    signed = person.post(
        f"/v1/documents/{document_id}/review",
        json={**sign, "expected_record_sha256": after["record_sha256"]},
    )
    assert signed.status_code in (200, 201), signed.text
    final = person.get(f"/v1/documents/{document_id}/review").json()
    assert final["review"] and final["signature_valid"] is True


# --- C7: search ---------------------------------------------------------------------------


def test_search_finds_a_document_by_its_words_in_both_modes(org: Organisation) -> None:
    document_id = processed(org, "pair_005")
    extraction = org.client("integrator").get(f"/v1/documents/{document_id}/extraction").json()
    query = str(extraction.get("invoice_no") or "invoice")

    for mode in ("keyword", "hybrid"):
        reply = org.client("reviewer").get("/v1/search", params={"q": query, "mode": mode})
        assert reply.status_code == 200, (mode, reply.text)
        assert document_id in {hit["document_id"] for hit in reply.json()["results"]}, mode


# --- C11-C12: chat and knowledge bases ----------------------------------------------------

ANSWER_STATUSES = {"supported", "partly_supported", "unsupported", "not_found"}


def test_a_streamed_answer_has_its_stages_then_one_ending(org: Organisation) -> None:
    """The stream's shape. On this replayed stack a question never recorded ends in an error
    event (finding F1 in docs/real-world-coverage.md); a recorded one in an answer."""
    document_id = processed(org, "pair_006")
    with org.session().stream(
        "POST", "/v1/chat/stream",
        json={"question": "What is the invoice total?", "document_id": document_id},
        timeout=300,
    ) as stream:  # fmt: skip
        assert stream.status_code == 200
        received = events(stream)

    names = [name for name, _ in received]
    assert names[:2] == ["stage", "stage"] and names[-1] in ("answer", "error"), received
    assert names.count("answer") + names.count("error") == 1, received
    ending = received[-1][1]
    if names[-1] == "error":
        assert ending["status"] == 503 and ending["detail"]
        return
    assert ending["status"] in ANSWER_STATUSES
    for citation in ending["citations"]:
        assert citation["document_id"] == document_id  # the conversation's scope held


def test_a_knowledge_base_keeps_a_question_within_its_documents(org: Organisation) -> None:
    inside = processed(org, "pair_007")
    processed(org, "pair_008")
    admin = org.client("admin")
    made = admin.post("/v1/collections", json={"name": "System check", "description": ""})
    assert made.status_code == 201, made.text
    kb = made.json()["id"]
    added = admin.post(f"/v1/collections/{kb}/documents", json={"document_ids": [inside]})
    assert added.status_code in (200, 201, 204), added.text
    assert admin.post("/v1/collections", json={"name": "SYSTEM CHECK"}).status_code == 409

    answer = org.session().post(
        "/v1/chat", json={"question": "Who is the supplier?", "collection_id": kb}
    )
    assert answer.status_code in (200, 503), answer.text  # 503: not recorded (F1)
    if answer.status_code == 200:
        assert {c["document_id"] for c in answer.json()["citations"]} <= {inside}


# --- C14: AI agents -----------------------------------------------------------------------


def mcp(client: httpx.Client, method: str, params: dict[str, Any] | None = None) -> httpx.Response:
    return client.post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={"Accept": "application/json, text/event-stream"},
    )


def test_ai_agents_get_six_read_only_tools_with_a_reader_key_only(org: Organisation) -> None:
    init = {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "system-check", "version": "1"},
    }  # fmt: skip
    assert mcp(org.client("reader"), "initialize", init).status_code == 200
    tools = mcp(org.client("reader"), "tools/list")
    assert tools.status_code == 200, tools.text
    names = {tool["name"] for tool in tools.json()["result"]["tools"]}
    assert names == {
        "list_knowledge_bases", "list_documents", "search_documents", "ask",
        "get_document", "get_page_text",
    }  # fmt: skip

    for role in ("integrator", "admin"):
        assert mcp(org.client(role), "tools/list").status_code == 403, role
    foreign = org.client("reader").post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Origin": "https://evil.example", "Accept": "application/json"},
    )
    assert foreign.status_code == 403


# --- C16: audit log and webhooks ----------------------------------------------------------


def test_the_audit_log_is_listed_exported_and_its_chain_holds(org: Organisation) -> None:
    processed(org, "pair_009")
    admin = org.client("admin")

    page = admin.get("/v1/audit", params={"limit": 5})
    assert page.status_code == 200 and page.json()["items"]
    export = admin.get("/v1/audit/export.csv")
    assert export.status_code == 200 and export.text.splitlines()[0].startswith("id,")
    check = admin.get("/v1/audit/verification").json()
    assert check["consistent"] is True and check["entries"] > 0


def test_webhooks_take_only_public_https_and_show_their_secret_once(org: Organisation) -> None:
    admin = org.client("admin")
    for unsafe in ("http://example.com/hook", "https://127.0.0.1/hook", "https://localhost/x"):
        reply = admin.post("/v1/webhooks", json={"url": unsafe, "events": ["webhook.test"]})
        assert reply.status_code == 422, (unsafe, reply.text)

    made = admin.post(
        "/v1/webhooks", json={"url": "https://example.com/hook", "events": ["webhook.test"]}
    )
    assert made.status_code == 201, made.text
    assert made.headers["cache-control"] == "no-store" and made.json()["secret"]
    hook = made.json()["id"]
    listed = admin.get("/v1/webhooks").json()
    assert "secret" not in json.dumps(listed).replace("secret_rotated_at", "")

    assert admin.patch(f"/v1/webhooks/{hook}", json={"active": False}).status_code == 200
    assert admin.post(f"/v1/webhooks/{hook}/test").status_code == 409  # disabled
    assert admin.delete(f"/v1/webhooks/{hook}").status_code == 204


# --- C9: the edge -------------------------------------------------------------------------


def test_bodies_over_the_cap_are_refused_at_the_edge_even_chunked(org: Organisation) -> None:
    big = b"%PDF-1.7\n" + b"0" * (13 * 1024 * 1024)
    whole = org.upload(PAIRS / "pair_001" / "invoice.pdf", content=big, filename="big.pdf")
    assert whole.status_code == 413

    def chunks() -> Iterator[bytes]:  # a real multipart upload, sent without a length
        yield (b"--b\r\nContent-Disposition: form-data; name=\"doc_type\"\r\n\r\ninvoice\r\n"
               b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"big.pdf\"\r\n"
               b"Content-Type: application/pdf\r\n\r\n%PDF-1.7\n")  # fmt: skip
        for _ in range(13):
            yield b"0" * (1024 * 1024)
        yield b"\r\n--b--\r\n"

    with httpx.Client(base_url=BASE_URL, verify=org.tls(), timeout=60) as client:
        reply = client.post(
            "/v1/documents",
            content=chunks(),
            headers={"Authorization": f"Bearer {org.keys['integrator']}",
                     "Content-Type": "multipart/form-data; boundary=b"},
        )  # fmt: skip
    assert reply.status_code == 413


def test_files_that_are_not_accepted_are_refused_with_a_reason(org: Organisation) -> None:
    exe = org.upload(
        PAIRS / "pair_001" / "invoice.pdf", content=b"MZ\x90\x00" + b"\0" * 512,
        filename="tool.exe",
    )  # fmt: skip
    assert exe.status_code == 415 and exe.json()["detail"]
    kind = org.upload(PAIRS / "pair_001" / "invoice.pdf", "passport")
    assert kind.status_code == 422


def test_protected_and_older_office_files_are_refused_saying_why(org: Organisation) -> None:
    import io
    import secrets

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(PAIRS / "pair_001" / "invoice.pdf"))
    writer.encrypt(user_password=secrets.token_hex(8), owner_password=secrets.token_hex(8))
    locked = io.BytesIO()
    writer.write(locked)
    pdf = org.upload(PAIRS / "pair_001" / "invoice.pdf", content=locked.getvalue())
    assert pdf.status_code == 422 and "password" in pdf.json()["detail"]

    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 1024
    office = org.upload(PAIRS / "pair_001" / "invoice.pdf", content=ole, filename="old.xls")
    assert office.status_code == 415 and "password" in office.json()["detail"]
