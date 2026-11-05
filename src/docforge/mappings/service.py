"""The workspace's mapping master: how its own books - Tally, Marg - name each supplier and
item, so an export books a bill under the ledgers and stock items the books already have
instead of making "ABC Pharma (New)" beside "ABC Pharma" (V2 P1).

- A supplier is mapped by its seller GSTIN; every printed variation of its name resolves to
  the one mapping. An item is mapped by its printed name and pack (`keys.item_key`).
- Nothing is guessed. A bill whose supplier or item has no mapping (or none for the books
  being exported to) is a task; a suggestion is only offered, and a mapping is made only by a
  person's own call - `create_*` with `source="suggestion"` records that they confirmed one.
- Names imported from the books' masters seed suggestions and are never applied themselves.
- Every change is audited in its own transaction with who made it and what kind of change it
  was, never the names: they come from the bills.
- A mapping is edited in place or removed; an export run keeps the file it made, so a change
  reaches later exports only.
"""

import base64
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Select, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from docforge import audit
from docforge.db.models import ItemMapping, MappingMaster, SupplierMapping
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped
from docforge.extraction.schema import InvoiceExtraction
from docforge.gstin import is_valid_gstin
from docforge.mappings.keys import MAX_ITEM_KEY, gstin_key, item_key
from docforge.mappings.masters_csv import MasterRow, RejectedRow, parse_masters, printable
from docforge.mappings.suggest import Candidate, Suggester, Suggestion

Target = Literal["tally", "marg"]
Source = Literal["typed", "suggestion"]

MAX_NAME = 200  # a ledger, party or stock item name
MAX_PRINTED = 300
MAX_PACK = 100
MAX_TAX_TREATMENT = 100
MAX_SUPPLIERS = 5_000  # per workspace: the design is 2,000
MAX_ITEMS = 50_000  # per workspace: the design is 20,000
MAX_MASTERS = 50_000
MAX_PAGE = 200
_BATCH = 1_000
_SUGGESTERS = 3  # kept built per process: at the design size one holds ~40 MB
_HSN = re.compile(r"\d{4}(\d{2}){0,2}")
_SUPPLIER_FIELDS = (
    "printed_name", "tally_ledger", "marg_party", "purchase_ledger", "tax_treatment",
)  # fmt: skip
_ITEM_FIELDS = ("printed_hsn", "tally_item", "marg_item", "preferred_hsn")


class MappingInvalid(ValueError):
    """A mapping that could not be booked as given; the message says why, in plain words."""


class MappingNotFound(LookupError):
    """No such mapping in this workspace."""


class TooManyMappings(RuntimeError):
    """The workspace holds as many mappings of the kind as it may."""


@dataclass(frozen=True)
class SupplierMappingView:
    id: uuid.UUID
    seller_gstin: str
    printed_name: str
    tally_ledger: str | None
    marg_party: str | None
    purchase_ledger: str | None
    tax_treatment: str | None
    source: str
    created_by: str
    created_at: datetime
    updated_by: str | None
    updated_at: datetime | None

    def name_for(self, target: Target | None) -> str | None:
        """The name it is booked under in `target`'s books; any, with no target."""
        if target == "tally":
            return self.tally_ledger
        if target == "marg":
            return self.marg_party
        return self.tally_ledger or self.marg_party


@dataclass(frozen=True)
class ItemMappingView:
    id: uuid.UUID
    item_key: str
    product_name: str
    pack: str | None
    printed_hsn: str | None
    tally_item: str | None
    marg_item: str | None
    preferred_hsn: str | None
    source: str
    created_by: str
    created_at: datetime
    updated_by: str | None
    updated_at: datetime | None

    def name_for(self, target: Target | None) -> str | None:
        if target == "tally":
            return self.tally_item
        if target == "marg":
            return self.marg_item
        return self.tally_item or self.marg_item


def _booked(mapping: SupplierMappingView | ItemMappingView) -> str:
    if isinstance(mapping, SupplierMappingView):
        tally, marg, what = mapping.tally_ledger, mapping.marg_party, ("ledger", "party")
    else:
        tally, marg, what = mapping.tally_item, mapping.marg_item, ("item", "item")
    names = [
        f"Tally {what[0]} '{tally}'" if tally else "",
        f"Marg {what[1]} '{marg}'" if marg else "",
    ]
    return " and ".join(n for n in names if n)


class MappingExists(Exception):
    """The supplier's GSTIN, or the item's name and pack, is mapped already."""

    def __init__(self, existing: SupplierMappingView | ItemMappingView) -> None:
        self.existing = existing
        if isinstance(existing, SupplierMappingView):
            what = existing.seller_gstin
        else:
            what = existing.product_name + (f" ({existing.pack})" if existing.pack else "")
        super().__init__(
            f"{what} is already mapped to {_booked(existing)}, by {existing.created_by}. "
            "Change that mapping instead."
        )


