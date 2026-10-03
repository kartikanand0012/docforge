"""Indexing documents into chunks, and searching them by words, by meaning, or both.

Hybrid search runs both and fuses the two rankings by reciprocal rank (each list contributes
1 / (60 + rank)), which needs no score calibration between the two. Every result carries the
page and boxes of the blocks it came from. Tenants are separated twice: every query filters
by tenant, and row-level security applies underneath.
"""

import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import Select, func, literal_column, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, aliased

from docforge.db.models import ChunkRow, Document, DocumentVersion, Extraction, ParseOutput
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped, tenant_scope
from docforge.documents import DocumentNotFound
from docforge.extraction.coa import CoaExtraction
from docforge.extraction.purchase_order import PurchaseOrderExtraction
from docforge.extraction.schema import InvoiceExtraction
from docforge.parsing.base import ParsedDocument
from docforge.search.chunking import chunk_document
from docforge.search.embeddings import Embedder
from docforge.telemetry import tracer

Mode = Literal["keyword", "vector", "hybrid"]
_SCHEMAS: dict[str, type[BaseModel]] = {
    "invoice": InvoiceExtraction,
    "purchase_order": PurchaseOrderExtraction,
    "coa": CoaExtraction,
}
_CANDIDATES = 50
_RRF_K = 60
_CACHE_SIZE = 256
# Equal scores are common: the same file uploaded twice, or in two organisations, gives
# identical chunks. Older first, then by id, so a search gives the same order every time.
_TIE_BREAK = (ChunkRow.text, ChunkRow.chunk_no, ChunkRow.created_at, ChunkRow.id)
_WORD = re.compile(r"[0-9A-Za-z]+")


def _words(query: str) -> list[str]:
    return [word.lower() for word in _WORD.findall(query)]


_STRENGTH_OR_PACK = re.compile(r"\d+(?:mg|mcg|ml|g|kg|iu|l|x\d+)")


def _approximate(session: Session) -> None:
    session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    session.execute(text("SET LOCAL hnsw.ef_search = 100"))


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def _codes(words: list[str]) -> list[str]:
    """Distinctive codes: letters with digits (XGX944068, a GSTIN), or five digits or more
    (32001). Not years (2026), strengths (500mg) or pack sizes (10x10), which are everywhere."""
    codes = []
    for word in dict.fromkeys(words):
        has_digit = any(c.isdigit() for c in word)
        has_letter = any(c.isalpha() for c in word)
        if _STRENGTH_OR_PACK.fullmatch(word):
            continue
        if (has_digit and has_letter and len(word) >= 4) or (word.isdigit() and len(word) >= 5):
            codes.append(word)
    return codes


@dataclass(frozen=True)
class SearchHit:
    document_id: uuid.UUID
    doc_type: str
    filename: str
    kind: str
    page: int
    text: str
    score: float
    boxes: tuple[dict[str, Any], ...]


