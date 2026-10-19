"""Schedule H1 products a workspace has marked: read by anyone in it, marked and unmarked by
those who review (it is a judgement about the bills they check)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from docforge.api.auth import require
from docforge.auth import Principal
from docforge.h1_products import (
    MAX_MARKS,
    MAX_PRODUCT_NAME,
    H1ProductService,
    MarkedProduct,
    MarkNotFound,
    ProductNameInvalid,
    TooManyMarks,
)

Reader = Annotated[Principal, Depends(require("documents:read"))]
Reviewer = Annotated[Principal, Depends(require("review"))]


class ProductIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_name: Annotated[str, Field(min_length=1, max_length=MAX_PRODUCT_NAME)]


class ProductOut(BaseModel):
    id: uuid.UUID
    product_name: str
    marked_by: str
    created_at: datetime


def _out(product: MarkedProduct) -> ProductOut:
    return ProductOut(
        id=product.id,
        product_name=product.product_name,
        marked_by=product.marked_by,
        created_at=product.created_at,
    )


def schedule_h1_router(products: H1ProductService) -> APIRouter:
    router = APIRouter(prefix="/v1/schedule-h1/products")

    @router.get("", response_model=list[ProductOut])
    def list_products(principal: Reader) -> list[ProductOut]:
        return [_out(p) for p in products.list(principal.tenant_id)]

    @router.post("", response_model=ProductOut, status_code=201)
    def mark_product(body: ProductIn, principal: Reviewer, response: Response) -> ProductOut:
        """Mark a product as Schedule H1 for the workspace. 201 when marked now, 200 when it
        already was (the same name, case, spaces and punctuation aside)."""
        try:
            product, made = products.mark(
                principal.tenant_id, body.product_name, actor=principal.actor, name=principal.name
            )
        except ProductNameInvalid as error:
            raise HTTPException(422, "A product name needs letters or digits.") from error
        except TooManyMarks as error:
            full = f"This workspace has {MAX_MARKS} marked products already."
            raise HTTPException(409, full) from error
        if not made:
            response.status_code = 200
        return _out(product)

    @router.delete("/{product_id}", status_code=204)
    def unmark_product(product_id: uuid.UUID, principal: Reviewer) -> Response:
        try:
            products.unmark(principal.tenant_id, product_id, actor=principal.actor)
        except MarkNotFound as error:
            raise HTTPException(404, "No such marked product.") from error
        return Response(status_code=204)

    return router
