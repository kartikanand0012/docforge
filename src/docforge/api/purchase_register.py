"""The purchase register (V2 P4): a month's approved invoices, one row each, as the CA sets
them beside GSTR-2B.

`GET` previews the rows, their count and totals - exactly what the file will hold. `POST
.../file` makes the Excel file (with a "2B check" sheet when a GSTR-2B download is attached).
`POST .../2b-check` compares the month with a GSTR-2B download the user uploads (Excel or
JSON, at most 10 MB and 20,000 rows): a comparison view, not a reconciliation with the GST
portal and no filing.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from docforge.api.auth import require
from docforge.auth import Principal
from docforge.gstr2b import MAX_BYTES, TwoBInvalid, TwoBInvoice, read_2b
from docforge.purchase_register import (
    CHECK_NOTE,
    EXCLUDED,
    HEADERS,
    NOTE_HELP,
    Matched,
    PurchaseRegisterService,
    Register,
    RegisterEmpty,
    RegisterInvalid,
    RegisterRow,
    this_month,
)

Reader = Annotated[Principal, Depends(require("documents:read"))]
Month = Annotated[str, Form(pattern=r"^\d{4}-\d{2}$")]
Supplier = Annotated[str | None, Form(max_length=20)]
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class TotalsOut(BaseModel):
    taxable_value: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    total_tax: Decimal
    invoice_total: Decimal


class RowOut(BaseModel):
    document_id: uuid.UUID
    supplier_gstin: str | None
    supplier_name: str | None
    invoice_no: str | None
    invoice_date: date | None
    place_of_supply: str | None  # the 2-digit code
    taxable_value: Decimal | None
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    total_tax: Decimal
    invoice_total: Decimal | None
    note: str  # data flags from the checks; "" when none


class RegisterOut(BaseModel):
    month: str  # YYYY-MM
    supplier_gstin: str | None
    invoices: int
    totals: TotalsOut
    rows: list[RowOut]
    columns: list[str]  # the file's header row
    excluded: str  # what the register leaves out, said on screen
    note_help: str  # what the ITC Eligibility Note is, and is not


class TwoBInvoiceOut(BaseModel):
    supplier_gstin: str
    trade_name: str | None
    invoice_no: str
    invoice_date: date | None
    invoice_value: Decimal | None
    taxable_value: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal


class MatchedOut(BaseModel):
    status: Literal["matched", "amount_differs", "docforge_only"]
    document_id: uuid.UUID
    twob: TwoBInvoiceOut | None
    differences: dict[str, Decimal]  # DocForge less 2B, for each amount beyond 1 rupee


class TwoBCheckOut(BaseModel):
    month: str
    matched: int
    amount_differs: int
    docforge_only: int
    twob_only: int
    rows: list[MatchedOut]  # the register's rows, in its order
    twob_only_invoices: list[TwoBInvoiceOut]
    note: str


def _row(row: RegisterRow) -> RowOut:
    return RowOut(
        document_id=row.document_id, supplier_gstin=row.supplier_gstin,
        supplier_name=row.supplier_name, invoice_no=row.invoice_no,
        invoice_date=row.invoice_date, place_of_supply=row.place_of_supply,
        taxable_value=row.taxable_value, cgst=row.cgst, sgst=row.sgst, igst=row.igst,
        total_tax=row.total_tax, invoice_total=row.invoice_total, note=row.note,
    )  # fmt: skip


def _register(register: Register) -> RegisterOut:
    t = register.totals
    return RegisterOut(
        month=register.month.strftime("%Y-%m"), supplier_gstin=register.supplier_gstin,
        invoices=len(register.rows),
        totals=TotalsOut(taxable_value=t.taxable_value, cgst=t.cgst, sgst=t.sgst, igst=t.igst,
                         total_tax=t.total_tax, invoice_total=t.invoice_total),
        rows=[_row(r) for r in register.rows], columns=list(HEADERS), excluded=EXCLUDED,
        note_help=NOTE_HELP,
    )  # fmt: skip


def _twob(invoice: TwoBInvoice) -> TwoBInvoiceOut:
    return TwoBInvoiceOut(
        supplier_gstin=invoice.supplier_gstin, trade_name=invoice.trade_name,
        invoice_no=invoice.invoice_no, invoice_date=invoice.invoice_date,
        invoice_value=invoice.invoice_value, taxable_value=invoice.taxable_value,
        cgst=invoice.cgst, sgst=invoice.sgst, igst=invoice.igst,
    )  # fmt: skip


def _matched(m: Matched) -> MatchedOut | None:
    if m.row is None or m.status == "twob_only":
        return None
    return MatchedOut(
        status=m.status, document_id=m.row.document_id,
        twob=_twob(m.twob) if m.twob else None, differences=m.differences,
    )  # fmt: skip


def _read_2b(file: UploadFile) -> list[TwoBInvoice]:
    data = file.file.read(MAX_BYTES + 1)
    try:
        return read_2b(data)
    except TwoBInvalid as error:
        raise HTTPException(413 if len(data) > MAX_BYTES else 422, str(error)) from error


def purchase_register_router(registers: PurchaseRegisterService) -> APIRouter:
    router = APIRouter(prefix="/v1/exports/purchase-register")

    @router.get("", response_model=RegisterOut)
    def preview(
        principal: Reader,
        month: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
        supplier_gstin: Annotated[str | None, Query(max_length=20)] = None,
    ) -> RegisterOut:
        """The month's rows (invoices dated in it, India's calendar, whose newest version
        was approved), their count and totals: what the file will hold. This month by
        default."""
        try:
            register = registers.build(
                principal.tenant_id, month or this_month(), supplier_gstin=supplier_gstin
            )
        except RegisterInvalid as error:
            raise HTTPException(422, str(error)) from error
        return _register(register)

    @router.post(
        "/file",
        response_class=Response,
        responses={200: {"content": {_XLSX: {}}},
                   422: {"description": "No approved invoice in the month, or a bad month"}},
    )  # fmt: skip
    def download(
        principal: Reader, month: Month, supplier_gstin: Supplier = None,
        gstr2b: UploadFile | None = None,
    ) -> Response:  # fmt: skip
        """The register as an Excel file: "Purchase Register" (one row per invoice and a
        totals row), "About" and, with a GSTR-2B download attached, "2B check"."""
        twob = _read_2b(gstr2b) if gstr2b is not None else None
        try:
            filename, data, _ = registers.workbook(
                principal.tenant_id, month, supplier_gstin=supplier_gstin, twob=twob,
                actor=principal.actor, person=principal.name,
            )  # fmt: skip
        except RegisterInvalid as error:
            raise HTTPException(422, str(error)) from error
        except RegisterEmpty as error:
            return JSONResponse(
                status_code=422, content={"detail": str(error), "code": "register_empty"}
            )
        return Response(
            data,
            media_type=_XLSX,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    @router.post("/2b-check", response_model=TwoBCheckOut)
    def check(
        principal: Reader, file: UploadFile, month: Month, supplier_gstin: Supplier = None
    ) -> TwoBCheckOut:
        """Set the month's register beside a GSTR-2B download: each row matched, in DocForge
        only, or with the amounts that differ; and the 2B invoices with no approved bill."""
        twob = _read_2b(file)
        try:
            register, found = registers.check_2b(
                principal.tenant_id, month, twob, supplier_gstin=supplier_gstin,
                actor=principal.actor,
            )  # fmt: skip
        except RegisterInvalid as error:
            raise HTTPException(422, str(error)) from error
        return TwoBCheckOut(
            month=register.month.strftime("%Y-%m"), matched=found.count("matched"),
            amount_differs=found.count("amount_differs"),
            docforge_only=found.count("docforge_only"), twob_only=found.count("twob_only"),
            rows=[out for m in found.rows if (out := _matched(m)) is not None],
            twob_only_invoices=[_twob(m.twob) for m in found.twob_only if m.twob is not None],
            note=CHECK_NOTE,
        )  # fmt: skip

    return router
