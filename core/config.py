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

    # ── Application ──────────────────────────────────────────────────────────
    app_name: str = "CryptoPayments"
    debug: bool = False

    # Admin API secret — если пустой, admin эндпоинты недоступны
    admin_secret: str = Field(
        default="",
        alias="ADMIN_SECRET",
        description="Secret for admin-only API endpoints. Must be set to enable admin API.",
    )

    # ── PostgreSQL ───────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/crypto_payments",
        alias="DATABASE_URL",
    )
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ── Redis ────────────────────────────────────────────────────────────────
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    redis_queue_tx_processing: str = Field(default="tx_processing", alias="REDIS_QUEUE_TX_PROCESSING")
    redis_queue_tx_retry: str = Field(default="tx_retry", alias="REDIS_QUEUE_TX_RETRY")
    redis_channel_invoice_created: str = Field(default="invoice_created", alias="REDIS_CHANNEL_INVOICE_CREATED")

    # ── Bitcoin ──────────────────────────────────────────────────────────────
    btc_xpub: str = Field(default="", alias="BTC_XPUB")
    btc_xpriv: str = Field(default="", alias="BTC_XPRIV")
    btc_testnet: bool = Field(default=True, alias="BTC_TESTNET")

    # ── Bitcoin RPC ──────────────────────────────────────────────────────────
    bitcoin_rpc_url: str = Field(default="http://127.0.0.1:18332", alias="BITCOIN_RPC_URL")
    bitcoin_rpc_user: str = Field(default="rpcuser", alias="BITCOIN_RPC_USER")
    bitcoin_rpc_password: str = Field(default="rpcpassword", alias="BITCOIN_RPC_PASSWORD")

    # ── Bitcoin ZMQ ──────────────────────────────────────────────────────────
    bitcoin_zmq_raw_tx: str = Field(default="tcp://127.0.0.1:28333", alias="BITCOIN_ZMQ_RAW_TX")
    bitcoin_zmq_raw_block: str = Field(default="tcp://127.0.0.1:28332", alias="BITCOIN_ZMQ_RAW_BLOCK")

    # ── Watcher ──────────────────────────────────────────────────────────────
    watcher_poll_interval: int = Field(default=30, alias="WATCHER_POLL_INTERVAL")
    required_confirmations: int = Field(default=1, alias="REQUIRED_CONFIRMATIONS")
    zmq_reconnect_delay: float = Field(default=5.0, alias="ZMQ_RECONNECT_DELAY")

    # ── Ledger ───────────────────────────────────────────────────────────────
    ledger_required_confirmations: int = Field(default=3, alias="LEDGER_REQUIRED_CONFIRMATIONS")
    ledger_retry_delay: int = Field(default=60, alias="LEDGER_RETRY_DELAY")
    ledger_max_retries: int = Field(default=144, alias="LEDGER_MAX_RETRIES")
    ledger_consumer_timeout: float = Field(default=2.0, alias="LEDGER_CONSUMER_TIMEOUT")
    ledger_concurrency: int = Field(default=4, alias="LEDGER_CONCURRENCY")

    # ── Payout ───────────────────────────────────────────────────────────────
    payout_change_index: int = Field(default=1_000_000, alias="PAYOUT_CHANGE_INDEX")
    payout_fee_rate_sat_vbyte: int = Field(default=10, alias="PAYOUT_FEE_RATE_SAT_VBYTE")
    payout_min_amount_sat: int = Field(default=10_000, alias="PAYOUT_MIN_AMOUNT_SAT")
    payout_dust_threshold_sat: int = Field(default=546, alias="PAYOUT_DUST_THRESHOLD_SAT")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()