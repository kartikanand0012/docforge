"""V2 1.1 part 4's gate: e-invoice QRs on the clean invoices, replayed from recordings."""

from pathlib import Path

from docforge.evals.einvoice import EinvoiceReport, format_einvoice_report, run_einvoice_eval
from docforge.evals.trust import trust_pipelines

REPO = Path(__file__).resolve().parents[2]
BASELINE = REPO / "evals" / "baselines" / "einvoice.json"


def committed() -> EinvoiceReport:
    return EinvoiceReport.model_validate_json(BASELINE.read_text(encoding="utf-8"))


def replayed() -> EinvoiceReport:
    invoices, _ = trust_pipelines(REPO / "tests" / "fixtures" / "recorded", committed().model)
    return run_einvoice_eval(REPO / "tests" / "fixtures" / "synthetic", invoices)


def test_the_committed_report_is_reproduced_offline() -> None:
    assert replayed() == committed()


def test_every_forged_qr_is_caught() -> None:
    summary = committed().summary

    assert summary.pairs == 20
    assert summary.forged_cases == 2 * summary.pairs  # a copied QR and an altered one each
    assert summary.forged_caught == summary.forged_cases


def test_a_qr_that_proves_nothing_changes_no_decision() -> None:
    summary = committed().summary

    assert summary.harmless_cases == 2 * summary.pairs  # a UPI QR and an unknown key each
    assert summary.harmless_unchanged == summary.harmless_cases


def test_a_genuine_qr_changes_a_decision_only_where_the_reading_was_wrong() -> None:
    report = committed()
    changed = [case for case in report.cases if case.kind == "genuine" and not case.expected_met]

    assert report.summary.genuine_unchanged == report.summary.pairs - len(changed)
    # Where a genuine QR disagrees with the page, the reading was wrong and is now caught.
    assert all(case.decision == "review" and case.findings for case in changed)


def test_the_report_holds_no_qr_text_or_key() -> None:
    text = BASELINE.read_text(encoding="utf-8")

    assert "eyJ" not in text  # base64url of a JSON object: a QR's header or payload
    assert "BEGIN" not in text


def test_the_summary_reads_plainly() -> None:
    lines = format_einvoice_report(committed())

    assert "forged QRs caught" in lines and "unchanged" in lines
