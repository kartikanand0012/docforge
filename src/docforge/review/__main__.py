"""`python -m docforge.review add-reviewer --name NAME --email EMAIL`: add a reviewer.

`python -m docforge.review make-platform-admin --tenant NAME --email EMAIL` makes an existing
reviewer the platform administrator (the owner), who may see every workspace's figures and
look into one, read-only. It runs as the database owner: the application cannot do it.

The PIN is asked for twice without echo, or read from the first line of standard input when
that is not a terminal. It is never taken from the command line, where it would be kept in
the shell history and visible to other users in the process list.
"""

import argparse
import getpass
import sys
from collections.abc import Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from docforge.admin import tenant_by_name
from docforge.config import get_settings
from docforge.wiring import build_review


def make_platform_admin(owner_url: URL | str, tenant: str, email: str) -> None:
    """Mark the reviewer as the platform administrator; doing it again changes nothing."""
    engine = create_engine(owner_url)
    try:
        with engine.begin() as conn:
            tenant_id = conn.execute(
                text("SELECT id FROM tenants WHERE name = :name"), {"name": tenant.strip()}
            ).scalar_one_or_none()
            if tenant_id is None:
                raise ValueError(f"there is no organisation called {tenant!r}")
            marked = conn.execute(
                text(
                    "UPDATE reviewers SET platform_admin = true WHERE tenant_id = :tenant "
                    "AND email = :email AND deactivated_at IS NULL RETURNING id"
                ),
                {"tenant": tenant_id, "email": email.strip().lower()},
            ).scalar_one_or_none()
            if marked is None:
                raise ValueError(f"there is no active reviewer {email} in {tenant!r}")
    finally:
        engine.dispose()


def _read_pin() -> str:
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    pin = getpass.getpass("PIN (6 digits or more): ")
    if getpass.getpass("PIN again: ") != pin:
        raise ValueError("the two PINs differ")
    return pin


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m docforge.review")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add-reviewer", help="add a reviewer who can correct and sign")
    add.add_argument("--name", required=True)
    add.add_argument("--email", required=True)
    add.add_argument("--tenant", default="default", help="the organisation's name")
    add.add_argument("--admin", action="store_true", help="may also upload and manage keys")
    owner = commands.add_parser(
        "make-platform-admin", help="let a reviewer see every workspace, read-only"
    )
    owner.add_argument("--tenant", required=True, help="the reviewer's organisation's name")
    owner.add_argument("--email", required=True)
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.command == "make-platform-admin":
        try:
            make_platform_admin(
                settings.migration_database_url.get_secret_value(), args.tenant, args.email
            )
        except ValueError as error:
            print(f"Not done: {error}", file=sys.stderr)
            return 1
        print(f"{args.email} is the platform administrator")
        return 0
    try:
        tenant_id = tenant_by_name(settings, args.tenant)
        reviewer_id = build_review(settings).add_reviewer(
            tenant_id,
            name=args.name,
            email=args.email,
            pin=_read_pin(),
            role="admin" if args.admin else "reviewer",
        )
    except ValueError as error:
        print(f"Not added: {error}", file=sys.stderr)
        return 1
    print(f"Added reviewer {args.email} ({reviewer_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
