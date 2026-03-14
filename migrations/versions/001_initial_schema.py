import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def _create_enum_column_table(table_name: str, status_col: str, enum_type: str, default_val: str) -> None:
    op.execute(f"ALTER TABLE {table_name} ALTER COLUMN {status_col} DROP DEFAULT")
    op.execute(f"""
        ALTER TABLE {table_name}
            ALTER COLUMN {status_col} TYPE {enum_type}
            USING {status_col}::{enum_type}
    """)
    op.execute(f"ALTER TABLE {table_name} ALTER COLUMN {status_col} SET DEFAULT '{default_val}'::{enum_type}")


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE invoice_status AS ENUM ('NEW','PENDING','PARTIAL','PAID','EXPIRED');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE payout_status AS ENUM ('PENDING','PROCESSING','SENT','CONFIRMED','FAILED');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    op.create_table(
        "coins",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.create_index("ix_coins_symbol", "coins", ["symbol"], unique=True)

    op.create_table(
        "merchants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("api_key", sa.String(64), nullable=False),
        sa.Column("commission_pcent", sa.Numeric(5, 2), nullable=False, server_default="1.00"),
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_merchants_api_key", "merchants", ["api_key"], unique=True)

    op.create_table(
        "invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("coin_id", sa.Integer(),
                  sa.ForeignKey("coins.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("address", sa.String(128), nullable=False),
        sa.Column("amount_expected", sa.Numeric(28, 8), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),  # ← VARCHAR, no default
        sa.Column("derivation_index", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE invoices SET status = 'NEW' WHERE status = ''")
    _create_enum_column_table("invoices", "status", "invoice_status", "NEW")

    op.create_index("ix_invoices_coin_id", "invoices", ["coin_id"])
    op.create_index("ix_invoices_merchant_id", "invoices", ["merchant_id"])
    op.create_index("ix_invoices_address", "invoices", ["address"])
    op.create_index("ix_invoices_status", "invoices", ["status"])

    op.create_table(
        "balances",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("coin_id", sa.Integer(),
                  sa.ForeignKey("coins.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount_available", sa.Numeric(28, 8), nullable=False, server_default="0"),
        sa.Column("amount_locked", sa.Numeric(28, 8), nullable=False, server_default="0"),
        sa.UniqueConstraint("merchant_id", "coin_id", name="uq_balance_merchant_coin"),
    )
    op.create_index("ix_balances_merchant_id", "balances", ["merchant_id"])

    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("txid", sa.String(128), nullable=False),
        sa.Column("amount_received", sa.Numeric(28, 8), nullable=False),
        sa.Column("confirmations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credited", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_transactions_invoice_id", "transactions", ["invoice_id"])
    op.create_index("ix_transactions_txid", "transactions", ["txid"])

    op.create_table(
        "system_fee_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("coin_id", sa.Integer(),
                  sa.ForeignKey("coins.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("gross_amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("commission_pcent", sa.Numeric(5, 2), nullable=False),
        sa.Column("fee_amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("net_amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_system_fee_log_transaction_id", "system_fee_log", ["transaction_id"])
    op.create_index("ix_system_fee_log_merchant_id", "system_fee_log", ["merchant_id"])

    op.create_table(
        "utxos",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("txid", sa.String(128), nullable=False),
        sa.Column("vout", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("script_pubkey", sa.String(256), nullable=True),
        sa.Column("is_spent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("spent_by_payout_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("txid", "vout", name="uq_utxo_txid_vout"),
    )
    op.create_index("ix_utxos_invoice_id", "utxos", ["invoice_id"])
    op.create_index("ix_utxos_txid", "utxos", ["txid"])
    op.create_index("ix_utxos_is_spent", "utxos", ["is_spent"])
    op.create_index("ix_utxos_spent_by_payout_id", "utxos", ["spent_by_payout_id"])

    op.create_table(
        "payouts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("coin_id", sa.Integer(),
                  sa.ForeignKey("coins.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("destination_address", sa.String(128), nullable=False),
        sa.Column("amount_requested", sa.Numeric(28, 8), nullable=False),
        sa.Column("miner_fee", sa.Numeric(28, 8), nullable=True),
        sa.Column("amount_net", sa.Numeric(28, 8), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),  # ← VARCHAR, no default
        sa.Column("txid", sa.String(128), nullable=True),
        sa.Column("raw_tx_hex", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE payouts SET status = 'PENDING' WHERE status = ''")
    _create_enum_column_table("payouts", "status", "payout_status", "PENDING")

    op.create_index("ix_payouts_merchant_id", "payouts", ["merchant_id"])
    op.create_index("ix_payouts_status", "payouts", ["status"])
    op.create_index("ix_payouts_txid", "payouts", ["txid"])


def downgrade() -> None:
    op.drop_table("payouts")
    op.drop_table("utxos")
    op.drop_table("system_fee_log")
    op.drop_table("transactions")
    op.drop_table("balances")
    op.drop_table("invoices")
    op.drop_table("merchants")
    op.drop_table("coins")
    op.execute("DROP TYPE IF EXISTS payout_status")
    op.execute("DROP TYPE IF EXISTS invoice_status")
