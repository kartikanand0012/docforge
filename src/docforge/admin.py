"""`python -m docforge.admin`: organisations and API keys.

    python -m docforge.admin create-tenant acme
    python -m docforge.admin create-key --tenant acme --role integrator --name "ERP import"
    python -m docforge.admin revoke-key --tenant acme --prefix 0123456789ab

A key is printed once, when it is made; only a hash of it is kept.
"""

import argparse
import sys
import uuid
from collections.abc import Sequence

from sqlalchemy import select

from docforge.auth import PERMISSIONS
from docforge.config import Settings, get_settings
from docforge.db.models import Tenant
from docforge.db.session import make_engine, make_session_factory
from docforge.wiring import build_authenticator


def tenant_by_name(settings: Settings, name: str) -> uuid.UUID:
    sessions = make_session_factory(make_engine(settings.database_url.get_secret_value()))
    with sessions() as session:
        tenant_id = session.scalar(select(Tenant.id).where(Tenant.name == name))
    if tenant_id is None:
        raise ValueError(f"there is no organisation called {name!r}")
    return tenant_id


def create_tenant(settings: Settings, name: str) -> uuid.UUID:
    name = name.strip()
    if not name:
        raise ValueError("an organisation needs a name")
    sessions = make_session_factory(make_engine(settings.database_url.get_secret_value()))
    with sessions.begin() as session:
        if session.scalar(select(Tenant.id).where(Tenant.name == name)) is not None:
            raise ValueError(f"an organisation called {name!r} already exists")
        tenant = Tenant(name=name)
        session.add(tenant)
        session.flush()
        return tenant.id


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m docforge.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-tenant", help="add an organisation")
    create.add_argument("name")
    key = commands.add_parser("create-key", help="make an API key (printed once)")
    key.add_argument("--tenant", default="default")
    key.add_argument("--role", choices=sorted(PERMISSIONS), required=True)
    key.add_argument("--name", required=True, help="what the key is for")
    revoke = commands.add_parser("revoke-key", help="stop an API key working")
    revoke.add_argument("--tenant", default="default")
    revoke.add_argument("--prefix", required=True, help="the 12 characters after dfk_")
    args = parser.parse_args(argv)

    settings = get_settings()
    try:
        if args.command == "create-tenant":
            print(f"Created organisation {args.name} ({create_tenant(settings, args.name)})")
        elif args.command == "create-key":
            tenant_id = tenant_by_name(settings, args.tenant)
            token = build_authenticator(settings).create_api_key(
                tenant_id, name=args.name, role=args.role
            )
            print(token)
            print("Keep it now: it is not stored and cannot be shown again.", file=sys.stderr)
        else:
            tenant_id = tenant_by_name(settings, args.tenant)
            build_authenticator(settings).revoke_api_key(tenant_id, args.prefix)
            print(f"Revoked key {args.prefix}")
    except (ValueError, LookupError) as error:
        print(f"Not done: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