class SearchService:
    def __init__(self, sessions: SessionFactory, embedder: Embedder) -> None:
        self._sessions = sessions
        self._embedder = embedder
        self._cache: dict[str, list[float]] = {}

    # Indexing

    def index_version(self, version_id: uuid.UUID) -> int:
        """For the worker: the job carries only the version."""
        with self._sessions() as session:
            tenant_id = session.scalar(select(func.docforge_version_tenant(version_id)))
        if tenant_id is None:
            raise DocumentNotFound(version_id)
        with tenant_scope(tenant_id), self._sessions() as session:
            document_id = session.scalar(
                select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
            )
        assert document_id is not None  # noqa: S101 - found just above, same tenant
        return self.index_document(tenant_id, document_id)

    @scoped
    def index_document(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> int:
        """Index the newest extracted version. Returns the chunks added (0 if already done)."""
        with tracer.start_as_current_span("search.index") as span:
            span.set_attribute("docforge.document_id", str(document_id))
            added = self._index_document(tenant_id, document_id)
            span.set_attribute("docforge.chunks_added", added)
            return added

    def _index_document(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> int:
        with self._sessions() as session:
            row = session.execute(
                select(Document, DocumentVersion, Extraction, ParseOutput)
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .join(Extraction, Extraction.document_version_id == DocumentVersion.id)
                .join(ParseOutput, ParseOutput.document_version_id == DocumentVersion.id)
                .where(Document.tenant_id == tenant_id, Document.id == document_id)
                .order_by(DocumentVersion.version_no.desc())
                .limit(1)
            ).first()
            if row is None:
                raise DocumentNotFound(document_id)
            document, version, extraction, parse_output = row
            if session.scalar(
                select(func.count()).where(ChunkRow.document_version_id == version.id)
            ):
                return 0
            schema = _SCHEMAS.get(document.doc_type)
            if schema is None:
                return 0
            chunks = chunk_document(
                document.doc_type,
                document.filename,
                ParsedDocument.model_validate(parse_output.data),
                schema.model_validate(extraction.data),
            )
            version_id = version.id
        vectors = self._embedder.embed([c.text for c in chunks], "document")
        with self._sessions.begin() as session:
            # Two jobs for one version: the second waits here, then finds the chunks there.
            session.execute(
                select(func.pg_advisory_xact_lock(func.hashtext(f"index:{version_id}")))
            )
            if session.scalar(
                select(func.count()).where(ChunkRow.document_version_id == version_id)
            ):
                return 0
            for number, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                session.add(
                    ChunkRow(
                        tenant_id=tenant_id,
                        document_id=document_id,
                        document_version_id=version_id,
                        chunk_no=number,
                        kind=chunk.kind,
                        page=chunk.page,
                        block_ids=list(chunk.block_ids),
                        text=chunk.text[:4000],
                        embedding_model=self._embedder.model,
                        embedding=vector,
                    )
                )
        return len(chunks)

    # Searching

    @scoped
    def search(
        self,
        tenant_id: uuid.UUID,
        query: str,
        *,
        k: int = 10,
        mode: Mode = "hybrid",
        doc_type: str | None = None,
    ) -> list[SearchHit]:
        # The question itself is not recorded: it may name a patient, a price or a supplier.
        with tracer.start_as_current_span("search.query") as span:
            span.set_attribute("docforge.search.mode", mode)
            span.set_attribute("docforge.search.k", k)
            span.set_attribute("docforge.search.doc_type", doc_type or "")
            hits = self._search(tenant_id, query, k, mode, doc_type)
            span.set_attribute("docforge.search.hits", len(hits))
            return hits

    def _search(
        self, tenant_id: uuid.UUID, query: str, k: int, mode: Mode, doc_type: str | None
    ) -> list[SearchHit]:
        ranked: list[list[uuid.UUID]] = []
        # Embedded before a connection is taken, so a slow embedding holds no database session.
        vector = self._query_vector(query) if mode in ("vector", "hybrid") else None
        with self._sessions() as session:
            if mode in ("keyword", "hybrid"):
                ranked.append(self._keyword(session, tenant_id, query, doc_type))
            if vector is not None:
                ranked.append(self._vector(session, tenant_id, vector, doc_type))
            # A question naming a code is answered by the documents that print it: their
            # words count double against documents that are only similar in meaning.
            weights = [2.0 if mode == "hybrid" and _codes(_words(query)) else 1.0, 1.0]
            scores: dict[uuid.UUID, float] = {}
            for weight, ranking in zip(weights, ranked, strict=False):
                for rank, chunk_id in enumerate(ranking, start=1):
                    scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (_RRF_K + rank)
            # Ties keep the order in which the rankings produced them, which is repeatable.
            order = {
                chunk_id: n
                for n, chunk_id in enumerate(dict.fromkeys(i for r in ranked for i in r))
            }
            top = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], order[chunk_id]))[:k]
            return self._hits(session, top, scores)

    def _query_vector(self, query: str) -> list[float]:
        """A question's vector; the most recent ones are kept, so a repeat costs no call."""
        cached = self._cache.pop(query, None)
        if cached is None:
            cached = self._embedder.embed([query], "query")[0]
        self._cache[query] = cached
        while len(self._cache) > _CACHE_SIZE:
            self._cache.pop(next(iter(self._cache)))
        return cached

    def _filters(self, tenant_id: uuid.UUID, doc_type: str | None) -> list[Any]:
        # Only each document's newest indexed version: a reprocessed document's old text is
        # not current, though its chunks are kept (the record is append-only).
        indexed = aliased(ChunkRow)
        newest = (
            select(func.max(DocumentVersion.version_no))
            .join(indexed, indexed.document_version_id == DocumentVersion.id)
            .where(DocumentVersion.document_id == ChunkRow.document_id)
            .correlate(ChunkRow)
            .scalar_subquery()
        )
        current = select(DocumentVersion.id).where(
            DocumentVersion.document_id == ChunkRow.document_id,
            DocumentVersion.version_no == newest,
        )
        filters: list[Any] = [
            ChunkRow.tenant_id == tenant_id,
            ChunkRow.document_version_id.in_(current.scalar_subquery()),
        ]
        if doc_type is not None:
            filters.append(Document.doc_type == doc_type)
        return filters

    def _keyword(
        self, session: Session, tenant_id: uuid.UUID, query: str, doc_type: str | None
    ) -> list[uuid.UUID]:
        words = _words(query)
        if not words:
            return []
        # Any word may match; chunks matching more of them, more densely, rank higher.
        tsquery = func.to_tsquery("simple", " | ".join(dict.fromkeys(words)))
        # Normalised by length (flag 1), so the row that prints a value outranks a long
        # summary that only lists it.
        rank = func.ts_rank_cd(literal_column("chunks.tsv"), tsquery, 1)
        # A word with a digit in it is a code (a batch, an invoice or order number): when the
        # question names one, only chunks printing one of its codes are candidates, so generic
        # words cannot outrank it. Full-text ranking has no notion of a rare word.
        codes = _codes(words)
        required = (
            [literal_column("chunks.tsv").op("@@")(func.to_tsquery("simple", " | ".join(codes)))]
            if codes
            else []
        )
        rows = session.execute(
            select(ChunkRow.id)
            .join(Document, Document.id == ChunkRow.document_id)
            .where(
                *self._filters(tenant_id, doc_type),
                literal_column("chunks.tsv").op("@@")(tsquery),
                *required,
            )
            .order_by(rank.desc(), *_TIE_BREAK)
            .limit(_CANDIDATES)
        )
        return [row[0] for row in rows]

    def _vector(
        self, session: Session, tenant_id: uuid.UUID, vector: list[float], doc_type: str | None
    ) -> list[uuid.UUID]:
        # When the approximate index is used, keep scanning until enough rows pass the tenant
        # filter (pgvector 0.8), rather than filtering a fixed handful of nearest rows.
        _approximate(session)
        return list(session.scalars(self._vector_query(tenant_id, vector, doc_type)))

    def _vector_query(
        self, tenant_id: uuid.UUID, vector: list[float], doc_type: str | None
    ) -> Select[uuid.UUID]:
        return (
            select(ChunkRow.id)
            .join(Document, Document.id == ChunkRow.document_id)
            .where(
                *self._filters(tenant_id, doc_type),
                ChunkRow.embedding_model == self._embedder.model,
            )
            .order_by(ChunkRow.embedding.cosine_distance(vector), *_TIE_BREAK)
            .limit(_CANDIDATES)
        )

    @scoped
    def explain_vector(
        self, tenant_id: uuid.UUID, vector: list[float], doc_type: str | None = None
    ) -> str:
        """The plan Postgres gives the vector query, as the application runs it: for checking
        whether the approximate index is used at a given size."""
        with self._sessions() as session:
            _approximate(session)
            statement = self._vector_query(tenant_id, vector, doc_type).compile(
                dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
            )
            params = {
                name: _vector_literal(value) if isinstance(value, list) else value
                for name, value in statement.params.items()
            }
            rows = session.connection().exec_driver_sql(f"EXPLAIN {statement}", params)
            return "\n".join(row[0] for row in rows)

    @staticmethod
    def _hits(
        session: Session, chunk_ids: list[uuid.UUID], scores: dict[uuid.UUID, float]
    ) -> list[SearchHit]:
        if not chunk_ids:
            return []
        rows = {
            row.ChunkRow.id: (row.ChunkRow, row.Document)
            for row in session.execute(
                select(ChunkRow, Document)
                .join(Document, Document.id == ChunkRow.document_id)
                .where(ChunkRow.id.in_(chunk_ids))
            ).all()
        }
        versions = {chunk.document_version_id for chunk, _ in rows.values()}
        blocks: dict[uuid.UUID, dict[str, Any]] = {}
        for version_id, data in session.execute(
            select(ParseOutput.document_version_id, ParseOutput.data).where(
                ParseOutput.document_version_id.in_(versions)
            )
        ):
            blocks[version_id] = {block["id"]: block for block in data["blocks"]}
        hits = []
        for chunk_id in chunk_ids:
            chunk, document = rows[chunk_id]
            found = blocks.get(chunk.document_version_id, {})
            hits.append(
                SearchHit(
                    document_id=document.id,
                    doc_type=document.doc_type,
                    filename=document.filename,
                    kind=chunk.kind,
                    page=chunk.page,
                    text=chunk.text,
                    score=round(scores[chunk_id], 6),
                    boxes=tuple(
                        {"page": found[b]["page"], **found[b]["bbox"]}
                        for b in chunk.block_ids
                        if b in found
                    ),
                )
            )
        return hits


__all__ = ["SearchHit", "SearchService"]
