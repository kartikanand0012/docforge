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


def test_a_per_minute_quota_is_waited_out_as_the_server_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.genai import errors

    from docforge.search import embeddings

    waits: list[float] = []
    monkeypatch.setattr("docforge.search.embeddings.time.sleep", waits.append)

    class Response:
        def __init__(self, n: int) -> None:
            self.embeddings = [type("E", (), {"values": [1.0, 0.0]})() for _ in range(n)]

    class Models:
        calls = 0

        def embed_content(self, **kwargs: object) -> Response:
            Models.calls += 1
            if Models.calls == 1:
                raise errors.ClientError(
                    429,
                    {
                        "error": {
                            "code": 429,
                            "message": "quota",
                            "status": "RESOURCE_EXHAUSTED",
                            "details": [
                                {
                                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                    "retryDelay": "32s",
                                }
                            ],
                        }
                    },
                    None,
                )
            return Response(len(kwargs["contents"]))  # type: ignore[arg-type]

    embedder = embeddings.GeminiEmbedder.__new__(embeddings.GeminiEmbedder)
    embedder.model, embedder.dimensions = "gemini-embedding-001", 2
    embedder._client = type("C", (), {"models": Models()})()

    vectors = embedder.embed(["a", "b"], "document")

    assert len(vectors) == 2 and waits == [33.0]


def test_a_rejected_key_is_an_error_not_a_busy_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a quota (429) means "try words only"; a bad key or model must surface."""
    from google.genai import errors

    from docforge.search import embeddings

    monkeypatch.setattr("docforge.search.embeddings.time.sleep", lambda _: None)

    class Models:
        def embed_content(self, **kwargs: object) -> object:
            raise errors.ClientError(403, {"error": {"code": 403, "message": "denied"}}, None)

    embedder = embeddings.GeminiEmbedder.__new__(embeddings.GeminiEmbedder)
    embedder.model, embedder.dimensions = "gemini-embedding-001", 2
    embedder._client = type("C", (), {"models": Models()})()

    with pytest.raises(errors.ClientError) as raised:
        embedder.embed(["a"], "query")
    assert not isinstance(raised.value, embeddings.EmbeddingUnavailable)
