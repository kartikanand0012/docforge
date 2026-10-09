"""Who is calling: API keys for systems, sessions for people, and what each role may do.

Tokens look like `dfk_<prefix>_<secret>` (API key) or `dfs_<prefix>_<secret>` (session). The
prefix finds the row; only a SHA-256 of the secret is stored, compared in constant time. The
tenant always comes from the credential, never from the request.
"""

import hashlib
import hmac
import re
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from docforge import audit
from docforge.accounts import (
    AccountTaken,
    check_email,
    check_name,
    check_password,
    normalise_email,
    workspace_label,
    workspace_name,
)
from docforge.db.models import ApiKey, AuditEntry, Reviewer, SessionToken, Tenant
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped, tenant_scope
from docforge.review.signing import hash_pin, verify_pin

Role = Literal["integrator", "reviewer", "admin", "reader", "member", "observer"]
Kind = Literal["api_key", "session"]

PERMISSIONS: dict[str, frozenset[str]] = {
    "integrator": frozenset({"documents:read", "documents:write"}),
    "reviewer": frozenset({"documents:read", "review"}),
    "admin": frozenset({"documents:read", "documents:write", "review", "admin"}),
    # For AI agents (the MCP server): reads, and nothing else.
    "reader": frozenset({"documents:read"}),
    # A self-service account's person, in its own workspace: everything but administration.
    "member": frozenset({"documents:read", "documents:write", "review"}),
    # A platform administrator looking into a workspace: reads, and nothing else.
    "observer": frozenset({"documents:read"}),
}
# Roles a person signed in may hold, and the one an account's person holds.
MEMBER = "member"
OBSERVER = "observer"
_VIEWED_AUDIT_EVERY = timedelta(hours=1)
_TOKEN = re.compile(r"^(dfk|dfs)_([0-9a-f]{12})_([A-Za-z0-9_-]{43})$")
_KINDS: dict[str, Kind] = {"dfk": "api_key", "dfs": "session"}
_SESSION_HOURS = 8
_DUMMY_PIN_HASH = "scrypt$16384$8$1$00$00"
_MAX_FAILED_PINS = 5  # as for a PIN re-entered to correct or sign
_LOCK_FOR = timedelta(minutes=15)


@dataclass(frozen=True)
class Principal:
    tenant_id: uuid.UUID
    kind: Kind
    subject_id: uuid.UUID  # the API key's id, or the reviewer's for a session
    role: str
    name: str
    # The owner of the platform: may read every workspace's figures and look into one.
    platform_admin: bool = False
    # Set while a platform administrator looks into another workspace (`tenant_id`): their
    # own workspace. They read there as an observer and change nothing.
    home_tenant_id: uuid.UUID | None = None

    @property
    def actor(self) -> str:
        """How this caller appears in the audit log."""
        return f"{'key' if self.kind == 'api_key' else 'reviewer'}:{self.subject_id}"

    def can(self, permission: str) -> bool:
        return permission in PERMISSIONS.get(self.role, frozenset())


def new_token(kind: Literal["dfk", "dfs"]) -> tuple[str, str, str]:
    """A fresh token, its prefix, and the digest to store."""
    prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    return f"{kind}_{prefix}_{secret}", prefix, hashlib.sha256(secret.encode()).hexdigest()


def parse_token(token: str) -> tuple[str, str] | None:
    """(kind, prefix) of a well-formed token, else None."""
    match = _TOKEN.fullmatch(token)
    return (match[1], match[2]) if match else None


def token_matches(token: str, digest: str) -> bool:
    match = _TOKEN.fullmatch(token)
    if match is None:
        return False
    actual = hashlib.sha256(match[3].encode()).hexdigest()
    return hmac.compare_digest(actual, digest)


class LoginFailed(Exception):
    """Unknown tenant, unknown or deactivated reviewer, or wrong PIN: deliberately one error."""


class NotAWorkspace(LookupError):
    """No workspace has that id."""


@dataclass(frozen=True)
class Caller:
    """Who a credential stands for, as the web app shows it."""

    email: str | None
    organisation: str  # the workspace's name to show
    workspace: str  # personal or organisation
    credential: str | None  # pin or password for a person; None for a key


