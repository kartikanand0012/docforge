"""Self-service accounts: one private workspace each, with one member who signs in by email.

- Workspaces say what kind they are (`personal` for an account, `organisation` otherwise)
  and may carry a name to show ("Asha's workspace"); their own name stays an internal one.
- A reviewer may be a `member` (an account's person), signs in with a PIN or a password
  (both kept as scrypt hashes), and may be a platform administrator (the owner).
- `accounts` holds each account's email across every workspace, so an email is one account.
  The application's role cannot read or write it: `docforge_account_tenant` finds the
  workspace of an email (nothing else), `docforge_create_account` makes a workspace, its
  member and the account together, and `docforge_signups_since` counts new accounts.
- The application's role may insert and update reviewers only in the columns it uses, so
  it can never make anyone a platform administrator (`python -m docforge.review` does that,
  as the owner).

Revision ID: 0027
Revises: 0026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)
_APP_ROLE = "docforge_app"
_REVIEWER_INSERT = (
    "id, tenant_id, name, email, pin_hash, role, shared, failed_attempts, locked_until, "
    "deactivated_at, created_at"
)
_REVIEWER_UPDATE = "failed_attempts, locked_until, deactivated_at"

_FUNCTIONS = {
    "docforge_account_tenant(account_email text)": (
        "RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public, pg_temp AS $$ "
        "SELECT tenant_id FROM accounts WHERE email = lower(btrim(account_email)) $$"
    ),
    "docforge_signups_since(since timestamptz)": (
        "RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public, pg_temp AS $$ "
        "SELECT count(*) FROM accounts WHERE created_at >= since $$"
    ),
    (
        "docforge_create_account(workspace text, workspace_label text, person text, "
        "account_email text, secret_hash text)"
    ): """RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    new_tenant uuid;
    new_member uuid;
    normalised text := lower(btrim(account_email));
BEGIN
    IF secret_hash NOT LIKE 'scrypt$%' THEN
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
$$""",
}


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "tenants", sa.Column("kind", sa.Text, nullable=False, server_default="organisation")
    )
    op.create_check_constraint("ck_tenants_kind", "tenants", "kind IN ('personal', 'organisation')")
    op.add_column("tenants", sa.Column("display_name", sa.Text))
    op.drop_constraint("ck_reviewers_role", "reviewers")
    op.create_check_constraint(
        "ck_reviewers_role", "reviewers", "role IN ('reviewer', 'admin', 'member')"
    )
    op.add_column(
        "reviewers", sa.Column("credential", sa.Text, nullable=False, server_default="pin")
    )
    op.create_check_constraint(
        "ck_reviewers_credential", "reviewers", "credential IN ('pin', 'password')"
    )
    op.add_column(
        "reviewers",
        sa.Column("platform_admin", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.execute(f"REVOKE INSERT, UPDATE ON reviewers FROM {_APP_ROLE}")
    op.execute(f"GRANT INSERT ({_REVIEWER_INSERT}) ON reviewers TO {_APP_ROLE}")
    op.execute(f"GRANT UPDATE ({_REVIEWER_UPDATE}) ON reviewers TO {_APP_ROLE}")

    op.create_table(
        "accounts",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column(
            "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("reviewer_id", _UUID, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("email", name="uq_accounts_email"),
        sa.UniqueConstraint("tenant_id", name="uq_accounts_tenant"),
        sa.ForeignKeyConstraint(
            ["reviewer_id", "tenant_id"],
            ["reviewers.id", "reviewers.tenant_id"],
            name="fk_accounts_reviewer",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("email = lower(btrim(email))", name="ck_accounts_email"),
    )
    op.create_index("ix_accounts_created_at", "accounts", ["created_at"])
    # Never read by the application itself: no rights, and a policy that admits nothing.
    op.execute(f"REVOKE ALL ON accounts FROM {_APP_ROLE}")
    op.execute("ALTER TABLE accounts ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY accounts_none ON accounts TO {_APP_ROLE} USING (false)")
    for signature, body in _FUNCTIONS.items():
        op.execute(f"CREATE FUNCTION {signature} {body}")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_APP_ROLE}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for signature in _FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature.split('(')[0]}")
    op.drop_table("accounts")
    op.execute(f"REVOKE INSERT ({_REVIEWER_INSERT}) ON reviewers FROM {_APP_ROLE}")
    op.execute(f"REVOKE UPDATE ({_REVIEWER_UPDATE}) ON reviewers FROM {_APP_ROLE}")
    op.execute(f"GRANT INSERT, UPDATE ON reviewers TO {_APP_ROLE}")
    op.drop_column("reviewers", "platform_admin")
    op.drop_constraint("ck_reviewers_credential", "reviewers")
    op.drop_column("reviewers", "credential")
    op.drop_constraint("ck_reviewers_role", "reviewers")
    op.create_check_constraint("ck_reviewers_role", "reviewers", "role IN ('reviewer', 'admin')")
    op.drop_column("tenants", "display_name")
    op.drop_constraint("ck_tenants_kind", "tenants")
    op.drop_column("tenants", "kind")
