"""Knowledge bases: named collections of an organisation's documents, to search and ask
within. A document can be in several; removing it from one leaves the document as it is.
Every read and write is scoped to the organisation, as everywhere, under row-level security.

Anyone who may add documents may change any of the organisation's knowledge bases; whoever
created one is recorded, not its owner. Per-base roles are for later (roadmap section 7).
"""

import builtins
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from docforge import audit
from docforge.db.models import Collection, CollectionDocument, Document, live
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped

MAX_DOCUMENTS_PER_CALL = 500


class CollectionNotFound(LookupError):
    """No knowledge base with that id in this organisation."""


class CollectionNameTaken(ValueError):
    """The organisation already has a knowledge base of that name (case and spaces aside)."""


class DocumentsNotFound(LookupError):
    """Some of the documents named are not this organisation's; none was added."""


@dataclass(frozen=True)
class CollectionSummary:
    id: uuid.UUID
    name: str
    description: str
    documents: int
    created_by: str
    created_at: datetime


@dataclass(frozen=True)
class Member:
    id: uuid.UUID
    filename: str
    doc_type: str
    stage: str
    added_at: datetime


def _is_name_clash(error: IntegrityError) -> bool:
    """Only a clash with another knowledge base's name is "name taken"."""
    constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    return bool(constraint == "uq_collections_tenant_name")


def _name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not 1 <= len(cleaned) <= 100:
        raise ValueError("a name is 1 to 100 characters")
    return cleaned


