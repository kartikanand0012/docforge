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
from sqlalchemy import func, literal_column, select
from sqlalchemy.orm import Session

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

Mode = Literal["keyword", "vector", "hybrid"]
_SCHEMAS: dict[str, type[BaseModel]] = {
    "invoice": InvoiceExtraction,
    "purchase_order": PurchaseOrderExtraction,
    "coa": CoaExtraction,
}
_CANDIDATES = 50
_RRF_K = 60
_WORD = re.compile(r"[0-9A-Za-z]+")


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
        ranked: list[list[uuid.UUID]] = []
        with self._sessions() as session:
            if mode in ("keyword", "hybrid"):
                ranked.append(self._keyword(session, tenant_id, query, doc_type))
            if mode in ("vector", "hybrid"):
                vector = self._embedder.embed([query], "query")[0]
                ranked.append(self._vector(session, tenant_id, vector, doc_type))
            scores: dict[uuid.UUID, float] = {}
            for ranking in ranked:
                for rank, chunk_id in enumerate(ranking, start=1):
                    scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
            # Ties keep the order in which the rankings produced them, which is repeatable.
            order = {
                chunk_id: n
                for n, chunk_id in enumerate(dict.fromkeys(i for r in ranked for i in r))
            }
            top = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], order[chunk_id]))[:k]
            return self._hits(session, top, scores)

    def _filters(self, tenant_id: uuid.UUID, doc_type: str | None) -> list[Any]:
        filters: list[Any] = [ChunkRow.tenant_id == tenant_id]
        if doc_type is not None:
            filters.append(Document.doc_type == doc_type)
        return filters

    def _keyword(
        self, session: Session, tenant_id: uuid.UUID, query: str, doc_type: str | None
    ) -> list[uuid.UUID]:
        words = [w.lower() for w in _WORD.findall(query)]
        if not words:
            return []
        # Any word may match; chunks matching more of them, more densely, rank higher.
        tsquery = func.to_tsquery("simple", " | ".join(dict.fromkeys(words)))
        # Normalised by length (flag 1), so the row that prints a value outranks a long
        # summary that only lists it.
        rank = func.ts_rank_cd(literal_column("chunks.tsv"), tsquery, 1)
        rows = session.execute(
            select(ChunkRow.id)
            .join(Document, Document.id == ChunkRow.document_id)
            .where(
                *self._filters(tenant_id, doc_type), literal_column("chunks.tsv").op("@@")(tsquery)
            )
            .order_by(rank.desc(), ChunkRow.text, ChunkRow.chunk_no)
            .limit(_CANDIDATES)
        )
        return [row[0] for row in rows]

    def _vector(
        self, session: Session, tenant_id: uuid.UUID, vector: list[float], doc_type: str | None
    ) -> list[uuid.UUID]:
        rows = session.execute(
            select(ChunkRow.id)
            .join(Document, Document.id == ChunkRow.document_id)
            .where(
                *self._filters(tenant_id, doc_type),
                ChunkRow.embedding_model == self._embedder.model,
            )
            .order_by(ChunkRow.embedding.cosine_distance(vector), ChunkRow.text, ChunkRow.chunk_no)
            .limit(_CANDIDATES)
        )
        return [row[0] for row in rows]

    @staticmethod
    def _hits(
        session: Session, chunk_ids: list[uuid.UUID], scores: dict[uuid.UUID, float]
    ) -> list[SearchHit]:
        if not chunk_ids:
            return []
        rows = {
            chunk.id: (chunk, document)
            for chunk, document in session.execute(
                select(ChunkRow, Document)
                .join(Document, Document.id == ChunkRow.document_id)
                .where(ChunkRow.id.in_(chunk_ids))
            ).tuples()
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
