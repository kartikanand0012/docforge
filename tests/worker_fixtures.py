"""A pipeline for the worker-crash test: it blocks until told to continue.

Loaded inside a real worker process through the `PIPELINE_FACTORY` setting.
"""

import os
import time
from pathlib import Path

from docforge.config import Settings
from docforge.documents import Pipeline
from docforge.extraction.pipeline import InvoicePipeline, PipelineResult
from fakes import FakeParser, ScriptedProvider

BLOCK_FILE_VAR = "DOCFORGE_TEST_BLOCK_FILE"
REPLY_FILE_VAR = "DOCFORGE_TEST_REPLY_FILE"


class BlockingPipeline:
    """Waits while the block file exists, then extracts with a scripted model reply."""

    def run(self, pdf: bytes) -> PipelineResult:
        block = Path(os.environ[BLOCK_FILE_VAR])
        while block.exists():
            time.sleep(0.05)
        reply = Path(os.environ[REPLY_FILE_VAR]).read_text(encoding="utf-8")
        return InvoicePipeline(FakeParser(), ScriptedProvider([reply])).run(pdf)


def build_pipelines(settings: Settings) -> dict[str, Pipeline]:
    return {"invoice": BlockingPipeline()}
