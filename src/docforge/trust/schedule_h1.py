"""Schedule H1 drugs on a purchase bill: which lines a chemist must enter in the H1 register.

Schedule H1 of the Drugs Rules, 1945 lists molecules sold only on prescription with each sale
recorded in a separate register kept three years. The list below is as notified, each entry
with the notification that added it and the day it came into force (checked 2026-10-17):

- 46 entries, G.S.R. 588(E) of 30 August 2013, in force 1 March 2014 (the gazette prints
  doripenem as "Donpenem" and faropenem as "Feropenem"; both spellings are matched);
- oxytocin, notification of 21 August 2018, in force 1 September 2018;
- tapentadol, G.S.R. 258(E) of 7 April 2021, in force 1 November 2021;
- oseltamivir and zanamivir, notification of 5 February 2024, in force that day;
- pregabalin, G.S.R. 377(E) of 13 May 2026, published 20 May 2026, in force 180 days later.

Draft G.S.R. 745(E) of 19 August 2026 (flupentixol, zopiclone, gabapentin, carisoprodol) is
not final and is not here. A molecule counts when its name is a whole word of the product's
name ("LEVOFLOXACIN 500 TAB", "Cefixime + Ofloxacin"); a brand name ("ALPRAX 0.5") names no
molecule, which is what a workspace's marked products are for (`docforge.h1_products`).
"""

import re
from dataclasses import dataclass
from datetime import date

_GAZETTE_2013 = "G.S.R. 588(E), 30 August 2013"
_IN_FORCE_2014 = date(2014, 3, 1)


@dataclass(frozen=True)
class H1Molecule:
    name: str  # as the schedule names it
    notification: str
    in_force: date
    spellings: tuple[str, ...] = ()  # other spellings matched besides the name's first word

    @property
    def words(self) -> tuple[str, ...]:
        return (self.name.split()[0].lower(), *self.spellings)


def _gazetted(name: str, *spellings: str) -> H1Molecule:
    return H1Molecule(name, _GAZETTE_2013, _IN_FORCE_2014, spellings)


SCHEDULE_H1: tuple[H1Molecule, ...] = (
    _gazetted("Alprazolam"),
    _gazetted("Balofloxacin"),
    _gazetted("Buprenorphine"),
    _gazetted("Capreomycin"),
    _gazetted("Cefdinir"),
    _gazetted("Cefditoren"),
    _gazetted("Cefepime"),
    _gazetted("Cefetamet"),
    _gazetted("Cefixime"),
    _gazetted("Cefoperazone"),
    _gazetted("Cefotaxime"),
    _gazetted("Cefpirome"),
    _gazetted("Cefpodoxime"),
    _gazetted("Ceftazidime"),
    _gazetted("Ceftibuten"),
    _gazetted("Ceftizoxime"),
    _gazetted("Ceftriaxone"),
    _gazetted("Chlordiazepoxide"),
    _gazetted("Clofazimine"),
    _gazetted("Codeine"),
    _gazetted("Cycloserine"),
    _gazetted("Diazepam"),
    _gazetted("Diphenoxylate"),
    _gazetted("Doripenem", "donpenem"),
    _gazetted("Ertapenem"),
    _gazetted("Ethambutol Hydrochloride"),
    _gazetted("Ethionamide"),
    _gazetted("Feropenem", "faropenem"),
    _gazetted("Gemifloxacin"),
    _gazetted("Imipenem"),
    _gazetted("Isoniazid"),
    _gazetted("Levofloxacin"),
    _gazetted("Meropenem"),
    _gazetted("Midazolam"),
    _gazetted("Moxifloxacin"),
    _gazetted("Nitrazepam"),
    _gazetted("Pentazocine"),
    _gazetted("Prulifloxacin"),
    _gazetted("Pyrazinamide"),
    _gazetted("Rifabutin"),
    _gazetted("Rifampicin", "rifampin"),
    H1Molecule(
        "Sodium Para-aminosalicylate",
        _GAZETTE_2013,
        _IN_FORCE_2014,
        ("para-aminosalicylate", "aminosalicylate", "para-aminosalicylic"),
    ),
    _gazetted("Sparfloxacin"),
    _gazetted("Thiacetazone", "thioacetazone"),
    _gazetted("Tramadol"),
    _gazetted("Zolpidem"),
    H1Molecule("Oxytocin", "notification of 21 August 2018", date(2018, 9, 1)),
    H1Molecule("Tapentadol", "G.S.R. 258(E), 7 April 2021", date(2021, 11, 1)),
    H1Molecule("Oseltamivir", "notification of 5 February 2024", date(2024, 2, 5)),
    H1Molecule("Zanamivir", "notification of 5 February 2024", date(2024, 2, 5)),
    H1Molecule("Pregabalin", "G.S.R. 377(E), 13 May 2026", date(2026, 11, 16)),
)

# "Sodium Para-aminosalicylate" is matched by its molecule's words, not by "sodium".
_WORDS = {
    word: molecule
    for molecule in SCHEDULE_H1
    for word in (molecule.spellings if molecule.name.startswith("Sodium") else molecule.words)
}
_PATTERN = re.compile(
    r"(?<![a-z])("
    + "|".join(re.escape(w) for w in sorted(_WORDS, key=len, reverse=True))
    + r")(?![a-z])",
    re.IGNORECASE,
)


def h1_molecule(product_name: str | None) -> H1Molecule | None:
    """The first Schedule H1 molecule named in a product's name, if any."""
    if not product_name:
        return None
    found = _PATTERN.search(product_name)
    return _WORDS[found.group(1).lower()] if found else None


def product_key(product_name: str) -> str:
    """A product's name as compared with the workspace's marked products: lower case, letters,
    digits and decimal points only ("Alprax 0.5 mg Tab." and "ALPRAX 0.5MG TAB" are one)."""
    return re.sub(r"[^a-z0-9.]", "", product_name.lower()).strip(".")
