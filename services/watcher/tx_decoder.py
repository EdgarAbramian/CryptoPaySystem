"""
Pure-Python Bitcoin raw transaction decoder.

We use this to parse rawtx messages from Bitcoin Core's ZMQ interface
**without** making an RPC round-trip for every transaction.  The RPC
`decoderawtransaction` call is used only as a fallback for complex script
types we don't handle here.

Reference: https://en.bitcoin.it/wiki/Protocol_documentation#tx
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TxOutput:
    value_sat: int          # satoshis
    script_pubkey: bytes
    testnet: bool = False

    @property
    def address(self) -> str | None:
        """Best-effort address extraction from scriptPubKey."""
        return _script_to_address(self.script_pubkey, self.testnet)


@dataclass
class TxInput:
    prev_txid: str
    prev_vout: int
    script_sig: bytes
    sequence: int


@dataclass
class RawTx:
    txid: str
    version: int
    locktime: int
    inputs: list[TxInput] = field(default_factory=list)
    outputs: list[TxOutput] = field(default_factory=list)
    is_segwit: bool = False
    testnet: bool = False

    def output_addresses(self) -> list[str]:
        """Return all non-None addresses from outputs."""
        return [a for o in self.outputs if (a := o.address) is not None]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class _Reader:
    """Cursor over a bytes buffer."""

    __slots__ = ("_data", "_pos")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, n: int) -> bytes:
        chunk = self._data[self._pos : self._pos + n]
        if len(chunk) < n:
            raise ValueError(f"Unexpected end of data at pos {self._pos}")
        self._pos += n
        return chunk

    def read_varint(self) -> int:
        first = self.read(1)[0]
        if first < 0xFD:
            return first
        if first == 0xFD:
            return struct.unpack_from("<H", self.read(2))[0]
        if first == 0xFE:
            return struct.unpack_from("<I", self.read(4))[0]
        return struct.unpack_from("<Q", self.read(8))[0]

    def read_u32(self) -> int:
        return struct.unpack_from("<I", self.read(4))[0]

    def read_i32(self) -> int:
        return struct.unpack_from("<i", self.read(4))[0]

    def read_u64(self) -> int:
        return struct.unpack_from("<Q", self.read(8))[0]

    @property
    def pos(self) -> int:
        return self._pos


def decode_raw_tx(raw: bytes, testnet: bool = False) -> RawTx:
    """
    Decode a raw Bitcoin transaction (legacy or segwit).

    Args:
        raw: Raw bytes as received from ZMQ rawtx topic.

    Returns:
        Populated :class:`RawTx` instance.
    """
    import hashlib

    r = _Reader(raw)
    version = r.read_i32()

    # Detect SegWit marker / flag (BIP141)
    is_segwit = False
    marker = raw[r.pos : r.pos + 2]
    if marker == b"\x00\x01":
        is_segwit = True
        r.read(2)  # consume marker + flag

    # Inputs
    in_count = r.read_varint()
    inputs: list[TxInput] = []
    for _ in range(in_count):
        prev_txid_le = r.read(32)
        prev_txid = prev_txid_le[::-1].hex()   # little-endian → big-endian hex
        prev_vout = r.read_u32()
        script_len = r.read_varint()
        script_sig = r.read(script_len)
        sequence = r.read_u32()
        inputs.append(TxInput(prev_txid, prev_vout, script_sig, sequence))

    # Outputs
    out_count = r.read_varint()
    outputs: list[TxOutput] = []
    for _ in range(out_count):
        value_sat = r.read_u64()
        script_len = r.read_varint()
        script_pubkey = r.read(script_len)
        outputs.append(TxOutput(value_sat, script_pubkey, testnet=testnet))

    # SegWit witness data (skip, not needed for address extraction)
    if is_segwit:
        for _ in range(in_count):
            stack_items = r.read_varint()
            for _ in range(stack_items):
                item_len = r.read_varint()
                r.read(item_len)

    locktime = r.read_u32()

    # Compute txid (double-SHA256 of the non-witness serialisation)
    # For segwit we need to reconstruct the legacy serialisation.
    if is_segwit:
        txid_bytes = _legacy_serialise(version, inputs, outputs, locktime)
    else:
        txid_bytes = raw

    txid = hashlib.sha256(hashlib.sha256(txid_bytes).digest()).digest()[::-1].hex()

    return RawTx(
        txid=txid,
        version=version,
        locktime=locktime,
        inputs=inputs,
        outputs=outputs,
        is_segwit=is_segwit,
        testnet=testnet,
    )


def _legacy_serialise(
    version: int,
    inputs: list[TxInput],
    outputs: list[TxOutput],
    locktime: int,
) -> bytes:
    """Re-serialise without witness data to compute the txid."""
    buf = bytearray()
    buf += struct.pack("<i", version)
    buf += _encode_varint(len(inputs))
    for inp in inputs:
        buf += bytes.fromhex(inp.prev_txid)[::-1]
        buf += struct.pack("<I", inp.prev_vout)
        buf += _encode_varint(len(inp.script_sig))
        buf += inp.script_sig
        buf += struct.pack("<I", inp.sequence)
    buf += _encode_varint(len(outputs))
    for out in outputs:
        buf += struct.pack("<Q", out.value_sat)
        buf += _encode_varint(len(out.script_pubkey))
        buf += out.script_pubkey
    buf += struct.pack("<I", locktime)
    return bytes(buf)


def _encode_varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


# ---------------------------------------------------------------------------
# scriptPubKey → address
# ---------------------------------------------------------------------------

def _script_to_address(script: bytes, testnet: bool = False) -> str | None:
    """
    Extract a Bitcoin address from a scriptPubKey.

    Supports:
      P2PKH  : OP_DUP OP_HASH160 <20-byte-hash> OP_EQUALVERIFY OP_CHECKSIG
      P2SH   : OP_HASH160 <20-byte-hash> OP_EQUAL
      P2WPKH : OP_0 <20-byte-hash>
      P2WSH  : OP_0 <32-byte-hash>
    """
    # Version bytes: mainnet P2PKH=0x00, testnet P2PKH=0x6F
    #                mainnet P2SH =0x05, testnet P2SH =0xC4
    p2pkh_ver = b"\x6f" if testnet else b"\x00"
    p2sh_ver  = b"\xc4" if testnet else b"\x05"
    bech32_hrp = "tb" if testnet else "bc"

    if len(script) == 25 and script[:3] == b"\x76\xa9\x14" and script[23:] == b"\x88\xac":
        # P2PKH
        return _base58check(p2pkh_ver + script[3:23])

    if len(script) == 23 and script[0:2] == b"\xa9\x14" and script[22] == 0x87:
        # P2SH
        return _base58check(p2sh_ver + script[2:22])

    if len(script) == 22 and script[0:2] == b"\x00\x14":
        # P2WPKH (bech32)
        return _bech32_encode(bech32_hrp, 0, script[2:])

    if len(script) == 34 and script[0:2] == b"\x00\x20":
        # P2WSH (bech32)
        return _bech32_encode(bech32_hrp, 0, script[2:])

    return None  # OP_RETURN, bare multisig, etc.


# ---------------------------------------------------------------------------
# Minimal Base58Check encoder
# ---------------------------------------------------------------------------

_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58check(payload: bytes) -> str:
    import hashlib
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    data = payload + checksum
    # Count leading zero bytes
    count = 0
    for byte in data:
        if byte == 0:
            count += 1
        else:
            break
    n = int.from_bytes(data, "big")
    chars = []
    while n:
        n, rem = divmod(n, 58)
        chars.append(_B58_ALPHABET[rem : rem + 1])
    result = b"1" * count + b"".join(reversed(chars))
    return result.decode("ascii")


# ---------------------------------------------------------------------------
# Minimal bech32 encoder (BIP173)
# ---------------------------------------------------------------------------

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_GENERATOR = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]


def _bech32_polymod(values: list[int]) -> int:
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= _BECH32_GENERATOR[i] if ((b >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_create_checksum(hrp: str, data: list[int]) -> list[int]:
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _convertbits(data: bytes, frombits: int, tobits: int, pad: bool = True) -> list[int]:
    acc, bits, ret, maxv = 0, 0, [], (1 << tobits) - 1
    for value in data:
        acc = ((acc << frombits) | value) & 0xFFFFFFFF
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def _bech32_encode(hrp: str, witver: int, witprog: bytes) -> str | None:
    data = [witver] + _convertbits(witprog, 8, 5)
    checksum = _bech32_create_checksum(hrp, data)
    return hrp + "1" + "".join(_BECH32_CHARSET[d] for d in data + checksum)