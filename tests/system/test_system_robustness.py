"""Every hostile file through the stack with the real parser and converter (`make robustness`):
each refused or finished with a reason a person can read, among the outcomes safe for it -
and after each, the API, worker and converter still up, never killed for memory, never
restarted."""

import json
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from docforge.synth.adversarial import Case, cases, one_page_pdf
from system.stack import BASE_URL, Organisation, compose

pytestmark = pytest.mark.robustness

FINISHED = {"ready", "processed", "failed"}
CONTAINERS = ("api", "worker", "converter")


@pytest.fixture(scope="module", autouse=True)
def capacity_stack() -> Iterator[None]:
    """The run proves nothing unless the files met the real parser: check before starting."""
    probe = "from docforge.config import get_settings; print(get_settings().simulated_model)"
    if compose("exec", "-T", "api", "python", "-c", probe).strip() != "True":
        pytest.exit(
            "The stack replays recordings, so hostile files never reach the parser. "
            "Run `make robustness`, which starts it with deploy/compose.capacity.yml.",
            returncode=2,
        )
    yield


def state(service: str) -> dict[str, Any]:
    container = compose("ps", "-q", service).strip()
    out = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        ["docker", "inspect", container],  # noqa: S607 - the operator's own docker
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout  # fmt: skip
    found = json.loads(out)[0]
    return {
        "running": found["State"]["Running"],
        "oom_killed": found["State"]["OOMKilled"],
        "started_at": found["State"]["StartedAt"],
    }


@pytest.fixture(scope="module")
def before() -> dict[str, dict[str, Any]]:
    return {service: state(service) for service in CONTAINERS}


def outcome(org: Organisation, case: Case) -> tuple[str, str]:
    """What happened to the file, and the reason given."""
    reply = org.upload(Path(case.filename), case.doc_type, content=case.build())
    if reply.status_code in (413, 415, 422):
        return str(reply.status_code), str(reply.json()["detail"])
    assert reply.status_code in (200, 202), f"{reply.status_code}: {reply.text}"
    document_id = reply.json()["document"]["id"]
    document = org.wait(document_id, lambda d: d["stage"] in FINISHED, timeout=900)
    timeline = org.client("integrator").get(f"/v1/documents/{document_id}/timeline").json()
    if document["stage"] == "failed":
        return "failed", str(timeline[-1].get("detail") or "")
    return "processed", "processed"


@pytest.mark.parametrize("case", cases(), ids=lambda case: case.name)
def test_a_hostile_file_is_refused_or_finished_safely(
    org: Organisation, case: Case, before: dict[str, dict[str, Any]]
) -> None:
    started = time.monotonic()
    result, reason = outcome(org, case)

    print(f"{case.name}: {result} in {time.monotonic() - started:.1f}s - {reason[:160]}")
    assert result in case.expect, f"{case.name} ({case.why}): {result}: {reason}"
    assert reason.strip(), f"{case.name}: {result} without a reason"
    for service in CONTAINERS:
        now = state(service)
        assert now["running"] and not now["oom_killed"], (service, now)
        assert now["started_at"] == before[service]["started_at"], f"{service} restarted"
    healthy = httpx.get(f"{BASE_URL}/healthz", verify=org.tls(), timeout=10)
    assert healthy.status_code == 200


def test_after_them_all_an_ordinary_invoice_is_still_read(org: Organisation) -> None:
    reply = org.upload(Path("ordinary.pdf"), content=one_page_pdf(b"<< /Ordinary true >>"))
    assert reply.status_code == 202, reply.text
    document = org.wait(reply.json()["document"]["id"], lambda d: d["stage"] in FINISHED)
    assert document["stage"] != "failed", document
