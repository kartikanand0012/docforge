"""The PO variance (V2 P7): one bill against its PO in rupees, and a month of them as a report.

The document's review read carries its `variance` (`VarianceOut`). `GET
/v1/exports/po-variance` previews a month (per supplier, per invoice, and the invoices not
checked with their reasons); `POST .../file` makes the Excel file, audited.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from docforge.api.auth import require
from docforge.auth import Principal
from docforge.purchase_register import RegisterInvalid, this_month
from docforge.trust.variance import Difference, LineVariance, Tolerances, Unpaired, Variance
from docforge.variance_report import (
    SIGN_NOTE,
    NotChecked,
    PeriodReport,
    SupplierSummary,
    VarianceEmpty,
    VarianceReportService,
)

Reader = Annotated[Principal, Depends(require("documents:read"))]
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class DifferenceOut(BaseModel):
    field: Literal["rate", "qty", "free_qty", "taxable"]
    label: str  # Rate, Qty, Free Qty, Taxable Amount
    status: Literal["variance", "within_tolerance", "not_compared"]
    po_value: Decimal | int | None
    billed_value: Decimal | int | None
    difference: Decimal | int | None  # billed less PO
    money_impact: Decimal | None  # null when matched within tolerance or not compared
    working: str | None  # the arithmetic, in words
    note: str | None


class LineOut(BaseModel):
    line: int  # the bill's line, from 1
    order_line: int  # the PO's line, from 1
    product: str | None
    clean: bool  # matched, perhaps within a tolerance
    note: str | None
    differences: list[DifferenceOut]


class UnpairedOut(BaseModel):
    line: int  # from 1, on its own document
    product: str | None
    qty: int | None
    rate: Decimal | None
    value: Decimal | None
    working: str | None
    note: str | None


class VarianceTolerancesOut(BaseModel):
    rate: Decimal
    qty: int
    free_qty: int
    amount: Decimal


class VarianceOut(BaseModel):
    invoice_no: str | None
    po_no: str | None
    supplier_name: str | None
    supplier_gstin: str | None
    invoice_total: Decimal | None
    po_total: Decimal  # PO quantity x rate over the paired lines
    net_impact: Decimal
    net_percent: Decimal | None  # of the PO total; null when that is unknown
    above_po: Decimal  # the positive impacts only
    lines_checked: int
    lines_matched: int
    variances: int
    not_compared: int
    lines: list[LineOut]  # every paired line; the clean ones are collapsed on screen
    not_in_po: list[UnpairedOut]
    not_billed: list[UnpairedOut]
    not_in_po_value: Decimal
    not_billed_value: Decimal
    tolerances: VarianceTolerancesOut
    sign_note: str


def tolerances_out(t: Tolerances) -> VarianceTolerancesOut:
    return VarianceTolerancesOut(rate=t.rate, qty=t.qty, free_qty=t.free_qty, amount=t.amount)


def _difference(d: Difference) -> DifferenceOut:
    return DifferenceOut(
        field=d.field, label=d.label, status=d.status, po_value=d.po_value,
        billed_value=d.billed_value, difference=d.difference, money_impact=d.money_impact,
        working=d.working, note=d.note,
    )  # fmt: skip


def _line(line: LineVariance) -> LineOut:
    return LineOut(
        line=line.invoice_line + 1, order_line=line.order_line + 1, product=line.product,
        clean=line.clean, note=line.note,
        differences=[_difference(d) for d in line.differences],
    )  # fmt: skip


def _unpaired(u: Unpaired) -> UnpairedOut:
    return UnpairedOut(line=u.line + 1, product=u.product, qty=u.qty, rate=u.rate,
                       value=u.value, working=u.working, note=u.note)  # fmt: skip


def variance_out(v: Variance) -> VarianceOut:
    return VarianceOut(
        invoice_no=v.invoice_no, po_no=v.po_no, supplier_name=v.supplier_name,
        supplier_gstin=v.supplier_gstin, invoice_total=v.invoice_total, po_total=v.po_total,
        net_impact=v.net_impact, net_percent=v.net_percent, above_po=v.above_po,
        lines_checked=len(v.lines), lines_matched=v.matched_lines, variances=v.variances,
        not_compared=v.not_compared, lines=[_line(line) for line in v.lines],
        not_in_po=[_unpaired(u) for u in v.not_in_po],
        not_billed=[_unpaired(u) for u in v.not_billed],
        not_in_po_value=v.not_in_po_value, not_billed_value=v.not_billed_value,
        tolerances=tolerances_out(v.tolerances), sign_note=SIGN_NOTE,
    )  # fmt: skip


class SupplierVarianceOut(BaseModel):
    supplier_gstin: str | None
    supplier_name: str | None
    invoices_checked: int
    lines_checked: int
    lines_matched: int
    variances: int
    not_compared: int
    not_in_po_lines: int
    not_billed_lines: int
    above_po: Decimal
    net_impact: Decimal


class InvoiceOut(BaseModel):
    document_id: uuid.UUID
    order_document_id: uuid.UUID
    invoice_no: str | None
    invoice_date: date | None
    po_no: str | None
    supplier_gstin: str | None
    supplier_name: str | None
    invoice_total: Decimal | None
    po_total: Decimal
    net_impact: Decimal
    net_percent: Decimal | None
    above_po: Decimal
    lines_checked: int
    lines_matched: int
    variances: int
    not_in_po_lines: int
    not_billed_lines: int


class NotCheckedOut(BaseModel):
    document_id: uuid.UUID
    invoice_no: str | None
    invoice_date: date | None
    supplier_gstin: str | None
    supplier_name: str | None
    reason: str


class ReportOut(BaseModel):
    month: str  # YYYY-MM
    supplier_gstin: str | None
    tolerances: VarianceTolerancesOut
    totals: SupplierVarianceOut
    suppliers: list[SupplierVarianceOut]  # the most charged above the PO first
    invoices: list[InvoiceOut]  # the checked invoices, by date
    not_checked: list[NotCheckedOut]  # every other invoice dated in the month, with its reason
    sign_note: str


def _supplier(s: SupplierSummary) -> SupplierVarianceOut:
    return SupplierVarianceOut(**vars(s))


def _not_checked(n: NotChecked) -> NotCheckedOut:
    return NotCheckedOut(**vars(n))


def _report(report: PeriodReport) -> ReportOut:
    return ReportOut(
        month=report.month.strftime("%Y-%m"),
        supplier_gstin=report.supplier_gstin,
        tolerances=tolerances_out(report.tolerances),
        totals=_supplier(report.totals),
        suppliers=[_supplier(s) for s in report.suppliers],
        invoices=[
            InvoiceOut(
                document_id=c.document_id,
                order_document_id=c.order_document_id,
                invoice_no=c.variance.invoice_no,
                invoice_date=c.invoice_date,
                po_no=c.variance.po_no,
                supplier_gstin=c.variance.supplier_gstin,
                supplier_name=c.supplier_name,
                invoice_total=c.variance.invoice_total,
                po_total=c.variance.po_total,
                net_impact=c.variance.net_impact,
                net_percent=c.variance.net_percent,
                above_po=c.variance.above_po,
                lines_checked=len(c.variance.lines),
                lines_matched=c.variance.matched_lines,
                variances=c.variance.variances,
                not_in_po_lines=len(c.variance.not_in_po),
                not_billed_lines=len(c.variance.not_billed),
            )
            for c in report.checked
        ],
        not_checked=[_not_checked(n) for n in report.not_checked],
        sign_note=SIGN_NOTE,
    )


def variance_router(reports: VarianceReportService) -> APIRouter:
    router = APIRouter(prefix="/v1/exports/po-variance")

    @router.get("", response_model=ReportOut)
    def preview(
        principal: Reader,
        month: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
        supplier_gstin: Annotated[str | None, Query(max_length=20)] = None,
    ) -> ReportOut:
        """The month's report: each supplier's sums, each checked invoice, and every invoice
        dated in the month that was not checked, with its reason. This month by default."""
        try:
            report = reports.build(
                principal.tenant_id, month or this_month(), supplier_gstin=supplier_gstin
            )
        except RegisterInvalid as error:
            raise HTTPException(422, str(error)) from error
        return _report(report)

    @router.post(
        "/file",
        response_class=Response,
        responses={200: {"content": {_XLSX: {}}},
                   422: {"description": "No invoice in the month, or a bad month or GSTIN"}},
    )  # fmt: skip
    def download(
        principal: Reader,
        month: Annotated[str, Form(pattern=r"^\d{4}-\d{2}$")],
        supplier_gstin: Annotated[str | None, Form(max_length=20)] = None,
    ) -> Response:
        """The report as an Excel file: Summary (by supplier, with totals), Invoices,
        Variances (each with its arithmetic), Not in PO - Not billed, Not checked, About."""
        try:
            filename, data, _ = reports.workbook(
                principal.tenant_id, month, supplier_gstin=supplier_gstin,
                actor=principal.actor, person=principal.name,
            )  # fmt: skip
        except RegisterInvalid as error:
            raise HTTPException(422, str(error)) from error
        except VarianceEmpty as error:
            return JSONResponse(
                status_code=422, content={"detail": str(error), "code": "variance_empty"}
            )
        return Response(
            data,
            media_type=_XLSX,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    return router
