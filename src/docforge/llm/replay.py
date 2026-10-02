"""Record model responses to disk and replay them, so evals and CI run offline.

A recording is keyed by everything that determines the reply: model, prompt version,
system instruction, prompt and reply schema. Change any of them and it is a miss.
"""

import hashlib
import json
from pathlib import Path

from docforge.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse


class RecordingProvider:
    """Serves recorded responses. With `inner`, a miss is fetched live and recorded.

    Without `inner` it is replay-only: a miss raises instead of calling out.
    """

    def __init__(self, directory: Path, model: str, inner: LLMProvider | None = None) -> None:
        if inner is not None and inner.model != model:
            raise ValueError(f"inner provider uses model {inner.model!r}, not {model!r}")
        self.name = inner.name if inner is not None else "replay"
        self.model = model
        self._directory = directory
        self._inner = inner

    def _key(self, request: LLMRequest) -> str:
        identity = {
            "model": self.model,
            "prompt_version": request.prompt_version,
            "system": request.system,
            "prompt": request.prompt,
            "schema": request.schema.model_json_schema(),
        }
        encoded = json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def generate(self, request: LLMRequest) -> LLMResponse:
        key = self._key(request)
        path = self._directory / f"{key[:24]}.json"
        if path.exists():
            recording = json.loads(path.read_text(encoding="utf-8"))
            if recording["key"] == key:
                return LLMResponse.model_validate(recording["response"])
        if self._inner is None:
            raise LLMError(
                f"no recorded response for this request (key {key[:24]}); "
                "record one with a live provider"
            )
        response = self._inner.generate(request)
        self._directory.mkdir(parents=True, exist_ok=True)
        recording = {
            "key": key,
            "model": self.model,
            "prompt_version": request.prompt_version,
            "response": response.model_dump(mode="json"),
        }
        path.write_text(json.dumps(recording, indent=2) + "\n", encoding="utf-8")
        return response
