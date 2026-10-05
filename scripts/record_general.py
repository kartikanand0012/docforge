"""Record what the browser test and the demo replay for the general documents in
tests/fixtures/general: the PDF made of each file, its parse, and the embeddings of its
chunks and of the questions asked about it.

Conversions need LibreOffice (run them where it is installed, or in the worker image);
embeddings need GEMINI_API_KEY. What is already recorded is reused, so a run only fills gaps.

    uv run python scripts/record_general.py [question ...]
"""

import sys
from pathlib import Path

from docforge.config import get_settings
from docforge.conversion import FileConverter, RecordingConverter
from docforge.extraction.general import GeneralPipeline
from docforge.formats import sniff
from docforge.parsing.cache import CachingParser
from docforge.parsing.docling_parser import DoclingParser
from docforge.search.chunking import chunk_document
from docforge.search.embeddings import GeminiEmbedder, RecordingEmbedder

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "general"
RECORDED = ROOT / "tests" / "fixtures" / "recorded"
QUESTIONS = ("What happens to goods above 8 °C?",)


def main(questions: list[str]) -> int:
    settings = get_settings()
    key = settings.gemini_api_key
    live = GeminiEmbedder(settings.embedding_model, key.get_secret_value()) if key else None
    embedder = RecordingEmbedder(RECORDED / "embeddings", live, model=settings.embedding_model)
    inner = FileConverter() if FileConverter.find_soffice() else None
    converter = RecordingConverter(RECORDED / "renditions", inner)
    pipeline = GeneralPipeline(CachingParser(RECORDED / "parsed", DoclingParser()))
    for path in sorted(FIXTURES.iterdir()):
        fmt = sniff(path.read_bytes())
        if fmt is None:
            continue
        result = pipeline.run(converter.to_pdf(path.read_bytes(), fmt))
        chunks = chunk_document("general", path.name, result.parsed, result.extraction)
        embedder.embed([chunk.text for chunk in chunks], "document")
        print(f"{path.name}: {len(result.parsed.pages)} pages, {len(chunks)} chunks recorded")
    embedder.embed(questions or list(QUESTIONS), "query")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
