"""What the database itself holds true of accounts, whatever the application sends.

- One member per workspace: an account's workspace has one person in it
  (`uq_reviewers_one_member`, a unique index on the workspace over member rows only).
- An account's email is an address, as the application checks it (one @, no space, a dot in
  the domain), of at most 254 characters (`ck_accounts_email_format`).

Revision ID: 0035
Revises: 0034
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# accounts.EMAIL's pattern, in POSIX classes.
_EMAIL = "length(email) <= 254 AND email ~ '^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:].]+$'"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_index(
        "uq_reviewers_one_member",
        "reviewers",
        ["tenant_id"],
        unique=True,
        postgresql_where="role = 'member'",
    )
    op.create_check_constraint("ck_accounts_email_format", "accounts", _EMAIL)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_accounts_email_format", "accounts")
    op.drop_index("uq_reviewers_one_member", "reviewers")
