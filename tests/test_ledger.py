"""
Unit tests for Ledger — accounting, fee calculation, and processor logic.
No network, no database required.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fee calculation
# ---------------------------------------------------------------------------

class TestFeeCalculation:
    def _calc(self, gross_str: str, pct_str: str):
        from services.ledger.accounting import calculate_fee
        return calculate_fee(Decimal(gross_str), Decimal(pct_str))

    def test_one_percent(self):
        r = self._calc("1.00000000", "1.00")
        assert r.fee == Decimal("0.01000000")
        assert r.net == Decimal("0.99000000")
        assert r.gross == r.fee + r.net

    def test_zero_commission(self):
        r = self._calc("0.50000000", "0.00")
        assert r.fee == Decimal("0.00000000")
        assert r.net == Decimal("0.50000000")

    def test_rounding_down(self):
        # 0.00000001 BTC * 1% = 0.0000000001 → rounds to 0 (ROUND_DOWN)
        r = self._calc("0.00000001", "1.00")
        assert r.fee == Decimal("0.00000000")
        assert r.net == Decimal("0.00000001")

    def test_fee_plus_net_equals_gross(self):
        for gross, pct in [("0.12345678", "2.50"), ("1.00000000", "0.75"), ("0.00010000", "5.00")]:
            r = self._calc(gross, pct)
            assert r.fee + r.net == Decimal(gross), f"Failed for gross={gross} pct={pct}"

    def test_high_commission(self):
        r = self._calc("1.00000000", "99.99")
        assert r.fee == Decimal("0.99990000")
        assert r.net == Decimal("0.00010000")


# ---------------------------------------------------------------------------
# LedgerJob serialisation
# ---------------------------------------------------------------------------

class TestLedgerJobSerialization:
    def _make_job(self, **kwargs):
        from services.ledger.queue import LedgerJob
        defaults = dict(
            txid="abc123", invoice_id=str(uuid.uuid4()),
            address="tb1qtest", amount_sat=100_000,
            coin_symbol="BTC", merchant_id=str(uuid.uuid4()),
            confirmations=0, retry_count=0,
        )
        defaults.update(kwargs)
        return LedgerJob(**defaults)

    def test_roundtrip(self):
        from services.ledger.queue import LedgerJob
        job = self._make_job(retry_count=3)
        restored = LedgerJob.from_json(job.to_json())
        assert restored.txid == job.txid
        assert restored.retry_count == 3
        assert restored.amount_sat == 100_000

    def test_amount_coin_conversion(self):
        job = self._make_job(amount_sat=100_000_000)
        assert job.amount_coin == Decimal("1.00000000")

    def test_backwards_compat_no_retry_count(self):
        """Watcher pushes TxMatch without retry_count — must not crash."""
        import json
        from services.ledger.queue import LedgerJob
        payload = json.dumps({
            "txid": "abc", "invoice_id": str(uuid.uuid4()),
            "address": "addr", "amount_sat": 1000,
            "coin_symbol": "BTC", "merchant_id": str(uuid.uuid4()),
            "confirmations": 0,
        })
        job = LedgerJob.from_json(payload)
        assert job.retry_count == 0


# ---------------------------------------------------------------------------
# AlreadyCreditedError guard
# ---------------------------------------------------------------------------

class TestAlreadyCreditedGuard:
    def test_raises_if_credited(self):
        from services.ledger.accounting import AccountingEngine, AlreadyCreditedError
        session = MagicMock()
        engine = AccountingEngine(session)

        tx = MagicMock()
        tx.credited = True   # already done

        with pytest.raises(AlreadyCreditedError):
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                engine.credit(tx, MagicMock(), MagicMock(), MagicMock())
            )


# ---------------------------------------------------------------------------
# TxProcessor — max retries guard
# ---------------------------------------------------------------------------

class TestProcessorMaxRetries:
    @pytest.mark.asyncio
    async def test_discards_after_max_retries(self):
        from services.ledger.processor import TxProcessor
        from services.ledger.queue import LedgerJob, LedgerQueue

        queue = MagicMock(spec=LedgerQueue)
        processor = TxProcessor(queue)

        job = MagicMock(spec=LedgerJob)
        job.retry_count = 9999   # way over limit
        job.txid = "deadbeef"

        # Should return immediately without calling provider or DB
        await processor.process(job)
        queue.push_retry.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
