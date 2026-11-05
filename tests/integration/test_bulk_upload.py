"""V2 P6 part A: many files at once - dropped together or in one ZIP - each its own document,
through the upload's own checks and entry point; one bad file never fails the batch.
"""

import io
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from docforge.api.app import create_app
from docforge.batches import BatchService
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry, Document, DocumentVersion
from docforge.db.session import SessionFactory
from docforge.db.tenancy import tenant_scope
from docforge.documents import DocumentService
from docforge.extraction.pipeline import InvoicePipeline
from docforge.storage import MemoryObjectStore
from docforge.upload_checks import UNREADABLE_PDF_MESSAGE, too_large_message
from docforge.zipsafe import NESTED
from fakes import FakeParser, ScriptedProvider, signed_in

pytestmark = pytest.mark.integration

INVOICE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "pair_001" / "invoice.pdf"
).read_bytes()
CORRUPT = b"%PDF-1.4\nthis is not really a PDF\n"
ZIP_LIMIT = 500 * 1024 * 1024


def invoice(n: int) -> bytes:
    """A valid one-page invoice PDF, different bytes for each `n`."""
    return INVOICE + f"\n%copy {n}\n".encode()


def make_zip(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return out.getvalue()


class Desk:
    def __init__(
        self, sessions: SessionFactory, *, max_upload_bytes: int = 10 * 1024 * 1024,
        max_zip_bytes: int = ZIP_LIMIT,
    ) -> None:  # fmt: skip
        self.sessions = sessions
        self.store = MemoryObjectStore()
        self.queued: list[uuid.UUID] = []
        # Every reading fails (a reply that never fits), so retries can be seen.
        pipeline = InvoicePipeline(FakeParser(), ScriptedProvider(["not json"] * 1000))
        self.service = DocumentService(
            sessions, self.store, {"invoice": pipeline},
            lambda session, version: self.queued.append(version.id),
        )  # fmt: skip
        self.batches = BatchService(sessions, self.service, max_upload_bytes=max_upload_bytes)
        self.app = create_app(
            None, service=self.service, batches=self.batches, max_upload_bytes=max_upload_bytes,
            max_zip_bytes=max_zip_bytes,
        )  # fmt: skip

    def client(self, **who: Any) -> TestClient:
        return TestClient(signed_in(self.app, **who))

    def documents(self, tenant: uuid.UUID = DEFAULT_TENANT_ID) -> int:
        with tenant_scope(tenant), self.sessions() as session:
            return session.scalar(
                select(func.count()).select_from(Document).where(Document.tenant_id == tenant)
            ) or 0  # fmt: skip

    def stored(self) -> int:
        return len(self.store._objects)

    def audit(self, action: str) -> list[AuditEntry]:
        with self.sessions() as session:
            return list(session.scalars(select(AuditEntry).where(AuditEntry.action == action)))


@pytest.fixture
def desk(sessions: SessionFactory) -> Desk:
    return Desk(sessions)


def send_zip(client: TestClient, data: bytes, name: str = "bills.zip") -> Any:
    return client.post("/v1/batches/zip", files={"file": (name, data, "application/zip")})


# AC-P6-1: 10 valid, 1 corrupt, 1 already in the workspace: 12 accounted for


def test_a_zip_of_ten_bills_a_corrupt_file_and_a_duplicate_reconciles_to_twelve(
    desk: Desk,
) -> None:
    client = desk.client()
    known = client.post("/v1/documents", files={"file": ("earlier.pdf", invoice(99))})
    assert known.status_code == 202
    entries = {f"october/bill-{n:02d}.pdf": invoice(n) for n in range(10)}
    entries["october/broken.pdf"] = CORRUPT
    entries["october/again.pdf"] = invoice(99)

    answer = send_zip(client, make_zip(entries))

    assert answer.status_code == 201, answer.text
    batch = answer.json()
    counts = batch["counts"]
    assert (counts["total"], counts["received"], counts["already_received"],
            counts["refused"]) == (12, 10, 1, 1)  # fmt: skip
    assert counts["received"] + counts["already_received"] + counts["refused"] == counts["total"]
    assert counts["waiting"] + counts["processed"] == counts["total"]
    refused = [i for i in batch["items"] if i["outcome"] == "refused"]
    assert [(i["filename"], i["reason"]) for i in refused] == [
        ("broken.pdf", UNREADABLE_PDF_MESSAGE)
    ]
    again = next(i for i in batch["items"] if i["filename"] == "again.pdf")
    assert again["outcome"] == "already_received"
    assert again["document_id"] == known.json()["document"]["id"]
    assert desk.documents() == 11  # 10 new, and the one there before
    assert len(desk.queued) == 11  # nothing read twice
    (made,) = desk.audit("batch.created")
    assert made.details == {"kind": "zip", "files": 12, "received": 10,
                            "already_received": 1, "refused": 1}  # fmt: skip


# AC-P6-2: an unsafe ZIP is refused whole; nothing is made, nothing written


@pytest.mark.parametrize("unsafe", ["../../etc/passwd", "/etc/cron.d/x", "..\\..\\boot.ini"])
def test_a_zip_with_an_entry_reaching_outside_it_is_refused_and_makes_nothing(
    desk: Desk, unsafe: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "a" / "b"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)  # were an entry written by its name, it would land under tmp_path
    data = make_zip({"good.pdf": invoice(1), unsafe: invoice(2)})

    answer = send_zip(desk.client(), data)

    assert answer.status_code == 422
    assert "path outside the ZIP" in answer.json()["detail"]
    assert desk.documents() == 0 and desk.stored() == 0
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_a_zip_bomb_is_refused_and_makes_nothing(desk: Desk) -> None:
    bomb = make_zip({"good.pdf": invoice(1), "bill.pdf": b"\0" * (20 * 1024 * 1024)})
    assert len(bomb) < 100 * 1024  # 20 MB unpacked from a few KB

    answer = send_zip(desk.client(), bomb)

    assert answer.status_code == 422 and "looks unsafe" in answer.json()["detail"]
    assert desk.documents() == 0 and desk.stored() == 0


def test_a_zip_inside_the_zip_is_refused_alone_with_a_clear_message(desk: Desk) -> None:
    data = make_zip({"inner.zip": make_zip({"x.pdf": invoice(1)}), "ok.pdf": invoice(2)})

    batch = send_zip(desk.client(), data).json()

    assert [(i["filename"], i["outcome"], i["reason"]) for i in batch["items"]] == [
        ("inner.zip", "refused", NESTED), ("ok.pdf", "received", None),
    ]  # fmt: skip


def test_a_zip_over_the_size_limit_is_refused_before_it_is_read(sessions: SessionFactory) -> None:
    desk = Desk(sessions, max_zip_bytes=1000)

    answer = send_zip(desk.client(), make_zip({f"{n}.pdf": invoice(n) for n in range(5)}))

    assert answer.status_code == 413 and "ZIP is larger than the limit" in answer.json()["detail"]
    assert desk.documents() == 0


# Dropped files: one request each into a batch; each held to the upload's limit


def test_files_dropped_together_are_each_taken_as_an_upload_is(sessions: SessionFactory) -> None:
    limit = len(invoice(1)) + 100
    desk = Desk(sessions, max_upload_bytes=limit)
    client = desk.client()
    batch = client.post("/v1/batches", json={"files": 3}).json()
    route = f"/v1/batches/{batch['id']}/files"

    good = client.post(route, files={"file": ("a.pdf", invoice(1))})
    large = client.post(route, files={"file": ("big.pdf", invoice(1) + b"x" * 200)})
    corrupt = client.post(route, files={"file": ("c.pdf", CORRUPT)})
    extra = client.post(route, files={"file": ("d.pdf", invoice(4))})

    assert [r.status_code for r in (good, large, corrupt)] == [201, 201, 201]
    assert good.json()["outcome"] == "received"
    assert large.json()["reason"] == too_large_message(limit)
    assert corrupt.json()["reason"] == UNREADABLE_PDF_MESSAGE
    assert extra.status_code == 409 and "has its 3 files already" in extra.json()["detail"]
    counts = client.get(f"/v1/batches/{batch['id']}").json()["counts"]
    assert (counts["total"], counts["received"], counts["refused"], counts["failed"]) == (
        3, 1, 2, 2,
    )  # fmt: skip


def test_files_not_sent_are_counted_and_what_arrived_goes_on(desk: Desk) -> None:
    """The page was left after two of five files: the two are read; three were never sent."""
    client = desk.client()
    batch = client.post("/v1/batches", json={"files": 5}).json()
    for n in range(2):
        client.post(f"/v1/batches/{batch['id']}/files", files={"file": (f"{n}.pdf", invoice(n))})

    counts = client.get(f"/v1/batches/{batch['id']}").json()["counts"]

    assert (counts["total"], counts["not_sent"], counts["waiting"]) == (2, 3, 2)
    assert len(desk.queued) == 2


def test_more_than_fifty_files_at_once_are_pointed_to_a_zip(desk: Desk) -> None:
    answer = desk.client().post("/v1/batches", json={"files": 63})

    assert answer.status_code == 422
    assert "Up to 50 files" in answer.json()["detail"] and "ZIP" in answer.json()["detail"]


# The batch view, and a retry of the failed files only


def test_retry_reads_again_only_the_files_whose_reading_failed(desk: Desk) -> None:
    client = desk.client()
    data = make_zip({"a.pdf": invoice(1), "b.pdf": invoice(2), "bad.pdf": CORRUPT})
    batch = send_zip(client, data).json()
    first, _second = desk.queued
    assert desk.service.process(first) == "failed"  # b.pdf is still waiting

    view = client.get(f"/v1/batches/{batch['id']}").json()
    counts = view["counts"]
    assert (counts["failed"], counts["waiting"], counts["ready"]) == (2, 1, 0)
    failed = next(i for i in view["items"] if i["state"] == "failed")
    assert failed["error"] == "The model reply did not fit the schema."

    after = client.post(f"/v1/batches/{batch['id']}/retry").json()

    assert after["counts"]["waiting"] == 2 and after["counts"]["failed"] == 1  # the refused one
    with desk.sessions() as session:
        versions = dict(session.execute(
            select(DocumentVersion.document_id, func.count()).group_by(DocumentVersion.document_id)
        ).all())  # fmt: skip
    assert sorted(versions.values()) == [1, 2]
    (retried,) = desk.audit("batch.retried")
    assert retried.details == {"documents": 1}


def test_a_batch_is_listed_with_its_counts(desk: Desk) -> None:
    client = desk.client()
    send_zip(client, make_zip({"a.pdf": invoice(1)}), name="week-44.zip")

    (listed,) = client.get("/v1/batches").json()

    assert listed["kind"] == "zip" and listed["name"] == "week-44.zip"
    assert listed["counts"]["total"] == 1 and listed["person"] == "test caller"


# AC-P6-8: each workspace's own, and only those who may upload


def test_a_workspace_sees_only_its_own_batches(desk: Desk, other_tenant: uuid.UUID) -> None:
    batch = send_zip(desk.client(), make_zip({"a.pdf": invoice(1)})).json()
    other = desk.client(tenant_id=other_tenant)

    assert other.get(f"/v1/batches/{batch['id']}").status_code == 404
    assert other.get("/v1/batches").json() == []
    sent = other.post(f"/v1/batches/{batch['id']}/files", files={"file": ("x.pdf", invoice(9))})
    assert sent.status_code == 404
    assert other.post(f"/v1/batches/{batch['id']}/retry").status_code == 404
    assert desk.documents(other_tenant) == 0


def test_only_those_who_may_upload_make_batches(desk: Desk) -> None:
    viewer = desk.client(role="reader")

    assert viewer.post("/v1/batches", json={"files": 2}).status_code == 403
    assert send_zip(viewer, make_zip({"a.pdf": invoice(1)})).status_code == 403
    assert viewer.get("/v1/batches").status_code == 200


# Scale: 200 one-page invoices queued in under 60 seconds


def test_a_zip_of_200_invoices_is_queued_in_under_a_minute(desk: Desk) -> None:
    data = make_zip({f"bill-{n:03d}.pdf": invoice(n) for n in range(200)})

    started = time.monotonic()
    answer = send_zip(desk.client(), data)
    seconds = time.monotonic() - started

    assert answer.status_code == 201
    assert answer.json()["counts"]["received"] == 200
    assert len(desk.queued) == 200
    assert seconds < 60, seconds
