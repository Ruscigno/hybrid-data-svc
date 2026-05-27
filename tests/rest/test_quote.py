from __future__ import annotations

import time


def test_quote_happy_path(client, seed_asset, seed_bar):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", currency="USD")
    now = int(time.time())
    seed_bar("BTC/USDT:USDT", "1D", now - 60, close=42_000.5)

    r = client.get("/v1/quote/BINANCE:BTCUSDT")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "symbol": "BINANCE:BTCUSDT",
        "timeframe": "1D",
        "price": 42_000.5,
        "currency": "USD",
        "ts": now - 60,
        "stale": False,
    }


def test_quote_unknown_symbol_404(client):
    r = client.get("/v1/quote/UNKNOWN:XYZ")
    assert r.status_code == 404
    body = r.json()
    assert body == {"error": "unknown_symbol", "message": "UNKNOWN:XYZ is not in the asset catalog"}


def test_quote_no_data_503(client, seed_asset):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT")
    r = client.get("/v1/quote/BINANCE:BTCUSDT")
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "no_data"


def test_quote_stale_flag_true(client, seed_asset, seed_bar):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT")
    old_ts = int(time.time()) - 10_000
    seed_bar("BTC/USDT:USDT", "1D", old_ts, close=10.0)

    r = client.get("/v1/quote/BINANCE:BTCUSDT?max_age_seconds=60")
    assert r.status_code == 200
    body = r.json()
    assert body["stale"] is True
    assert body["ts"] == old_ts


def test_quote_timeframe_override(client, seed_asset, seed_bar):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT")
    now = int(time.time())
    seed_bar("BTC/USDT:USDT", "1h", now - 60, close=1.0)
    seed_bar("BTC/USDT:USDT", "1D", now - 60, close=2.0)

    assert client.get("/v1/quote/BINANCE:BTCUSDT").json()["price"] == 2.0
    assert client.get("/v1/quote/BINANCE:BTCUSDT?timeframe=1h").json()["price"] == 1.0


def test_quote_bad_symbol_format_422(client):
    """Symbol path-param must match the documented pattern."""
    r = client.get("/v1/quote/lowercase:symbol")
    assert r.status_code == 422
