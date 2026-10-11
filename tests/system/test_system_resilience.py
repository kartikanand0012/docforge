"""C2 on the running stack: a killed worker, a restarted database and storage going away
lose nothing, and the stack comes back by itself. Each test leaves every service running."""

import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from system.stack import BASE_URL, Organisation, advisory_lock, compose

PAIRS = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
FINISHED = {"ready", "processed", "failed"}


def healthy(org: Organisation, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{BASE_URL}/healthz", verify=org.tls(), timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise TimeoutError("the API did not come back")


@pytest.fixture
def services_restored() -> Iterator[None]:
    yield
    compose("up", "-d", "--wait", "worker", "postgres", "minio", "api", timeout=600)


def test_a_worker_killed_mid_document_loses_nothing(
    org: Organisation, services_restored: None
) -> None:
    compose("stop", "worker")  # the document waits until its indexing lock is held
    document_id = org.upload(PAIRS / "pair_002" / "invoice.pdf").json()["document"]["id"]

    with advisory_lock(f"index:{document_id}"):  # the worker stops there, mid-document
        compose("start", "worker")
        org.wait(document_id, lambda d: d["stage"] == "indexing", timeout=120)
        time.sleep(1)  # waiting on the lock now
        compose("kill", "-s", "SIGKILL", "worker")
    assert org.wait(document_id, lambda d: True)["stage"] == "indexing"
    compose("up", "-d", "worker")

    document = org.wait(document_id, lambda d: d["stage"] in FINISHED, timeout=300)
    assert document["stage"] == "ready", document
    timeline = org.client("integrator").get(f"/v1/documents/{document_id}/timeline").json()
    assert sum(1 for step in timeline if step["stage"] == "extracting") == 1  # not read twice


def test_after_the_database_restarts_uploads_work_again(
    org: Organisation, services_restored: None
) -> None:
    compose("restart", "postgres", timeout=300)
    healthy(org)

    deadline = time.monotonic() + 60
    while True:  # the first requests may meet connections the restart closed
        reply = org.upload(PAIRS / "pair_002" / "purchase_order.pdf", "purchase_order")
        if reply.status_code in (200, 202) or time.monotonic() > deadline:
            break
        assert reply.status_code == 503, reply.text  # unavailable, never a 500
        time.sleep(2)
    assert reply.status_code in (200, 202), reply.text
    org.wait(reply.json()["document"]["id"], lambda d: d["stage"] in FINISHED, timeout=300)


def test_with_storage_down_an_upload_is_refused_as_unavailable(
    org: Organisation, services_restored: None
) -> None:
    compose("stop", "minio")
    try:
        reply = org.upload(PAIRS / "pair_003" / "purchase_order.pdf", "purchase_order")
        assert reply.status_code == 503, reply.text
        assert reply.json()["detail"]
    finally:
        compose("start", "minio")

    # Seen once (2026-10-08, the first run after an image rebuild): still refused after 60 s.
    # The wait is longer and a failure says what storage was doing.
    deadline = time.monotonic() + 120
    while True:
        reply = org.upload(PAIRS / "pair_003" / "purchase_order.pdf", "purchase_order")
        if reply.status_code in (200, 202) or time.monotonic() > deadline:
            break
        time.sleep(2)
    storage = compose("ps", "-a", "minio", "--format", "{{.Status}}").strip()
    assert reply.status_code in (200, 202), f"{reply.text} (storage: {storage})"
