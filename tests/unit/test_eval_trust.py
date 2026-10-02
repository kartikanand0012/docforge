"""The C3 gate in CI: the trust report replays from recordings and every seeded defect is caught."""

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