@dataclass(frozen=True)
class Page[T]:
    items: tuple[T, ...]
    next_after: str | None  # pass as `after` for the next page; None at the end
    total: int  # matching the search, on every page


@dataclass(frozen=True)
class ImportReport:
    rows: int
    imported: int
    already_known: int  # the workspace (or the file, higher up) had the name already
    rejected: tuple[RejectedRow, ...]


# What an export needs mapped


@dataclass(frozen=True)
class Bill:
    """An invoice to be booked: its document and its values (as signed, or as corrected)."""

    document_id: uuid.UUID
    filename: str
    invoice: InvoiceExtraction


@dataclass(frozen=True)
class SupplierTask:
    seller_gstin: str
    printed_names: tuple[str, ...]  # every name the bills print for it, as printed
    bills: int
    mapping_id: uuid.UUID | None = None  # mapped, but with no name for these books
    suggestions: tuple[Suggestion, ...] = ()


@dataclass(frozen=True)
class ItemTask:
    item_key: str
    product_name: str  # as the first bill printed it
    pack: str | None
    printed_hsn: str | None
    bills: int
    mapping_id: uuid.UUID | None = None
    suggestions: tuple[Suggestion, ...] = ()


@dataclass(frozen=True)
class BillProblem:
    """Something on a bill to correct before it can be booked at all: never guessed past."""

    document_id: uuid.UUID
    filename: str
    problem: str
    line: int | None = None  # the bill's line, from 1, when the problem is one line's


