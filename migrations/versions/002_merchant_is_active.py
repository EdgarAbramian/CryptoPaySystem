"""002_merchant_is_active

Добавляет колонку is_active в таблицу merchants.

Revision ID: 002
Revises: 001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "merchants",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.create_index("ix_merchants_is_active", "merchants", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_merchants_is_active", table_name="merchants")
    op.drop_column("merchants", "is_active")