"""An answer is checked statement by statement: each needs a quote found in its passage, and
every figure it states (an amount, a quantity, a percentage, a code, a date) must be in one
of its quotes, or in the question or conversation it repeats. A statement that fails is
dropped; one real quote can no longer carry a wrong figure."""

from docforge.chat.prompt import Passage
from docforge.chat.statements import RawCitation, RawStatement, check_statements

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


# --- review findings (hardening) ----------------------------------------------------------

ROW = [
    Passage(
        n=1,
        filename="coa.pdf",
        page=1,
        text="Amoxicillin Capsules IP 250 mg | Assay 95.0 - 105.0 % | 96.3 % | Rs. 500 | Loss 500",
    )
]


def keep(text: str, quote: str, given: str = "") -> bool:
    return bool(check_statements([statement(text, (1, quote))], ROW, given=given).kept)


def test_ordinary_ways_of_writing_a_figure_are_the_same_figure() -> None:
    assert keep("Each capsule is 250mg.", "Amoxicillin Capsules IP 250 mg")
    assert keep(
        "The limit is 95.0-105.0 %.", "Assay 95.0 - 105.0 %", given="What is the assay limit?"
    )
    assert keep("It costs Rs.500.", "Rs. 500", given="What does it cost?")


def test_a_sign_and_a_list_are_not_lost() -> None:
    assert not keep("A loss of -500.", "Loss 500")
    assert not keep("Pages 1,2,3 show it.", "Loss 500")  # read as 1, 2 and 3, not 123


def test_a_figure_only_repeated_from_the_question_does_not_stand_without_its_own() -> None:
    """'Is the total 5,000?' cannot be answered 'the total is 5,000' on a quote of 500."""
    assert not keep("The total is 5,000.", "Loss 500", given="Is the total 5,000?")
    # A code named in the question may be repeated next to a figure its quote holds.
    assert keep("Batch XGX944068 assayed 96.3 %.", "96.3 %", given="Assay of batch XGX944068?")


def test_a_quote_may_join_distant_parts_of_a_summary_but_not_of_other_passages() -> None:
    far = (
        "Invoice NVM/26-27/09004 dated 3 May 2026 "
        + "Products: "
        + "Paracetamol, " * 50
        + "Grand total 31674.00."
    )
    quote = "Invoice NVM/26-27/09004 [...] Grand total 31674.00."
    said = "The grand total of invoice NVM/26-27/09004 is 31674.00."
    summary = [Passage(n=1, filename="i.pdf", page=1, text=far, kind="summary")]
    text = [Passage(n=1, filename="i.pdf", page=1, text=far, kind="text")]
    assert check_statements([statement(said, (1, quote))], summary, given="").kept
    assert not check_statements([statement(said, (1, quote))], text, given="").kept


# --- wording and labels (limits) -----------------------------------------------------------

INVOICE = [
    Passage(
        n=1,
        filename="inv.pdf",
        page=1,
        text=(
            "Invoice NVM/26-27/32001 from Navjivan Medical Agencies | Discount 0.00 "
            "| Grand total 98,697.00"
        ),
    ),
    Passage(
        n=2,
        filename="coa.pdf",
        page=1,
        text="Test: Assay | Specification: 95.0 - 105.0 % | Result: 96.3 %",
        kind="table_row",
    ),
]


def kept(text: str, quote: str, n: int = 1, given: str = "") -> bool:
    return bool(check_statements([statement(text, (n, quote))], INVOICE, given=given).kept)


def test_a_statement_that_says_what_its_passage_does_not_is_dropped() -> None:
    """No figure to check, but the words are not the passage's: 'paid in full' is said nowhere."""
    assert not kept("The invoice was paid in full and approved.", "Navjivan Medical Agencies")
    assert kept("The invoice is from Navjivan Medical Agencies.", "Navjivan Medical Agencies")


def test_a_figure_must_stand_beside_what_the_statement_calls_it() -> None:
    assert kept("The grand total is 98,697.00.", "Grand total 98,697.00")
    assert not kept("The discount is 98,697.00.", "Grand total 98,697.00")


def test_a_table_cell_quoted_alone_is_read_with_its_row() -> None:
    assert kept("The assay result was 96.3 %.", "96.3 %", n=2)
    assert not kept("The water content was 96.3 %.", "96.3 %", n=2)