class CollectionService:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    @scoped
    def create(
        self, tenant_id: uuid.UUID, name: str, *, actor: str, description: str = ""
    ) -> CollectionSummary:
        try:
            with self._sessions.begin() as session:
                collection = Collection(
                    tenant_id=tenant_id,
                    name=_name(name),
                    description=description.strip(),
                    created_by=actor,
                )
                session.add(collection)
                session.flush()
                self._audit(session, collection, actor, "collection.created", name=collection.name)
                return self._summary(collection, 0)
        except IntegrityError as error:
            if _is_name_clash(error):
                raise CollectionNameTaken(name) from error
            raise

    @scoped
    def list(self, tenant_id: uuid.UUID) -> builtins.list[CollectionSummary]:
        with self._sessions() as session:
            counts = (
                select(CollectionDocument.collection_id, func.count().label("n"))
                .join(Document, Document.id == CollectionDocument.document_id)
                .where(CollectionDocument.tenant_id == tenant_id, live())
                .group_by(CollectionDocument.collection_id)
                .subquery()
            )
            rows = session.execute(
                select(Collection, func.coalesce(counts.c.n, 0))
                .outerjoin(counts, counts.c.collection_id == Collection.id)
                .where(Collection.tenant_id == tenant_id)
                .order_by(func.lower(Collection.name), Collection.id)
            ).all()
            return [self._summary(collection, n) for collection, n in rows]

    @scoped
    def rename(
        self,
        tenant_id: uuid.UUID,
        collection_id: uuid.UUID,
        name: str,
        description: str | None = None,
        *,
        actor: str,
    ) -> CollectionSummary:
        try:
            with self._sessions.begin() as session:
                collection = self._find(session, tenant_id, collection_id)
                collection.name = _name(name)
                if description is not None:
                    collection.description = description.strip()
                session.flush()
                self._audit(session, collection, actor, "collection.renamed", name=collection.name)
                n = session.scalar(
                    select(func.count()).where(CollectionDocument.collection_id == collection.id)
                )
                return self._summary(collection, n or 0)
        except IntegrityError as error:
            if _is_name_clash(error):
                raise CollectionNameTaken(name) from error
            raise

    @scoped
    def delete(self, tenant_id: uuid.UUID, collection_id: uuid.UUID, *, actor: str) -> None:
        """The knowledge base goes; its documents stay. Conversations about it remain to be
        read, but can no longer be continued."""
        with self._sessions.begin() as session:
            collection = self._find(session, tenant_id, collection_id)
            self._audit(session, collection, actor, "collection.deleted", name=collection.name)
            session.delete(collection)

    @scoped
    def add(
        self,
        tenant_id: uuid.UUID,
        collection_id: uuid.UUID,
        document_ids: Sequence[uuid.UUID],
        *,
        actor: str,
    ) -> int:
        """All of them or none: raises `DocumentsNotFound` if any is not the organisation's.
        Returns how many were not already in it."""
        wanted = list(dict.fromkeys(document_ids))
        if len(wanted) > MAX_DOCUMENTS_PER_CALL:
            raise ValueError(f"at most {MAX_DOCUMENTS_PER_CALL} documents at a time")
        with self._sessions.begin() as session:
            collection = self._find(session, tenant_id, collection_id)
            found = set(
                session.scalars(
                    select(Document.id).where(
                        Document.tenant_id == tenant_id, Document.id.in_(wanted), live()
                    )
                )
            )
            if missing := [d for d in wanted if d not in found]:
                raise DocumentsNotFound(missing)
            if not wanted:
                self._audit(session, collection, actor, "collection.documents_added", count=0)
                return 0
            try:
                added = session.execute(
                    insert(CollectionDocument)
                    .values(
                        [
                            {
                                "collection_id": collection_id,
                                "document_id": d,
                                "tenant_id": tenant_id,
                            }
                            for d in wanted
                        ]
                    )
                    .on_conflict_do_nothing()
                    .returning(CollectionDocument.document_id)
                ).all()
            except IntegrityError as error:  # a document deleted since it was found
                raise DocumentsNotFound(wanted) from error
            self._audit(session, collection, actor, "collection.documents_added", count=len(added))
            return len(added)

    @scoped
    def remove(
        self,
        tenant_id: uuid.UUID,
        collection_id: uuid.UUID,
        document_ids: Sequence[uuid.UUID],
        *,
        actor: str,
    ) -> None:
        if len(document_ids) > MAX_DOCUMENTS_PER_CALL:
            raise ValueError(f"at most {MAX_DOCUMENTS_PER_CALL} documents at a time")
        with self._sessions.begin() as session:
            collection = self._find(session, tenant_id, collection_id)
            removed = session.execute(
                delete(CollectionDocument)
                .where(
                    CollectionDocument.collection_id == collection_id,
                    CollectionDocument.document_id.in_(list(document_ids)),
                )
                .returning(CollectionDocument.document_id)
            ).all()
            self._audit(
                session, collection, actor, "collection.documents_removed", count=len(removed)
            )

    @scoped
    def documents(self, tenant_id: uuid.UUID, collection_id: uuid.UUID) -> builtins.list[Member]:
        with self._sessions() as session:
            self._find(session, tenant_id, collection_id)
            rows = session.execute(
                select(Document, CollectionDocument.added_at)
                .join(CollectionDocument, CollectionDocument.document_id == Document.id)
                .where(CollectionDocument.collection_id == collection_id, live())
                .order_by(CollectionDocument.added_at, Document.id)
            ).all()
            return [
                Member(
                    id=document.id,
                    filename=document.filename,
                    doc_type=document.doc_type,
                    stage=document.stage,
                    added_at=added_at,
                )
                for document, added_at in rows
            ]

    @staticmethod
    def exists(session: Session, tenant_id: uuid.UUID, collection_id: uuid.UUID) -> bool:
        found = session.scalar(
            select(Collection.id).where(
                Collection.tenant_id == tenant_id, Collection.id == collection_id
            )
        )
        return found is not None

    @staticmethod
    def _find(session: Session, tenant_id: uuid.UUID, collection_id: uuid.UUID) -> Collection:
        collection = session.scalar(
            select(Collection).where(
                Collection.tenant_id == tenant_id, Collection.id == collection_id
            )
        )
        if collection is None:
            raise CollectionNotFound(collection_id)
        return collection

    @staticmethod
    def _audit(
        session: Session, collection: Collection, actor: str, action: str, **details: str | int
    ) -> None:
        """In the change's own transaction: the entry exists if and only if the change does.
        Only a count of documents, not which: one call may name hundreds."""
        audit.append(
            session,
            tenant_id=collection.tenant_id,
            actor=actor,
            action=action,
            target_type="collection",
            target_id=str(collection.id),
            details=dict(details),
        )

    @staticmethod
    def _summary(collection: Collection, documents: int) -> CollectionSummary:
        return CollectionSummary(
            id=collection.id,
            name=collection.name,
            description=collection.description,
            documents=documents,
            created_by=collection.created_by,
            created_at=collection.created_at,
        )
