"""
⚠️  DEV / TESTNET ONLY — temporary endpoints for development & QA.

This file is completely self-contained.  To disable in production:
  1. Set DEBUG=false in .env  (the router will not be mounted)
  2. Optionally delete this file — the rest of the system is unaffected.

Endpoints:
  GET  /api/v1/dev/generate-wallet      — generate a fresh BIP44 testnet HD wallet
  POST /api/v1/dev/simulate-payment     — send real tBTC to an address via the node
  GET  /api/v1/dev/derive-address       — derive a testnet address from any xpub + index
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime
from decimal import Decimal

import bip32utils
from fastapi import APIRouter, HTTPException, Query, status, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from core.config import settings
from core.database import DbSession
from core.models import Invoice, Transaction, InvoiceStatus
from providers.bitcoin_rpc import BitcoinRPCClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dev", tags=["🔧 Dev / Testnet"])


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _b58encode(data: bytes) -> str:
    """Base58Check encode."""
    ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(data, "big")
    result = []
    while n:
        n, remainder = divmod(n, 58)
        result.append(ALPHABET[remainder])
    for byte in data:
        if byte == 0:
            result.append(ALPHABET[0])
        else:
            break
    return bytes(reversed(result)).decode()


def _derive_testnet_address(xpub: str, index: int, change: int = 0) -> str:
    """Derive a testnet P2PKH address at m/.../change/index from an xpub."""
    import hashlib

    account_key = bip32utils.BIP32Key.fromExtendedKey(xpub, public=True)
    change_node = account_key.ChildKey(change)
    child = change_node.ChildKey(index)

    pub = child.PublicKey()
    sha = hashlib.sha256(pub).digest()
    ripe = hashlib.new("ripemd160", sha).digest()
    versioned = bytes([0x6F]) + ripe                          # 0x6F = testnet P2PKH
    checksum = hashlib.sha256(hashlib.sha256(versioned).digest()).digest()[:4]
    return _b58encode(versioned + checksum)


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class NodeWalletResponse(BaseModel):
    address: str
    balance: float
    note: str = "Use this address to fund the local node via a testnet faucet."


class GenerateWalletResponse(BaseModel):
    seed_hex: str
    xpub: str
    xpriv: str
    first_address: str
    note: str = "⚠️  TESTNET ONLY — never use these keys on mainnet or in production"


class SimulatePaymentRequest(BaseModel):
    invoice_id: str | None = Field(None, description="ID инвойса для симуляции оплаты")
    amount_received: Decimal | None = Field(None, description="Сумма оплаты (в монетах)")
    
    address: str | None = Field(
        None,
        description="The invoice Bitcoin testnet address to send tBTC to",
    )
    amount_btc: Decimal | None = Field(
        None,
        gt=Decimal("0"),
        description="Amount in BTC to send (e.g. 0.0001)",
    )


class SimulatePaymentResponse(BaseModel):
    txid: str
    address: str
    amount_btc: Decimal
    note: str


class DeriveAddressResponse(BaseModel):
    xpub: str
    index: int
    change: int
    address: str


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/generate-wallet",
    response_model=GenerateWalletResponse,
    summary="[DEV] Generate a fresh BIP44 testnet HD wallet",
    description=(
        "Generates a brand-new BIP44 HD wallet for **BTC testnet** from a random seed.\n\n"
        "Use the returned `xpub` / `xpriv` pair to populate `BTC_XPUB` / `BTC_XPRIV` in `.env` "
        "when you need a clean wallet for testing the payout system.\n\n"
        "**⚠️ This endpoint is only available when `DEBUG=true`.**"
    ),
)
async def generate_wallet() -> GenerateWalletResponse:
    """
    Generate a fresh BIP44 HD wallet on BTC testnet.

    Path: m/44'/1'/0'  (BIP44, coin_type=1 for testnet)
    """
    try:
        seed = os.urandom(32)

        master = bip32utils.BIP32Key.fromEntropy(seed)
        purpose = master.ChildKey(44 + bip32utils.BIP32_HARDEN)
        coin = purpose.ChildKey(1 + bip32utils.BIP32_HARDEN)   # 1 = testnet
        account = coin.ChildKey(0 + bip32utils.BIP32_HARDEN)

        xpub: str = account.ExtendedKey(private=False)
        xpriv: str = account.ExtendedKey(private=True)
        first_address = _derive_testnet_address(xpub, index=0, change=0)

        logger.info("[DEV] Generated testnet wallet — first address: %s", first_address)

        return GenerateWalletResponse(
            seed_hex=seed.hex(),
            xpub=xpub,
            xpriv=xpriv,
            first_address=first_address,
        )
    except Exception as exc:
        logger.exception("[DEV] generate_wallet failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Wallet generation failed: {exc}")


@router.post(
    "/simulate-payment",
    response_model=SimulatePaymentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="[DEV] Simulate a user sending tBTC to an invoice address",
    description=(
        "Calls `sendtoaddress` on the connected **BTC testnet node** to send real testnet coins "
        "to the given address.\n\n"
        "The existing **Watcher → Ledger** pipeline picks up the transaction automatically — "
        "exactly the same flow as a real payment from a user.\n\n"
        "Equivalent to running:\n"
        "```bash\n"
        "bitcoin-cli -testnet -rpcuser=... -rpcpassword=... sendtoaddress <address> <amount>\n"
        "```\n\n"
        "**⚠️ This endpoint is only available when `DEBUG=true`.**"
    ),
)
async def simulate_payment(
    payload: SimulatePaymentRequest,
    db: DbSession,
) -> SimulatePaymentResponse:
    """
    Simulates an invoice payment by sending real tBTC via the Bitcoin node.
    
    If 'invoice_id' is provided, the transaction is sent to the invoice's derived address.
    If 'address' is provided, it sends to that specific address.
    In both cases, the Watcher/Ledger pipeline will process the transaction normally.
    """
    address = payload.address
    amount = payload.amount_btc or payload.amount_received

    # 1. Resolve address and amount from invoice_id if provided
    if payload.invoice_id:
        result = await db.execute(select(Invoice).where(Invoice.id == payload.invoice_id))
        invoice = result.scalar_one_or_none()
        if not invoice:
            raise HTTPException(status_code=404, detail=f"Invoice {payload.invoice_id} not found")
        
        address = invoice.address
        if not amount:
            amount = invoice.amount_expected

    # 2. Validation
    if not address or not amount:
        raise HTTPException(
            status_code=400, 
            detail="Specify either 'invoice_id' or 'address' + 'amount_btc'"
        )

    addr = str(address).strip()
    if not addr.startswith(("m", "n", "2", "tb1")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Address '{addr}' does not look like a BTC testnet address. "
                "Testnet addresses start with m, n, 2, or tb1."
            ),
        )

    # 3. Blockchain simulation
    rpc = BitcoinRPCClient()
    logger.info("[DEV] simulate_payment → sendtoaddress %s %s BTC", addr, amount)

    try:
        # Bitcoin Core sendtoaddress expects at most 8 decimal places
        amount_float = float(Decimal(str(amount)).quantize(Decimal("0.00000001")))
        txid: str = await rpc._call("sendtoaddress", addr, amount_float)
        
        return SimulatePaymentResponse(
            txid=txid,
            address=addr,
            amount_btc=amount,
            note="[SIMULATION] Real testnet transaction sent via node. Watcher will process it shortly."
        )
    except Exception as exc:
        error_msg = str(exc)
        logger.exception("[DEV] sendtoaddress failed: %s", error_msg)

        # Surface a helpful message for common node errors
        if "Insufficient funds" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Node wallet has insufficient testnet funds. "
                    "Fund it via https://coinfaucet.eu/en/btc-testnet/ or "
                    "https://testnet-faucet.mempool.co"
                ),
            )
        if "Invalid address" in error_msg or "does not refer" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Bitcoin node rejected address '{addr}': {error_msg}",
            )
        
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bitcoin node RPC error: {error_msg}",
        )


@router.get(
    "/derive-address",
    response_model=DeriveAddressResponse,
    summary="[DEV] Derive a BTC testnet address from any xpub",
    description=(
        "Utility endpoint: derives a P2PKH testnet address at path `m/.../change/index` "
        "from the given `xpub`.\n\n"
        "Useful for verifying your HD wallet derivation, inspecting invoice addresses, "
        "or testing with a custom xpub.\n\n"
        "**⚠️ This endpoint is only available when `DEBUG=true`.**"
    ),
)
async def derive_address(
    xpub: str = Query(..., description="Account-level extended public key (xpub...)"),
    index: int = Query(0, ge=0, description="BIP44 address_index"),
    change: int = Query(0, ge=0, le=1, description="0 = external (receiving), 1 = internal (change)"),
) -> DeriveAddressResponse:
    """Derive a BTC testnet P2PKH address at m/.../change/index."""
    try:
        address = _derive_testnet_address(xpub, index=index, change=change)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Failed to derive address: {exc}",
        )

    logger.debug("[DEV] derive_address xpub=%s… index=%d change=%d → %s", xpub[:20], index, change, address)

    return DeriveAddressResponse(xpub=xpub, index=index, change=change, address=address)


@router.get(
    "/node-wallet",
    response_model=NodeWalletResponse,
    summary="[DEV] Get the local node's wallet address and balance",
)
async def get_node_wallet():
    """Returns a new address from the node's wallet and the current balance."""
    rpc = BitcoinRPCClient()
    try:
        address = await rpc._call("getnewaddress", "dev-simulation")
        balance = await rpc._call("getbalance")
        return NodeWalletResponse(address=address, balance=float(balance))
    except Exception as exc:
        logger.exception("[DEV] get_node_wallet failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to query Bitcoin node: {exc}",
        )
