"""Which converted PDF each reading of a document was made from. A converted PDF is now kept
under its converter's version, so an upgraded LibreOffice converts again; page images come
from the PDF the newest reading used. Earlier readings keep NULL and the unversioned key.

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("document_versions", sa.Column("rendition_key", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("document_versions", "rendition_key")