def _plain(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    return value


@dataclass(frozen=True)
class Tasks:
    suppliers: tuple[SupplierTask, ...] = ()
    items: tuple[ItemTask, ...] = ()
    problems: tuple[BillProblem, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.suppliers or self.items or self.problems)

    def to_json(self) -> dict[str, Any]:
        """As an export run stores it: without suggestions, which are worked out when asked."""

        def row(task: Any) -> dict[str, Any]:
            return {k: _plain(v) for k, v in vars(task).items() if k != "suggestions"}

        return {
            "suppliers": [row(t) for t in self.suppliers],
            "items": [row(t) for t in self.items],
            "problems": [row(p) for p in self.problems],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Tasks":
        def ident(value: Any) -> uuid.UUID | None:
            return uuid.UUID(value) if value else None

        return cls(
            suppliers=tuple(
                SupplierTask(
                    seller_gstin=t["seller_gstin"],
                    printed_names=tuple(t["printed_names"]),
                    bills=t["bills"],
                    mapping_id=ident(t.get("mapping_id")),
                )
                for t in data.get("suppliers", ())
            ),
            items=tuple(
                ItemTask(
                    item_key=t["item_key"],
                    product_name=t["product_name"],
                    pack=t.get("pack"),
                    printed_hsn=t.get("printed_hsn"),
                    bills=t["bills"],
                    mapping_id=ident(t.get("mapping_id")),
                )
                for t in data.get("items", ())
            ),
            problems=tuple(
                BillProblem(uuid.UUID(p["document_id"]), p["filename"], p["problem"], p.get("line"))
                for p in data.get("problems", ())
            ),
        )


@dataclass(frozen=True)
class Resolution:
    """Each bill's supplier and items as the books name them, or what is still to map."""

    suppliers: dict[str, SupplierMappingView]  # by seller GSTIN
    items: dict[str, ItemMappingView]  # by item key
    tasks: Tasks


@dataclass
class _Seen:
    names: dict[str, None] = field(default_factory=dict)
    bills: set[uuid.UUID] = field(default_factory=set)
    first: tuple[str, str | None, str | None] | None = None  # name, pack, hsn as printed


def _problem(bill: Bill, problem: str) -> BillProblem:
    return BillProblem(bill.document_id, bill.filename, problem)


def resolve(
    session: Session, tenant_id: uuid.UUID, bills: Sequence[Bill], target: Target | None
) -> Resolution:
    """What `bills` need mapped for `target`'s books (any books, with no target)."""
    suppliers: dict[str, _Seen] = {}
    items: dict[str, _Seen] = {}
    problems: list[BillProblem] = []
    for bill in bills:
        gstin = gstin_key(bill.invoice.seller.gstin.value)
        if gstin is None:
            problems.append(
                _problem(
                    bill,
                    "This bill has no seller GSTIN: correct it on the bill, then export again.",
                )
            )
        elif not is_valid_gstin(gstin):
            problems.append(
                _problem(
                    bill,
                    f"This bill's seller GSTIN {gstin} is not valid: correct it on the bill, "
                    "then export again.",
                )
            )
        else:
            seen = suppliers.setdefault(gstin, _Seen())
            seen.names.setdefault(bill.invoice.seller.name.value or gstin, None)
            seen.bills.add(bill.document_id)
        for index, line in enumerate(bill.invoice.lines, start=1):
            name, pack = line.product_name.value, line.pack.value
            key = item_key(name, pack)
            if key is None or name is None:
                problems.append(
                    _problem(
                        bill,
                        f"Line {index} has no product name: correct it on the bill, then "
                        "export again.",
                    )
                )
                continue
            seen = items.setdefault(key, _Seen())
            seen.first = seen.first or (name, pack, line.hsn.value)
            seen.bills.add(bill.document_id)
    found_suppliers = _supplier_rows(session, tenant_id, suppliers)
    found_items = _item_rows(session, tenant_id, items)
    supplier_tasks = []
    for gstin, seen in suppliers.items():
        mapped = found_suppliers.get(gstin)
        if mapped is None or mapped.name_for(target) is None:
            supplier_tasks.append(
                SupplierTask(
                    gstin, tuple(seen.names), len(seen.bills), mapped.id if mapped else None
                )
            )
    item_tasks = []
    for key, seen in items.items():
        mapped_item = found_items.get(key)
        if mapped_item is None or mapped_item.name_for(target) is None:
            name, pack, hsn = seen.first or ("", None, None)
            item_tasks.append(
                ItemTask(
                    key, name, pack, hsn, len(seen.bills), mapped_item.id if mapped_item else None
                )
            )
    tasks = Tasks(
        suppliers=tuple(
            sorted(supplier_tasks, key=lambda t: (t.printed_names[0].casefold(), t.seller_gstin))
        ),
        items=tuple(sorted(item_tasks, key=lambda t: (t.product_name.casefold(), t.item_key))),
        problems=tuple(problems),
    )
    return Resolution(found_suppliers, found_items, tasks)


def _supplier_rows(
    session: Session, tenant_id: uuid.UUID, gstins: Iterable[str]
) -> dict[str, SupplierMappingView]:
    wanted, found = list(gstins), {}
    for offset in range(0, len(wanted), _BATCH):
        for row in session.scalars(
            select(SupplierMapping).where(
                SupplierMapping.tenant_id == tenant_id,
                SupplierMapping.seller_gstin.in_(wanted[offset : offset + _BATCH]),
            )
        ):
            found[row.seller_gstin] = _supplier_view(row)
    return found


def _item_rows(
    session: Session, tenant_id: uuid.UUID, keys: Iterable[str]
) -> dict[str, ItemMappingView]:
    wanted, found = list(keys), {}
    for offset in range(0, len(wanted), _BATCH):
        for row in session.scalars(
            select(ItemMapping).where(
                ItemMapping.tenant_id == tenant_id,
                ItemMapping.item_key.in_(wanted[offset : offset + _BATCH]),
            )
        ):
            found[row.item_key] = _item_view(row)
    return found


# Checking what a person typed


def _name(value: str | None, what: str, limit: int = MAX_NAME) -> str | None:
    """A name as it will be booked: its ends trimmed, the rest exactly as typed, since the
    books match it letter for letter. None when empty."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if not printable(text):
        raise MappingInvalid(f"The {what} has characters that cannot be shown.")
    if len(text) > limit:
        raise MappingInvalid(f"A {what} is at most {limit} characters.")
    return text


def _printed(value: str | None, what: str, limit: int) -> str | None:
    text = " ".join((value or "").split())
    if not text:
        return None
    if not printable(text):
        raise MappingInvalid(f"The {what} has characters that cannot be shown.")
    if len(text) > limit:
        raise MappingInvalid(f"The {what} is longer than {limit} characters.")
    return text


def _supplier_values(values: Mapping[str, str | None]) -> dict[str, str | None]:
    printed = _printed(values.get("printed_name"), "printed name", MAX_PRINTED)
    if printed is None:
        raise MappingInvalid("Give the supplier's printed name, as the bill shows it.")
    out = {
        "printed_name": printed,
        "tally_ledger": _name(values.get("tally_ledger"), "ledger or party name"),
        "marg_party": _name(values.get("marg_party"), "ledger or party name"),
        "purchase_ledger": _name(values.get("purchase_ledger"), "ledger or party name"),
        "tax_treatment": _name(values.get("tax_treatment"), "tax treatment", MAX_TAX_TREATMENT),
    }
    if out["tally_ledger"] is None and out["marg_party"] is None:
        raise MappingInvalid(
            "Give the Tally ledger or the Marg party this supplier is booked under."
        )
    return out


def _item_values(values: Mapping[str, str | None]) -> dict[str, str | None]:
    hsn = (values.get("preferred_hsn") or "").strip() or None
    if hsn is not None and not _HSN.fullmatch(hsn):
        raise MappingInvalid("A preferred HSN is 4, 6 or 8 digits.")
    out = {
        "printed_hsn": _printed(values.get("printed_hsn"), "printed HSN", 20),
        "tally_item": _name(values.get("tally_item"), "stock item name"),
        "marg_item": _name(values.get("marg_item"), "stock item name"),
        "preferred_hsn": hsn,
    }
    if out["tally_item"] is None and out["marg_item"] is None:
        raise MappingInvalid(
            "Give the Tally stock item or the Marg item this product is booked under."
        )
    return out


def _source(source: str) -> Source:
    if source == "typed":
        return "typed"
    if source == "suggestion":
        return "suggestion"
    raise MappingInvalid("A mapping is typed, or confirmed from a suggestion.")


# Pages


def _cursor(sort_key: str, ident: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{sort_key}\x00{ident}".encode()).decode()


def _after(value: str) -> tuple[str, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(value.encode()).decode()
        sort_key, _, ident = raw.rpartition("\x00")
        return sort_key, uuid.UUID(ident)
    except ValueError as error:  # UnicodeDecodeError and binascii.Error are ValueErrors
        raise MappingInvalid("That is not a page cursor from this list.") from error


def _searched(query: Select[Any], q: str | None, columns: Sequence[Any]) -> Select[Any]:
    """Rows where every word of `q` is in one of `columns`, case aside."""
    for word in (q or "").casefold().split()[:10]:
        query = query.where(or_(*(func.lower(c).contains(word, autoescape=True) for c in columns)))
    return query


def _next(rows: Sequence[Any], limit: int) -> str | None:
    return _cursor(rows[limit - 1].sort_key, rows[limit - 1].id) if len(rows) > limit else None


class _Suggesters:
    """Suggesters built from a workspace's names, kept while those names stand: each is keyed
    by the workspace, the kind, the books and a fingerprint of its masters and mappings (their
    count and newest change), so a new, changed or removed name builds it anew. Only the
    workspace's own names are ever in one."""

    def __init__(self, size: int = _SUGGESTERS) -> None:
        self._size = size
        self._lock = threading.Lock()
        self._built: OrderedDict[tuple[Any, ...], Suggester] = OrderedDict()

    def get(self, key: tuple[Any, ...], build: Callable[[], Suggester]) -> Suggester:
        with self._lock:
            found = self._built.get(key)
            if found is not None:
                self._built.move_to_end(key)
                return found
        made = build()  # outside the lock: two at once may both build, and one is kept
        with self._lock:
            for stale in [k for k in self._built if k[:3] == key[:3]]:
                del self._built[stale]
            self._built[key] = made
            while len(self._built) > self._size:
                self._built.popitem(last=False)
        return made


def _fingerprint(session: Session, tenant_id: uuid.UUID, kind: str) -> tuple[Any, ...]:
    masters = session.execute(
        select(func.count(), func.max(MappingMaster.created_at)).where(
            MappingMaster.tenant_id == tenant_id, MappingMaster.kind == kind
        )
    ).one()
    model: type[SupplierMapping] | type[ItemMapping] = (
        SupplierMapping if kind == "supplier" else ItemMapping
    )
    mapped = session.execute(
        select(func.count(), func.max(model.created_at), func.max(model.updated_at)).where(
            model.tenant_id == tenant_id
        )
    ).one()
    return (*masters, *mapped)


class MappingService:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions
        self._suggesters = _Suggesters()

    def _suggester(
        self, session: Session, tenant_id: uuid.UUID, kind: str, target: Target | None
    ) -> Suggester:
        key = (tenant_id, kind, target, _fingerprint(session, tenant_id, kind))
        candidates = _supplier_candidates if kind == "supplier" else _item_candidates
        return self._suggesters.get(key, lambda: Suggester(candidates(session, tenant_id, target)))

    # Reading

    @scoped
    def list_suppliers(
        self,
        tenant_id: uuid.UUID,
        *,
        q: str | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> Page[SupplierMappingView]:
        """In printed-name order; `q` finds by words of the name, GSTIN or books' names."""
        searched = (
            SupplierMapping.printed_name, SupplierMapping.seller_gstin,
            SupplierMapping.tally_ledger, SupplierMapping.marg_party,
        )  # fmt: skip
        with self._sessions() as session:
            rows, total = _page(session, SupplierMapping, tenant_id, q, limit, after, searched)
            items = tuple(_supplier_view(r) for r in rows[:limit])
            return Page(items, _next(rows, limit), total)

    @scoped
    def list_items(
        self,
        tenant_id: uuid.UUID,
        *,
        q: str | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> Page[ItemMappingView]:
        """In printed-name order; `q` finds by words of the name, pack or books' names."""
        searched = (
            ItemMapping.product_name, ItemMapping.pack, ItemMapping.tally_item,
            ItemMapping.marg_item,
        )  # fmt: skip
        with self._sessions() as session:
            rows, total = _page(session, ItemMapping, tenant_id, q, limit, after, searched)
            return Page(tuple(_item_view(r) for r in rows[:limit]), _next(rows, limit), total)

    # Suppliers

    @scoped
    def create_supplier(
        self,
        tenant_id: uuid.UUID,
        *,
        seller_gstin: str,
        printed_name: str,
        tally_ledger: str | None = None,
        marg_party: str | None = None,
        purchase_ledger: str | None = None,
        tax_treatment: str | None = None,
        source: str = "typed",
        actor: str,
        name: str,
    ) -> SupplierMappingView:
        """Raises `MappingInvalid`, `MappingExists` (naming the mapping there is) or
        `TooManyMappings`."""
        gstin = gstin_key(seller_gstin)
        if gstin is None or not is_valid_gstin(gstin):
            raise MappingInvalid(
                f"'{seller_gstin.strip()}' is not a valid GSTIN: check its 15 characters."
            )
        values = _supplier_values(
            {"printed_name": printed_name, "tally_ledger": tally_ledger, "marg_party": marg_party,
             "purchase_ledger": purchase_ledger, "tax_treatment": tax_treatment}
        )  # fmt: skip
        kind = _source(source)
        with self._sessions.begin() as session:
            _room(session, SupplierMapping, tenant_id, MAX_SUPPLIERS, "supplier")
            made = session.scalar(
                insert(SupplierMapping)
                .values(
                    tenant_id=tenant_id,
                    seller_gstin=gstin,
                    sort_key=(values["printed_name"] or "").casefold(),
                    source=kind,
                    created_by=name[:200],
                    **values,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "seller_gstin"])
                .returning(SupplierMapping)
            )
            if made is None:
                existing = session.scalars(
                    select(SupplierMapping).where(
                        SupplierMapping.tenant_id == tenant_id,
                        SupplierMapping.seller_gstin == gstin,
                    )
                ).one()
                raise MappingExists(_supplier_view(existing))
            _audit(session, tenant_id, actor, "mapping.created", made.id,
                   {"kind": "supplier", "source": kind})  # fmt: skip
            return _supplier_view(made)

    @scoped
    def update_supplier(
        self,
        tenant_id: uuid.UUID,
        mapping_id: uuid.UUID,
        changes: Mapping[str, str | None],
        *,
        actor: str,
        name: str,
    ) -> SupplierMappingView:
        """Change the names it is booked under (its GSTIN is what it is). Raises
        `MappingNotFound` or `MappingInvalid`."""
        unknown = set(changes) - set(_SUPPLIER_FIELDS)
        if unknown:
            raise MappingInvalid(f"A supplier mapping has no {', '.join(sorted(unknown))}.")
        with self._sessions.begin() as session:
            row = _row(session, SupplierMapping, tenant_id, mapping_id)
            values = _supplier_values(
                {f: changes[f] if f in changes else getattr(row, f) for f in _SUPPLIER_FIELDS}
            )
            changed = [f for f in _SUPPLIER_FIELDS if getattr(row, f) != values[f]]
            if changed:
                for f in changed:
                    setattr(row, f, values[f])
                row.sort_key = row.printed_name.casefold()
                _touch(session, row, name)
                _audit(session, tenant_id, actor, "mapping.updated", row.id,
                       {"kind": "supplier", "fields": changed})  # fmt: skip
            return _supplier_view(row)

    @scoped
    def delete_supplier(self, tenant_id: uuid.UUID, mapping_id: uuid.UUID, *, actor: str) -> None:
        """Raises `MappingNotFound`. Past export runs keep the names they were made with."""
        with self._sessions.begin() as session:
            session.delete(_row(session, SupplierMapping, tenant_id, mapping_id))
            _audit(session, tenant_id, actor, "mapping.deleted", mapping_id, {"kind": "supplier"})

    # Items

    @scoped
    def create_item(
        self,
        tenant_id: uuid.UUID,
        *,
        product_name: str,
        pack: str | None = None,
        printed_hsn: str | None = None,
        tally_item: str | None = None,
        marg_item: str | None = None,
        preferred_hsn: str | None = None,
        source: str = "typed",
        actor: str,
        name: str,
    ) -> ItemMappingView:
        """Raises `MappingInvalid`, `MappingExists` (naming the mapping there is) or
        `TooManyMappings`."""
        printed = _printed(product_name, "printed name", MAX_PRINTED)
        if printed is None:
            raise MappingInvalid("Give the product's printed name, as the bill shows it.")
        shown_pack = _printed(pack, "pack", MAX_PACK)
        key = item_key(printed, shown_pack)
        if key is None or len(key) > MAX_ITEM_KEY:
            raise MappingInvalid("A product name needs letters or digits.")
        values = _item_values(
            {"printed_hsn": printed_hsn, "tally_item": tally_item, "marg_item": marg_item,
             "preferred_hsn": preferred_hsn}
        )  # fmt: skip
        kind = _source(source)
        with self._sessions.begin() as session:
            _room(session, ItemMapping, tenant_id, MAX_ITEMS, "item")
            made = session.scalar(
                insert(ItemMapping)
                .values(
                    tenant_id=tenant_id,
                    item_key=key,
                    product_name=printed,
                    pack=shown_pack,
                    sort_key=printed.casefold(),
                    source=kind,
                    created_by=name[:200],
                    **values,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "item_key"])
                .returning(ItemMapping)
            )
            if made is None:
                existing = session.scalars(
                    select(ItemMapping).where(
                        ItemMapping.tenant_id == tenant_id, ItemMapping.item_key == key
                    )
                ).one()
                raise MappingExists(_item_view(existing))
            _audit(session, tenant_id, actor, "mapping.created", made.id,
                   {"kind": "item", "source": kind})  # fmt: skip
            return _item_view(made)

    @scoped
    def update_item(
        self,
        tenant_id: uuid.UUID,
        mapping_id: uuid.UUID,
        changes: Mapping[str, str | None],
        *,
        actor: str,
        name: str,
    ) -> ItemMappingView:
        """Change the names it is booked under, or its HSN (its printed name and pack are
        what it is). Raises `MappingNotFound` or `MappingInvalid`."""
        unknown = set(changes) - set(_ITEM_FIELDS)
        if unknown:
            raise MappingInvalid(f"An item mapping has no {', '.join(sorted(unknown))}.")
        with self._sessions.begin() as session:
            row = _row(session, ItemMapping, tenant_id, mapping_id)
            values = _item_values(
                {f: changes[f] if f in changes else getattr(row, f) for f in _ITEM_FIELDS}
            )
            changed = [f for f in _ITEM_FIELDS if getattr(row, f) != values[f]]
            if changed:
                for f in changed:
                    setattr(row, f, values[f])
                _touch(session, row, name)
                _audit(session, tenant_id, actor, "mapping.updated", row.id,
                       {"kind": "item", "fields": changed})  # fmt: skip
            return _item_view(row)

    @scoped
    def delete_item(self, tenant_id: uuid.UUID, mapping_id: uuid.UUID, *, actor: str) -> None:
        """Raises `MappingNotFound`. Past export runs keep the names they were made with."""
        with self._sessions.begin() as session:
            session.delete(_row(session, ItemMapping, tenant_id, mapping_id))
            _audit(session, tenant_id, actor, "mapping.deleted", mapping_id, {"kind": "item"})

    # Masters

    @scoped
    def import_masters(
        self, tenant_id: uuid.UUID, data: bytes, *, actor: str, name: str
    ) -> ImportReport:
        """The names a CSV of the books' masters holds, kept to be offered as suggestions;
        never applied. Raises `MastersFileInvalid` for a file that cannot be read at all."""
        parsed = parse_masters(data)
        rejected = list(parsed.rejected)
        fresh: dict[tuple[str, str], MasterRow] = {}
        for row in parsed.rows:
            fresh.setdefault((row.kind, row.name.casefold()), row)
        repeated = len(parsed.rows) - len(fresh)
        with self._sessions.begin() as session:
            held = session.scalar(
                select(func.count())
                .select_from(MappingMaster)
                .where(MappingMaster.tenant_id == tenant_id)
            )
            known = _known_masters(session, tenant_id, list(fresh))
            new = [row for key, row in fresh.items() if key not in known]
            room = max(MAX_MASTERS - (held or 0), 0)
            rejected.extend(
                RejectedRow(
                    row.line,
                    f"The workspace holds {MAX_MASTERS:,} names already; this one was not "
                    "imported.",
                )
                for row in new[room:]
            )
            kept = new[:room]
            imported = 0
            for offset in range(0, len(kept), _BATCH):
                rows = [
                    {"tenant_id": tenant_id, "kind": r.kind, "name": r.name,
                     "name_key": r.name.casefold(), "gstin": r.gstin, "created_by": name[:200]}
                    for r in kept[offset : offset + _BATCH]
                ]  # fmt: skip
                made = session.scalars(
                    insert(MappingMaster)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["tenant_id", "kind", "name_key"])
                    .returning(MappingMaster.id)
                ).all()
                imported += len(made)
            report = ImportReport(
                rows=len(parsed.rows) + len(parsed.rejected),
                imported=imported,
                # Named higher up the file, held already, or imported by someone else meanwhile.
                already_known=repeated + len(known) + (len(kept) - imported),
                rejected=tuple(sorted(rejected, key=lambda r: r.line)),
            )
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="mapping.imported",
                target_type="workspace", target_id=str(tenant_id),
                details={"rows": report.rows, "imported": report.imported,
                         "already_known": report.already_known, "rejected": len(report.rejected)},
            )  # fmt: skip
            return report

    # What is still to map, and what may be offered for it

    @scoped
    def tasks_for(
        self, tenant_id: uuid.UUID, bills: Sequence[Bill], *, target: Target | None
    ) -> Tasks:
        with self._sessions() as session:
            return resolve(session, tenant_id, bills, target).tasks

    @scoped
    def tasks_for_items(
        self,
        tenant_id: uuid.UUID,
        products: Sequence[tuple[str, str | None, str | None]],
        *,
        target: Target | None,
    ) -> Tasks:
        """What is still to map of these products (printed name, pack, HSN)."""
        wanted: dict[str, tuple[str, str | None, str | None]] = {}
        for product_name, pack, hsn in products:
            key = item_key(product_name, pack)
            if key is not None:
                wanted.setdefault(key, (product_name, pack, hsn))
        with self._sessions() as session:
            found = _item_rows(session, tenant_id, wanted)
        tasks = []
        for key, (product_name, pack, hsn) in wanted.items():
            mapped = found.get(key)
            if mapped is None or mapped.name_for(target) is None:
                tasks.append(
                    ItemTask(key, product_name, pack, hsn, 1, mapped.id if mapped else None)
                )
        return Tasks(items=tuple(tasks))

    @scoped
    def suggestions(self, tenant_id: uuid.UUID, tasks: Tasks, *, target: Target | None) -> Tasks:
        """`tasks` with names to offer for each, from the workspace's masters and its other
        mappings; a task mapped since it was listed is left out. Nothing is made."""
        with self._sessions() as session:
            have_suppliers = _supplier_rows(
                session, tenant_id, (t.seller_gstin for t in tasks.suppliers)
            )
            have_items = _item_rows(session, tenant_id, (t.item_key for t in tasks.items))
            open_suppliers = [
                t for t in tasks.suppliers
                if (m := have_suppliers.get(t.seller_gstin)) is None or m.name_for(target) is None
            ]  # fmt: skip
            open_items = [
                t for t in tasks.items
                if (n := have_items.get(t.item_key)) is None or n.name_for(target) is None
            ]  # fmt: skip
            empty = Suggester([])
            suppliers = (
                self._suggester(session, tenant_id, "supplier", target) if open_suppliers else empty
            )
            items = self._suggester(session, tenant_id, "item", target) if open_items else empty
        return replace(
            tasks,
            suppliers=tuple(
                replace(t, suggestions=_best(suppliers, t.printed_names, t.seller_gstin))
                for t in open_suppliers
            ),
            items=tuple(
                replace(t, suggestions=_best(items, [f"{t.product_name} {t.pack or ''}"], None))
                for t in open_items
            ),
        )


