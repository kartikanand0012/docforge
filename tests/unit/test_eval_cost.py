"""Cost per document from the eval report: thinking tokens are billed as output."""

import json
from pathlib import Path

from docforge.api.review import summarise_evals


def test_cost_counts_thinking_tokens_as_output(tmp_path: Path) -> None:
    report = json.loads(Path("evals/baselines/invoice.json").read_text())
    report["summary"]["documents"] = 2
    report["usage"].update(
        input_tokens=1_000_000, output_tokens=1_000_000, thinking_tokens=1_000_000
    )
    (tmp_path / "invoice.json").write_text(json.dumps(report))

    cost = summarise_evals(tmp_path, (0.30, 2.50))["cost"]

    # (1M x 0.30 + 2M x 2.50) / 2 documents
    assert cost["per_document_usd"] == 2.65
