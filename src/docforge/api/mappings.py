"""The workspace's mapping master (V2 P1): how its Tally / Marg books name each supplier and
item. Read by anyone in the workspace; made, changed, removed and seeded by those who review
(the accountant who exports), as Schedule H1 marks are.

A suggestion is only ever offered here: a mapping is made by a POST, which a person's click
sends, with `source: "suggestion"` when they confirmed one.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from docforge.api.auth import require
from docforge.auth import Principal
from docforge.documents import DocumentNotFound
from docforge.export_runs import ExportRunService, RunNotFound
from docforge.mappings.masters_csv import MastersFileInvalid
from docforge.mappings.service import (
    Bill,
    ItemMappingView,
    MappingExists,
    MappingInvalid,
    MappingNotFound,
    MappingService,
    SupplierMappingView,
    Tasks,
    TooManyMappings,
)
from docforge.mappings.suggest import Suggestion
from docforge.review.service import NotReviewable, ReviewService

Reader = Annotated[Principal, Depends(require("documents:read"))]
Reviewer = Annotated[Principal, Depends(require("review"))]
MAX_IMPORT_BYTES = 2 * 1024 * 1024
_TEXT = 1_000  # longer than any field allows; the service says what the limit is
Text = Annotated[str, Field(max_length=_TEXT)]
MaybeText = Annotated[str | None, Field(max_length=_TEXT)]


class SupplierOut(BaseModel):
    id: uuid.UUID
    seller_gstin: str
    printed_name: str
    tally_ledger: str | None
    marg_party: str | None
    purchase_ledger: str | None
    tax_treatment: str | None
    source: Literal["typed", "suggestion"]  # suggestion: confirmed from one by a click
    created_by: str
    created_at: datetime
    updated_by: str | None
    updated_at: datetime | None


class ItemOut(BaseModel):
    id: uuid.UUID
    item_key: str  # the printed name and pack as compared
    product_name: str
    pack: str | None
    printed_hsn: str | None
    tally_item: str | None
    marg_item: str | None
    preferred_hsn: str | None  # booked in place of the printed HSN
    source: Literal["typed", "suggestion"]
    created_by: str
    created_at: datetime
    updated_by: str | None
    updated_at: datetime | None


class SupplierPage(BaseModel):
    items: list[SupplierOut]
    total: int  # matching `q`
    next_after: str | None  # pass as `after` for the next page; null at the end


class ItemPage(BaseModel):
    items: list[ItemOut]
    total: int
    next_after: str | None


class SupplierIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seller_gstin: Text
    printed_name: Text
    tally_ledger: MaybeText = None
    marg_party: MaybeText = None
    purchase_ledger: MaybeText = None
    tax_treatment: MaybeText = None
    source: Literal["typed", "suggestion"] = "typed"


class SupplierChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    printed_name: MaybeText = None
    tally_ledger: MaybeText = None
    marg_party: MaybeText = None
    purchase_ledger: MaybeText = None
    tax_treatment: MaybeText = None


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_name: Text
    pack: MaybeText = None
    printed_hsn: MaybeText = None
    tally_item: MaybeText = None
    marg_item: MaybeText = None
    preferred_hsn: MaybeText = None
    source: Literal["typed", "suggestion"] = "typed"


class ItemChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    printed_hsn: MaybeText = None
    tally_item: MaybeText = None
    marg_item: MaybeText = None
    preferred_hsn: MaybeText = None


class RejectedOut(BaseModel):
    line: int  # in the file, the header being line 1
    reason: str


class ImportOut(BaseModel):
    rows: int
    imported: int
    already_known: int
    rejected: list[RejectedOut]


class SuggestionOut(BaseModel):
    name: str  # what would be booked
    gstin: str | None
    source: Literal["master", "mapping"]  # an imported master name, or another mapping's
    score: float
    why: Literal["same GSTIN", "same name", "similar name"]


class SupplierTaskOut(BaseModel):
    seller_gstin: str
    printed_names: list[str]
    bills: int
    mapping_id: uuid.UUID | None  # mapped, but with no name for these books: change it
    suggestions: list[SuggestionOut]


class ItemTaskOut(BaseModel):
    item_key: str
    product_name: str
    pack: str | None
    printed_hsn: str | None
    bills: int
    mapping_id: uuid.UUID | None
    suggestions: list[SuggestionOut]


class ProblemOut(BaseModel):
    document_id: uuid.UUID
    filename: str
    problem: str  # what to correct on the bill, in a sentence


class TasksOut(BaseModel):
    suppliers: list[SupplierTaskOut]
    items: list[ItemTaskOut]
    problems: list[ProblemOut]


def supplier_out(view: SupplierMappingView) -> SupplierOut:
    return SupplierOut.model_validate(view, from_attributes=True)


def item_out(view: ItemMappingView) -> ItemOut:
    return ItemOut.model_validate(view, from_attributes=True)


def _suggestions(found: tuple[Suggestion, ...]) -> list[SuggestionOut]:
    return [SuggestionOut.model_validate(s, from_attributes=True) for s in found]


def tasks_out(tasks: Tasks) -> TasksOut:
    return TasksOut(
        suppliers=[
            SupplierTaskOut(
                seller_gstin=t.seller_gstin,
                printed_names=list(t.printed_names),
                bills=t.bills,
                mapping_id=t.mapping_id,
                suggestions=_suggestions(t.suggestions),
            )
            for t in tasks.suppliers
        ],
        items=[
            ItemTaskOut(
                item_key=t.item_key,
                product_name=t.product_name,
                pack=t.pack,
                printed_hsn=t.printed_hsn,
                bills=t.bills,
                mapping_id=t.mapping_id,
                suggestions=_suggestions(t.suggestions),
            )
            for t in tasks.items
        ],
        problems=[
            ProblemOut(document_id=p.document_id, filename=p.filename, problem=p.problem)
            for p in tasks.problems
        ],
    )


def _exists(error: MappingExists) -> JSONResponse:
    return JSONResponse(
        status_code=409, content={"detail": str(error), "existing_id": str(error.existing.id)}
    )


def _page_args(
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    after: Annotated[str | None, Query(max_length=1_000)] = None,
) -> dict[str, Any]:
    return {"q": q, "limit": limit, "after": after}


PageArgs = Annotated[dict[str, Any], Depends(_page_args)]


def mappings_router(
    mappings: MappingService,
    review: ReviewService | None = None,
    runs: ExportRunService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1/settings/mappings")

    @router.get("/suppliers", response_model=SupplierPage)
    def list_suppliers(principal: Reader, args: PageArgs) -> SupplierPage:
        """In printed-name order; `q` finds by words of the name, GSTIN, ledger or party."""
        try:
            page = mappings.list_suppliers(principal.tenant_id, **args)
        except MappingInvalid as error:
            raise HTTPException(422, str(error)) from error
        return SupplierPage(
            items=[supplier_out(v) for v in page.items], total=page.total,
            next_after=page.next_after,
        )  # fmt: skip

    @router.post(
        "/suppliers", response_model=SupplierOut, status_code=201,
        responses={409: {"description": "The GSTIN is mapped already: `existing_id` names it"}},
    )  # fmt: skip
    def create_supplier(body: SupplierIn, principal: Reviewer) -> Any:
        """Map a seller GSTIN to the names the books use. Every later bill from the GSTIN,
        under any printed name, is booked under them."""
        try:
            made = mappings.create_supplier(
                principal.tenant_id, **body.model_dump(), actor=principal.actor,
                name=principal.name,
            )  # fmt: skip
        except MappingInvalid as error:
            raise HTTPException(422, str(error)) from error
        except MappingExists as error:
            return _exists(error)
        except TooManyMappings as error:
            raise HTTPException(409, str(error)) from error
        return supplier_out(made)

    @router.patch("/suppliers/{mapping_id}", response_model=SupplierOut)
    def change_supplier(
        mapping_id: uuid.UUID, body: SupplierChange, principal: Reviewer
    ) -> SupplierOut:
        """Change the names it is booked under. Later exports use them; past ones keep theirs."""
        try:
            changed = mappings.update_supplier(
                principal.tenant_id, mapping_id, body.model_dump(exclude_unset=True),
                actor=principal.actor, name=principal.name,
            )  # fmt: skip
        except MappingNotFound as error:
            raise HTTPException(404, "No such supplier mapping.") from error
        except MappingInvalid as error:
            raise HTTPException(422, str(error)) from error
        return supplier_out(changed)

    @router.delete("/suppliers/{mapping_id}", status_code=204)
    def remove_supplier(mapping_id: uuid.UUID, principal: Reviewer) -> Response:
        try:
            mappings.delete_supplier(principal.tenant_id, mapping_id, actor=principal.actor)
        except MappingNotFound as error:
            raise HTTPException(404, "No such supplier mapping.") from error
        return Response(status_code=204)

    @router.get("/items", response_model=ItemPage)
    def list_items(principal: Reader, args: PageArgs) -> ItemPage:
        """In printed-name order; `q` finds by words of the name, pack or stock item."""
        try:
            page = mappings.list_items(principal.tenant_id, **args)
        except MappingInvalid as error:
            raise HTTPException(422, str(error)) from error
        return ItemPage(
            items=[item_out(v) for v in page.items], total=page.total, next_after=page.next_after
        )

    @router.post(
        "/items", response_model=ItemOut, status_code=201,
        responses={409: {"description": "The name and pack are mapped already: `existing_id`"}},
    )  # fmt: skip
    def create_item(body: ItemIn, principal: Reviewer) -> Any:
        """Map a printed product name and pack to the stock item the books use."""
        try:
            made = mappings.create_item(
                principal.tenant_id, **body.model_dump(), actor=principal.actor,
                name=principal.name,
            )  # fmt: skip
        except MappingInvalid as error:
            raise HTTPException(422, str(error)) from error
        except MappingExists as error:
            return _exists(error)
        except TooManyMappings as error:
            raise HTTPException(409, str(error)) from error
        return item_out(made)

    @router.patch("/items/{mapping_id}", response_model=ItemOut)
    def change_item(mapping_id: uuid.UUID, body: ItemChange, principal: Reviewer) -> ItemOut:
        """Change the names it is booked under, or its HSN. Later exports use them."""
        try:
            changed = mappings.update_item(
                principal.tenant_id, mapping_id, body.model_dump(exclude_unset=True),
                actor=principal.actor, name=principal.name,
            )  # fmt: skip
        except MappingNotFound as error:
            raise HTTPException(404, "No such item mapping.") from error
        except MappingInvalid as error:
            raise HTTPException(422, str(error)) from error
        return item_out(changed)

    @router.delete("/items/{mapping_id}", status_code=204)
    def remove_item(mapping_id: uuid.UUID, principal: Reviewer) -> Response:
        try:
            mappings.delete_item(principal.tenant_id, mapping_id, actor=principal.actor)
        except MappingNotFound as error:
            raise HTTPException(404, "No such item mapping.") from error
        return Response(status_code=204)

    @router.post("/import", response_model=ImportOut)
    def import_masters(file: UploadFile, principal: Reviewer) -> ImportOut:
        """A CSV of the books' existing names - columns name, type (supplier or item) and,
        for suppliers, gstin - kept to be offered as suggestions, never applied. Each bad row
        is reported by its line (the header is line 1); the good rows are kept."""
        data = file.file.read(MAX_IMPORT_BYTES + 1)
        if len(data) > MAX_IMPORT_BYTES:
            raise HTTPException(413, "The file is larger than 2 MB: split it and upload each part.")
        try:
            report = mappings.import_masters(
                principal.tenant_id, data, actor=principal.actor, name=principal.name
            )
        except MastersFileInvalid as error:
            raise HTTPException(422, str(error)) from error
        return ImportOut(
            rows=report.rows, imported=report.imported, already_known=report.already_known,
            rejected=[RejectedOut(line=r.line, reason=r.reason) for r in report.rejected],
        )  # fmt: skip

    @router.get("/suggestions", response_model=TasksOut)
    def suggestions(
        principal: Reader,
        document_id: uuid.UUID | None = None,
        export_run_id: uuid.UUID | None = None,
        target: Literal["tally", "marg"] | None = None,
    ) -> TasksOut:
        """What a bill, or an export run, still needs mapped, each with names to offer. Only
        offered: nothing is mapped until a mapping is made with POST."""
        if (document_id is None) == (export_run_id is None):
            raise HTTPException(422, "Name a document_id or an export_run_id, not both.")
        tenant = principal.tenant_id
        if export_run_id is not None:
            if runs is None:
                raise HTTPException(404, "Not found.")
            try:
                run = runs.get(tenant, export_run_id)
            except RunNotFound as error:
                raise HTTPException(404, "No such export run.") from error
            return tasks_out(mappings.suggestions(tenant, run.tasks or Tasks(), target=run.target))
        if review is None or document_id is None:
            raise HTTPException(404, "Not found.")
        try:
            filename, invoice = review.effective_invoice(tenant, document_id)
        except DocumentNotFound as error:
            raise HTTPException(404, "Not found.") from error
        except NotReviewable as error:
            raise HTTPException(
                422, "Only an invoice that has been read has suppliers and items to map."
            ) from error
        bill = Bill(document_id, filename, invoice)
        tasks = mappings.tasks_for(tenant, [bill], target=target)
        return tasks_out(mappings.suggestions(tenant, tasks, target=target))

    return router
