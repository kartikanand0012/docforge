"""V2 P7 at scale: a month of 3,000 invoices reported in under 30 seconds, and the variance
panel adding under 200 ms to a bill's review read in a workspace of 10,000 invoices.

One bill of pair_001, approved and paired with its order, is cloned in SQL (as the database
owner): the bill (document, version, reading, signed review) and an order of its own (document,
version, reading, not signed) for each, and their pair, each with its own ids and content hash.
"""

import statistics
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from test_mappings import Books
from test_variance_report import MONTH, Ledger
from test_visible_flags import Desk

from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
REPORT_SECONDS = 30.0
PANEL_SECONDS = 0.2


def _columns(conn: Connection, table: str) -> list[str]:
    return list(
        conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t ORDER BY ordinal_position"
            ),
            {"t": table},
        ).scalars()
    )


def _copy(conn: Connection, table: str, where: str, swap: dict[str, str]) -> None:
    """Each row of `table` matching `where` once per clone, with `swap`'s columns replaced."""
    columns = _columns(conn, table)
    chosen = ", ".join(swap.get(c, f"s.{c}") for c in columns)
    conn.execute(
        text(
            f"INSERT INTO {table} ({', '.join(columns)}) "  # noqa: S608 - names from the catalogue
            f"SELECT {chosen} FROM {table} s, clones c WHERE {where}"
        )
    )


def clone(owner: Engine, bill: uuid.UUID, copies: int) -> None:
    """`copies` more of the bill, each paired with a copy of its order of its own, so every
    order's values are worked out from its own reading (the slowest case)."""
    with owner.begin() as conn:
        version, order_version = conn.execute(
            text(
                "SELECT m.invoice_version_id, m.order_version_id FROM matches m "
                "JOIN document_versions v ON v.id = m.invoice_version_id "
                "WHERE v.document_id = :d"
            ),
            {"d": bill},
        ).one()
        order = conn.execute(
            text("SELECT document_id FROM document_versions WHERE id = :v"), {"v": order_version}
        ).scalar_one()
        conn.execute(
            text(
                "CREATE TEMP TABLE clones ON COMMIT DROP AS SELECT n, gen_random_uuid() AS d, "
                "gen_random_uuid() AS v, gen_random_uuid() AS od, gen_random_uuid() AS ov "
                "FROM generate_series(1, :n) n"
            ),
            {"n": copies},
        )
        hashed = "encode(sha256(convert_to({}::text, 'UTF8')), 'hex')"
        for document, ver, new_doc, new_ver in (
            (uuid.UUID(str(bill)), uuid.UUID(str(version)), "c.d", "c.v"),
            (uuid.UUID(str(order)), uuid.UUID(str(order_version)), "c.od", "c.ov"),
        ):  # ids only, never text from a bill
            _copy(conn, "documents", f"s.id = '{document}'",
                  {"id": new_doc, "sha256": hashed.format(new_doc)})  # fmt: skip
            _copy(conn, "document_versions", f"s.id = '{ver}'",
                  {"id": new_ver, "document_id": new_doc})  # fmt: skip
            same = f"s.document_version_id = '{ver}'"
            _copy(conn, "extractions", same,
                  {"id": "gen_random_uuid()", "document_version_id": new_ver})  # fmt: skip
            _copy(conn, "parse_outputs", same,
                  {"id": "gen_random_uuid()", "document_version_id": new_ver})  # fmt: skip
        same = f"s.document_version_id = '{uuid.UUID(str(version))}'"
        _copy(conn, "reviews", same, {"id": "gen_random_uuid()", "document_version_id": "c.v"})
        _copy(conn, "matches", f"s.invoice_version_id = '{uuid.UUID(str(version))}'",
              {"id": "gen_random_uuid()", "invoice_version_id": "c.v",
               "order_version_id": "c.ov"})  # fmt: skip
    with owner.connect() as conn:  # as autovacuum would, so plans see the rows
        conn.execute(text("ANALYZE"))
        conn.commit()


@pytest.fixture
def ledger(
    sessions: SessionFactory, raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> Ledger:  # fmt: skip
    return Ledger(Books(sessions, Desk(sessions, raw_invoice_from_label, raw_order_from_label)))


def test_a_month_of_3000_invoices_and_the_panel_at_10000(
    ledger: Ledger, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.order()
    bill = ledger.bill()
    clone(owner_engine, bill, 2_999)

    started = time.perf_counter()
    report = ledger.reports.build(DEFAULT_TENANT_ID, MONTH)
    built = time.perf_counter() - started
    started = time.perf_counter()
    _, data, _ = ledger.reports.workbook(DEFAULT_TENANT_ID, MONTH, actor="cli", person="Asha")
    filed = time.perf_counter() - started

    assert len(report.checked) == 3_000 and report.totals.variances == 6_000
    assert filed < REPORT_SECONDS
    print(f"\n3,000 invoices: preview {built:.2f} s, file {filed:.2f} s ({len(data):,} bytes)")

    clone(owner_engine, bill, 7_000)  # 10,000 bills in the workspace

    def read() -> float:
        started = time.perf_counter()
        ledger.desk.review.detail(DEFAULT_TENANT_ID, bill)
        return time.perf_counter() - started

    read()  # warm
    timed: dict[bool, list[float]] = {True: [], False: []}
    for _ in range(7):  # alternately, so neither side has the warmer cache
        for panel in (True, False):
            with monkeypatch.context() as patch:
                if not panel:
                    patch.setattr("docforge.review.service.compare", lambda *_: None)
                timed[panel].append(read())
    with_panel, without = statistics.median(timed[True]), statistics.median(timed[False])

    print(f"review read at 10,000: {with_panel * 1000:.0f} ms with the variance, "
          f"{without * 1000:.0f} ms without")  # fmt: skip
    assert with_panel - without < PANEL_SECONDS
