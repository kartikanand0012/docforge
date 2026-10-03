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

from sqlalchemy import func, select

from docforge import audit
from docforge.db.models import ApiKey, Reviewer, SessionToken, Tenant
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped, tenant_scope
from docforge.review.signing import verify_pin

Role = Literal["integrator", "reviewer", "admin"]
Kind = Literal["api_key", "session"]

PERMISSIONS: dict[str, frozenset[str]] = {
    "integrator": frozenset({"documents:read", "documents:write"}),
    "reviewer": frozenset({"documents:read", "review"}),
    "admin": frozenset({"documents:read", "documents:write", "review", "admin"}),
}
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
    def create_api_key(self, tenant_id: uuid.UUID, *, name: str, role: str) -> str:
        """A new key. The token is returned once and cannot be recovered later."""
        if role not in PERMISSIONS:
            raise ValueError(f"unknown role {role!r}")
        token, prefix, digest = new_token("dfk")
        with self._sessions.begin() as session:
            session.add(
                ApiKey(
                    tenant_id=tenant_id, prefix=prefix, digest=digest, name=name.strip(), role=role
                )
            )
        return token

    @scoped
    def revoke_api_key(self, tenant_id: uuid.UUID, token_or_prefix: str) -> None:
        parsed = parse_token(token_or_prefix)
        prefix = parsed[1] if parsed else token_or_prefix
        with self._sessions.begin() as session:
            key = session.scalar(
                select(ApiKey).where(ApiKey.tenant_id == tenant_id, ApiKey.prefix == prefix)
            )
            if key is None:
                raise LookupError("no such key")
            key.revoked_at = key.revoked_at or datetime.now(UTC)

    # Sessions

    def login(self, tenant_name: str, email: str, pin: str, client: str) -> str:
        """A session token for a reviewer, after their PIN.

        A wrong PIN counts against both the client address and the reviewer: five in a row
        lock the reviewer for 15 minutes, from wherever the attempts come.
        """
        self.limiter.check(client)
        with self._sessions() as session:
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
                if reviewer.failed_attempts >= _MAX_FAILED_PINS:
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
                reviewer.tenant_id, "session", reviewer.id, reviewer.role, reviewer.name
            )
