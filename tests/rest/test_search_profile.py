from __future__ import annotations


def test_search_by_symbol_substring(client, seed_asset):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", name="Bitcoin")
    seed_asset("BINANCE:ETHUSDT", "ETH/USDT:USDT", name="Ethereum")

    r = client.get("/v1/search?q=btc")
    assert r.status_code == 200
    body = r.json()
    syms = [a["symbol"] for a in body["results"]]
    assert "BINANCE:BTCUSDT" in syms
    assert "BINANCE:ETHUSDT" not in syms


def test_search_by_name_substring(client, seed_asset):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", name="Bitcoin")
    seed_asset("NASDAQ:AAPL", "AAPL", name="Apple Inc.", asset_class="EQUITY")

    r = client.get("/v1/search?q=apple")
    assert r.status_code == 200
    syms = [a["symbol"] for a in r.json()["results"]]
    assert syms == ["NASDAQ:AAPL"]


def test_search_limit_clamping(client, seed_asset):
    for i in range(60):
        seed_asset(f"X:S{i:03d}", f"X/S{i}", name=f"Asset {i}")
    r = client.get("/v1/search?q=asset&limit=50")
    assert len(r.json()["results"]) == 50

    # > 50 → 422 (FastAPI validation rejects it before we see it).
    r = client.get("/v1/search?q=asset&limit=51")
    assert r.status_code == 422


def test_search_q_required(client):
    r = client.get("/v1/search")
    assert r.status_code == 422


def test_profile_happy_path(client, seed_asset):
    seed_asset(
        "NASDAQ:AAPL",
        "AAPL",
        name="Apple Inc.",
        exchange="NASDAQ",
        currency="USD",
        asset_class="EQUITY",
        asset_subclass="COMMON",
        isin="US0378331005",
        country="US",
    )
    r = client.get("/v1/profile/NASDAQ:AAPL")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "symbol": "NASDAQ:AAPL",
        "name": "Apple Inc.",
        "exchange": "NASDAQ",
        "currency": "USD",
        "assetClass": "EQUITY",
        "assetSubClass": "COMMON",
        "isin": "US0378331005",
        "country": "US",
    }


def test_profile_optional_fields_omitted(client, seed_asset):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", name="Bitcoin")
    body = client.get("/v1/profile/BINANCE:BTCUSDT").json()
    # Nullable fields not seeded → must be omitted (not null).
    assert "isin" not in body
    assert "country" not in body
    assert "assetSubClass" not in body


def test_profile_unknown_symbol_404(client):
    r = client.get("/v1/profile/UNKNOWN:XYZ")
    assert r.status_code == 404
