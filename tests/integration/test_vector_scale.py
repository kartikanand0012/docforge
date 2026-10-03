"""Vector search at scale, as the application runs it (its role, row-level security, the
tenant filter): latency, whether the approximate index is used, recall against an exact
search, and that every result belongs to the asker. Run small here; `make load` runs it big."""

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from docforge.db import DEFAULT_TENANT_ID, alembic_config
from docforge.db.session import SessionFactory
from docforge.evals.vector_scale import run_vector_scale
from docforge.search.embeddings import FakeEmbedder
from docforge.search.service import SearchService

pytestmark = pytest.mark.integration


def test_a_small_run_reports_latency_recall_and_no_cross_tenant_results(
    empty_database_url: URL,
) -> None:
    report = run_vector_scale(chunks=600, tenants=3, queries=12, k=10, seed=7)

    assert report.chunks == 600 and report.tenants == 3 and report.queries == 12
    assert 0 < report.latency_ms_p50 <= report.latency_ms_p95
    assert 0.0 <= report.recall_at_k_min <= report.recall_at_k_mean <= 1.0
    assert report.cross_tenant_hits == 0
    assert isinstance(report.index_used, bool)
    assert report.plan  # the plan the query actually got, for the record


def test_the_same_seed_gives_the_same_data_and_queries() -> None:
    first = run_vector_scale(chunks=200, tenants=2, queries=5, k=5, seed=3)
    second = run_vector_scale(chunks=200, tenants=2, queries=5, k=5, seed=3)

    assert first.recall_at_k_mean == second.recall_at_k_mean


def test_the_vector_query_has_no_per_row_subquery(sessions: SessionFactory) -> None:
    """Measured at 50,000 chunks: a correlated "newest version" subquery per chunk took
    170 of 180 ms. The current version is read from the document instead."""
    search = SearchService(sessions, FakeEmbedder())

    plan = search.explain_vector(DEFAULT_TENANT_ID, [0.1] * 768)

    assert "SubPlan" not in plan
    assert "indexed_version_id" in plan


def test_existing_documents_point_at_their_newest_indexed_version_after_upgrade(
    empty_database_url: URL,
) -> None:
    config = alembic_config(empty_database_url)
    command.upgrade(config, "0011")
    owner = create_engine(empty_database_url)
    with owner.begin() as conn:
        document = conn.execute(
            text(
                "INSERT INTO documents (tenant_id, doc_type, sha256, storage_key, filename, "
                "size_bytes) VALUES (:t, 'invoice', :sha, 'k', 'a.pdf', 1) RETURNING id"
            ),
            {"t": DEFAULT_TENANT_ID, "sha": "d" * 64},
        ).scalar_one()
        versions = [
            conn.execute(
                text(
                    "INSERT INTO document_versions (tenant_id, document_id, version_no) "
                    "VALUES (:t, :d, :n) RETURNING id"
                ),
                {"t": DEFAULT_TENANT_ID, "d": document, "n": n},
            ).scalar_one()
            for n in (1, 2, 3)
        ]
        for version in versions[:2]:  # version 3 is not indexed yet
            conn.execute(
                text(
                    "INSERT INTO chunks (tenant_id, document_id, document_version_id, chunk_no, "
                    "kind, page, block_ids, text, embedding_model, embedding) VALUES (:t, :d, :v, "
                    "0, 'summary', 1, '{}', 'x', 'm', CAST(:e AS vector))"
                ),
                {"t": DEFAULT_TENANT_ID, "d": document, "v": version, "e": str([0.1] * 768)},
            )

    command.upgrade(config, "head")

    with owner.connect() as conn:
        indexed = conn.execute(
            text("SELECT indexed_version_id FROM documents WHERE id = :d"), {"d": document}
        ).scalar_one()
    assert indexed == versions[1]
    command.downgrade(config, "0011")
    command.upgrade(config, "head")
    owner.dispose()
