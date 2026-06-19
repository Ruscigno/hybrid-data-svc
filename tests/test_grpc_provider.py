"""Tests for provider-scoped gRPC handlers (GetRecentBars, GetBarsInRange, HealthCheck)."""
from __future__ import annotations

import grpc
import pytest

from data_svc.db.cache import BarCache
from data_svc.grpc_server.proto import bars_pb2 as _pb
from data_svc.grpc_server.service import BarServiceServicer


def _servicer(pg_url: str) -> BarServiceServicer:
    return BarServiceServicer(BarCache(pg_url), pg_url)


def test_get_recent_bars_precedence_and_override(pg_url, reset_db, seed_bar):
    seed_bar("AAA", "1h", 3600, close=1.0, provider="tradingview")
    seed_bar("AAA", "1h", 3600, close=2.0, provider="yahoo")
    svc = _servicer(pg_url)

    # No provider -> TradingView precedence.
    resp = svc.GetRecentBars(
        _pb.GetRecentBarsRequest(symbol="AAA", timeframe="1h", count=10), None)
    assert resp.bars[-1].close == 1.0

    # Explicit yahoo override.
    resp = svc.GetRecentBars(
        _pb.GetRecentBarsRequest(symbol="AAA", timeframe="1h", count=10, provider="yahoo"), None)
    assert resp.bars[-1].close == 2.0


def test_get_recent_bars_falls_back_to_yahoo(pg_url, reset_db, seed_bar):
    seed_bar("BBB", "1h", 3600, close=7.0, provider="yahoo")  # only yahoo
    svc = _servicer(pg_url)
    resp = svc.GetRecentBars(
        _pb.GetRecentBarsRequest(symbol="BBB", timeframe="1h", count=10), None)
    assert resp.bars[-1].close == 7.0


def test_get_bars_in_range_precedence_and_override(pg_url, reset_db, seed_bar) -> None:
    seed_bar("AAA", "1h", 3600, close=1.0, provider="tradingview")
    seed_bar("AAA", "1h", 3600, close=2.0, provider="yahoo")
    svc = _servicer(pg_url)

    # No provider -> TradingView wins (precedence).
    resp = svc.GetBarsInRange(
        _pb.GetBarsInRangeRequest(symbol="AAA", timeframe="1h", from_ts=0, to_ts=10000, limit=10),
        None,
    )
    assert resp.bars[-1].close == 1.0

    # Explicit yahoo override.
    resp = svc.GetBarsInRange(
        _pb.GetBarsInRangeRequest(
            symbol="AAA", timeframe="1h", from_ts=0, to_ts=10000, limit=10, provider="yahoo"
        ),
        None,
    )
    assert resp.bars[-1].close == 2.0


def test_health_check_precedence_and_override(pg_url, reset_db, seed_bar) -> None:
    # Both providers for AAA/1h.
    seed_bar("AAA", "1h", 3600, provider="tradingview")
    seed_bar("AAA", "1h", 3600, provider="yahoo")
    svc = _servicer(pg_url)

    # Default -> TradingView: ready=True, bars_available=1.
    resp = svc.HealthCheck(
        _pb.HealthRequest(symbol="AAA", timeframe="1h", min_bars=1), None
    )
    assert resp.ready is True
    assert resp.bars_available == 1

    # Explicit yahoo: also ready.
    resp = svc.HealthCheck(
        _pb.HealthRequest(symbol="AAA", timeframe="1h", min_bars=1, provider="yahoo"), None
    )
    assert resp.ready is True

    # Only yahoo for BBB: no-provider should fall back to yahoo -> ready.
    seed_bar("BBB", "1h", 3600, provider="yahoo")
    resp = svc.HealthCheck(
        _pb.HealthRequest(symbol="BBB", timeframe="1h", min_bars=1), None
    )
    assert resp.ready is True


class _FakeContext:
    """Minimal gRPC context that raises on abort so we can catch it in tests."""

    def abort(self, code: grpc.StatusCode, details: str) -> None:
        raise _AbortError(code, details)


class _AbortError(Exception):
    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        self.code = code
        self.details = details


def test_unknown_provider_aborts(pg_url, reset_db) -> None:
    svc = _servicer(pg_url)
    ctx = _FakeContext()
    with pytest.raises(_AbortError) as exc_info:
        svc.GetRecentBars(
            _pb.GetRecentBarsRequest(symbol="AAA", timeframe="1h", count=10, provider="bogus"),
            ctx,
        )
    assert exc_info.value.code == grpc.StatusCode.INVALID_ARGUMENT
