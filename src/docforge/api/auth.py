"""Authentication for the API: the credential on each request, and sign-in for people."""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from docforge.auth import Authenticator, LoginFailed, Principal, TooManyAttempts

_UNAUTHENTICATED = HTTPException(
    401, "A valid API key or session is needed.", headers={"WWW-Authenticate": "Bearer"}
)


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
    return principal


def require(permission: str) -> Callable[..., object]:
    async def dependency(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if not principal.can(permission):
            raise HTTPException(403, "This credential is not allowed to do that.")
        return principal

    return dependency


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str = Field(max_length=200)
    email: str = Field(max_length=320)
    pin: str = Field(max_length=64)


class SessionOut(BaseModel):
    token: str
    expires_in_seconds: int


def sessions_router(authenticator: Authenticator, session_hours: int = 8) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.post("/sessions", status_code=201, response_model=SessionOut)
    def sign_in(body: LoginIn, request: Request) -> SessionOut:
        """Sign a reviewer in to the review screen with their email and PIN."""
        try:
            token = authenticator.login(body.tenant, body.email, body.pin, client_address(request))
        except TooManyAttempts as error:
            raise HTTPException(
                429,
                "Too many failed attempts from this address. Try again later.",
                headers={"Retry-After": str(error.retry_after)},
            ) from error
        except LoginFailed as error:
            raise HTTPException(401, "The organisation, email or PIN is not right.") from error
        return SessionOut(token=token, expires_in_seconds=session_hours * 3600)

    @router.delete("/sessions/current", status_code=204)
    def sign_out(
        request: Request, principal: Annotated[Principal, Depends(current_principal)]
    ) -> Response:
        authenticator.logout(principal, bearer(request))
        return Response(status_code=204)

    return router
