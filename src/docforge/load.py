"""`python -m docforge.load`: a load test against a running API and worker.

Each organisation uploads its files at once (bounded by `--concurrency`), the run waits for
every document to be processed, then searches and reads the review queue under the same
concurrency. Latency percentiles are reported per kind of request; errors are counted, not
dropped. `scripts/load.sh` starts a fresh stack on recorded parses and model replies, so the
numbers are the system's own: model time is measured separately, in the live eval.
"""

import argparse
import asyncio
import json
import math
import os
import platform
import subprocess
import time
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import BaseModel

FINAL = {"extracted", "failed"}


@dataclass(frozen=True)
class Sample:
    name: str
    status: int  # 0 when the request did not complete
    ms: float


@dataclass(frozen=True)
class Tenant:
    key: str
    files: list[tuple[str, bytes]]  # (document type, PDF)


class Latency(BaseModel):
    """Percentiles of the successful requests only; none without successes, and no tail
    percentiles from fewer than `_TAIL_SAMPLES` (the 95th of five is just the largest)."""

    count: int
    errors: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    max_ms: float | None


class Documents(BaseModel):
    uploaded: int
    processed: int
    failed: int
    not_finished: int


class LoadReport(BaseModel):
    concurrency: int
    tenants: int
    wall_seconds: float
    environment: dict[str, str]  # where it ran: numbers from another machine differ
    documents: Documents
    # With model replies replayed (no model time): the system's own ceiling, not capacity.
    # Live throughput is bound by the model's latency and rate limits.
    replay_documents_per_minute: float
    requests: dict[str, Latency]


_TAIL_SAMPLES = 20


def environment() -> dict[str, str]:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - a developer's own checkout
            capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
    except (OSError, subprocess.CalledProcessError):
        sha = "unknown"
    return {
        "git_sha": sha,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpus": str(os.cpu_count() or 0),
    }


def run_failed(report: "LoadReport", expected_uploads: int) -> bool:
    """A load run passes only if every upload got through, every document finished and
    no request failed: a run against an API that never came up must not pass."""
    return (
        report.documents.uploaded < expected_uploads
        or report.documents.failed > 0
        or report.documents.not_finished > 0
        or any(latency.errors for latency in report.requests.values())
    )


def _percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def summarise(samples: Sequence[Sample]) -> dict[str, Latency]:
    by_name: dict[str, list[Sample]] = {}
    for sample in samples:
        by_name.setdefault(sample.name, []).append(sample)
    out = {}
    for name, group in sorted(by_name.items()):
        ok = [s.ms for s in group if 0 < s.status < 400]
        tail = len(ok) >= _TAIL_SAMPLES
        out[name] = Latency(
            count=len(group),
            errors=sum(not 0 < s.status < 400 for s in group),
            p50_ms=round(_percentile(ok, 0.50), 1) if ok else None,
            p95_ms=round(_percentile(ok, 0.95), 1) if tail else None,
            p99_ms=round(_percentile(ok, 0.99), 1) if tail else None,
            max_ms=round(max(ok), 1) if ok else None,
        )
    return out


async def _timed(
    samples: list[Sample], name: str, call: Awaitable[httpx.Response]
) -> httpx.Response | None:
    started = time.perf_counter()
    try:
        response = await call
    except httpx.HTTPError:
        samples.append(Sample(name, 0, (time.perf_counter() - started) * 1000))
        return None
    samples.append(Sample(name, response.status_code, (time.perf_counter() - started) * 1000))
    return response


