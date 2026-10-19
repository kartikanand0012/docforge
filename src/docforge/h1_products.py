"""Schedule H1 on a purchase bill: the lines a chemist must enter in the H1 register, from the
official molecule list (`docforge.trust.schedule_h1`) and from the products a reviewer marked
for the workspace (bills mostly print brand names, which name no molecule).

Marking and removing a mark are audited in the change's own transaction. H1 is neither a pass
nor a fail and never holds a bill: it is shown, not checked.
"""

import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from docforge import audit
from docforge.db.models import H1Product
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped
from docforge.extraction.schema import InvoiceExtraction
from docforge.trust.schedule_h1 import h1_molecule, product_key

MAX_PRODUCT_NAME = 300
MAX_KEY = 200
MAX_MARKS = 5000  # per workspace: far more products than a chemist stocks


class ProductNameInvalid(ValueError):
    """A product name with nothing to compare by, or too long."""


class TooManyMarks(RuntimeError):
    """The workspace already has `MAX_MARKS` marked products."""


class MarkNotFound(LookupError):
    """No marked product with that id in this workspace."""


@dataclass(frozen=True)
class MarkedProduct:
    id: uuid.UUID
    product_name: str
    marked_by: str
    created_at: datetime


@dataclass(frozen=True)
class H1Line:
    path: str  # lines[i]
    product_name: str
    molecule: str | None  # the schedule's name, when the product names one
    notification: str | None  # the one that added the molecule
    product_id: uuid.UUID | None  # the workspace's mark, when the product is marked
    joins_on: date | None  # the molecule's day in force, when that is after the bill's date


def schedule_h1_lines(
    session: Session, tenant_id: uuid.UUID, invoice: InvoiceExtraction, *, today: date
) -> tuple[H1Line, ...]:
    """The bill's H1 lines, as named by the schedule or marked by the workspace. A bill with
    no date is taken as dated `today` (India's date)."""
    names = [(index, line.product_name.value) for index, line in enumerate(invoice.lines)]
    keys = {product_key(name) for _, name in names if name}
    marks = _marks(session, tenant_id, keys - {""})
    billed = invoice.invoice_date.value or today
    found = []
    for index, name in names:
        if not name:
            continue
        molecule = h1_molecule(name, on=billed)
        mark = marks.get(product_key(name))
        if molecule is None and mark is None:
            continue
        found.append(
            H1Line(
                path=f"lines[{index}]",
                product_name=name,
                molecule=molecule.name if molecule else None,
                notification=molecule.notification if molecule else None,
                product_id=mark.id if mark else None,
                # Only while the day is still ahead: stock billed before it is sold after it.
                joins_on=(
                    molecule.in_force
                    if molecule and max(billed, today) < molecule.in_force
                    else None
                ),
            )
        )
    return tuple(found)


def _marks(session: Session, tenant_id: uuid.UUID, keys: Iterable[str]) -> dict[str, H1Product]:
    wanted = list(keys)
    if not wanted:
        return {}
    rows = session.scalars(
        select(H1Product).where(H1Product.tenant_id == tenant_id, H1Product.product_key.in_(wanted))
    )
    return {row.product_key: row for row in rows}


class H1ProductService:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    @scoped
    def list(self, tenant_id: uuid.UUID) -> list[MarkedProduct]:
        with self._sessions() as session:
            rows = session.scalars(
                select(H1Product)
                .where(H1Product.tenant_id == tenant_id)
                .order_by(H1Product.product_key, H1Product.id)
            )
            return [_view(row) for row in rows]

    @scoped
    def mark(
        self, tenant_id: uuid.UUID, product_name: str, *, actor: str, name: str
    ) -> tuple[MarkedProduct, bool]:
        """The product marked as H1, and whether this call marked it (False: it already was).
        Raises `ProductNameInvalid` or `TooManyMarks`."""
        shown = " ".join(product_name.split())
        key = product_key(shown)
        unprintable = any(unicodedata.category(c).startswith("C") for c in shown)
        if not key or unprintable or len(shown) > MAX_PRODUCT_NAME or len(key) > MAX_KEY:
            raise ProductNameInvalid(product_name)
        # Twice at most: a mark removed between finding it taken and reading it is made anew.
        for _ in range(2):
            found = self._mark_once(tenant_id, key, shown, actor=actor, name=name)
            if found is not None:
                return found
        raise RuntimeError("the mark changed twice while it was being made")

    def _mark_once(
        self, tenant_id: uuid.UUID, key: str, shown: str, *, actor: str, name: str
    ) -> tuple[MarkedProduct, bool] | None:
        with self._sessions.begin() as session:
            existing = session.scalars(
                select(H1Product).where(
                    H1Product.tenant_id == tenant_id, H1Product.product_key == key
                )
            ).one_or_none()
            if existing is not None:
                return _view(existing), False
            count = session.scalar(
                select(func.count()).select_from(H1Product).where(H1Product.tenant_id == tenant_id)
            )
            if (count or 0) >= MAX_MARKS:
                raise TooManyMarks(tenant_id)
            made = session.scalar(
                insert(H1Product)
                .values(
                    tenant_id=tenant_id, product_key=key, product_name=shown, marked_by=name[:200]
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "product_key"])
                .returning(H1Product)
            )
            if made is None:
                return None  # marked by someone else meanwhile: read it on the next turn
            self._audit(session, made, actor, "h1_product.marked")
            return _view(made), True

    @scoped
    def unmark(self, tenant_id: uuid.UUID, product_id: uuid.UUID, *, actor: str) -> None:
        """Raises `MarkNotFound`."""
        with self._sessions.begin() as session:
            removed = session.scalar(
                delete(H1Product)
                .where(H1Product.tenant_id == tenant_id, H1Product.id == product_id)
                .returning(H1Product)
            )
            if removed is None:
                raise MarkNotFound(product_id)
            self._audit(session, removed, actor, "h1_product.unmarked")

    @staticmethod
    def _audit(session: Session, product: H1Product, actor: str, action: str) -> None:
        audit.append(
            session,
            tenant_id=product.tenant_id,
            actor=actor,
            action=action,
            target_type="h1_product",
            target_id=str(product.id),
            details={"product_name": product.product_name},
        )


def _view(row: H1Product) -> MarkedProduct:
    return MarkedProduct(row.id, row.product_name, row.marked_by, row.created_at)
