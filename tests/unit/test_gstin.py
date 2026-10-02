import pytest

from docforge.gstin import gstin_check_char, is_valid_gstin, make_gstin

# Widely published sample GSTINs with correct check characters.
KNOWN_VALID = ["27AAPFU0939F1ZV", "29AAGCB7383J1Z4"]


@pytest.mark.parametrize("gstin", KNOWN_VALID)
def test_known_valid_gstins_pass(gstin: str) -> None:
    assert is_valid_gstin(gstin)


@pytest.mark.parametrize("gstin", KNOWN_VALID)
def test_check_char_matches_known_gstins(gstin: str) -> None:
    assert gstin_check_char(gstin[:14]) == gstin[14]


@pytest.mark.parametrize(
    "gstin",
    [
        "27AAPFU0939F1ZW",  # wrong check character
        "27AAPFU0939F1Z",  # too short
        "27AAPFU0939F1ZVV",  # too long
        "27aapfu0939f1zv",  # lower case
        "2AAAPFU0939F1ZV",  # state code not numeric
        "27AAPFU0939F1YV",  # 14th character must be Z
        "",
    ],
)
def test_malformed_gstins_fail(gstin: str) -> None:
    assert not is_valid_gstin(gstin)


def test_single_character_substitution_is_detected() -> None:
    assert not is_valid_gstin("27AAPFU0938F1ZV")


def test_make_gstin_produces_a_valid_gstin() -> None:
    gstin = make_gstin(state_code="24", pan="ABCDE1234F")

    assert gstin.startswith("24ABCDE1234F1Z")
    assert is_valid_gstin(gstin)


def test_check_char_rejects_wrong_length() -> None:
    with pytest.raises(ValueError, match="14"):
        gstin_check_char("27AAPFU0939F1")


def test_check_char_rejects_invalid_characters() -> None:
    with pytest.raises(ValueError, match="character"):
        gstin_check_char("27AAPFU0939F1-")
