"""add_failed_to_invoice_status

Revision ID: 4ff0edd4fb0e
Revises: f76d484a7c1e
Create Date: 2026-03-22 13:42:57.281144

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4ff0edd4fb0e'
down_revision: Union[str, None] = 'f76d484a7c1e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot be run inside a transaction block in older Postgres,
    # but Alembic usually handles this if we use op.execute.
    # Note: If this fails, it might need to be run with autocommit=True.
    op.execute("ALTER TYPE invoice_status ADD VALUE 'FAILED'")


def downgrade() -> None:
    pass
