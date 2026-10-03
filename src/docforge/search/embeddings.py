"""Turning text into vectors: Gemini for real, recorded for replay, a stand-in for tests.

The model and the number of dimensions are fixed per deployment: vectors from different
models cannot be compared, so a recording is keyed by model, task, dimensions and text.
"""

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Literal, Protocol

Task = Literal["document", "query"]
DIMENSIONS = 768
_GEMINI_TASKS = {"document": "RETRIEVAL_DOCUMENT", "query": "RETRIEVAL_QUERY"}
_BATCH = 100


class EmbeddingMissing(LookupError):
    """Replay only, and this text was never recorded."""


class Embedder(Protocol):
    model: str
    dimensions: int

    def embed(self, texts: list[str], task: str) -> list[list[float]]: ...


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


class FakeEmbedder:
    """Hashed bag of words: texts sharing words are close. Deterministic, offline."""

    model = "fake"

    def __init__(self, dimensions: int = DIMENSIONS) -> None:
        self.dimensions = dimensions

    def embed(self, texts: list[str], task: str) -> list[list[float]]:
        out = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for word in re.findall(r"\w+", text.lower()):
                digest = hashlib.sha256(word.encode()).digest()
                vector[int.from_bytes(digest[:4], "big") % self.dimensions] += 1.0
            out.append(_unit(vector))
        return out


class GeminiEmbedder:
    def __init__(self, model: str, api_key: str, dimensions: int = DIMENSIONS) -> None:
        from google import genai

        self.model = model
        self.dimensions = dimensions
        self._client = genai.Client(api_key=api_key)

    def embed(self, texts: list[str], task: str) -> list[list[float]]:
        from google.genai import types

        out: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            response = self._client.models.embed_content(
                model=self.model,
                contents=texts[start : start + _BATCH],  # type: ignore[arg-type]
                config=types.EmbedContentConfig(
                    output_dimensionality=self.dimensions, task_type=_GEMINI_TASKS[task]
                ),
            )
            # Shortened Gemini embeddings are not unit length; cosine distance wants them so.
            out += [_unit(list(e.values or [])) for e in response.embeddings or []]
        return out


class RecordingEmbedder:
    """Serves recorded vectors; with `inner`, records what is missing (only that is sent)."""

    def __init__(
        self, directory: Path, inner: Embedder | None, *, model: str | None = None
    ) -> None:
        self._directory = directory
        self._inner = inner
        self.model = inner.model if inner is not None else (model or "")
        self.dimensions = inner.dimensions if inner is not None else DIMENSIONS

    def _path(self, text: str, task: str) -> Path:
        key = hashlib.sha256(f"{self.model}|{task}|{self.dimensions}|{text}".encode()).hexdigest()
        return self._directory / f"{key[:32]}.json"

    def embed(self, texts: list[str], task: str) -> list[list[float]]:
        found: dict[str, list[float]] = {}
        missing: list[str] = []
        for text in dict.fromkeys(texts):
            path = self._path(text, task)
            if path.exists():
                found[text] = json.loads(path.read_text(encoding="utf-8"))
            else:
                missing.append(text)
        if missing:
            if self._inner is None:
                raise EmbeddingMissing(f"{len(missing)} texts have no recorded embedding")
            self._directory.mkdir(parents=True, exist_ok=True)
            for text, vector in zip(missing, self._inner.embed(missing, task), strict=True):
                path = self._path(text, task)
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps([round(x, 7) for x in vector]), encoding="utf-8")
                os.replace(temporary, path)
                found[text] = json.loads(path.read_text(encoding="utf-8"))
        return [found[text] for text in texts]
