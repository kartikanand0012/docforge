"""Exports of signed records: what an ERP or a spreadsheet takes in."""

import csv
import io
from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response

from docforge.api.auth import require
from docforge.auth import Principal
from docforge.review.service import ReviewService

Reader = Annotated[Principal, Depends(require("documents:read"))]
# A cell starting with one of these is read as a formula by spreadsheet programs.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_START) else text


def to_csv(rows: Sequence[dict[str, Any]]) -> str:
    """Rows as CSV, every cell safe to open in a spreadsheet."""
    if not rows:
        return ""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _cell(value) for key, value in row.items()})
    return out.getvalue()


def exports_router(review: ReviewService) -> APIRouter:
    router = APIRouter(prefix="/v1/exports")

    @router.get("/documents.csv", response_class=Response)
    def documents_csv(principal: Reader) -> Response:
        """Every signed record of the organisation, one row each, newest first."""
        return Response(
            to_csv(review.export(principal.tenant_id)),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="docforge-documents.csv"'},
        )

    @router.get("/documents.json")
    def documents_json(principal: Reader) -> list[dict[str, Any]]:
        """The same, with each signed record in full."""
        return review.export(principal.tenant_id, include_records=True)

    return router