class TooManyAttempts(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"too many failed attempts; retry after {retry_after} s")
        self.retry_after = retry_after


class FailureLimiter:
    """Failed sign-ins and PINs per client address, in a sliding window, in this process.

    One process's view only: behind several API processes a shared limit (at the proxy, or
    in Redis) is needed as well. It slows guessing across all reviewers from one address,
    which the per-reviewer lockout does not.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, client: str) -> None:
        now = time.monotonic()
        with self._lock:
            recent = self._failures.get(client)
            if recent is None:
                return
            while recent and recent[0] <= now - self._window:
                recent.popleft()
            if len(recent) >= self._limit:
                raise TooManyAttempts(int(recent[0] + self._window - now) + 1)

    def failed(self, client: str) -> None:
        with self._lock:
            self._failures.setdefault(client, deque()).append(time.monotonic())


class Authenticator:
    def __init__(
        self,
        sessions: SessionFactory,
        *,
        failed_logins_per_window: int = 20,
        window_seconds: float = 300,
        session_hours: int = _SESSION_HOURS,
    ) -> None:
        self._sessions = sessions
        self.limiter = FailureLimiter(failed_logins_per_window, window_seconds)
        self._session_hours = session_hours

    # API keys

    @scoped
    def create_api_key(
        self, tenant_id: uuid.UUID, *, name: str, role: str, actor: str = "cli"
    ) -> str:
        """A new key. The token is returned once and cannot be recovered later."""
        if role not in PERMISSIONS:
            raise ValueError(f"unknown role {role!r}")
        if not name.strip():
            raise ValueError("a key needs a name")
        token, prefix, digest = new_token("dfk")
        with self._sessions.begin() as session:
            key = ApiKey(
                tenant_id=tenant_id,
                prefix=prefix,
                digest=digest,
                name=name.strip(),
                role=role,
                created_by=None if actor == "cli" else actor,
            )
            session.add(key)
            session.flush()
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="api_key.created",
                target_type="api_key", target_id=prefix, details={"name": key.name, "role": role},
            )  # fmt: skip
        return token

    @scoped
    def revoke_api_key(
        self, tenant_id: uuid.UUID, token_or_prefix: str, actor: str = "cli"
    ) -> None:
        parsed = parse_token(token_or_prefix)
        prefix = parsed[1] if parsed else token_or_prefix
        with self._sessions.begin() as session:
            key = session.scalar(
                select(ApiKey).where(ApiKey.tenant_id == tenant_id, ApiKey.prefix == prefix)
            )
            if key is None:
                raise LookupError("no such key")
            if key.revoked_at is None:
                key.revoked_at = datetime.now(UTC)
                audit.append(
                    session, tenant_id=tenant_id, actor=actor, action="api_key.revoked",
                    target_type="api_key", target_id=prefix, details={"name": key.name},
                )  # fmt: skip

    @scoped
    def api_keys(self, tenant_id: uuid.UUID) -> list[ApiKey]:
        """The organisation's keys, newest first. Their secrets are not kept, so not shown."""
        with self._sessions() as session:
            return list(
                session.scalars(
                    select(ApiKey)
                    .where(ApiKey.tenant_id == tenant_id)
                    .order_by(ApiKey.created_at.desc())
                )
            )

    # Sessions

    def login(self, tenant_name: str | None, email: str, pin: str, client: str) -> str:
        """A session token for a reviewer, after their PIN or password.

        Without `tenant_name` the email is an account's, and its workspace is the account's.
        A wrong PIN or password counts against both the client address and the reviewer:
        five in a row lock the reviewer for 15 minutes, from wherever the attempts come.
        """
        self.limiter.check(client)
        with self._sessions() as session:
            if tenant_name is None:
                # Found without reading the accounts table, which this role cannot read.
                tenant_id = session.scalar(
                    select(func.docforge_account_tenant(normalise_email(email)))
                )
            else:
                tenant_id = session.scalar(select(Tenant.id).where(Tenant.name == tenant_name))
        if tenant_id is None:
            verify_pin(pin, _DUMMY_PIN_HASH)
            self.limiter.failed(client)
            raise LoginFailed
        with tenant_scope(tenant_id), self._sessions.begin() as session:
            reviewer = session.scalar(
                select(Reviewer)
                .where(
                    Reviewer.tenant_id == tenant_id,
                    Reviewer.email == email.strip().lower(),
                    Reviewer.deactivated_at.is_(None),
                )
                .with_for_update(key_share=True)
            )
            now = datetime.now(UTC)
            if reviewer is None:
                verify_pin(pin, _DUMMY_PIN_HASH)
                ok = False
            elif reviewer.locked_until is not None and reviewer.locked_until > now:
                ok = False
            elif verify_pin(pin, reviewer.pin_hash):
                reviewer.failed_attempts, reviewer.locked_until = 0, None
                ok = True
            else:
                # Committed with this transaction, which then ends normally: the count holds.
                audit.append(
                    session,
                    tenant_id=tenant_id,
                    actor="api",
                    action="reviewer.pin_failed",
                    target_type="reviewer",
                    target_id=str(reviewer.id),
                    details={"attempt": reviewer.failed_attempts + 1, "at": "sign-in"},
                )
                reviewer.failed_attempts += 1
                if reviewer.failed_attempts >= _MAX_FAILED_PINS and not reviewer.shared:
                    reviewer.failed_attempts, reviewer.locked_until = 0, now + _LOCK_FOR
                ok = False
            token = None
            if ok and reviewer is not None:
                token, prefix, digest = new_token("dfs")
                session.add(
                    SessionToken(
                        tenant_id=reviewer.tenant_id,
                        reviewer_id=reviewer.id,
                        prefix=prefix,
                        digest=digest,
                        expires_at=now + timedelta(hours=self._session_hours),
                    )
                )
        if token is None:
            self.limiter.failed(client)
            raise LoginFailed
        return token

    def logout(self, principal: Principal, token: str) -> None:
        parsed = parse_token(token)
        if parsed is None or principal.kind != "session":
            return
        with tenant_scope(principal.tenant_id), self._sessions.begin() as session:
            row = session.scalar(select(SessionToken).where(SessionToken.prefix == parsed[1]))
            if row is not None:
                row.revoked_at = row.revoked_at or datetime.now(UTC)

    def describe(self, principal: Principal) -> Caller:
        """The caller's email and credential (a person's; None for a key), and the name and
        kind of the workspace the request is served in."""
        with self._sessions() as session:
            tenant = session.scalar(select(Tenant).where(Tenant.id == principal.tenant_id))
        person = None
        if principal.kind == "session":
            # An observer is a person of another workspace: theirs is where they are found.
            home = principal.home_tenant_id or principal.tenant_id
            with tenant_scope(home), self._sessions() as session:
                person = session.execute(
                    select(Reviewer.email, Reviewer.credential).where(
                        Reviewer.id == principal.subject_id
                    )
                ).first()
        return Caller(
            email=person.email if person else None,
            organisation=(tenant.display_name or tenant.name) if tenant else "",
            workspace=tenant.kind if tenant else "organisation",
            credential=person.credential if person else None,
        )

    # Accounts

    def create_account(self, name: str, email: str, password: str) -> str:
        """A new account - a private workspace with its one member - signed in: the session
        token. Raises `ValueError` for an unacceptable name, email or password, and
        `AccountTaken` when the email already has an account."""
        name, email = check_name(name), check_email(email)
        secret = hash_pin(check_password(password))
        now = datetime.now(UTC)
        try:
            with self._sessions.begin() as session:
                # As the owner, in one statement: this role cannot make workspaces itself.
                tenant_id = session.scalar(
                    select(
                        func.docforge_create_account(
                            workspace_name(), workspace_label(name), name, email, secret
                        )
                    )
                )
                assert tenant_id is not None  # noqa: S101 - the function returns it or raises
                # The rest belongs to the new workspace: its scope, for this transaction only.
                session.execute(
                    text("SELECT set_config('docforge.tenant_id', :tenant, true)"),
                    {"tenant": str(tenant_id)},
                )
                member = session.scalar(
                    select(Reviewer).where(Reviewer.tenant_id == tenant_id, Reviewer.email == email)
                )
                assert member is not None  # noqa: S101 - made just above
                audit.append(
                    session, tenant_id=tenant_id, actor=f"reviewer:{member.id}",
                    action="account.created", target_type="reviewer", target_id=str(member.id),
                    details={},
                )  # fmt: skip
                token, prefix, digest = new_token("dfs")
                session.add(
                    SessionToken(
                        tenant_id=tenant_id,
                        reviewer_id=member.id,
                        prefix=prefix,
                        digest=digest,
                        expires_at=now + timedelta(hours=self._session_hours),
                    )
                )
        except IntegrityError as error:
            if "uq_accounts_email" in str(error.orig):
                raise AccountTaken(email) from None
            raise
        return token

    def signups_since(self, since: datetime) -> int:
        with self._sessions() as session:
            return int(session.scalar(select(func.docforge_signups_since(since))) or 0)

    # Platform administrators

    def observe(self, principal: Principal, workspace: uuid.UUID) -> Principal:
        """A platform administrator's session looking into `workspace`, read-only.

        Raises `PermissionError` for anyone else, and `NotAWorkspace`. The look is audited in
        that workspace, at most once an hour per administrator.
        """
        if principal.kind != "session" or not principal.platform_admin:
            raise PermissionError("not a platform administrator")
        with self._sessions() as session:
            if session.scalar(select(Tenant.id).where(Tenant.id == workspace)) is None:
                raise NotAWorkspace(workspace)
        actor = principal.actor
        with tenant_scope(workspace), self._sessions.begin() as session:
            # One look at a time per administrator and workspace, so two at once log once.
            session.execute(
                select(func.pg_advisory_xact_lock(func.hashtext(f"viewed:{workspace}:{actor}")))
            )
            recent = session.scalar(
                select(AuditEntry.id)
                .where(
                    AuditEntry.tenant_id == workspace,
                    AuditEntry.action == "platform.workspace_viewed",
                    AuditEntry.actor == actor,
                    AuditEntry.occurred_at > datetime.now(UTC) - _VIEWED_AUDIT_EVERY,
                )
                .limit(1)
            )
            if recent is None:
                audit.append(
                    session, tenant_id=workspace, actor=actor,
                    action="platform.workspace_viewed", target_type="tenant",
                    target_id=str(workspace), details={},
                )  # fmt: skip
        return Principal(
            tenant_id=workspace,
            kind="session",
            subject_id=principal.subject_id,
            role=OBSERVER,
            name=principal.name,
            platform_admin=True,
            home_tenant_id=principal.tenant_id,
        )

    # Checking

    def authenticate(self, token: str) -> Principal | None:
        """The caller a token stands for; None if malformed, unknown, revoked or expired."""
        parsed = parse_token(token)
        if parsed is None:
            return None
        kind, prefix = parsed
        now = datetime.now(UTC)
        lookup = (
            func.docforge_api_key_tenant
            if _KINDS[kind] == "api_key"
            else func.docforge_session_tenant
        )
        with self._sessions() as session:
            tenant_id = session.scalar(select(lookup(prefix)))
        if tenant_id is None:
            return None
        with tenant_scope(tenant_id), self._sessions.begin() as session:
            if _KINDS[kind] == "api_key":
                key = session.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
                if (
                    key is None
                    or key.revoked_at is not None
                    or not token_matches(token, key.digest)
                ):
                    return None
                if key.last_used_at is None or now - key.last_used_at > timedelta(minutes=5):
                    key.last_used_at = now  # coarse, so a busy key is not a write per request
                return Principal(key.tenant_id, "api_key", key.id, key.role, key.name)
            row = session.execute(
                select(SessionToken, Reviewer)
                .join(Reviewer, Reviewer.id == SessionToken.reviewer_id)
                .where(SessionToken.prefix == prefix)
            ).first()
            if row is None:
                return None
            token_row, reviewer = row
            if (
                token_row.revoked_at is not None
                or token_row.expires_at <= now
                or reviewer.deactivated_at is not None
                or not token_matches(token, token_row.digest)
            ):
                return None
            return Principal(
                reviewer.tenant_id,
                "session",
                reviewer.id,
                reviewer.role,
                reviewer.name,
                platform_admin=reviewer.platform_admin,
            )
