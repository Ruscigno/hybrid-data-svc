from __future__ import annotations


def test_healthz_open_no_auth(client):
    """/healthz is always open even when auth would gate /v1/*."""
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"status", "db_reachable", "assets_loaded"}
    assert body["db_reachable"] is True
    # Empty catalog (conftest does not seed) → degraded status, assets_loaded False.
    assert body["assets_loaded"] is False
    assert body["status"] == "degraded"


def test_healthz_status_ok_after_assets(client, seed_asset):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT")
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["assets_loaded"] is True
    assert body["status"] == "ok"
