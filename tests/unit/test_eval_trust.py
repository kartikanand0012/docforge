"""The C3 gate in CI: the trust report replays from recordings and every seeded defect is caught."""

import json
from pathlib import Path

from docforge.evals.trust import TrustReport, format_trust_report, run_trust_eval, trust_pipelines

REPO = Path(__file__).resolve().parents[2]
BASELINE = REPO / "evals" / "baselines" / "trust.json"


def committed() -> TrustReport:
    return TrustReport.model_validate_json(BASELINE.read_text(encoding="utf-8"))


def test_the_committed_trust_report_is_reproduced_offline() -> None:
    report = committed()
    invoices, orders = trust_pipelines(REPO / "tests" / "fixtures" / "recorded", report.model)

    replayed = run_trust_eval(
        REPO / "tests" / "fixtures" / "synthetic",
        REPO / "tests" / "fixtures" / "seeded",
        invoices,
        orders,
    )

    assert replayed == report


def test_every_seeded_defect_is_caught() -> None:
    """Lowering this is a decision, not a side effect of re-recording."""
    summary = committed().summary

    assert summary.seeded_cases == 9
    assert summary.seeded_cases_caught == summary.seeded_cases
    assert summary.seeded_findings_caught == summary.seeded_findings_expected
    assert summary.clean_pairs == 20


def test_correct_pairs_are_not_sent_to_review_more_often_than_measured() -> None:
    """A floor at the measured value: stricter checks must not quietly raise the review load."""
    assert committed().summary.clean_pairs_accepted >= 12


def test_the_text_report_names_every_case_that_went_to_review() -> None:
    report = committed()

    text = format_trust_report(report)

    assert f"{report.summary.seeded_cases_caught} of 9 cases caught" in text
    for result in report.clean:
        assert (f"review {result.pair_id}" in text) == (not result.accepted)


def test_the_browser_e2e_reviews_an_invoice_with_exactly_one_doubtful_value() -> None:
    """The e2e confirms one doubtful value and then approves; a re-recording that changes
    its invoice's outcome must fail here, by name, not as a timeout in a browser."""
    import re

    spec = Path("web/tests/e2e/review.spec.ts").read_text(encoding="utf-8")
    match = re.search(r"tests/fixtures/synthetic/(pair_\d+)", spec)
    assert match is not None
    report = json.loads(Path("evals/baselines/trust.json").read_text(encoding="utf-8"))
    (pair,) = [p for p in report["clean"] if p["pair_id"] == match[1]]

    assert pair["invoice_reasons"] == ["1 value was not found in the source text it cites"], (
        f"{match[1]} no longer has exactly one doubtful value: pick another for the e2e"
    )
    assert pair["order_decision"] == "accept"
