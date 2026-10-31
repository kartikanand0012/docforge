"""Export runs (V2 P1, P3): approved bills booked under the workspace's own Tally / Marg names,
as the mapped CSV or as a Marg purchase import in the workspace's own profile.

A run that meets an unmapped supplier or item, or a bill that fails pre-flight, makes no
file and says what to map or correct; fixed, the same request runs clean. A ready run's file
is kept as it was made: downloading it again, after any mapping or profile changed, returns
the same bytes. A Marg import with no active profile is refused (409, `no_active_profile`)
with what is needed.
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from docforge.api.auth import require
from docforge.api.mappings import TasksOut, tasks_out
from docforge.auth import Principal
from docforge.export_runs import (
    MAX_LIST,
    MAX_RANGE_DAYS,
    ExportRunService,
    NoFile,
    RunNotFound,
    RunRangeInvalid,
    RunView,
    TooManyInvoices,
    media_type,
)
from docforge.profiles.service import NoActiveProfile

Reader = Annotated[Principal, Depends(require("documents:read"))]


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    target: Literal["tally", "marg"]
    from_: date = Field(alias="from")  # India's dates the bills were approved, both included
    to: date
    format: Literal["mapped_csv", "marg_import"] = "mapped_csv"  # marg_import: Marg only


class FileOut(BaseModel):
    filename: str
    sha256: str
    size_bytes: int


class ProfileRefOut(BaseModel):
    """The profile version a Marg import was made in."""

    version_id: uuid.UUID
    profile_id: uuid.UUID
    name: str
    version_no: int
    sample: bool  # the made-up SAMPLE layout: not verified with any Marg import


class OverlapOut(BaseModel):
    """An earlier run whose file booked some of these bills: a warning, not a refusal."""

    run_id: uuid.UUID
    created_at: datetime
    format: Literal["mapped_csv", "marg_import"]
    invoices: int


class RunOut(BaseModel):
    id: uuid.UUID
    target: Literal["tally", "marg"]
    format: Literal["mapped_csv", "marg_import"]
    from_: date = Field(serialization_alias="from")
    to: date
    status: Literal["needs_mapping", "ready"]
    invoices: int
    lines: int
    tasks: TasksOut | None  # what to map before it can run clean; null once it has
    file: FileOut | None  # null when it met something unmapped
    profile: ProfileRefOut | None  # a Marg import's profile version
    overlaps: list[OverlapOut]  # earlier runs that booked some of the same bills
    created_by: str
    created_at: datetime


def _out(run: RunView) -> RunOut:
    file = (
        FileOut(filename=run.filename, sha256=run.sha256, size_bytes=run.size_bytes)
        if run.filename and run.sha256 and run.size_bytes is not None
        else None
    )
    profile = (
        ProfileRefOut(
            version_id=run.profile.id, profile_id=run.profile.profile_id, name=run.profile.name,
            version_no=run.profile.version_no, sample=run.profile.basis == "sample",
        )
        if run.profile
        else None
    )  # fmt: skip
    return RunOut(
        id=run.id, target=run.target, format=run.format, from_=run.start, to=run.end,
        status=run.status, invoices=run.invoices, lines=run.lines,
        tasks=tasks_out(run.tasks) if run.tasks is not None else None, file=file,
        profile=profile,
        overlaps=[OverlapOut(run_id=o.run_id, created_at=o.created_at, format=o.format,
                             invoices=o.invoices) for o in run.overlaps],
        created_by=run.created_by, created_at=run.created_at,
    )  # fmt: skip


def export_runs_router(runs: ExportRunService) -> APIRouter:
    router = APIRouter(prefix="/v1/exports/runs")

    @router.post(
        "",
        response_model=RunOut,
        status_code=201,
        responses={409: {"description": "A Marg import with no active Marg profile"}},
    )
    def create_run(body: RunIn, principal: Reader) -> RunOut | JSONResponse:
        """Export the invoices approved in the range for Tally or Marg. Nothing is guessed:
        with anything unmapped, or a bill that fails pre-flight, the run lists it and makes
        no file."""
        if body.format == "marg_import" and body.target != "marg":
            raise HTTPException(422, "A Marg purchase import is for Marg: choose Marg.")
        try:
            run = runs.create(
                principal.tenant_id, target=body.target, start=body.from_, end=body.to,
                actor=principal.actor, name=principal.name, file_format=body.format,
            )  # fmt: skip
        except NoActiveProfile as error:
            return JSONResponse(
                status_code=409, content={"detail": str(error), "code": "no_active_profile"}
            )
        except RunRangeInvalid as error:
            raise HTTPException(
                422,
                f"That range cannot be exported: {error}. Choose a range of at most "
                f"{MAX_RANGE_DAYS} days that ends on or after the day it starts.",
            ) from error
        except TooManyInvoices as error:
            raise HTTPException(422, str(error)) from error
        return _out(run)

    @router.get("", response_model=list[RunOut])
    def list_runs(
        principal: Reader, limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50
    ) -> list[RunOut]:
        """Newest first."""
        return [_out(run) for run in runs.list(principal.tenant_id, limit=limit)]

    @router.get("/{run_id}", response_model=RunOut)
    def read_run(run_id: uuid.UUID, principal: Reader) -> RunOut:
        try:
            return _out(runs.get(principal.tenant_id, run_id))
        except RunNotFound as error:
            raise HTTPException(404, "No such export run.") from error

    @router.get(
        "/{run_id}/file",
        response_class=Response,
        responses={
            200: {
                "content": {
                    "text/csv": {},
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {},
                }
            },
            409: {"description": "No file made"},
        },
    )
    def download_run(run_id: uuid.UUID, principal: Reader) -> Response:
        """The run's file, byte for byte as it was made."""
        try:
            filename, data = runs.file(principal.tenant_id, run_id, actor=principal.actor)
        except RunNotFound as error:
            raise HTTPException(404, "No such export run.") from error
        except NoFile as error:
            raise HTTPException(
                409,
                "This run made no file: map its suppliers and items and correct the bills it "
                "lists, then run the export again.",
            ) from error
        return Response(
            data,
            media_type=media_type(filename),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    return router
