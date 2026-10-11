"""A model that is not called: a valid, empty reply of whatever schema is asked for, after a
model time chosen for the run.

Robustness and capacity runs measure the parser, the converter, the queue and the database;
a real model would add its own time, cost and rate limits, which are measured separately
(the live tier). Every reply says `simulated`, and costs nothing.
"""

import json
import math
import random
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from docforge.llm.base import LLMRequest, LLMResponse

NAME = "simulated"
MAX_SECONDS = 600.0
# C1's measured model time for one invoice: p50 14.4 s, p95 26.6 s. A log-normal through both.
_C1_MEDIAN, _C1_P95 = 14.4, 26.6
_Z95 = 1.6448536269514722
# Values a field must take for an empty reply to mean what it says.
_MEANING = {"unanswerable": True, "missing": "nothing: the model is simulated"}


def minimal_reply(schema: type[BaseModel]) -> str:
    """The smallest reply `schema` accepts: every required field present, every value empty
    or null - the model "found nothing"."""
    json_schema = schema.model_json_schema()
    definitions: dict[str, Any] = json_schema.get("$defs", {})

    def value(node: dict[str, Any], name: str | None = None) -> Any:
        if name in _MEANING:
            return _MEANING[name]
        if "$ref" in node:
            return value(definitions[node["$ref"].rsplit("/", 1)[-1]])
        if "anyOf" in node:
            options = node["anyOf"]
            if any(option.get("type") == "null" for option in options):
                return None
            return value(options[0])
        if "enum" in node:
            return node["enum"][0]
        kind = node.get("type")
        if kind == "object":
            properties = node.get("properties", {})
            return {key: value(properties[key], key) for key in node.get("required", [])}
        return {"array": [], "string": "", "integer": 0, "number": 0, "boolean": False}.get(
            str(kind)
        )

    return json.dumps(value(json_schema))


def model_time(spec: str, rng: random.Random) -> Callable[[], float]:
    """Seconds each reply takes: `zero`, `fixed:N` (0 to 600), or `c1` (as C1 measured)."""
    if spec == "zero":
        return lambda: 0.0
    if spec == "c1":
        mu = math.log(_C1_MEDIAN)
        sigma = (math.log(_C1_P95) - mu) / _Z95
        return lambda: min(rng.lognormvariate(mu, sigma), MAX_SECONDS)
    kind, _, number = spec.partition(":")
    try:
        seconds = float(number)
    except ValueError:
        seconds = math.nan
    if kind != "fixed" or not math.isfinite(seconds) or not 0 <= seconds <= MAX_SECONDS:
        raise ValueError(
            f"SIMULATED_MODEL_TIME must be zero, c1 or fixed:N (0 to {MAX_SECONDS:.0f}), "
            f"not {spec!r}"
        )
    return lambda: seconds


class SimulatedProvider:
    """Answers every request with `minimal_reply`, after the run's model time."""

    name = NAME
    model = NAME

    def __init__(
        self,
        spec: str = "zero",
        *,
        seed: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # Model timing, not security: a seeded generator makes a run repeatable.
        self._draw = model_time(spec, random.Random(seed))  # noqa: S311
        self._sleep = sleep

    def generate(self, request: LLMRequest) -> LLMResponse:
        seconds = self._draw()
        if seconds:
            self._sleep(seconds)
        return LLMResponse(
            text=minimal_reply(request.schema),
            provider=NAME,
            model=NAME,
            input_tokens=0,
            output_tokens=0,
            latency_ms=seconds * 1000,
        )
