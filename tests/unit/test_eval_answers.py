"""The answer eval: questions with known answers from the synthetic labels, and questions the
documents cannot answer. Scored on correctness, citations on the right document, abstention,
and citations from another organisation (which must be none)."""

from pathlib import Path

import pytest

from docforge.evals.answers import (
    AnswerResult,
    Expect,
    build_questions,
    correct,
    score_answers,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.mark.parametrize(
    ("text", "expect", "right"),
    [
        ("The grand total is ₹98,697.00.", Expect(kind="number", value="98697.00"), True),
        ("It comes to 98697 rupees.", Expect(kind="number", value="98697.00"), True),
        ("The total is 98,696.77.", Expect(kind="number", value="98697.00"), False),
        ("Invoice 2026 has a total of 98,697.", Expect(kind="number", value="98697.00"), True),
        ("Navjivan Medical Agencies issued it.", Expect(kind="text", value="Navjivan Medical Agencies"), True),
        ("The assay was 96.3%.", Expect(kind="text", value="96.3 %"), True),
        ("The assay was 96.8 %.", Expect(kind="text", value="96.3 %"), False),
    ],
)
def test_an_answer_is_right_when_it_states_the_expected_value(
    text: str, expect: Expect, right: bool
) -> None:
    assert correct(text, expect) is right


def test_questions_come_from_the_labels_with_answers_and_unanswerables() -> None:
    questions = build_questions(FIXTURES / "synthetic", FIXTURES / "coa", pairs=2)

    assert len({q.id for q in questions}) == len(questions)
    answerable = [q for q in questions if q.expect is not None]
    unanswerable = [q for q in questions if q.expect is None]
    assert len(answerable) == 8 and len(unanswerable) == 6
    total = next(q for q in questions if q.id == "pair_001-total")
    assert total.expect == Expect(kind="number", value="98697.00")
    assert total.documents == ("pair_001/invoice",)
    assert {q.tenant for q in questions} == {"a", "b"}
    assert questions == build_questions(FIXTURES / "synthetic", FIXTURES / "coa", pairs=2)


def result(qid: str, status: str, text: str = "", docs: tuple[str, ...] = (), leaks: int = 0) -> AnswerResult:
    return AnswerResult(
        question_id=qid, status=status, text=text, cited_documents=docs, cross_tenant=leaks,
        input_tokens=1000, output_tokens=100,
    )  # fmt: skip


def test_the_report_counts_right_answers_citations_abstention_and_leaks() -> None:
    questions = build_questions(FIXTURES / "synthetic", FIXTURES / "coa", pairs=1)
    by_id = {q.id: q for q in questions}
    results = []
    for q in questions:
        if q.id == "pair_001-total":  # right, cited on the invoice
            results.append(result(q.id, "supported", "Total 98,697.00.", ("pair_001/invoice",)))
        elif q.id == "pair_001-seller":  # wrong value, though cited
            results.append(result(q.id, "supported", "Jalaram Pharmacy.", ("pair_001/invoice",)))
        elif q.expect is not None:  # abstained on an answerable question
            results.append(result(q.id, "not_found"))
        elif q.id.endswith("bank"):  # answered something it should not have
            results.append(result(q.id, "supported", "1234", ("pair_001/invoice",), leaks=1))
        else:
            results.append(result(q.id, "not_found"))

    report = score_answers(questions, results, model="m", prompt_version="chat-1")

    assert report.questions == len(questions) == len(by_id)
    assert report.answered_correctly == pytest.approx(1 / 4)
    assert report.cited_expected_document == pytest.approx(2 / 4)
    assert report.false_abstention == pytest.approx(2 / 4)
    assert report.wrong_answers == 1
    assert report.abstained_when_no_answer == pytest.approx(2 / 3)
    assert report.cross_tenant_citations == 1
    assert report.tokens_per_question == (1000, 100)
    assert "pair_001-seller" in report.missed
