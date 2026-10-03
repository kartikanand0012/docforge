"""Embeddings: recorded once, replayed offline, keyed so a change of model is never mixed in."""

from pathlib import Path

import pytest

from docforge.search.embeddings import EmbeddingMissing, FakeEmbedder, RecordingEmbedder


class Counting(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str], task: str) -> list[list[float]]:
        self.calls.append(texts)
        return super().embed(texts, task)


def test_the_fake_embedder_is_repeatable_and_unit_length() -> None:
    fake = FakeEmbedder(dimensions=16)

    a, b = fake.embed(["batch XGX944068", "batch XGX944068"], "document")

    assert a == b and len(a) == 16
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9


def test_recordings_replay_without_calling_the_model(tmp_path: Path) -> None:
    live = Counting()
    recorded = RecordingEmbedder(tmp_path, live)
    first = recorded.embed(["alpha", "beta"], "document")

    replay = RecordingEmbedder(tmp_path, None, model=live.model)

    assert replay.embed(["beta", "alpha"], "document") == [first[1], first[0]]
    assert live.calls == [["alpha", "beta"]]


def test_only_texts_not_yet_recorded_are_sent(tmp_path: Path) -> None:
    live = Counting()
    recorded = RecordingEmbedder(tmp_path, live)
    recorded.embed(["alpha"], "document")

    recorded.embed(["alpha", "gamma"], "document")

    assert live.calls == [["alpha"], ["gamma"]]


def test_a_query_and_a_document_are_different_recordings(tmp_path: Path) -> None:
    recorded = RecordingEmbedder(tmp_path, FakeEmbedder())
    recorded.embed(["alpha"], "document")

    with pytest.raises(EmbeddingMissing):
        RecordingEmbedder(tmp_path, None, model="fake").embed(["alpha"], "query")
