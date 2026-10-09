"""Authentication for the API: the credential on each request, and sign-in for people."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Self

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from docforge.accounts import MAX_NAME, MAX_PASSWORD, AccountTaken, has_control
from docforge.auth import Authenticator, LoginFailed, NotAWorkspace, Principal, TooManyAttempts
from docforge.limits import Limits

_UNAUTHENTICATED = HTTPException(
    401, "A valid API key or session is needed.", headers={"WWW-Authenticate": "Bearer"}
)
# A platform administrator's session sends this to read another workspace (GET only).
WORKSPACE_HEADER = "X-DocForge-Workspace"
READ_METHODS = frozenset({"GET", "HEAD"})
NOT_ALLOWED = "This credential is not allowed to do that."


def client_address(request: Request) -> str:
    """The address failures are counted against. Behind a proxy, the proxy must set it."""
    return request.client.host if request.client else "unknown"


def bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


async def current_principal(request: Request) -> Principal:
    """The authenticated caller. Every /v1 route depends on this, directly or through `require`."""
    authenticator: Authenticator | None = getattr(request.app.state, "authenticator", None)
    token = bearer(request)
    if authenticator is None or not token:
        raise _UNAUTHENTICATED
    principal = await run_in_threadpool(authenticator.authenticate, token)
    if principal is None:
        raise _UNAUTHENTICATED
    workspace = request.headers.get(WORKSPACE_HEADER)
    if workspace is None:
        return principal
    # Refused, never ignored: a caller who sends it must not be served its own workspace
    # thinking it is another's, or the other way round.
    if request.method not in READ_METHODS or not principal.platform_admin:
        raise HTTPException(403, NOT_ALLOWED)
    try:
        viewed = uuid.UUID(workspace)
        return await run_in_threadpool(authenticator.observe, principal, viewed)
    except PermissionError:
        raise HTTPException(403, NOT_ALLOWED) from None
    except (ValueError, NotAWorkspace):
        raise HTTPException(404, "No such workspace.") from None


def require(permission: str) -> Callable[..., object]:
    async def dependency(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if not principal.can(permission):
            raise HTTPException(403, NOT_ALLOWED)
        return principal

    return dependency


async def platform_admin(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    """A platform administrator's own session (not a key, not anyone else)."""
    if principal.kind != "session" or not principal.platform_admin:
        raise HTTPException(403, NOT_ALLOWED)
    return principal


class LoginIn(BaseModel):
    """An organisation's reviewer gives its name, the email and the PIN; an account's person
    gives only the email and the password. Either secret may travel in either field."""

    model_config = ConfigDict(extra="forbid")

    tenant: str | None = Field(default=None, max_length=200)
    email: str = Field(max_length=320)
    pin: str | None = Field(default=None, max_length=MAX_PASSWORD)
    password: str | None = Field(default=None, max_length=MAX_PASSWORD)

    @field_validator("tenant", "email")
    @classmethod
    def _no_control(cls, value: str | None) -> str | None:
        # Refused here: the database cannot store a NUL byte, and would fail on one.
        if value is not None and has_control(value):
            raise ValueError("no control characters")
        return value

    @model_validator(mode="after")
    def _one_secret(self) -> Self:
        if (self.pin is None) == (self.password is None):
            raise ValueError("give a pin or a password")
        return self

    @property
    def secret(self) -> str:
        return self.pin if self.pin is not None else str(self.password)


class AccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME)
    email: str = Field(max_length=320)
    password: str = Field(max_length=MAX_PASSWORD)


class SessionOut(BaseModel):
    token: str
    expires_in_seconds: int


class CallerOut(BaseModel):
    kind: str  # "session" (a person) or "api_key"
    name: str
    email: str | None
    role: str  # admin, reviewer, member, integrator, reader; observer while looking in
    organisation: str  # the workspace's name to show
    credential: str | None  # "pin" or "password" for a person; null for a key
    platform_admin: bool
    workspace: str  # "personal" (an account's) or "organisation"


_TOO_MANY = "Too many failed attempts from this address. Try again later."


def sessions_router(authenticator: Authenticator, session_hours: int = 8) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.post("/sessions", status_code=201, response_model=SessionOut)
    def sign_in(body: LoginIn, request: Request) -> SessionOut:
        """Sign in: an organisation's reviewer with its name, their email and PIN; an
        account's person with their email and password alone."""
        try:
            token = authenticator.login(
                body.tenant, body.email, body.secret, client_address(request)
            )
        except TooManyAttempts as error:
            raise HTTPException(
                429, _TOO_MANY, headers={"Retry-After": str(error.retry_after)}
            ) from error
        except LoginFailed as error:
            # One answer whatever was wrong: an unknown email is not told from a wrong secret.
            detail = (
                "The email or password is not right."
                if body.tenant is None
                else "The organisation, email or PIN is not right."
            )
            raise HTTPException(401, detail) from error
        return SessionOut(token=token, expires_in_seconds=session_hours * 3600)

    @router.get("/sessions/current", response_model=CallerOut)
    def who_am_i(
        response: Response, principal: Annotated[Principal, Depends(current_principal)]
    ) -> CallerOut:
        """Who the credential stands for: for the web app's name and role-aware navigation."""
        caller = authenticator.describe(principal)
        response.headers["Cache-Control"] = "no-store"
        return CallerOut(
            kind=principal.kind, name=principal.name, email=caller.email, role=principal.role,
            organisation=caller.organisation, credential=caller.credential,
            platform_admin=principal.platform_admin, workspace=caller.workspace,
        )  # fmt: skip

    @router.delete("/sessions/current", status_code=204)
    def sign_out(
        request: Request, principal: Annotated[Principal, Depends(current_principal)]
    ) -> Response:
        authenticator.logout(principal, bearer(request))
        return Response(status_code=204)

    return router


def accounts_router(
    authenticator: Authenticator,
    limits: Limits,
    *,
    per_address_per_hour: int = 5,
    per_day: int = 200,
    session_hours: int = 8,
) -> APIRouter:
    """Sign-up: present only when the deployment allows it (otherwise the route is 404)."""
    router = APIRouter(prefix="/v1")

    @router.post("/accounts", status_code=201, response_model=SessionOut)
    def sign_up(body: AccountIn, request: Request) -> SessionOut:
        """Make an account - a private workspace with you in it - and sign in to it."""
        # Every attempt from an address counts, made or refused: no one probes for which
        # emails have accounts, or makes them, faster than this.
        if not limits.allow_hourly(f"signup:{client_address(request)}", per_address_per_hour):
            raise HTTPException(
                429,
                "Too many accounts were made from this address. Try again later.",
                headers={"Retry-After": "3600"},
            )
        if authenticator.signups_since(datetime.now(UTC) - timedelta(days=1)) >= per_day:
            raise HTTPException(
                429,
                "New accounts are paused for today. Try again tomorrow.",
                headers={"Retry-After": "3600"},
            )
        try:
            token = authenticator.create_account(body.name, body.email, body.password)
        except AccountTaken as error:
            raise HTTPException(409, "An account with this email already exists.") from error
        except ValueError as error:
            raise HTTPException(422, f"{str(error)[0].upper()}{str(error)[1:]}.") from error
        return SessionOut(token=token, expires_in_seconds=session_hours * 3600)

    return router
