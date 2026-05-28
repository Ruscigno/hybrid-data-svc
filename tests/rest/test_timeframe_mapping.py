"""Verify the REST→storage timeframe translation in /v1/quote and
/v1/historical.

The spec exposes `15m` / `30m` (with the `m` suffix) but the internal
stack writes `15` / `30`. Without translation in the routes, queries
for the with-`m` form return 503 no_data even when bars exist.
"""

from __future__ import annotations

import time

import pytest

from data_svc.rest._timeframes import rest_to_storage


@pytest.mark.parametrize(
    "rest,storage",
    [
        ("1m", "1"),
        ("3m", "3"),
        ("5m", "5"),
        ("15m", "15"),
        ("30m", "30"),
        ("1h", "1h"),
        ("4h", "4h"),
        ("1D", "1D"),
        ("1W", "1W"),
        # Idempotent on unknown values too — fall through unchanged.
        ("unknown", "unknown"),
    ],
)
def test_rest_to_storage(rest: str, storage: str) -> None:
    assert rest_to_storage(rest) == storage


def test_quote_15m_translates_to_15_in_db(client, seed_asset, seed_bar):
    """Bar seeded under timeframe `15` (storage form) is returned by a
    REST request asking for `15m` (spec form)."""
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", currency="USD")
    now = int(time.time())
    seed_bar("BTC/USDT:USDT", "15", now - 60, close=42_000.5)

    r = client.get("/v1/quote/BINANCE:BTCUSDT?timeframe=15m")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["timeframe"] == "15m"
    assert body["price"] == 42_000.5


def test_historical_15m_translates_to_15_in_db(client, seed_asset, seed_bar):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", currency="USD")
    for i, ts in enumerate([100, 200, 300, 400]):
        seed_bar("BTC/USDT:USDT", "15", ts, close=10.0 + i)

    r = client.get("/v1/historical/BINANCE:BTCUSDT?from=100&to=350&interval=15m")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["interval"] == "15m"
    assert [b["ts"] for b in body["bars"]] == [100, 200, 300]
