"""`python -m docforge.review add-reviewer --name NAME --email EMAIL`: add a reviewer.

The PIN is asked for twice without echo, or read from the first line of standard input when
that is not a terminal. It is never taken from the command line, where it would be kept in
the shell history and visible to other users in the process list.
"""

import argparse
import getpass
import sys
from collections.abc import Sequence

from docforge.admin import tenant_by_name
from docforge.config import get_settings
from docforge.wiring import build_review


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
    args = parser.parse_args(argv)

    settings = get_settings()
    try:
        tenant_id = tenant_by_name(settings, args.tenant)
        reviewer_id = build_review(settings).add_reviewer(
            tenant_id, name=args.name, email=args.email, pin=_read_pin()
        )
    except ValueError as error:
        print(f"Not added: {error}", file=sys.stderr)
        return 1
    print(f"Added reviewer {args.email} ({reviewer_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