def _page(
    session: Session,
    model: type[SupplierMapping] | type[ItemMapping],
    tenant_id: uuid.UUID,
    q: str | None,
    limit: int,
    after: str | None,
    searched: Sequence[Any],
) -> tuple[list[Any], int]:
    if not 1 <= limit <= MAX_PAGE:
        raise MappingInvalid(f"A page holds 1 to {MAX_PAGE} mappings.")
    base = _searched(select(model).where(model.tenant_id == tenant_id), q, searched)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    query = base
    if after:
        sort_key, ident = _after(after)
        query = query.where(tuple_(model.sort_key, model.id) > tuple_(sort_key, ident))
    rows = list(session.scalars(query.order_by(model.sort_key, model.id).limit(limit + 1)))
    return rows, total


def _row[M: (SupplierMapping, ItemMapping)](
    session: Session, model: type[M], tenant_id: uuid.UUID, mapping_id: uuid.UUID
) -> M:
    row = session.scalars(
        select(model).where(model.tenant_id == tenant_id, model.id == mapping_id).with_for_update()
    ).one_or_none()
    if row is None:
        raise MappingNotFound(mapping_id)
    return row


def _touch(session: Session, row: SupplierMapping | ItemMapping, name: str) -> None:
    row.updated_by = name[:200]
    row.updated_at = func.now()  # the database's clock
    session.flush()
    session.refresh(row)


