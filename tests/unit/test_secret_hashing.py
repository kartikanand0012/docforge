"""PINs and passwords: argon2id at OWASP's minimum for every new hash, older scrypt hashes
still checked (and marked for replacing), and the hash checked when there is nothing to check
made the same way, so a refusal takes as long either way."""

import docforge.auth
from docforge.review.signing import hash_pin, needs_rehash, verify_pin
from fakes import scrypt_hash

OWASP_MINIMUM = "$argon2id$v=19$m=19456,t=2,p=1$"


def test_a_new_hash_is_argon2id_at_owasps_minimum() -> None:
    stored = hash_pin("correct horse battery staple")

    assert stored.startswith(OWASP_MINIMUM)
    assert verify_pin("correct horse battery staple", stored)
    assert not verify_pin("correct horse battery stapler", stored)
    assert not needs_rehash(stored)


def test_an_older_scrypt_hash_is_still_checked_and_marked_for_replacing() -> None:
    stored = scrypt_hash("482913")

    assert verify_pin("482913", stored) and not verify_pin("482914", stored)
    assert needs_rehash(stored)


def test_a_weaker_argon2_hash_is_marked_for_replacing() -> None:
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1).hash("482913")

    assert verify_pin("482913", weak) and needs_rehash(weak)


def test_anything_else_is_never_right() -> None:
    for stored in ("", "plain", "scrypt$1$2", "$argon2id$v=19$m=19456,t=2,p=1$broken"):
        assert not verify_pin("482913", stored)


def test_the_hash_checked_for_an_unknown_person_costs_what_a_real_one_does() -> None:
    assert docforge.auth._DUMMY_PIN_HASH.startswith(OWASP_MINIMUM)
