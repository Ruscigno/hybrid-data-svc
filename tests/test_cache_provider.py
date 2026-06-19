"""Tests for provider-scoped BarCache reads and resolve_provider logic."""
from __future__ import annotations

import pytest

from data_svc.db.cache import BarCache


def test_seed_and_count_are_provider_scoped(pg_url, reset_db, seed_bar):
    cache = BarCache(pg_url)
    seed_bar("AAA", "1h", 3600, close=1.0, provider="tradingview")
    seed_bar("AAA", "1h", 3600, close=2.0, provider="yahoo")

    # Two providers hold a bar at the same (symbol, timeframe, ts); both persist.
    assert cache.bar_count("AAA", "1h", "tradingview") == 1
    assert cache.bar_count("AAA", "1h", "yahoo") == 1


def test_reads_filter_by_provider(pg_url, reset_db, seed_bar):
    cache = BarCache(pg_url)
    seed_bar("AAA", "1h", 3600, close=1.0, provider="tradingview")
    seed_bar("AAA", "1h", 3600, close=2.0, provider="yahoo")

    assert cache.read_bars("AAA", "1h", 10, "tradingview").iloc[-1]["close"] == 1.0
    assert cache.read_bars("AAA", "1h", 10, "yahoo").iloc[-1]["close"] == 2.0
    assert cache.latest_bar("AAA", "1h", "yahoo")["close"] == 2.0
    rows, _ = cache.get_bars_in_range("AAA", "1h", 0, 10_000, 10, "yahoo")
    assert rows[-1]["close"] == 2.0


def test_resolve_provider_precedence(pg_url, reset_db, seed_bar):
    cache = BarCache(pg_url)
    # Both present -> TradingView wins.
    seed_bar("AAA", "1h", 3600, provider="tradingview")
    seed_bar("AAA", "1h", 3600, provider="yahoo")
    assert cache.resolve_provider("AAA", "1h", "") == "tradingview"
    # Only Yahoo present -> falls back to Yahoo.
    seed_bar("BBB", "1h", 3600, provider="yahoo")
    assert cache.resolve_provider("BBB", "1h", "") == "yahoo"
    # Explicit request always wins, even past precedence.
    assert cache.resolve_provider("AAA", "1h", "yahoo") == "yahoo"
    # Nothing present -> default to the first precedence entry.
    assert cache.resolve_provider("ZZZ", "1h", "") == "tradingview"


def test_resolve_provider_rejects_unknown(pg_url, reset_db) -> None:
    cache = BarCache(pg_url)
    with pytest.raises(ValueError, match="unknown provider"):
        cache.resolve_provider("AAA", "1h", "bogus")


def test_assert_provider_schema_passes_on_migrated_db(pg_url: str) -> None:
    """The boot guard accepts a fully-migrated schema (004 applied by conftest)."""
    from data_svc.db.postgres import assert_provider_schema
    assert_provider_schema(pg_url)  # must not raise