def _room(
    session: Session,
    model: type[SupplierMapping] | type[ItemMapping],
    tenant_id: uuid.UUID,
    most: int,
    kind: str,
) -> None:
    count = session.scalar(
        select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
    )
    if (count or 0) >= most:
        raise TooManyMappings(f"This workspace has {most:,} {kind} mappings already.")


def _best(suggester: Suggester, texts: Iterable[str], gstin: str | None) -> tuple[Suggestion, ...]:
    best: dict[str, Suggestion] = {}
    for text in texts:
        for s in suggester.suggest(text, gstin=gstin):
            if s.name not in best or s.score > best[s.name].score:
                best[s.name] = s
    ranked = sorted(best.values(), key=lambda s: (-s.score, s.name.casefold(), s.name))
    return tuple(ranked[:5])


def _names_for(target: Target | None, tally: str | None, marg: str | None) -> list[str]:
    if target == "tally":
        return [tally] if tally else []
    if target == "marg":
        return [marg] if marg else []
    return [n for n in (tally, marg) if n]


def _supplier_candidates(
    session: Session, tenant_id: uuid.UUID, target: Target | None
) -> list[Candidate]:
    found = [
        Candidate(name=n, gstin=g, source="master")
        for n, g in session.execute(
            select(MappingMaster.name, MappingMaster.gstin).where(
                MappingMaster.tenant_id == tenant_id, MappingMaster.kind == "supplier"
            )
        )
    ]
    for printed, tally, marg in session.execute(
        select(
            SupplierMapping.printed_name, SupplierMapping.tally_ledger, SupplierMapping.marg_party
        ).where(SupplierMapping.tenant_id == tenant_id)
    ):
        for booked in _names_for(target, tally, marg):
            # Compared by the name its bills print, without its GSTIN (mapped already):
            # offered for a name alike, such as the same company's branch in another state.
            found.append(Candidate(name=booked, gstin=None, source="mapping", match_text=printed))
    return found