async def run_load(
    client: httpx.AsyncClient,
    tenants: Sequence[Tenant],
    *,
    concurrency: int,
    questions: Sequence[str],
    poll_seconds: float = 0.5,
    timeout_seconds: float = 1800,
) -> LoadReport:
    samples: list[Sample] = []
    gate = asyncio.Semaphore(concurrency)
    begun = time.perf_counter()

    async def upload(
        tenant: Tenant, doc_type: str, pdf: bytes, n: int
    ) -> tuple[str, str, float] | None:
        async with gate:
            sent = time.perf_counter()
            response = await _timed(
                samples,
                "upload",
                client.post(
                    "/v1/documents",
                    headers={"Authorization": f"Bearer {tenant.key}"},
                    data={"doc_type": doc_type},
                    files={"file": (f"load-{n}.pdf", pdf, "application/pdf")},
                ),
            )
        if response is None or response.status_code >= 400:
            return None
        return tenant.key, response.json()["document"]["id"], sent

    uploads = await asyncio.gather(
        *(
            upload(tenant, doc_type, pdf, n)
            for tenant in tenants
            for n, (doc_type, pdf) in enumerate(tenant.files)
        )
    )
    accepted = [u for u in uploads if u is not None]

    async def finish(key: str, document_id: str, sent: float) -> str:
        deadline = sent + timeout_seconds
        while time.perf_counter() < deadline:
            try:
                async with gate:
                    response = await client.get(
                        f"/v1/documents/{document_id}",
                        headers={"Authorization": f"Bearer {key}"},
                    )
                status = (
                    response.json()["document"]["status"] if response.status_code == 200 else ""
                )
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                status = ""  # a poll lost under load is asked again, not the end of the run
            if status in FINAL:
                samples.append(Sample("processed", 200, (time.perf_counter() - sent) * 1000))
                return status
            await asyncio.sleep(poll_seconds)
        return "not_finished"

    outcomes = await asyncio.gather(*(finish(*u) for u in accepted))
    processed_at = time.perf_counter()

    async def read(key: str, name: str, path: str, params: dict[str, str]) -> None:
        async with gate:
            await _timed(
                samples,
                name,
                client.get(path, params=params, headers={"Authorization": f"Bearer {key}"}),
            )

    reads = []
    for tenant in tenants:
        for question in questions:
            for mode in ("keyword", "hybrid"):
                reads.append(
                    read(tenant.key, f"search.{mode}", "/v1/search", {"q": question, "mode": mode})
                )
        reads.append(read(tenant.key, "review.queue", "/v1/review/queue", {}))
    await asyncio.gather(*reads)

    processing_minutes = max(processed_at - begun, 1e-9) / 60
    done = sum(o in FINAL for o in outcomes)
    return LoadReport(
        concurrency=concurrency,
        tenants=len(tenants),
        wall_seconds=round(time.perf_counter() - begun, 1),
        documents=Documents(
            uploaded=len(accepted),
            processed=sum(o == "extracted" for o in outcomes),
            failed=sum(o == "failed" for o in outcomes),
            not_finished=sum(o == "not_finished" for o in outcomes),
        ),
        environment={},
        replay_documents_per_minute=round(done / processing_minutes, 1),
        requests=summarise(samples),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m docforge.load", description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--keys", type=Path, required=True, help="one admin API key per line")
    parser.add_argument("--fixtures", type=Path, default=Path("tests/fixtures/synthetic"))
    parser.add_argument("--coa", type=Path, default=Path("tests/fixtures/coa"))
    parser.add_argument("--questions", type=Path, required=True, help="a JSON list of questions")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    files: list[tuple[str, bytes]] = []
    for pdf in sorted(args.fixtures.glob("*/invoice.pdf")):
        files.append(("invoice", pdf.read_bytes()))
    for pdf in sorted(args.fixtures.glob("*/purchase_order.pdf")):
        files.append(("purchase_order", pdf.read_bytes()))
    for pdf in sorted(args.coa.glob("*/coa.pdf")):
        files.append(("coa", pdf.read_bytes()))
    keys = [line.strip() for line in args.keys.read_text().splitlines() if line.strip()]
    tenants = [Tenant(key, files) for key in keys]
    questions = json.loads(args.questions.read_text())

    async def go() -> LoadReport:
        limits = httpx.Limits(max_connections=args.concurrency)
        async with httpx.AsyncClient(base_url=args.api, timeout=120, limits=limits) as client:
            return await run_load(
                client, tenants, concurrency=args.concurrency, questions=questions
            )

    report = asyncio.run(go()).model_copy(update={"environment": environment()})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(report.model_dump_json(indent=2))
    return 1 if run_failed(report, expected_uploads=len(files) * len(tenants)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
