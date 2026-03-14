"""
Centralised settings loaded from environment variables / .env file.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "CryptoPayments"
    debug: bool = False

    # PostgreSQL
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/crypto_payments",
        alias="DATABASE_URL",
    )
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # Redis queues
    redis_queue_tx_processing: str = Field(
        default="tx_processing",
        alias="REDIS_QUEUE_TX_PROCESSING",
    )
    redis_queue_tx_retry: str = Field(
        default="tx_retry",
        alias="REDIS_QUEUE_TX_RETRY",
    )

    # Bitcoin (Testnet)
    btc_xpub: str = Field(
        default="",
        alias="BTC_XPUB",
        description="Extended public key for HD wallet derivation (BIP44).",
    )
    btc_testnet: bool = Field(default=True, alias="BTC_TESTNET")

    # Bitcoin Node RPC
    bitcoin_rpc_url: str = Field(
        default="http://127.0.0.1:18332",
        alias="BITCOIN_RPC_URL",
        description="Bitcoin Core JSON-RPC endpoint (testnet default port 18332).",
    )
    bitcoin_rpc_user: str = Field(default="rpcuser", alias="BITCOIN_RPC_USER")
    bitcoin_rpc_password: str = Field(default="rpcpassword", alias="BITCOIN_RPC_PASSWORD")

    # Bitcoin Node ZMQ
    bitcoin_zmq_raw_tx: str = Field(
        default="tcp://127.0.0.1:28333",
        alias="BITCOIN_ZMQ_RAW_TX",
        description="ZMQ endpoint publishing raw transactions (zmqpubrawtx).",
    )
    bitcoin_zmq_raw_block: str = Field(
        default="tcp://127.0.0.1:28332",
        alias="BITCOIN_ZMQ_RAW_BLOCK",
        description="ZMQ endpoint publishing raw blocks (zmqpubrawblock).",
    )

    # Watcher
    watcher_poll_interval: int = Field(
        default=30,
        alias="WATCHER_POLL_INTERVAL",
        description="How often (seconds) the watcher polls for new transactions.",
    )
    required_confirmations: int = Field(
        default=1,
        alias="REQUIRED_CONFIRMATIONS",
        description="Number of confirmations to mark an invoice as PAID.",
    )
    zmq_reconnect_delay: float = Field(
        default=5.0,
        alias="ZMQ_RECONNECT_DELAY",
        description="Seconds to wait before reconnecting ZMQ on failure.",
    )

    # Ledger / Processor
    ledger_required_confirmations: int = Field(
        default=3,
        alias="LEDGER_REQUIRED_CONFIRMATIONS",
        description="Confirmations needed to mark invoice PAID and credit balance.",
    )
    ledger_retry_delay: int = Field(
        default=60,
        alias="LEDGER_RETRY_DELAY",
        description="Seconds to wait before re-checking an unconfirmed tx.",
    )
    ledger_max_retries: int = Field(
        default=144,
        alias="LEDGER_MAX_RETRIES",
        description="Max re-check attempts (~144 = 24 h at 10 min/block).",
    )
    ledger_consumer_timeout: float = Field(
        default=2.0,
        alias="LEDGER_CONSUMER_TIMEOUT",
        description="BRPOP block timeout in seconds.",
    )
    ledger_concurrency: int = Field(
        default=4,
        alias="LEDGER_CONCURRENCY",
        description="Max concurrent tx-processing coroutines.",
    )

    # Payout Service
    btc_xpriv: str = Field(
        default="",
        alias="BTC_XPRIV",
        description="Account-level extended PRIVATE key for signing payout txns.",
    )
    payout_change_index: int = Field(
        default=1_000_000,
        alias="PAYOUT_CHANGE_INDEX",
        description="BIP44 index used for internal change outputs (change=1 branch).",
    )
    payout_fee_rate_sat_vbyte: int = Field(
        default=10,
        alias="PAYOUT_FEE_RATE_SAT_VBYTE",
        description="Sat/vByte to use when estimating miner fee for payouts.",
    )
    payout_min_amount_sat: int = Field(
        default=10_000,
        alias="PAYOUT_MIN_AMOUNT_SAT",
        description="Minimum payout in satoshis (dust protection).",
    )
    payout_dust_threshold_sat: int = Field(
        default=546,
        alias="PAYOUT_DUST_THRESHOLD_SAT",
        description="Outputs below this (satoshis) are considered dust and omitted.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