def _item_candidates(
    session: Session, tenant_id: uuid.UUID, target: Target | None
) -> list[Candidate]:
    found = [
        Candidate(name=n, gstin=None, source="master")
        for n in session.scalars(
            select(MappingMaster.name).where(
                MappingMaster.tenant_id == tenant_id, MappingMaster.kind == "item"
            )
        )
    ]
    for printed, pack, tally, marg in session.execute(
        select(
            ItemMapping.product_name,
            ItemMapping.pack,
            ItemMapping.tally_item,
            ItemMapping.marg_item,
        ).where(ItemMapping.tenant_id == tenant_id)
    ):
        for booked in _names_for(target, tally, marg):
            # Compared by the name and pack its bills print: the books' own names are the
            # masters', imported.
            text = f"{printed} {pack or ''}"
            found.append(Candidate(name=booked, gstin=None, source="mapping", match_text=text))
    return found


def _known_masters(
    session: Session, tenant_id: uuid.UUID, keys: Sequence[tuple[str, str]]
) -> set[tuple[str, str]]:
    known: set[tuple[str, str]] = set()
    for offset in range(0, len(keys), _BATCH):
        rows = session.execute(
            select(MappingMaster.kind, MappingMaster.name_key).where(
                MappingMaster.tenant_id == tenant_id,
                tuple_(MappingMaster.kind, MappingMaster.name_key).in_(
                    keys[offset : offset + _BATCH]
                ),
            )
        )
        known.update((kind, key) for kind, key in rows)
    return known


