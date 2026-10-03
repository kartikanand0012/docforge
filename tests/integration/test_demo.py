"""The public demo's data: an organisation, a reviewer anyone can sign in as, and the
synthetic documents, queued for processing. Running it twice adds nothing."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from docforge.auth import Authenticator
from docforge.db.session import SessionFactory
from docforge.demo import demo_documents, seed_demo
from docforge.documents import DocumentService
from docforge.review.service import ReviewService
from docforge.storage import MemoryObjectStore
from docforge.wiring import build_replay_pipelines

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]


def test_the_demo_documents_are_the_synthetic_ones_and_only_those() -> None:
    documents = demo_documents(
        REPO / "tests" / "fixtures" / "synthetic", REPO / "tests" / "fixtures" / "coa"
    )

    kinds = [doc_type for doc_type, _, _ in documents]
    assert kinds.count("invoice") == 20 and kinds.count("purchase_order") == 20
    assert kinds.count("coa") == 20
    assert all(data.startswith(b"%PDF") for _, _, data in documents)


def test_seeding_twice_adds_nothing_the_second_time(
    sessions: SessionFactory, owner_engine: Engine, settings: object
) -> None:
    queued: list[uuid.UUID] = []
    service = DocumentService(
        sessions,
        MemoryObjectStore(),
        build_replay_pipelines(settings),  # type: ignore[arg-type]
        lambda session, version: queued.append(version.id),
    )
    review = ReviewService(sessions, MemoryObjectStore(), {})
    documents = demo_documents(
        REPO / "tests" / "fixtures" / "synthetic", REPO / "tests" / "fixtures" / "coa"
    )[:4]

    first = seed_demo(owner_engine, service, review, documents, pin="482915")
    second = seed_demo(owner_engine, service, review, documents, pin="482915")

    assert (first.documents_added, first.reviewer_added) == (4, True)
    assert (second.documents_added, second.reviewer_added) == (0, False)
    assert len(queued) == 4
    token = Authenticator(sessions).login("demo", "demo@docforge.example", "482915", "test")
    assert token.startswith("dfs_")


def test_a_weak_demo_pin_is_refused(sessions: SessionFactory, owner_engine: Engine) -> None:
    review = ReviewService(sessions, MemoryObjectStore(), {})
    service = DocumentService(sessions, MemoryObjectStore(), {}, lambda session, version: None)

    with pytest.raises(ValueError):
        seed_demo(owner_engine, service, review, [], pin="12")


def test_seeding_with_no_documents_found_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from docforge.demo import main

    monkeypatch.setenv("DEMO_PIN", "482915")

    assert (
        main(["seed", "--synthetic", str(tmp_path / "none"), "--coa", str(tmp_path / "none")]) == 1
    )
    assert "no demo documents" in capsys.readouterr().err
