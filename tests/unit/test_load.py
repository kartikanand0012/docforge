"""The load runner: uploads, waits for processing, searches and reads under concurrency, and
reports latency percentiles per kind of request, counting errors rather than hiding them."""

import asyncio
import json
import uuid

import httpx
import pytest

from docforge.load import Sample, Tenant, run_load, summarise


def test_percentiles_and_errors_per_kind_of_request() -> None:
    samples = [Sample("upload", 202, float(ms)) for ms in range(1, 101)]
    samples += [Sample("upload", 500, 5.0), Sample("upload", 0, 7.0)]

    summary = summarise(samples)["upload"]

    assert summary.count == 102
    assert summary.errors == 2
    assert summary.p50_ms == pytest.approx(51, abs=1)
    assert summary.p95_ms == pytest.approx(96, abs=1)
    assert summary.max_ms == 100


def fake_api() -> httpx.MockTransport:
    polled: dict[str, int] = {}
    uploads = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal uploads
        assert request.headers["authorization"].startswith("Bearer dfk_")
        path = request.url.path
        if path == "/v1/documents" and request.method == "POST":
            uploads += 1
            if uploads == 3:
                return httpx.Response(500, json={"detail": "Internal error."})
            document_id = str(uuid.uuid4())
            return httpx.Response(202, json={"document": {"id": document_id, "status": "received"}})
        if path.startswith("/v1/documents/"):
            document_id = path.rsplit("/", 1)[-1]
            polled[document_id] = polled.get(document_id, 0) + 1
            status = "extracted" if polled[document_id] > 1 else "processing"
            return httpx.Response(200, json={"document": {"id": document_id, "status": status}})
        if path == "/v1/search":
            return httpx.Response(200, json={"hits": []})
        if path == "/v1/review/queue":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def test_a_run_reports_every_phase_and_counts_failed_uploads() -> None:
    tenants = [
        Tenant("dfk_a", [("invoice", b"%PDF-a"), ("invoice", b"%PDF-b")]),
        Tenant("dfk_b", [("purchase_order", b"%PDF-c"), ("invoice", b"%PDF-d")]),
    ]

    async def go() -> object:
        async with httpx.AsyncClient(transport=fake_api(), base_url="http://api") as client:
            return await run_load(
                client, tenants, concurrency=3, questions=["batch 1"], poll_seconds=0.0
            )

    report = asyncio.run(go())
    summary = json.loads(report.model_dump_json())  # type: ignore[attr-defined]

    assert summary["documents"] == {"uploaded": 3, "processed": 3, "failed": 0, "not_finished": 0}
    assert summary["requests"]["upload"]["errors"] == 1
    for kind in ("upload", "processed", "search.keyword", "search.hybrid", "review.queue"):
        assert summary["requests"][kind]["count"] > 0, kind
    assert summary["concurrency"] == 3
