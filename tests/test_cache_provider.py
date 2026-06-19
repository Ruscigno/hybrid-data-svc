from __future__ import annotations

from data_svc.db.cache import BarCache


def test_seed_and_count_are_provider_scoped(pg_url, reset_db, seed_bar):
    cache = BarCache(pg_url)
    seed_bar("AAA", "1h", 3600, close=1.0, provider="tradingview")
    seed_bar("AAA", "1h", 3600, close=2.0, provider="yahoo")

    # Two providers hold a bar at the same (symbol, timeframe, ts); both persist.
    assert cache.bar_count("AAA", "1h", "tradingview") == 1
    assert cache.bar_count("AAA", "1h", "yahoo") == 1
