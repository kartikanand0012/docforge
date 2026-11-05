"""A workspace's import-file profiles (V2 P3): made, versioned, made active.

A profile holds its name and whether it is the workspace's active one for its books (at
most one is). Its layout lives in versions, which are never changed: saving an edit makes
the next version, and an export run names the version it used - so a past run's file stays
what it was, and can be made again, whatever is edited later.

A version records its basis: `upload` (worked out from an import file the books accepted),
`manual`, or `sample` - the built-in made-up layout, labelled SAMPLE wherever it shows.

Each change is audited with the layout's shape (its version, basis, column count), never
values from a bill or a sample.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from docforge import audit
from docforge.db.models import ExportProfile, ExportProfileVersion
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped
from docforge.profiles.spec import Profile

Basis = Literal["sample", "upload", "manual"]
BASES: tuple[Basis, ...] = ("sample", "upload", "manual")
MAX_PROFILES = 20
NO_PROFILE = (
    "There is no active Marg profile. We need one purchase import file your Marg accepted. "
    "Your dealer or your last supplier import will have one. Upload it in Settings > Marg "
    "profile and we will match it."
)


class ProfileInvalid(ValueError):
    """The layout cannot be made, said in words."""


class ProfileNotFound(LookupError):
    """No such profile (or version) in this workspace."""


class ProfileNameTaken(ValueError):
    """The workspace has a profile of that name."""


class NoActiveProfile(LookupError):
    """The workspace has no active profile for its books: nothing can be exported in it."""

    def __init__(self) -> None:
        super().__init__(NO_PROFILE)


@dataclass(frozen=True)
class VersionView:
    id: uuid.UUID
    profile_id: uuid.UUID
    profile_name: str
    version_no: int
    basis: Basis
    spec: Profile
    created_by: str
    created_at: datetime


@dataclass(frozen=True)
class ProfileView:
    id: uuid.UUID
    target: Literal["marg"]
    name: str
    active: bool
    latest: VersionView
    created_by: str
    created_at: datetime
    updated_by: str | None
    updated_at: datetime | None


def parse_profile(spec: Any) -> Profile:
    """A layout from its JSON. Raises `ProfileInvalid` naming what is wrong."""
    if isinstance(spec, Profile):
        return spec
    try:
        return Profile.model_validate(spec)
    except ValidationError as error:
        reasons = []
        for found in error.errors()[:5]:
            where = ".".join(str(part) for part in found["loc"])
            message = str(found["msg"]).removeprefix("Value error, ")
            reasons.append(f"{where}: {message}" if where else message)
        raise ProfileInvalid("This layout cannot be used: " + "; ".join(reasons) + ".") from error


def _basis(value: str) -> Basis:
    for basis in BASES:
        if value == basis:
            return basis
    raise ProfileInvalid("A profile is made from an upload, by hand, or from the sample.")


def _name(value: str) -> str:
    name = " ".join(value.split())
    if not 1 <= len(name) <= 200:
        raise ProfileInvalid("A profile's name is 1 to 200 characters.")
    return name


def _taken(name: str | None) -> ProfileNameTaken:
    return ProfileNameTaken(f"There is already a profile named '{name}': choose another name.")


def _serialised(session: Session, tenant_id: uuid.UUID) -> None:
    """One change to a workspace's profiles at a time: two activations, or two first
    profiles, cannot cross (no deadlock, never two active)."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('export_profiles:' || :tenant))"),
        {"tenant": str(tenant_id)},
    )


