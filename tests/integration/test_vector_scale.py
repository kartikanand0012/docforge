"""Vector search at scale, as the application runs it (its role, row-level security, the
tenant filter): latency, whether the approximate index is used, recall against an exact
search, and that every result belongs to the asker. Run small here; `make load` runs it big."""

import pytest
from sqlalchemy.engine import URL

from docforge.evals.vector_scale import run_vector_scale

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
