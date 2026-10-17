"""V2 3.6: Schedule H1 molecules named in a bill line's product, as notified and in force."""

from datetime import date

import pytest

from docforge.trust.schedule_h1 import SCHEDULE_H1, h1_molecule, product_key


def test_the_schedule_has_the_46_gazetted_entries_and_the_five_added_since() -> None:
    names = [m.name for m in SCHEDULE_H1]

    assert len(names) == len(set(names)) == 51
    assert sum(m.in_force == date(2014, 3, 1) for m in SCHEDULE_H1) == 46
    added = {m.name: m.in_force for m in SCHEDULE_H1 if m.in_force != date(2014, 3, 1)}
    assert added == {
        "Oxytocin": date(2018, 9, 1),
        "Tapentadol": date(2021, 11, 1),
        "Oseltamivir": date(2024, 2, 5),
        "Zanamivir": date(2024, 2, 5),
        "Pregabalin": date(2026, 11, 16),
    }
    # The 2026 draft is not final.
    for draft in ("Flupentixol", "Zopiclone", "Gabapentin", "Carisoprodol"):
        assert draft not in names


@pytest.mark.parametrize(
    ("product", "molecule"),
    [
        ("LEVOFLOXACIN 500MG TAB", "Levofloxacin"),
        ("Alprazolam 0.25 mg", "Alprazolam"),
        ("Cefixime + Ofloxacin Tablets", "Cefixime"),
        ("AMOXYCILLIN/CEFIXIME-200", "Cefixime"),
        ("Tramadol HCl 50mg Cap", "Tramadol"),
        ("Ethambutol 800", "Ethambutol Hydrochloride"),
        ("Rifampin 450", "Rifampicin"),
        ("Faropenem 200 tab", "Feropenem"),
        ("Sodium aminosalicylate granules", "Sodium Para-aminosalicylate"),
        ("Codeine Phosphate syrup 100ml", "Codeine"),
        ("Pregabalin 75 caps", "Pregabalin"),
        ("Moxifloxacin eye drops 5ml", "Moxifloxacin"),
    ],
)
def test_a_molecule_named_in_the_product_is_found(product: str, molecule: str) -> None:
    found = h1_molecule(product)

    assert found is not None
    assert found.name == molecule


@pytest.mark.parametrize(
    "product",
    [
        "ALPRAX 0.5 TAB",  # a brand: no molecule printed
        "Paracetamol 500",
        "Ofloxacin 200",  # not on the schedule
        "Gabapentin 300",  # a draft, not final
        "Sodium chloride 0.9%",  # "sodium" is not the molecule
        "Diazepamine 5",  # not a whole word
        "",
        None,
    ],
)
def test_nothing_else_is_taken_for_h1(product: str | None) -> None:
    assert h1_molecule(product) is None


def test_every_entry_is_found_by_its_own_name() -> None:
    for molecule in SCHEDULE_H1:
        assert h1_molecule(f"{molecule.name} 10 mg") == molecule


def test_a_product_is_compared_without_case_spaces_or_punctuation() -> None:
    assert product_key("Alprax 0.5 mg Tab.") == product_key("ALPRAX 0.5MG TAB") == "alprax0.5mgtab"
    assert product_key("Alprax 0.25 mg Tab") != product_key("Alprax 0.5 mg Tab")