def _version_view(row: ExportProfileVersion, name: str) -> VersionView:
    return VersionView(
        id=row.id,
        profile_id=row.profile_id,
        profile_name=name,
        version_no=row.version_no,
        basis=_basis(row.basis),
        spec=Profile.model_validate(row.spec),
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _view(row: ExportProfile, latest: ExportProfileVersion) -> ProfileView:
    return ProfileView(
        id=row.id,
        target="marg",
        name=row.name,
        active=row.active,
        latest=_version_view(latest, row.name),
        created_by=row.created_by,
        created_at=row.created_at,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


def _latest(session: Session, row: ExportProfile) -> ExportProfileVersion:
    return session.scalars(
        select(ExportProfileVersion).where(
            ExportProfileVersion.tenant_id == row.tenant_id,
            ExportProfileVersion.profile_id == row.id,
            ExportProfileVersion.version_no == row.latest_version,
        )
    ).one()


def _profile(
    session: Session, tenant_id: uuid.UUID, profile_id: uuid.UUID, *, lock: bool = False
) -> ExportProfile:
    query = select(ExportProfile).where(
        ExportProfile.tenant_id == tenant_id, ExportProfile.id == profile_id
    )
    row = session.scalars(query.with_for_update() if lock else query).one_or_none()
    if row is None:
        raise ProfileNotFound(profile_id)
    return row


def _audit(
    session: Session, tenant_id: uuid.UUID, actor: str, action: str, profile_id: uuid.UUID,
    details: dict[str, Any],
) -> None:  # fmt: skip
    audit.append(
        session, tenant_id=tenant_id, actor=actor, action=action, target_type="export_profile",
        target_id=str(profile_id), details=details,
    )  # fmt: skip


class ProfileService:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    @scoped
    def get(self, tenant_id: uuid.UUID, profile_id: uuid.UUID) -> ProfileView:
        """Raises `ProfileNotFound`."""
        with self._sessions() as session:
            row = _profile(session, tenant_id, profile_id)
            return _view(row, _latest(session, row))

    @scoped
    def versions(self, tenant_id: uuid.UUID, profile_id: uuid.UUID) -> list[VersionView]:
        """Newest first. Raises `ProfileNotFound`."""
        with self._sessions() as session:
            row = _profile(session, tenant_id, profile_id)
            found = session.scalars(
                select(ExportProfileVersion)
                .where(
                    ExportProfileVersion.tenant_id == tenant_id,
                    ExportProfileVersion.profile_id == profile_id,
                )
                .order_by(ExportProfileVersion.version_no.desc())
            )
            return [_version_view(version, row.name) for version in found]

    @scoped
    def version(self, tenant_id: uuid.UUID, version_id: uuid.UUID) -> VersionView:
        """One version, as a run used it. Raises `ProfileNotFound`."""
        with self._sessions() as session:
            found = session.execute(
                select(ExportProfileVersion, ExportProfile.name)
                .join(ExportProfile, ExportProfile.id == ExportProfileVersion.profile_id)
                .where(
                    ExportProfileVersion.tenant_id == tenant_id,
                    ExportProfileVersion.id == version_id,
                )
            ).one_or_none()
            if found is None:
                raise ProfileNotFound(version_id)
            return _version_view(found[0], found[1])

    @scoped
    def active_version(self, tenant_id: uuid.UUID) -> VersionView:
        """The latest version of the workspace's active Marg profile. Raises
        `NoActiveProfile`."""
        with self._sessions() as session:
            found = session.execute(
                select(ExportProfileVersion, ExportProfile.name)
                .join(
                    ExportProfile,
                    (ExportProfile.id == ExportProfileVersion.profile_id)
                    & (ExportProfile.latest_version == ExportProfileVersion.version_no),
                )
                .where(
                    ExportProfile.tenant_id == tenant_id,
                    ExportProfile.target == "marg",
                    ExportProfile.active,
                )
            ).one_or_none()
            if found is None:
                raise NoActiveProfile()
            return _version_view(found[0], found[1])

    @scoped
    def create(
        self,
        tenant_id: uuid.UUID,
        *,
        name: str,
        spec: Any,
        basis: str,
        actor: str,
        person: str,
    ) -> ProfileView:
        """Version 1 of a new profile; active when the workspace has no active one. Raises
        `ProfileInvalid` or `ProfileNameTaken`."""
        profile = parse_profile(spec)
        kind = _basis(basis)
        title = _name(name)
        with self._sessions.begin() as session:
            _serialised(session, tenant_id)
            count = session.scalar(select(func.count()).where(ExportProfile.tenant_id == tenant_id))
            if (count or 0) >= MAX_PROFILES:
                raise ProfileInvalid(
                    f"A workspace keeps at most {MAX_PROFILES} profiles: edit one instead."
                )
            has_active = session.scalar(
                select(ExportProfile.id).where(
                    ExportProfile.tenant_id == tenant_id,
                    ExportProfile.target == "marg",
                    ExportProfile.active,
                )
            )
            row = ExportProfile(
                tenant_id=tenant_id, target="marg", name=title, active=has_active is None,
                latest_version=1, created_by=person[:200],
            )  # fmt: skip
            session.add(row)
            try:
                session.flush()
            except IntegrityError as error:
                raise _taken(title) from error
            version = ExportProfileVersion(
                tenant_id=tenant_id, profile_id=row.id, version_no=1,
                spec=profile.model_dump(mode="json"), basis=kind, created_by=person[:200],
            )  # fmt: skip
            session.add(version)
            session.flush()
            _audit(session, tenant_id, actor, "export_profile.created", row.id,
                   {"target": "marg", "basis": kind, "columns": len(profile.columns)})  # fmt: skip
            if row.active:
                _audit(session, tenant_id, actor, "export_profile.activated", row.id,
                       {"version": 1})  # fmt: skip
            session.refresh(row)
            session.refresh(version)
            return _view(row, version)

    @scoped
    def save_version(
        self,
        tenant_id: uuid.UUID,
        profile_id: uuid.UUID,
        *,
        spec: Any,
        basis: str,
        actor: str,
        person: str,
        name: str | None = None,
    ) -> ProfileView:
        """The next version of the layout (and, given, a new name). Earlier versions, and
        the runs made with them, are left as they were. Raises `ProfileNotFound`,
        `ProfileInvalid` or `ProfileNameTaken`."""
        profile = parse_profile(spec)
        kind = _basis(basis)
        title = _name(name) if name is not None else None
        with self._sessions.begin() as session:
            _serialised(session, tenant_id)
            row = _profile(session, tenant_id, profile_id, lock=True)
            row.latest_version += 1
            if title is not None:
                row.name = title
            row.updated_by = person[:200]
            row.updated_at = datetime.now(UTC)
            version = ExportProfileVersion(
                tenant_id=tenant_id, profile_id=row.id, version_no=row.latest_version,
                spec=profile.model_dump(mode="json"), basis=kind, created_by=person[:200],
            )  # fmt: skip
            session.add(version)
            try:
                session.flush()
            except IntegrityError as error:
                raise _taken(title) from error
            _audit(session, tenant_id, actor, "export_profile.versioned", row.id,
                   {"version": row.latest_version, "basis": kind,
                    "columns": len(profile.columns)})  # fmt: skip
            session.refresh(row)
            session.refresh(version)
            return _view(row, version)

    @scoped
    def activate(
        self, tenant_id: uuid.UUID, profile_id: uuid.UUID, *, actor: str, person: str
    ) -> ProfileView:
        """Make this the workspace's active Marg profile (and no other). Raises
        `ProfileNotFound`."""
        with self._sessions.begin() as session:
            _serialised(session, tenant_id)
            row = _profile(session, tenant_id, profile_id, lock=True)
            if not row.active:
                session.execute(
                    update(ExportProfile)
                    .where(
                        ExportProfile.tenant_id == tenant_id,
                        ExportProfile.target == row.target,
                        ExportProfile.active,
                    )
                    .values(active=False)
                )
                row.active = True
                row.updated_by = person[:200]
                row.updated_at = datetime.now(UTC)
                session.flush()
                _audit(session, tenant_id, actor, "export_profile.activated", row.id,
                       {"version": row.latest_version})  # fmt: skip
            session.refresh(row)
            return _view(row, _latest(session, row))

    # Last: a method named `list` would hide the type in the annotations after it.
    @scoped
    def list(self, tenant_id: uuid.UUID) -> list[ProfileView]:
        """The active one first, then by name."""
        with self._sessions() as session:
            rows = session.execute(
                select(ExportProfile, ExportProfileVersion)
                .join(
                    ExportProfileVersion,
                    (ExportProfileVersion.profile_id == ExportProfile.id)
                    & (ExportProfileVersion.version_no == ExportProfile.latest_version),
                )
                .where(ExportProfile.tenant_id == tenant_id)
                .order_by(ExportProfile.active.desc(), ExportProfile.name, ExportProfile.id)
            ).all()
            return [_view(profile, version) for profile, version in rows]
