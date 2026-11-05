"""Import-file profiles (V2 P3): Settings > Marg profile.

A profile is made from an import file the workspace's Marg accepted: `read-sample` reads its
layout and offers a source for each column (nothing saved); a person checks each and saves.
Every edit is a new version; runs keep the version they used. `check` makes the profile's
file for some approved bills and compares it, cell by cell, with the file Marg accepted for
them - the definition of "the format is right".

The built-in `template` is labelled SAMPLE: a made-up layout to try the engine with, not a
Marg format. Those who review change profiles; everyone in the workspace reads them.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from docforge.api.auth import require
from docforge.api.mappings import TasksOut, tasks_out
from docforge.auth import Principal
from docforge.export_runs import CheckInvalid, ExportRunService
from docforge.profiles.compare import SampleUnreadable
from docforge.profiles.sample import MAX_SAMPLE_BYTES, read_sample
from docforge.profiles.service import (
    ProfileInvalid,
    ProfileNameTaken,
    ProfileNotFound,
    ProfileView,
    VersionView,
)
from docforge.profiles.spec import (
    COMPUTED_INVOICE_FIELDS,
    COMPUTED_LINE_FIELDS,
    INVOICE_FIELDS,
    LINE_FIELDS,
    MAPPING_INVOICE_FIELDS,
    MAPPING_LINE_FIELDS,
    SAMPLE_NAME,
    Column,
    Kind,
    Profile,
    sample_profile,
)

Reader = Annotated[Principal, Depends(require("documents:read"))]
Reviewer = Annotated[Principal, Depends(require("review"))]
Basis = Literal["sample", "upload", "manual"]
FieldSource = Literal["invoice", "line", "mapping", "computed"]

# What each field is, for the column picker, as a clerk would name it.
LABELS = {
    ("invoice", "invoice_no"): "Supplier's invoice number",
    ("invoice", "invoice_date"): "Invoice date",
    ("invoice", "po_no"): "Order number",
    ("invoice", "po_date"): "Order date",
    ("invoice", "place_of_supply_code"): "Place of supply (state code)",
    ("invoice", "seller_gstin"): "Supplier GSTIN",
    ("invoice", "seller_name"): "Supplier name as printed",
    ("invoice", "buyer_gstin"): "Buyer GSTIN",
    ("invoice", "taxable_value"): "Bill taxable value",
    ("invoice", "cgst"): "Bill CGST",
    ("invoice", "sgst"): "Bill SGST",
    ("invoice", "igst"): "Bill IGST",
    ("invoice", "round_off"): "Round-off",
    ("invoice", "grand_total"): "Bill total",
    ("line", "line_no"): "Line number",
    ("line", "product_name"): "Product as printed",
    ("line", "pack"): "Pack",
    ("line", "hsn"): "HSN as printed",
    ("line", "batch_no"): "Batch",
    ("line", "mfg"): "Manufacturing month",
    ("line", "expiry"): "Expiry month",
    ("line", "qty"): "Quantity",
    ("line", "free_qty"): "Free (scheme) quantity",
    ("line", "mrp"): "MRP",
    ("line", "ptr"): "Rate (PTR)",
    ("line", "discount_pct"): "Discount %",
    ("line", "taxable_value"): "Line taxable value",
    ("line", "gst_rate"): "GST %",
    ("line", "amount"): "Line amount",
    ("mapping", "party"): "Supplier as your Marg names it",
    ("mapping", "purchase_ledger"): "Purchase ledger",
    ("mapping", "tax_treatment"): "Tax treatment",
    ("mapping", "item"): "Item as your Marg names it",
    ("mapping", "hsn"): "HSN (preferred, else printed)",
    ("computed", "supply_type"): "Within state / across states",
    ("computed", "tax_total"): "Bill tax total",
    ("computed", "line_count"): "Number of lines",
    ("computed", "cgst_rate"): "CGST %",
    ("computed", "sgst_rate"): "SGST %",
    ("computed", "igst_rate"): "IGST %",
    ("computed", "cgst"): "Line CGST",
    ("computed", "sgst"): "Line SGST",
    ("computed", "igst"): "Line IGST",
    ("computed", "tax"): "Line tax",
    ("computed", "row_kind"): "Billed or free row",
}
_GROUPS: tuple[tuple[FieldSource, dict[str, Kind], bool], ...] = (
    ("invoice", INVOICE_FIELDS, False),
    ("line", LINE_FIELDS, True),
    ("mapping", MAPPING_INVOICE_FIELDS, False),
    ("mapping", MAPPING_LINE_FIELDS, True),
    ("computed", COMPUTED_INVOICE_FIELDS, False),
    ("computed", COMPUTED_LINE_FIELDS, True),
)


class FieldOut(BaseModel):
    source: FieldSource
    field: str
    kind: Kind
    label: str
    per_line_only: bool  # only in a one-row-per-line file


class ProfileVersionOut(BaseModel):
    id: uuid.UUID
    version_no: int
    basis: Basis
    sample: bool  # the made-up SAMPLE layout: not verified with any Marg import
    spec: Profile
    created_by: str
    created_at: datetime


class ProfileOut(BaseModel):
    id: uuid.UUID
    target: Literal["marg"]
    name: str
    active: bool
    latest: ProfileVersionOut
    created_by: str
    created_at: datetime
    updated_by: str | None
    updated_at: datetime | None


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=200)
    basis: Basis
    spec: dict[str, Any]  # a `Profile`; refused in words when it cannot be used


class VersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    basis: Basis
    spec: dict[str, Any]
    name: str | None = Field(default=None, max_length=200)


class TemplateOut(BaseModel):
    name: str
    basis: Literal["sample"]
    spec: Profile


class SampleColumnOut(BaseModel):
    header: str
    examples: list[str]
    offer: Column | None  # offered, never saved until a person saves the profile


class SampleOut(BaseModel):
    file_type: Literal["csv", "xlsx"]
    delimiter: str
    header_row: bool
    encoding: str
    sheet_name: str
    rows: int
    columns: list[SampleColumnOut]


class DiffOut(BaseModel):
    row: int
    column: str
    expected: str  # as the accepted file has it
    produced: str  # as this profile writes it


class CheckOut(BaseModel):
    version_no: int
    bills: int
    exact: bool
    rows_expected: int | None
    rows_produced: int | None
    cells: int | None
    equal: int | None
    diffs: list[DiffOut]  # the first 200
    tasks: TasksOut | None  # what to map or correct before the bills can be compared


def _version_out(version: VersionView) -> ProfileVersionOut:
    return ProfileVersionOut(
        id=version.id, version_no=version.version_no, basis=version.basis,
        sample=version.basis == "sample", spec=version.spec, created_by=version.created_by,
        created_at=version.created_at,
    )  # fmt: skip


def _out(view: ProfileView) -> ProfileOut:
    return ProfileOut(
        id=view.id, target=view.target, name=view.name, active=view.active,
        latest=_version_out(view.latest), created_by=view.created_by,
        created_at=view.created_at, updated_by=view.updated_by, updated_at=view.updated_at,
    )  # fmt: skip


def _fields() -> list[FieldOut]:
    return [
        FieldOut(
            source=source,
            field=name,
            kind=kind,
            label=LABELS.get((source, name), name),
            per_line_only=per_line,
        )
        for source, fields, per_line in _GROUPS
        for name, kind in fields.items()
    ]


def _read(file: UploadFile) -> bytes:
    data = file.file.read(MAX_SAMPLE_BYTES + 1)
    if len(data) > MAX_SAMPLE_BYTES:
        raise HTTPException(413, "The file is larger than 2 MB: one bill or a few are enough.")
    return data


_NOT_FOUND = "No such profile."


def export_profiles_router(runs: ExportRunService) -> APIRouter:
    router = APIRouter(prefix="/v1/settings/export-profiles")
    profiles = runs.profiles

    @router.get("", response_model=list[ProfileOut])
    def list_profiles(principal: Reader) -> list[ProfileOut]:
        """The workspace's Marg import profiles, the active one first."""
        return [_out(view) for view in profiles.list(principal.tenant_id)]

    @router.get("/template", response_model=TemplateOut)
    def template(principal: Reader) -> TemplateOut:
        """The built-in SAMPLE layout: made up, to try the engine with. Not a Marg format."""
        return TemplateOut(name=SAMPLE_NAME, basis="sample", spec=sample_profile())

    @router.get("/fields", response_model=list[FieldOut])
    def fields(principal: Reader) -> list[FieldOut]:
        """What a column can take, for the column picker."""
        return _fields()

    @router.post("/read-sample", response_model=SampleOut)
    def read_sample_file(file: UploadFile, principal: Reviewer) -> SampleOut:
        """Read the layout of an import file Marg accepted (CSV or Excel, at most 2 MB) and
        offer a source for each column. Nothing is saved, logged or audited."""
        try:
            layout = read_sample(_read(file))
        except SampleUnreadable as error:
            raise HTTPException(422, str(error)) from error
        return SampleOut(
            file_type=layout.file_type, delimiter=layout.delimiter,
            header_row=layout.header_row, encoding=layout.encoding,
            sheet_name=layout.sheet_name, rows=layout.rows,
            columns=[
                SampleColumnOut(header=c.header, examples=list(c.examples), offer=c.offer)
                for c in layout.columns
            ],
        )  # fmt: skip

    @router.post("", response_model=ProfileOut, status_code=201)
    def create_profile(body: ProfileIn, principal: Reviewer) -> ProfileOut:
        """Version 1 of a profile; it is made active when the workspace has none active."""
        try:
            view = profiles.create(
                principal.tenant_id, name=body.name, spec=body.spec, basis=body.basis,
                actor=principal.actor, person=principal.name,
            )  # fmt: skip
        except ProfileInvalid as error:
            raise HTTPException(422, str(error)) from error
        except ProfileNameTaken as error:
            raise HTTPException(409, str(error)) from error
        return _out(view)

    @router.get("/{profile_id}", response_model=ProfileOut)
    def read_profile(profile_id: uuid.UUID, principal: Reader) -> ProfileOut:
        try:
            return _out(profiles.get(principal.tenant_id, profile_id))
        except ProfileNotFound as error:
            raise HTTPException(404, _NOT_FOUND) from error

    @router.put("/{profile_id}", response_model=ProfileOut)
    def save_version(profile_id: uuid.UUID, body: VersionIn, principal: Reviewer) -> ProfileOut:
        """Save the layout as the next version. Past runs keep the version they used."""
        try:
            view = profiles.save_version(
                principal.tenant_id, profile_id, spec=body.spec, basis=body.basis,
                name=body.name, actor=principal.actor, person=principal.name,
            )  # fmt: skip
        except ProfileNotFound as error:
            raise HTTPException(404, _NOT_FOUND) from error
        except ProfileInvalid as error:
            raise HTTPException(422, str(error)) from error
        except ProfileNameTaken as error:
            raise HTTPException(409, str(error)) from error
        return _out(view)

    @router.get("/{profile_id}/versions", response_model=list[ProfileVersionOut])
    def list_versions(profile_id: uuid.UUID, principal: Reader) -> list[ProfileVersionOut]:
        """Newest first."""
        try:
            return [_version_out(v) for v in profiles.versions(principal.tenant_id, profile_id)]
        except ProfileNotFound as error:
            raise HTTPException(404, _NOT_FOUND) from error

    @router.post("/{profile_id}/activate", response_model=ProfileOut)
    def activate(profile_id: uuid.UUID, principal: Reviewer) -> ProfileOut:
        """Make this the workspace's Marg profile; the one active before stops being so."""
        try:
            view = profiles.activate(
                principal.tenant_id, profile_id, actor=principal.actor, person=principal.name
            )
        except ProfileNotFound as error:
            raise HTTPException(404, _NOT_FOUND) from error
        return _out(view)

    @router.post("/{profile_id}/check", response_model=CheckOut)
    def check(
        profile_id: uuid.UUID,
        file: UploadFile,
        document_ids: Annotated[str, Form(max_length=2000)],
        principal: Reviewer,
    ) -> CheckOut:
        """Make this profile's file for the approved bills named (document ids,
        comma-separated, in the file's order) and compare it, cell by cell, with the file
        Marg accepted for them. Nothing is stored; the comparison is audited by its counts."""
        try:
            ids = [uuid.UUID(part.strip()) for part in document_ids.split(",") if part.strip()]
        except ValueError as error:
            raise HTTPException(
                422, "Name the bills by their document ids, comma-separated."
            ) from error
        try:
            result = runs.check(
                principal.tenant_id, profile_id, document_ids=ids, accepted=_read(file),
                actor=principal.actor,
            )  # fmt: skip
        except ProfileNotFound as error:
            raise HTTPException(404, _NOT_FOUND) from error
        except (CheckInvalid, SampleUnreadable) as error:
            raise HTTPException(422, str(error)) from error
        found = result.comparison
        return CheckOut(
            version_no=result.version.version_no, bills=result.bills,
            exact=bool(found and found.exact),
            rows_expected=found.rows_expected if found else None,
            rows_produced=found.rows_produced if found else None,
            cells=found.cells if found else None, equal=found.equal if found else None,
            diffs=[DiffOut(row=d.row, column=d.column, expected=d.expected, produced=d.produced)
                   for d in (found.diffs if found else ())],
            tasks=tasks_out(result.tasks) if result.tasks else None,
        )  # fmt: skip

    return router