def _audit(
    session: Session,
    tenant_id: uuid.UUID,
    actor: str,
    action: str,
    mapping_id: uuid.UUID,
    details: dict[str, Any],
) -> None:
    audit.append(
        session, tenant_id=tenant_id, actor=actor, action=action,
        target_type=f"{details['kind']}_mapping", target_id=str(mapping_id), details=details,
    )  # fmt: skip


def _supplier_view(row: SupplierMapping) -> SupplierMappingView:
    return SupplierMappingView(
        id=row.id, seller_gstin=row.seller_gstin, printed_name=row.printed_name,
        tally_ledger=row.tally_ledger, marg_party=row.marg_party,
        purchase_ledger=row.purchase_ledger, tax_treatment=row.tax_treatment, source=row.source,
        created_by=row.created_by, created_at=row.created_at, updated_by=row.updated_by,
        updated_at=row.updated_at,
    )  # fmt: skip


def _item_view(row: ItemMapping) -> ItemMappingView:
    return ItemMappingView(
        id=row.id, item_key=row.item_key, product_name=row.product_name, pack=row.pack,
        printed_hsn=row.printed_hsn, tally_item=row.tally_item, marg_item=row.marg_item,
        preferred_hsn=row.preferred_hsn, source=row.source, created_by=row.created_by,
        created_at=row.created_at, updated_by=row.updated_by, updated_at=row.updated_at,
    )  # fmt: skip
