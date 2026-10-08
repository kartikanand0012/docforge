"""Exports of signed records: what an ERP or a spreadsheet takes in."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response

from docforge.api.auth import require
from docforge.auth import Principal
from docforge.csvsafe import to_csv
from docforge.review.service import ReviewService

Reader = Annotated[Principal, Depends(require("documents:read"))]
EXPORT_LIMIT = 10_000


def exports_router(review: ReviewService) -> APIRouter:
    router = APIRouter(prefix="/v1/exports")

    @router.get("/documents.csv", response_class=Response)
    def documents_csv(principal: Reader) -> Response:
        """Every signed record of the organisation, one row each, newest first."""
        rows = review.export(principal.tenant_id, limit=EXPORT_LIMIT + 1)
        headers = {"Content-Disposition": 'attachment; filename="docforge-documents.csv"'}
        if len(rows) > EXPORT_LIMIT:
            headers["X-DocForge-Truncated"] = f"only the newest {EXPORT_LIMIT} rows"
        return Response(
            to_csv(rows[:EXPORT_LIMIT]), media_type="text/csv; charset=utf-8", headers=headers
        )

    @router.get("/documents.json")
    def documents_json(principal: Reader) -> list[dict[str, Any]]:
        """The same, with each signed record in full."""
        return review.export(principal.tenant_id, include_records=True)

    return router
