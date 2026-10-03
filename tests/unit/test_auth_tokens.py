"""API keys and session tokens: format, storage as a hash, and the permission each role has."""

import pytest

from docforge.auth import PERMISSIONS, Principal, new_token, parse_token, token_matches


def test_a_new_key_is_returned_once_and_stored_only_as_a_hash() -> None:
    token, prefix, digest = new_token("dfk")

    assert token.startswith(f"dfk_{prefix}_")
    assert len(prefix) == 12
    assert digest not in token and len(digest) == 64
    assert token_matches(token, digest)
    assert not token_matches(token[:-1] + ("A" if token[-1] != "A" else "B"), digest)


@pytest.mark.parametrize(
    "header",
    ["", "dfk_abc", "Basic abc", "dfk_ZZZZZZZZZZZZ_secret", "xyz_0123456789ab_" + "a" * 43],
)
def test_malformed_tokens_are_not_parsed(header: str) -> None:
    assert parse_token(header) is None


def test_a_well_formed_token_gives_its_kind_and_prefix() -> None:
    token, prefix, _ = new_token("dfs")

    assert parse_token(token) == ("dfs", prefix)


def test_each_role_has_only_its_permissions() -> None:
    assert PERMISSIONS["integrator"] == {"documents:read", "documents:write"}
    assert PERMISSIONS["reviewer"] == {"documents:read", "review"}
    assert PERMISSIONS["admin"] >= {"documents:read", "documents:write", "review", "admin"}


def test_a_principal_names_itself_in_the_audit_log() -> None:
    import uuid

    key = Principal(
        tenant_id=uuid.uuid4(),
        kind="api_key",
        subject_id=uuid.uuid4(),
        role="integrator",
        name="erp",
    )
    person = Principal(
        tenant_id=uuid.uuid4(),
        kind="session",
        subject_id=uuid.uuid4(),
        role="reviewer",
        name="Asha",
    )

    assert key.actor == f"key:{key.subject_id}"
    assert person.actor == f"reviewer:{person.subject_id}"
    assert key.can("documents:write") and not key.can("review")
