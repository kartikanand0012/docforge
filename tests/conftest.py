"""Shared fixtures."""

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url

import tracing  # noqa: F401 - installs the in-memory span exporter before anything traces
from docforge.config import Settings, get_settings
from docforge.db import DEFAULT_TENANT_ID, alembic_config
from docforge.db.roles import ensure_app_login
from docforge.db.session import SessionFactory, make_engine, make_session_factory
from docforge.db.tenancy import tenant_scope

# Real-parser tests use the models already on disk when they are there. A new converter
# otherwise asks the model hub whether its files are current, and a dropped connection then
# fails a test that has nothing to do with the network. On a fresh machine the first such
# test downloads the models as before.
_LAYOUT_MODEL = (
    Path.home() / ".cache" / "huggingface" / "hub" / "models--docling-project--docling-layout-heron"
)
if _LAYOUT_MODEL.is_dir():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

TEST_DATABASE = "docforge_test"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


RAW_LINE_FIELDS = (
    "product_name",
    "pack",
    "hsn",
    "batch_no",
    "mfg",
    "expiry",
    "qty",
    "free_qty",
    "mrp",
    "ptr",
    "discount_pct",
    "taxable_value",
    "gst_rate",
    "amount",
)
RAW_TOTAL_FIELDS = ("taxable_value", "cgst", "sgst", "igst", "round_off", "grand_total")


@pytest.fixture(scope="session")
def raw_invoice_from_label() -> Callable[[dict[str, Any]], dict[str, Any]]:
    """What a perfect model would reply for a fixture label: every printed string, verbatim.

    Values the page does not print (see `unprinted` in the label) come back as null.
    """

    def build(label: dict[str, Any]) -> dict[str, Any]:
        printed = {box["path"]: box["text"] for box in label["documents"]["invoice"]["boxes"]}

        def field(path: str) -> dict[str, Any]:
            return {"text": printed.get(path), "block_ids": []}

        def party(prefix: str) -> dict[str, Any]:
            licences = len(label["invoice"][prefix]["drug_licence_nos"])
            return {
                "name": field(f"{prefix}.name"),
                "address": field(f"{prefix}.address"),
                "gstin": field(f"{prefix}.gstin"),
                "drug_licence_nos": [
                    field(f"{prefix}.drug_licence_nos[{index}]") for index in range(licences)
                ],
            }

        return {
            "invoice_no": field("invoice_no"),
            "invoice_date": field("invoice_date"),
            "po_no": field("po_no"),
            "po_date": field("po_date"),
            "place_of_supply": field("place_of_supply"),
            "seller": party("seller"),
            "buyer": party("buyer"),
            "lines": [
                {name: field(f"lines[{index}].{name}") for name in RAW_LINE_FIELDS}
                for index in range(len(label["invoice"]["lines"]))
            ],
            "totals": {name: field(f"totals.{name}") for name in RAW_TOTAL_FIELDS},
        }

    return build


@pytest.fixture(scope="session")
def raw_order_from_label() -> Callable[[dict[str, Any]], dict[str, Any]]:
    """What a perfect model would reply for a fixture purchase order."""

    def build(label: dict[str, Any]) -> dict[str, Any]:
        document = label["documents"]["purchase_order"]
        printed = {box["path"]: box["text"] for box in document["boxes"]}

        def field(path: str) -> dict[str, Any]:
            return {"text": printed.get(path), "block_ids": []}

        licences = len(label["purchase_order"]["buyer"]["drug_licence_nos"])
        return {
            "po_no": field("po_no"),
            "po_date": field("po_date"),
            "buyer": {
                "name": field("buyer.name"),
                "address": field("buyer.address"),
                "gstin": field("buyer.gstin"),
                "drug_licence_nos": [
                    field(f"buyer.drug_licence_nos[{index}]") for index in range(licences)
                ],
            },
            "supplier_name": field("supplier_name"),
            "supplier_gstin": field("supplier_gstin"),
            "lines": [
                {
                    name: field(f"lines[{index}].{name}")
                    for name in ("product_name", "pack", "qty", "scheme", "rate")
                }
                for index in range(len(label["purchase_order"]["lines"]))
            ],
        }

    return build


@pytest.fixture
def empty_database_url(settings: Settings) -> Iterator[URL]:
    """A freshly created, empty database on the Compose Postgres, dropped afterwards."""
    admin_url = make_url(settings.migration_database_url.get_secret_value())
    if admin_url.host not in LOCAL_HOSTS:
        # These tests create and drop a database; never do that on a shared server.
        pytest.fail(f"integration tests only run against a local Postgres, not {admin_url.host}")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DATABASE}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{TEST_DATABASE}"'))
    try:
        yield admin_url.set(database=TEST_DATABASE)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DATABASE}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(autouse=True)
def default_tenant_scope(request: pytest.FixtureRequest) -> Iterator[None]:
    """Tests read as the default tenant, as its own services would. A service call for
    another tenant opens that tenant's scope inside this one.

    Tests marked `no_ambient_tenant` get no scope at all: every route they call must scope
    its own work, or it sees nothing.
    """
    if request.node.get_closest_marker("no_ambient_tenant"):
        yield
        return
    with tenant_scope(DEFAULT_TENANT_ID):
        yield


@pytest.fixture(scope="session")
def app_login(settings: Settings) -> URL:
    """The application's own database login, created once: subject to row-level security."""
    ensure_app_login(
        settings.migration_database_url.get_secret_value(), settings.database_url.get_secret_value()
    )
    return make_url(settings.database_url.get_secret_value())


@pytest.fixture
def owner_engine(empty_database_url: URL) -> Iterator[Engine]:
    """The migrated test database as its owner: for setting up and inspecting, not for
    running services, which must work under the application's role."""
    command.upgrade(alembic_config(empty_database_url), "head")
    engine = make_engine(empty_database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def engine(owner_engine: Engine, empty_database_url: URL, app_login: URL) -> Iterator[Engine]:
    """The migrated test database as the application connects to it."""
    engine = make_engine(
        empty_database_url.set(username=app_login.username, password=app_login.password)
    )
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(engine: Engine) -> SessionFactory:
    return make_session_factory(engine)


@pytest.fixture
def owner_sessions(owner_engine: Engine) -> SessionFactory:
    """Sessions as the owner, which row-level security does not apply to: for tests of
    database mechanics across tenants, not of the services."""
    return make_session_factory(owner_engine)


@pytest.fixture
def other_tenant(owner_engine: Engine) -> uuid.UUID:
    with owner_engine.begin() as conn:
        tenant_id: uuid.UUID = conn.execute(
            text("INSERT INTO tenants (name) VALUES ('other') RETURNING id")
        ).scalar_one()
    return tenant_id


@pytest.fixture(scope="session")
def raw_coa_from_label() -> Callable[[dict[str, Any]], dict[str, Any]]:
    """What a perfect model would reply for a fixture certificate of analysis."""

    def build(label: dict[str, Any]) -> dict[str, Any]:
        printed = {box["path"]: box["text"] for box in label["document"]["boxes"]}

        def field(path: str) -> dict[str, Any]:
            return {"text": printed.get(path), "block_ids": []}

        return {
            **{
                name: field(name)
                for name in (
                    "coa_no",
                    "manufacturer",
                    "product_name",
                    "batch_no",
                    "mfg",
                    "expiry",
                    "analysis_date",
                    "conclusion",
                )
            },
            "tests": [
                {name: field(f"tests[{i}].{name}") for name in ("name", "specification", "result")}
                for i in range(len(label["coa"]["tests"]))
            ],
        }

    return build
