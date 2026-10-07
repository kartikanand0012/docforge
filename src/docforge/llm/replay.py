"""Record model responses to disk and replay them, so evals and CI run offline.

A recording is keyed by everything that determines the reply: model, prompt version,
system instruction, prompt and reply schema - and, for a provider other than Gemini, the
provider and its options. Gemini's keys are as they always were, so every committed recording
still replays. Change any of them and it is a miss.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from docforge.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse
from docforge.llm.schema import strict_json_schema


class RecordingProvider:
    """Serves recorded responses. With `inner`, a miss is fetched live and recorded.

    Without `inner` it is replay-only: a miss raises instead of calling out.
    """

    def __init__(
        self,
        directory: Path,
        model: str,
        inner: LLMProvider | None = None,
        *,
        provider: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> None:
        """`provider` names whose recordings these are; unnamed, they are keyed as Gemini's
        always were, and served under the live provider's name (or "replay")."""
        if inner is not None and inner.model != model:
            raise ValueError(f"inner provider uses model {inner.model!r}, not {model!r}")
        if inner is not None and provider is not None and inner.name != provider:
            raise ValueError(f"inner provider is {inner.name!r}, not {provider!r}")
        self.name = provider or (inner.name if inner is not None else "replay")
        self._provider = provider
        self.model = model
        self.directory = directory
        self._inner = inner
        self._options = options or {}

    def _key(self, request: LLMRequest) -> str:
        identity = {
            "model": self.model,
            "prompt_version": request.prompt_version,
            "system": request.system,
            "prompt": request.prompt,
            "schema": request.schema.model_json_schema(),
        }
        if self._provider not in (None, "gemini"):  # Gemini's keys stay as they were
            # What Claude and OpenAI are actually sent: the strict schema, and the request's
            # shape (temperature, output limit, format) - a change to either is a miss.
            identity |= {
                "provider": self._provider,
                "options": self._options,
                "strict_schema": strict_json_schema(request.schema),
            }
        encoded = json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def generate(self, request: LLMRequest) -> LLMResponse:
        key = self._key(request)
        path = self.directory / f"{key[:24]}.json"
        recorded = self._read(path, key)
        if recorded is not None:
            return recorded
        if self._inner is None:
            raise LLMError(
                f"no recorded response for this request (key {key[:24]}); "
                "record one with a live provider"
            )
        response = self._inner.generate(request)
        self.directory.mkdir(parents=True, exist_ok=True)
        recording = {
            "key": key,
            "model": self.model,
            "prompt_version": request.prompt_version,
            "response": response.model_dump(mode="json"),
        }
        # Write then rename, so an interrupted run never leaves half a file behind.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(recording, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        return response

    @staticmethod
    def _read(path: Path, key: str) -> LLMResponse | None:
        """The recorded response, or None if there is none or the file is damaged."""
        try:
            recording = json.loads(path.read_text(encoding="utf-8"))
            if recording["key"] != key:
                return None
            return LLMResponse.model_validate(recording["response"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
