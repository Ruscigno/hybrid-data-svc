from __future__ import annotations


def test_historical_happy_path(client, seed_asset, seed_bar):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT")
    for i, ts in enumerate([100, 200, 300, 400]):
        seed_bar("BTC/USDT:USDT", "1D", ts, close=10.0 + i)

    r = client.get("/v1/historical/BINANCE:BTCUSDT?from=100&to=350&interval=1D")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "BINANCE:BTCUSDT"
    assert body["interval"] == "1D"
    assert [b["ts"] for b in body["bars"]] == [100, 200, 300]
    assert "truncated" not in body  # exclude_none drops the False


def test_historical_unknown_symbol_404(client):
    r = client.get("/v1/historical/UNKNOWN:XYZ?from=0&to=1")
    assert r.status_code == 404


def test_historical_bad_range_400(client, seed_asset):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT")
    r = client.get("/v1/historical/BINANCE:BTCUSDT?from=200&to=100")
    assert r.status_code == 400
    assert r.json()["error"] == "bad_range"


def test_historical_bad_interval_422(client, seed_asset):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT")
    r = client.get("/v1/historical/BINANCE:BTCUSDT?from=0&to=1&interval=7d")
    assert r.status_code == 422


def test_historical_empty_range_returns_empty_bars(client, seed_asset):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT")
    r = client.get("/v1/historical/BINANCE:BTCUSDT?from=0&to=1")
    assert r.status_code == 200
    assert r.json()["bars"] == []
