"""Self-service accounts: what an account's name, email and password must be.

An account is one private workspace with one member in it. Its email is unique across every
account and is how its person signs in, with a password; the workspace's own name is an
internal one (`u-` and twelve hex digits), and the person's name is shown in its place.
"""

import re
import secrets
from dataclasses import dataclass

MIN_PASSWORD = 10
MAX_PASSWORD = 128
MAX_NAME = 120
MAX_EMAIL = 254
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s.]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class MemberCaps:
    """What one free workspace may hold and do; organisations are not held to these."""

    max_documents: int = 30  # live documents at once
    uploads_per_day: int = 15  # new documents a day (UTC), deleted ones included
    questions_per_day: int = 40  # questions a day (UTC), however asked


class AccountTaken(Exception):
    """An account with this email already exists."""


def normalise_email(email: str) -> str:
    """As stored and compared: no surrounding space, lower case."""
    return email.strip().lower()


def check_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > MAX_NAME or _CONTROL.search(name):
        raise ValueError(f"a name is 1 to {MAX_NAME} characters")
    return name


def check_email(email: str) -> str:
    email = normalise_email(email)
    # ASCII only: Python and Postgres would otherwise lower-case some letters differently.
    if len(email) > MAX_EMAIL or not email.isascii() or not _EMAIL.fullmatch(email):
        raise ValueError("that is not an email address")
    return email


def check_password(password: str) -> str:
    """Kept as given (spaces included): only its length and that it is not all spaces."""
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD or not password.strip():
        raise ValueError(f"a password is {MIN_PASSWORD} to {MAX_PASSWORD} characters")
    return password


def workspace_name() -> str:
    """A new workspace's internal name; people see `workspace_label` instead."""
    return f"u-{secrets.token_hex(6)}"


def workspace_label(name: str) -> str:
    return f"{name}'s workspace"
