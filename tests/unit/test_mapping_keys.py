"""V2 P1: how a supplier and an item are keyed for the workspace's mapping master, and how
mapping suggestions are ranked. Nothing here is ever applied without a person's click: these
only decide what is offered, and in what order."""

import time

from docforge.mappings.keys import gstin_key, item_key, name_tokens, supplier_name_key
from docforge.mappings.suggest import Candidate, Suggester


def test_a_gstin_is_keyed_upper_case_without_spaces() -> None:
    assert gstin_key(" 24aabfn7754k1z0 ") == "24AABFN7754K1Z0"
    assert gstin_key("24 AABFN 7754 K1Z0") == "24AABFN7754K1Z0"
    assert gstin_key("") is None
    assert gstin_key(None) is None


def test_printed_name_variations_of_a_supplier_have_one_key() -> None:
    keys = {
        supplier_name_key(name)
        for name in (
            "Navjivan Medical Agencies",
            "NAVJIVAN MEDICAL AGENCIES.",
            "Navjivan  Medical Agencies,",
            "navjivan medical agencies",
        )
    }
    assert len(keys) == 1


def test_an_item_is_its_printed_name_and_pack_not_its_hsn() -> None:
    plain = item_key("Amoxicillin Capsules IP 250mg", "10x10")
    assert plain == item_key("AMOXICILLIN CAPSULES IP 250 MG.", "10 X 10")
    assert plain != item_key("Amoxicillin Capsules IP 250mg", "1x10")  # another pack
    assert plain != item_key("Amoxicillin Capsules IP 500mg", "10x10")  # another strength
    assert item_key("Dolo 650 Tab", None) == item_key("Dolo 650 Tab", "")
    assert item_key("", "10x10") is None  # nothing to key by


def test_name_tokens_ignore_case_punctuation_and_the_forms_words() -> None:
    assert name_tokens("AMOXICILLIN CAPS. 250 MG") == name_tokens("Amoxicillin capsules 250mg")
    assert name_tokens("Dolo-650 Tablets") == name_tokens("DOLO 650 TAB")
    assert "pvt" not in name_tokens("ABC Pharma Pvt. Ltd.")


def _suppliers(*names: tuple[str, str | None]) -> Suggester:
    return Suggester([Candidate(name=n, gstin=g, source="master") for n, g in names])


def test_a_master_with_the_bills_gstin_is_offered_first() -> None:
    suggester = _suppliers(
        ("Navjivan Medicals", None),
        ("NAVJIVAN MED AGENCIES (AHMD)", "24AABFN7754K1Z0"),
    )
    (first, *_) = suggester.suggest("Navjivan Medical Agencies", gstin="24aabfn7754k1z0")

    assert first.name == "NAVJIVAN MED AGENCIES (AHMD)"
    assert first.why == "same GSTIN"
    assert first.score == 1.0


def test_the_same_name_in_another_case_and_punctuation_is_offered_as_the_same_name() -> None:
    suggester = _suppliers(("ABC PHARMA PVT. LTD.", None), ("XYZ Distributors", None))

    (first, *rest) = suggester.suggest("Abc Pharma Private Limited")

    assert (first.name, first.why) == ("ABC PHARMA PVT. LTD.", "same name")
    assert all(s.name != "XYZ Distributors" for s in rest)


def test_a_misspelt_name_is_still_offered_but_below_an_exact_one() -> None:
    suggester = _suppliers(("Shree Ganesh Pharma", None), ("Shri Ganesh Pharmaceuticals", None))

    found = suggester.suggest("Shree Ganesh Pharma")
    assert [s.name for s in found][:1] == ["Shree Ganesh Pharma"]

    fuzzy = suggester.suggest("Shree Ganesh Pharmaceutical")
    assert {s.name for s in fuzzy} >= {"Shri Ganesh Pharmaceuticals"}
    assert all(s.why in {"same name", "similar name", "same GSTIN"} for s in fuzzy)


def test_an_item_of_another_strength_is_ranked_below_the_right_strength() -> None:
    suggester = Suggester(
        [
            Candidate(name="AMOXICILLIN 500MG CAP 10X10", gstin=None, source="master"),
            Candidate(name="AMOXICILLIN 250MG CAP 10X10", gstin=None, source="master"),
        ]
    )

    found = suggester.suggest("Amoxicillin Capsules IP 250mg 10x10")

    assert found[0].name == "AMOXICILLIN 250MG CAP 10X10"
    assert len(found) == 1 or found[1].score < found[0].score / 2


def test_nothing_alike_is_offered_and_at_most_five_are() -> None:
    suggester = Suggester(
        [Candidate(name=f"Paracetamol {n}mg Tab", gstin=None, source="master") for n in range(9)]
        + [Candidate(name="Ganesh Traders", gstin=None, source="master")]
    )

    assert suggester.suggest("Insulin Injection 40IU") == []
    assert len(suggester.suggest("Paracetamol Tab")) <= 5


def test_suggestions_for_a_fifty_line_bill_from_twenty_thousand_names_take_under_two_seconds() -> (
    None
):
    words = ["Amoxicillin", "Cefixime", "Paracetamol", "Azithromycin", "Pantoprazole", "Dolo"]
    forms = ["Tab", "Cap", "Syp", "Inj"]
    names = [
        f"{words[i % 6]} {forms[i % 4]} {i}mg {1 + i % 10}x{10 + i % 5}" for i in range(20_000)
    ]
    started = time.perf_counter()
    suggester = Suggester([Candidate(name=n, gstin=None, source="master") for n in names])
    for i in range(50):
        suggester.suggest(f"{words[i % 6]} {forms[i % 4]} {i * 7}mg 10x10")
    assert time.perf_counter() - started < 2.0


def test_a_name_sharing_only_a_strength_is_not_offered() -> None:
    # The strength is the rarest word here, so on its own it would outweigh the rest.
    suggester = Suggester(
        [Candidate(name=f"Amoxicillin Capsules {n}mg", gstin=None, source="master")
         for n in range(100, 400)]
        + [Candidate(name=f"AZITHROMYCIN INJ {n}MG", gstin=None, source="master")
           for n in range(3, 300, 7)]
    )  # fmt: skip

    found = suggester.suggest("Amoxicillin Capsules 3mg")

    assert all(s.name != "AZITHROMYCIN INJ 3MG" for s in found)


def test_a_name_sharing_only_a_pack_is_not_offered() -> None:
    suggester = Suggester([Candidate(name="Cefixime Syp 10x10", gstin=None, source="master")])

    assert suggester.suggest("Amoxicillin Capsules 10x10") == []
