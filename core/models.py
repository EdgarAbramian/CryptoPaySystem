from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class InvoiceStatus(str, enum.Enum):
    NEW = "NEW"
    PENDING = "PENDING"  # tx seen on-chain, waiting for confirmations
    PARTIAL = "PARTIAL"  # partially paid
    PAID = "PAID"  # fully confirmed
    EXPIRED = "EXPIRED"


class Coin(Base):
    __tablename__ = "coins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    invoices: Mapped[list[Invoice]] = relationship("Invoice", back_populates="coin")
    balances: Mapped[list[Balance]] = relationship("Balance", back_populates="coin")

    def __repr__(self) -> str:
        return f"<Coin {self.symbol}>"


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    api_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    commission_pcent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("1.00"), nullable=False
    )
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    invoices: Mapped[list[Invoice]] = relationship("Invoice", back_populates="merchant")
    balances: Mapped[list[Balance]] = relationship("Balance", back_populates="merchant")

    def __repr__(self) -> str:
        return f"<Merchant {self.id}>"


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    coin_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("coins.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    address: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    amount_expected: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name="invoice_status"),
        default=InvoiceStatus.NEW,
        nullable=False,
        index=True,
    )
    derivation_index: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    coin: Mapped[Coin] = relationship("Coin", back_populates="invoices")
    merchant: Mapped[Merchant] = relationship("Merchant", back_populates="invoices")
    transactions: Mapped[list[Transaction]] = relationship(
        "Transaction", back_populates="invoice", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Invoice {self.id} status={self.status}>"


class Balance(Base):
    __tablename__ = "balances"
    __table_args__ = (
        UniqueConstraint("merchant_id", "coin_id", name="uq_balance_merchant_coin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    coin_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("coins.id", ondelete="CASCADE"), nullable=False
    )
    amount_available: Mapped[Decimal] = mapped_column(
        Numeric(28, 8), default=Decimal("0"), nullable=False
    )
    amount_locked: Mapped[Decimal] = mapped_column(
        Numeric(28, 8), default=Decimal("0"), nullable=False
    )

    merchant: Mapped[Merchant] = relationship("Merchant", back_populates="balances")
    coin: Mapped[Coin] = relationship("Coin", back_populates="balances")

    def __repr__(self) -> str:
        return f"<Balance merchant={self.merchant_id} coin={self.coin_id}>"


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    txid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    amount_received: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    confirmations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    credited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    invoice: Mapped[Invoice] = relationship("Invoice", back_populates="transactions")

    def __repr__(self) -> str:
        return f"<Transaction {self.txid[:16]}… conf={self.confirmations} credited={self.credited}>"


class SystemFeeLog(Base):
    __tablename__ = "system_fee_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    coin_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("coins.id", ondelete="RESTRICT"), nullable=False
    )
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    commission_pcent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<SystemFeeLog fee={self.fee_amount} net={self.net_amount}>"


class UTXO(Base):
    __tablename__ = "utxos"
    __table_args__ = (
        UniqueConstraint("txid", "vout", name="uq_utxo_txid_vout"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    txid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    vout: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    script_pubkey: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_spent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    # Which payout consumed this UTXO (NULL while unspent)
    spent_by_payout_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    invoice: Mapped[Invoice] = relationship("Invoice")

    def __repr__(self) -> str:
        return f"<UTXO {self.txid[:16]}…:{self.vout} amount={self.amount} spent={self.is_spent}>"


class PayoutStatus(str, enum.Enum):
    PENDING = "PENDING"  # created, balance locked
    PROCESSING = "PROCESSING"  # UTXOs selected, tx being built
    SENT = "SENT"  # broadcast to network
    CONFIRMED = "CONFIRMED"  # enough on-chain confirmations
    FAILED = "FAILED"  # broadcast failed or timed out


class Payout(Base):
    __tablename__ = "payouts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    coin_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("coins.id", ondelete="RESTRICT"), nullable=False
    )
    destination_address: Mapped[str] = mapped_column(String(128), nullable=False)
    amount_requested: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    miner_fee: Mapped[Decimal | None] = mapped_column(Numeric(28, 8), nullable=True)
    amount_net: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 8), nullable=True,
        comment="amount_requested - miner_fee; actual BTC sent to destination",
    )
    status: Mapped[PayoutStatus] = mapped_column(
        Enum(PayoutStatus, name="payout_status"),
        default=PayoutStatus.PENDING,
        nullable=False,
        index=True,
    )
    txid: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    raw_tx_hex: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Signed raw tx hex; cleared after broadcast"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    merchant: Mapped[Merchant] = relationship("Merchant")
    coin: Mapped[Coin] = relationship("Coin")

    def __repr__(self) -> str:
        return f"<Payout {self.id} status={self.status} amount={self.amount_requested}>"
