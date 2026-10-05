"""An answer's citations are checked against the passages they name, as extracted values are
checked against their source: a quote that is not in its passage is not a citation."""

from docforge.chat.verify import cited_blocks, normalise, quote_in


def test_a_quote_copied_from_the_passage_is_found() -> None:
    passage = "Record the temperature of cold-chain goods; anything above 8 °C is rejected."
    assert quote_in("anything above 8 °C is rejected", passage)


def test_case_spacing_line_breaks_and_typographic_marks_do_not_matter() -> None:
    passage = "Goods  receipt notes are kept\nfor five years — after the “expiry” of the batch."
    assert quote_in('goods receipt notes are kept for five years - after the "expiry"', passage)


def test_a_quote_that_is_not_in_the_passage_is_not_found() -> None:
    passage = "Anything above 8 °C is rejected."
    assert not quote_in("anything above 25 °C is rejected", passage)
    assert not quote_in("goods are accepted", passage)


def test_a_quote_too_short_to_mean_anything_is_not_a_citation() -> None:
    assert not quote_in("8", "Anything above 8 °C is rejected.")
    assert not quote_in("  ", "Anything above 8 °C is rejected.")


def test_fullwidth_brackets_used_to_fence_passages_match_the_original() -> None:
    assert normalise("a \uff1cb\uff1e c") == normalise("a <b> c")


def block(block_id: str, text: str) -> dict[str, object]:
    return {"id": block_id, "text": text, "page": 1, "x0": 0, "y0": 0, "x1": 1, "y1": 1}


def test_the_blocks_a_quote_comes_from_are_the_ones_shown() -> None:
    blocks = [
        block("b4", "Check the delivery challan and the purchase order number before unloading."),
        block("b6", "Record the temperature of cold-chain goods; anything above 8 °C is rejected."),
        block("b7", "Move accepted goods to quarantine with a yellow label."),
    ]
    assert [b["id"] for b in cited_blocks("anything above 8 °C is rejected", blocks)] == ["b6"]


def test_a_quote_across_two_blocks_shows_both() -> None:
    blocks = [block("b1", "Grand total"), block("b2", "1,23,456.00"), block("b3", "Bank details")]
    assert [b["id"] for b in cited_blocks("Grand total 1,23,456.00", blocks)] == ["b1", "b2"]


def test_a_short_block_is_not_cited_just_because_its_word_appears() -> None:
    blocks = [block("b1", "Batch"), block("b2", "The batch B-2041 was released on 3 June.")]
    assert [b["id"] for b in cited_blocks("batch B-2041 was released", blocks)] == ["b2"]


# --- review findings (C11) ---------------------------------------------------------------


def test_a_number_is_not_found_inside_a_longer_number() -> None:
    assert not quote_in("5,000.00", "Grand total 15,000.00")
    assert not quote_in("1234", "Invoice 21234 dated today")
    assert quote_in("15,000.00", "Grand total 15,000.00.")


def test_a_word_is_not_found_inside_a_longer_word() -> None:
    assert not quote_in("release", "The batch was released by QA.")


def test_a_lone_common_word_is_not_a_citation() -> None:
    assert not quote_in("page", "See page 2 of the procedure.")
    assert not quote_in("Date", "Date of analysis: 26-Jun-2026")
    assert quote_in("26-Jun-2026", "Date of analysis: 26-Jun-2026")
    assert quote_in("96.3 %", "Assay 95.0 - 105.0 % 96.3 %")
    assert quote_in("goods are counted", "All goods are counted on arrival.")


def test_a_short_number_cell_is_not_outlined_inside_a_longer_number() -> None:
    blocks = [block("b1", "1"), block("b2", "Grand total"), block("b3", "1,100.00")]
    assert [b["id"] for b in cited_blocks("Grand total 1,100.00", blocks)] == ["b2", "b3"]
