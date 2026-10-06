"""The extraction pipeline with Claude and OpenAI: the same pipeline, the same checks.

Each provider's reply comes through its real SDK over a fake transport; the pipeline
validates it, normalises it and assesses it exactly as it does Gemini's, and a malformed
reply is asked for again once.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from docforge.extraction.pipeline import InvoicePipeline
from docforge.llm.anthropic import AnthropicProvider
from docforge.llm.openai import OpenAIProvider
from fakes import FakeParser
from test_llm_providers import Recorder, claude_message, openai_response, transport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PDF = (FIXTURES / "pair_001" / "invoice.pdf").read_bytes()
LABEL = json.loads((FIXTURES / "pair_001" / "label.json").read_text(encoding="utf-8"))
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


def claude(*replies: httpx.Response) -> AnthropicProvider:
    import anthropic

    client = anthropic.Anthropic(api_key="t", max_retries=0, http_client=transport(Recorder(*replies)))
    return AnthropicProvider("claude-x", client=client, sleep=lambda _: None)


def gpt(*replies: httpx.Response) -> OpenAIProvider:
    import openai

    client = openai.OpenAI(api_key="t", max_retries=0, http_client=transport(Recorder(*replies)))
    return OpenAIProvider("o-x", client=client, sleep=lambda _: None)


@pytest.mark.parametrize(
    ("build", "reply", "name"),
    [(claude, claude_message, "anthropic"), (gpt, openai_response, "openai")],
)
def test_an_invoice_is_extracted_and_checked_as_with_gemini(
    raw_invoice_from_label: RawFromLabel, build: Any, reply: Any, name: str
) -> None:
    raw = json.dumps(raw_invoice_from_label(LABEL))
    result = InvoicePipeline(FakeParser(), build(reply("not json"), reply(raw))).run(PDF)

    assert result.extraction.invoice_no.value == LABEL["invoice"]["invoice_no"]
    assert result.extraction.seller.gstin.value == LABEL["invoice"]["seller"]["gstin"]
    assert [r.provider for r in result.responses] == [name, name]  # the malformed one, again
