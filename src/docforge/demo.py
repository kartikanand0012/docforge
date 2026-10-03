"""`python -m docforge.demo seed`: the public demo's data.

An organisation called `demo`, one reviewer anyone may sign in as (`demo@docforge.example`,
with the PIN from `DEMO_PIN`), and the synthetic invoices, purchase orders and certificates,
queued for the worker. Synthetic data only: nothing real is ever loaded into the demo. Safe
to run again; it adds only what is missing.
"""

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine

from docforge.db.models import Reviewer, Tenant
from docforge.db.session import make_session_factory
from docforge.db.tenancy import tenant_scope
from docforge.documents import DocumentService
from docforge.review.service import ReviewService

DEMO_TENANT = "demo"
DEMO_EMAIL = "demo@docforge.example"
_MIN_PIN_DIGITS = 6

Document = tuple[str, str, bytes]  # (document type, file name, PDF)


@dataclass(frozen=True)
class SeedResult:
    documents_added: int
    reviewer_added: bool


def demo_documents(synthetic: Path, coa: Path) -> list[Document]:
    """Orders before invoices, so each invoice finds its order when it is processed."""
    documents: list[Document] = []
    for name in ("purchase_order", "invoice"):
        for pdf in sorted(synthetic.glob(f"*/{name}.pdf")):
            documents.append((name, f"{pdf.parent.name}-{name}.pdf", pdf.read_bytes()))
    for pdf in sorted(coa.glob("*/coa.pdf")):
        documents.append(("coa", f"{pdf.parent.name}.pdf", pdf.read_bytes()))
    return documents


def seed_demo(
    owner: Engine,
    service: DocumentService,
    review: ReviewService,
    documents: Sequence[Document],
    *,
    pin: str,
) -> SeedResult:
    if len(pin) < _MIN_PIN_DIGITS or not pin.isdigit():
        raise ValueError(f"the demo PIN must be at least {_MIN_PIN_DIGITS} digits")
    # As the owner: the application's role may read organisations, not create them.
    with make_session_factory(owner).begin() as session:
        tenant_id = session.scalar(select(Tenant.id).where(Tenant.name == DEMO_TENANT))
        if tenant_id is None:
            tenant = Tenant(name=DEMO_TENANT)
            session.add(tenant)
            session.flush()
            tenant_id = tenant.id
        known = session.scalar(
            select(Reviewer.id).where(Reviewer.tenant_id == tenant_id, Reviewer.email == DEMO_EMAIL)
        )
    if known is None:
        review.add_reviewer(
            tenant_id, name="Demo Reviewer", email=DEMO_EMAIL, pin=pin, role="admin"
        )
    added = 0
    with tenant_scope(tenant_id):
        for doc_type, filename, data in documents:
            result = service.ingest(
                tenant_id=tenant_id, doc_type=doc_type, filename=filename, data=data, actor="demo"
            )
            added += result.created
    return SeedResult(documents_added=added, reviewer_added=known is None)


def main(argv: Sequence[str] | None = None) -> int:
    from sqlalchemy import create_engine

    from docforge.config import get_settings
    from docforge.wiring import build_review, build_service

    parser = argparse.ArgumentParser(prog="python -m docforge.demo", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed", help="add the demo organisation, reviewer and documents")
    seed.add_argument("--synthetic", type=Path, default=Path("demo/synthetic"))
    seed.add_argument("--coa", type=Path, default=Path("demo/coa"))
    args = parser.parse_args(argv)

    pin = os.environ.get("DEMO_PIN", "")
    settings = get_settings()
    owner = create_engine(settings.migration_database_url.get_secret_value())
    try:
        service, _queue = build_service(settings)
        result = seed_demo(
            owner,
            service,
            build_review(settings),
            demo_documents(args.synthetic, args.coa),
            pin=pin,
        )
    except ValueError as error:
        print(f"Not seeded: {error}", file=sys.stderr)
        return 1
    finally:
        owner.dispose()
    print(
        f"Demo ready: {result.documents_added} documents queued; "
        f"reviewer {'added' if result.reviewer_added else 'already there'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
