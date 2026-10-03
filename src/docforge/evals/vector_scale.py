"""`python -m docforge.evals.vector_scale`: vector search at a size where the index matters.

Random unit vectors (768 dimensions) are spread over several organisations in a database of
their own; questions are random vectors too. Each is searched the way the application does
it (its role, row-level security, the tenant and newest-version filters, iterative scans) and
compared with an exact search over the same organisation's chunks. Random vectors are the
hard case for an approximate index (no clusters), so recall here is a floor, not a forecast.
"""

import argparse
import math
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from docforge.db.session import make_engine, make_session_factory
from docforge.evals.database import temporary_database
from docforge.search.service import SearchService

_MODEL = "vector-scale"
_DIMENSIONS = 768
_PER_DOCUMENT = 50


class VectorScaleReport(BaseModel):
    chunks: int
    tenants: int
    queries: int
    k: int
    latency_ms_p50: float
    latency_ms_p95: float
    recall_at_k_mean: float
    recall_at_k_min: float
    cross_tenant_hits: int
    index_used: bool
    plan: str


class _Fixed:
    """Questions are vectors already: each query string names one."""

    model = _MODEL
    dimensions = _DIMENSIONS

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    def embed(self, texts: list[str], task: str) -> list[list[float]]:
        return [self._vectors[t] for t in texts]


def _unit(rng: np.random.Generator, n: int) -> np.ndarray:
    raw = rng.standard_normal((n, _DIMENSIONS)).astype(np.float32)
    return raw / np.linalg.norm(raw, axis=1, keepdims=True)


def _literal(vector: np.ndarray) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def populate(
    owner: Engine, vectors: np.ndarray, owners: np.ndarray, tenants: int
) -> list[uuid.UUID]:
    """Organisations, documents of 50 chunks each, and the chunks with their vectors."""
    with owner.begin() as conn:
        tenant_ids = [
            conn.execute(
                text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": f"scale-{t}"}
            ).scalar_one()
            for t in range(tenants)
        ]
        rows = []
        for t, tenant_id in enumerate(tenant_ids):
            mine = np.flatnonzero(owners == t)
            for start in range(0, len(mine), _PER_DOCUMENT):
                document_id = conn.execute(
                    text(
                        "INSERT INTO documents (tenant_id, doc_type, sha256, storage_key, "
                        "filename, size_bytes) VALUES (:t, 'invoice', :sha, 'k', 'x.pdf', 1) "
                        "RETURNING id"
                    ),
                    {"t": tenant_id, "sha": uuid.uuid4().hex * 2},
                ).scalar_one()
                version_id = conn.execute(
                    text(
                        "INSERT INTO document_versions (tenant_id, document_id, version_no, "
                        "status) VALUES (:t, :d, 1, 'succeeded') RETURNING id"
                    ),
                    {"t": tenant_id, "d": document_id},
                ).scalar_one()
                conn.execute(
                    text("UPDATE documents SET indexed_version_id = :v WHERE id = :d"),
                    {"v": version_id, "d": document_id},
                )
                for number, index in enumerate(mine[start : start + _PER_DOCUMENT]):
                    rows.append(
                        {
                            "t": tenant_id,
                            "d": document_id,
                            "v": version_id,
                            "n": number,
                            "text": f"chunk {index}",
                            "m": _MODEL,
                            "e": _literal(vectors[index]),
                        }
                    )
        for start in range(0, len(rows), 2000):
            conn.execute(
                text(
                    "INSERT INTO chunks (tenant_id, document_id, document_version_id, "
                    "chunk_no, kind, page, block_ids, text, embedding_model, embedding) "
                    "VALUES (:t, :d, :v, :n, 'text', 1, '{}', :text, :m, CAST(:e AS vector))"
                ),
                rows[start : start + 2000],
            )
    with owner.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("ANALYZE"))

    return tenant_ids


def run_vector_scale(
    *, chunks: int, tenants: int, queries: int, k: int = 10, seed: int = 0
) -> VectorScaleReport:
    rng = np.random.default_rng(seed)
    vectors = _unit(rng, chunks)
    owners = rng.integers(0, tenants, size=chunks)
    with temporary_database("docforge_vector_scale") as (owner_url, app_url):
        owner = create_engine(owner_url)
        tenant_ids = populate(owner, vectors, owners, tenants)
        asked = _unit(rng, queries)
        who = rng.integers(0, tenants, size=queries)
        names = {f"q{i}": asked[i].tolist() for i in range(queries)}
        engine = make_engine(app_url)
        search = SearchService(make_session_factory(engine), _Fixed(names))
        latencies, recalls, leaks = [], [], 0
        for i in range(queries):
            tenant_id = tenant_ids[who[i]]
            started = time.perf_counter()
            hits = search.search(tenant_id, f"q{i}", k=k, mode="vector")
            latencies.append((time.perf_counter() - started) * 1000)
            found = {hit.text for hit in hits}
            mine = np.flatnonzero(owners == who[i])
            leaks += sum(int(h.split()[1]) not in set(mine.tolist()) for h in found)
            exact = mine[np.argsort(-(vectors[mine] @ asked[i]), kind="stable")[:k]]
            recalls.append(len(found & {f"chunk {j}" for j in exact}) / max(1, min(k, len(mine))))
        plan = search.explain_vector(tenant_ids[0], asked[0].tolist())
        engine.dispose()
        owner.dispose()
    return VectorScaleReport(
        chunks=chunks,
        tenants=tenants,
        queries=queries,
        k=k,
        latency_ms_p50=round(_percentile(latencies, 0.50), 2),
        latency_ms_p95=round(_percentile(latencies, 0.95), 2),
        recall_at_k_mean=round(float(np.mean(recalls)), 4),
        recall_at_k_min=round(float(np.min(recalls)), 4),
        cross_tenant_hits=leaks,
        index_used="ix_chunks_embedding" in plan or "hnsw" in plan.lower(),
        plan=plan,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m docforge.evals.vector_scale", description=__doc__
    )
    parser.add_argument("--chunks", type=int, default=50_000)
    parser.add_argument("--tenants", type=int, default=10)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    report = run_vector_scale(
        chunks=args.chunks, tenants=args.tenants, queries=args.queries, k=args.k
    )
    body = report.model_dump_json(indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(body, encoding="utf-8")
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
