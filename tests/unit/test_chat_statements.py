"""An answer is checked statement by statement: each needs a quote found in its passage, and
every figure it states (an amount, a quantity, a percentage, a code, a date) must be in one
of its quotes, or in the question or conversation it repeats. A statement that fails is
dropped; one real quote can no longer carry a wrong figure."""

from docforge.chat.prompt import Passage
from docforge.chat.statements import RawStatement, check_statements
from docforge.chat.service import RawCitation

PASSAGES = [
    Passage(
        n=1, filename="inv.pdf", page=1, text="Invoice NVM/26-27/32001 | Grand total 98,697.00"
    ),
    Passage(
        n=2,
        filename="inv.pdf",
        page=1,
        text="Amoxicillin Capsules IP 250mg | Qty 20 | Batch XGX944068",
    ),
]


def statement(text: str, *quotes: tuple[int, str]) -> RawStatement:
    return RawStatement(text=text, citations=[RawCitation(passage=n, quote=q) for n, q in quotes])


def test_a_statement_whose_figure_is_in_its_quote_is_kept() -> None:
    checked = check_statements(
        [statement("The grand total is 98697.00.", (1, "Grand total 98,697.00"))],
        PASSAGES,
        given="",
    )
    assert [s.text for s in checked.kept] == ["The grand total is 98697.00."]
    assert checked.dropped_statements == 0


def test_a_statement_with_a_figure_its_quote_does_not_hold_is_dropped() -> None:
    """The quote is real, the figure is not in it: the statement goes."""
    checked = check_statements(
        [statement("The grand total is 99,999.00.", (1, "Grand total 98,697.00"))],
        PASSAGES,
        given="",
    )
    assert checked.kept == [] and checked.dropped_statements == 1


def test_a_statement_without_a_found_quote_is_dropped() -> None:
    checked = check_statements(
        [
            statement("Payment is due in 30 days."),
            statement("It was approved.", (1, "approved by the director")),
        ],
        PASSAGES,
        given="",
    )
    assert checked.kept == [] and checked.dropped_statements == 2 and checked.dropped_citations == 1


def test_figures_repeated_from_the_question_or_conversation_need_no_quote() -> None:
    given = "Which invoice billed batch XGX944068? Invoice NVM/26-27/32001 billed it."
    checked = check_statements(
        [statement("Invoice NVM/26-27/32001 billed 20 units of batch XGX944068.", (2, "Qty 20"))],
        PASSAGES,
        given=given,
    )
    assert len(checked.kept) == 1


def test_a_figure_written_differently_is_the_same_figure() -> None:
    checked = check_statements(
        [statement("It comes to ₹98,697.", (1, "Grand total 98,697.00"))], PASSAGES, given=""
    )
    assert len(checked.kept) == 1


def test_one_good_statement_and_one_bad_keeps_the_good() -> None:
    checked = check_statements(
        [
            statement("The grand total is 98,697.00.", (1, "Grand total 98,697.00")),
            statement("It billed 25 units.", (2, "Qty 20")),
        ],
        PASSAGES,
        given="",
    )
    assert [s.text for s in checked.kept] == ["The grand total is 98,697.00."]
    assert checked.dropped_statements == 1


def test_a_code_must_be_quoted_whole() -> None:
    checked = check_statements(
        [statement("The batch is XGX944069.", (2, "Batch XGX944068"))], PASSAGES, given=""
    )
    assert checked.kept == []
