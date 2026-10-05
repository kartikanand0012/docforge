"""Knowledge base endpoints: read by anyone in the organisation, changed by those who may
add documents."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from docforge.api.auth import require
from docforge.auth import Principal
from docforge.collections import (
    CollectionNameTaken,
    CollectionNotFound,
    CollectionService,
    DocumentsNotFound,
)

Reader = Annotated[Principal, Depends(require("documents:read"))]
Writer = Annotated[Principal, Depends(require("documents:write"))]
_TAKEN = "A knowledge base with that name already exists."


class CollectionIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    description: Annotated[str, Field(max_length=1000)] = ""


class CollectionChange(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    description: Annotated[str | None, Field(max_length=1000)] = None


class DocumentsIn(BaseModel):
    document_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=500)]


class CollectionOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    documents: int
    created_by: str
    created_at: datetime


class MemberOut(BaseModel):
    id: uuid.UUID
    filename: str
    doc_type: str
    stage: str
    added_at: datetime


def collections_router(collections: CollectionService) -> APIRouter:
    router = APIRouter(prefix="/v1/collections")

    @router.get("", response_model=list[CollectionOut])
    def list_collections(principal: Reader) -> list[CollectionOut]:
        return [CollectionOut(**vars(c)) for c in collections.list(principal.tenant_id)]

    @router.post("", response_model=CollectionOut, status_code=201)
    def create_collection(body: CollectionIn, principal: Writer) -> CollectionOut:
        try:
            created = collections.create(
                principal.tenant_id, body.name, actor=principal.actor, description=body.description
            )
        except CollectionNameTaken as error:
            raise HTTPException(409, _TAKEN) from error
        except ValueError as error:
            raise HTTPException(422, "A name is 1 to 100 characters.") from error
        return CollectionOut(**vars(created))

    @router.patch("/{collection_id}", response_model=CollectionOut)
    def change_collection(
        collection_id: uuid.UUID, body: CollectionChange, principal: Writer
    ) -> CollectionOut:
        try:
            changed = collections.rename(
                principal.tenant_id, collection_id, body.name, body.description
            )
        except CollectionNotFound as error:
            raise HTTPException(404, "Not found.") from error
        except CollectionNameTaken as error:
            raise HTTPException(409, _TAKEN) from error
        except ValueError as error:
            raise HTTPException(422, "A name is 1 to 100 characters.") from error
        return CollectionOut(**vars(changed))

    @router.delete("/{collection_id}", status_code=204)
    def delete_collection(collection_id: uuid.UUID, principal: Writer) -> Response:
        """The knowledge base goes; its documents stay."""
        try:
            collections.delete(principal.tenant_id, collection_id)
        except CollectionNotFound as error:
            raise HTTPException(404, "Not found.") from error
        return Response(status_code=204)

    @router.get("/{collection_id}/documents", response_model=list[MemberOut])
    def list_members(collection_id: uuid.UUID, principal: Reader) -> list[MemberOut]:
        try:
            members = collections.documents(principal.tenant_id, collection_id)
        except CollectionNotFound as error:
            raise HTTPException(404, "Not found.") from error
        return [MemberOut(**vars(m)) for m in members]

    @router.post("/{collection_id}/documents")
    def add_members(
        collection_id: uuid.UUID, body: DocumentsIn, principal: Writer
    ) -> dict[str, int]:
        """All of them or none: a document not in the organisation refuses the lot."""
        try:
            added = collections.add(principal.tenant_id, collection_id, body.document_ids)
        except (CollectionNotFound, DocumentsNotFound) as error:
            raise HTTPException(404, "Not found.") from error
        return {"added": added}

    @router.delete("/{collection_id}/documents/{document_id}", status_code=204)
    def remove_member(
        collection_id: uuid.UUID, document_id: uuid.UUID, principal: Writer
    ) -> Response:
        try:
            collections.remove(principal.tenant_id, collection_id, [document_id])
        except CollectionNotFound as error:
            raise HTTPException(404, "Not found.") from error
        return Response(status_code=204)

    return router
