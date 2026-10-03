"""The stages a document passes through, and how a pipeline reports reaching one.

A pipeline calls `report_stage` as it goes; whoever runs it (the document service) listens
with `stage_listener`. A context variable, so pipelines need no extra argument and code that
runs them without listening is unaffected.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# In order. `processed`: finished, but nothing indexes it for search and chat.
STAGES = (
    "stored",
    "parsing",
    "extracting",
    "checking",
    "indexing",
    "processed",
    "ready",
    "retrying",
    "failed",
)

_listener: ContextVar[Callable[[str], None] | None] = ContextVar("stage_listener", default=None)


def report_stage(stage: str) -> None:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    listener = _listener.get()
    if listener is not None:
        listener(stage)


@contextmanager
def stage_listener(callback: Callable[[str], None]) -> Iterator[None]:
    token = _listener.set(callback)
    try:
        yield
    finally:
        _listener.reset(token)
