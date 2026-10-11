"""PINs and passwords hashed with argon2id; older scrypt hashes replaced as they are used.

- `docforge_create_account` takes an argon2id hash. It still takes an scrypt one, so an API
  of the release before, still serving while this one starts, can make accounts; those are
  upgraded on their first sign-in like every other scrypt hash.
- `docforge_upgrade_secret` replaces one reviewer's hash, in the workspace in scope, and only
  from the hash it has now to an argon2id one. The application's role still cannot write a
  reviewer's hash itself.

Revision ID: 0031
Revises: 0030
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"
_CREATE = (
    "docforge_create_account(workspace text, workspace_label text, person text, "
    "account_email text, secret_hash text)"
)
_CREATE_BODY = """RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    new_tenant uuid;
    new_member uuid;
    normalised text := lower(btrim(account_email));
BEGIN
    IF NOT (ACCEPTED) THEN
        RAISE EXCEPTION 'an account''s password is stored hashed'
            USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO tenants (name, kind, display_name)
        VALUES (workspace, 'personal', workspace_label) RETURNING id INTO new_tenant;
    INSERT INTO reviewers (tenant_id, name, email, pin_hash, role, credential)
        VALUES (new_tenant, btrim(person), normalised, secret_hash, 'member', 'password')
        RETURNING id INTO new_member;
    INSERT INTO accounts (email, tenant_id, reviewer_id)
        VALUES (normalised, new_tenant, new_member);
    RETURN new_tenant;
END
$$"""
_ARGON2 = "secret_hash LIKE '$argon2id$%'"
_SCRYPT = "secret_hash LIKE 'scrypt$%'"

_UPGRADE = "docforge_upgrade_secret(reviewer uuid, old_hash text, new_hash text)"
_UPGRADE_BODY = f"""RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
UPDATE reviewers SET pin_hash = new_hash
WHERE id = reviewer AND tenant_id = {_TENANT} AND pin_hash = old_hash
    AND new_hash LIKE '$argon2id$%'
RETURNING true
$$"""  # noqa: S608 - fixed text, no input in it


def upgrade() -> None:
    accepted = f"{_ARGON2} OR {_SCRYPT}"
    op.execute(f"CREATE OR REPLACE FUNCTION {_CREATE} {_CREATE_BODY.replace('ACCEPTED', accepted)}")
    op.execute(f"CREATE FUNCTION {_UPGRADE} {_UPGRADE_BODY}")
    op.execute(f"REVOKE ALL ON FUNCTION {_UPGRADE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_UPGRADE} TO {_APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {_UPGRADE.split('(')[0]}")
    # argon2id hashes already stored stay; the release before cannot check them, so their
    # people need a new PIN or password from an administrator.
    op.execute(f"CREATE OR REPLACE FUNCTION {_CREATE} {_CREATE_BODY.replace('ACCEPTED', _SCRYPT)}")
