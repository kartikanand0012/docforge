"""What a document cost to read, and a workspace's figures at a glance - its own only."""

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.engine import Engine

from docforge.db.session import SessionFactory
from full_stack import FullStack
from worlds import World

pytestmark = [pytest.mark.integration, pytest.mark.no_ambient_tenant]

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
# The scripted reader answers each document in one call of 100 tokens in and 50 out.
PRICES = {"fake/fake-1": (1.0, 2.0), "fake/fake-chat-1": (0.5, 1.0)}
ONE_READING = (100 * 1.0 + 50 * 2.0) / 1_000_000


@pytest.fixture
def world(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> World:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    world.build()
    return world


def test_a_document_says_what_reading_it_cost(
    sessions: SessionFactory, world: World, owner_engine: Engine
) -> None:
    stack = FullStack(sessions, world, prices=PRICES)
    me = stack.sign_up("Asha", "asha@example.com")
    document = stack.read(FullStack.workspace_of(owner_engine, "asha@example.com"))

    detail = stack.client.get(f"/v1/documents/{document}", headers=me).json()
    listed = stack.client.get("/v1/documents", headers=me).json()["items"]

    assert detail["cost"] == {
        "model_cost_usd": pytest.approx(ONE_READING), "input_tokens": 100,
        "output_tokens": 50, "model_calls": 1,
    }  # fmt: skip
    assert listed[0]["model_cost_usd"] == pytest.approx(ONE_READING)


def test_every_reading_of_a_document_counts(
    sessions: SessionFactory, world: World, owner_engine: Engine
) -> None:
    stack = FullStack(sessions, world, prices=PRICES)
    me = stack.sign_up("Asha", "asha@example.com")
    document = stack.read(FullStack.workspace_of(owner_engine, "asha@example.com"))
    version = stack.client.post(f"/v1/documents/{document}/reprocess", headers=me)
    assert version.status_code == 202
    assert stack.documents.process(_version_id(stack, document)) == "succeeded"

    cost = stack.client.get(f"/v1/documents/{document}", headers=me).json()["cost"]

    assert (cost["model_calls"], cost["input_tokens"]) == (2, 200)
    assert cost["model_cost_usd"] == pytest.approx(2 * ONE_READING)


def _version_id(stack: FullStack, document: Any) -> Any:
    assert stack.world.queued
    return stack.world.queued[-1]


def test_without_a_price_the_cost_is_unknown_not_zero(
    sessions: SessionFactory, world: World, owner_engine: Engine
) -> None:
    stack = FullStack(sessions, world)
    me = stack.sign_up("Asha", "asha@example.com")
    document = stack.read(FullStack.workspace_of(owner_engine, "asha@example.com"))

    cost = stack.client.get(f"/v1/documents/{document}", headers=me).json()["cost"]
    listed = stack.client.get("/v1/documents", headers=me).json()["items"]
    stats = stack.client.get("/v1/stats", headers=me).json()

    assert cost["model_cost_usd"] is None and cost["model_calls"] == 1
    assert listed[0]["model_cost_usd"] is None and stats["model_cost_usd"] is None


def test_a_workspaces_figures_are_its_own(
    sessions: SessionFactory, world: World, owner_engine: Engine
) -> None:
    stack = FullStack(sessions, world, prices=PRICES)
    asha = stack.sign_up("Asha", "asha@example.com")
    ben = stack.sign_up("Ben", "ben@example.com")
    asha_ws = FullStack.workspace_of(owner_engine, "asha@example.com")
    stack.read(asha_ws, "purchase_order")
    invoice = stack.read(asha_ws, "invoice")
    stack.read(FullStack.workspace_of(owner_engine, "ben@example.com"), "invoice")

    stats = stack.client.get("/v1/stats", headers=asha)

    assert stats.status_code == 200
    body = stats.json()
    assert set(body) == {
        "documents", "ready", "awaiting_review", "signed", "failed", "documents_last_7_days",
        "median_read_seconds", "model_cost_usd",
    }  # fmt: skip
    assert (body["documents"], body["ready"], body["failed"], body["signed"]) == (2, 2, 0, 0)
    assert body["documents_last_7_days"] == 2
    assert body["awaiting_review"] == len(stack.client.get("/v1/review/queue", headers=asha).json())
    assert body["median_read_seconds"] is not None and body["median_read_seconds"] >= 0
    assert body["model_cost_usd"] == pytest.approx(2 * ONE_READING)
    # Ben's workspace has its own figures, and a deleted document leaves Asha's.
    assert stack.client.get("/v1/stats", headers=ben).json()["documents"] == 1
    stack.client.delete(f"/v1/documents/{invoice}", headers=asha)
    after = stack.client.get("/v1/stats", headers=asha).json()
    assert after["documents"] == 1
    assert after["model_cost_usd"] == pytest.approx(ONE_READING)


def test_questions_count_in_a_workspaces_cost(
    sessions: SessionFactory, world: World, owner_engine: Engine
) -> None:
    stack = FullStack(sessions, world, prices=PRICES)
    me = stack.sign_up("Asha", "asha@example.com")
    stack.read(FullStack.workspace_of(owner_engine, "asha@example.com"))
    stack.model.reply = lambda passages: {"statements": [], "unanswerable": True}
    assert stack.client.post("/v1/chat", headers=me, json={"question": "Total?"}).status_code == 200

    stats = stack.client.get("/v1/stats", headers=me).json()

    question = (1000 * 0.5 + 100 * 1.0) / 1_000_000
    assert stats["model_cost_usd"] == pytest.approx(ONE_READING + question)


def test_an_empty_workspace_has_nothing_yet(sessions: SessionFactory, world: World) -> None:
    stack = FullStack(sessions, world, prices=PRICES)
    me = stack.sign_up("Asha", "asha@example.com")

    body = stack.client.get("/v1/stats", headers=me).json()

    assert body == {
        "documents": 0, "ready": 0, "awaiting_review": 0, "signed": 0, "failed": 0,
        "documents_last_7_days": 0, "median_read_seconds": None, "model_cost_usd": 0.0,
    }  # fmt: skip
    assert stack.client.get("/v1/stats").status_code == 401
