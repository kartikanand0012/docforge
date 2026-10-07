"""The provider interface: one structured-output call in, one JSON reply with usage out."""

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class LLMError(Exception):
    """The provider could not produce a reply."""


class LLMQuotaExhausted(LLMError):
    """A quota that will not reset soon (for example per day) is used up."""


@dataclass(frozen=True)
class LLMRequest:
    system: str
    prompt: str
    schema: type[BaseModel]  # the shape the reply must have
    prompt_version: str


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str  # JSON, not yet validated
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    thinking_tokens: int | None = None  # billed like output, reported separately
    latency_ms: float
    # The model the provider says it served, when it says: a pin can be served as a snapshot.
    served_model: str | None = None


class LLMProvider(Protocol):
    name: str
    model: str

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Raises `LLMError` if no reply could be obtained."""
        ...
