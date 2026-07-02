"""Tests for /v1/assets (Phase 3 catalog management).

Mirrors the spec's acceptance criteria #7 in docs/hybrid-data-svc-rest-api-spec.md.
The fakes in tests/conftest.py exercise the real AssetsRepo (against the
ephemeral Postgres), so these tests cover the JOIN/aggregation SQL and the
validation rules — not just the route adapter.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# GET /v1/assets — listing
# ---------------------------------------------------------------------------


def test_list_empty(client):
    r = client.get("/v1/assets")
    assert r.status_code == 200
    body = r.json()
    assert body["assets"] == []
    # nextCursor omitted/None on an empty page.
    assert "nextCursor" not in body or body["nextCursor"] is None


def test_list_pagination(client, seed_asset):
    # 250 assets, 100 per page → 3 pages (100, 100, 50). nextCursor is
    # absent on the final page.
    for i in range(250):
        seed_asset(f"X:S{i:03d}", f"X/S{i:03d}", name=f"Asset {i}")

    seen = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = client.get("/v1/assets", params=params)
        assert r.status_code == 200
        body = r.json()
        pages += 1
        seen.extend(a["asset"]["symbol"] for a in body["assets"])
        cursor = body.get("nextCursor")
        if not cursor:
            break
        assert pages < 10, "guard against infinite paging loop"

    assert pages == 3
    assert len(seen) == 250
    assert seen == sorted(seen)
    # Spot-check first + last cursor walks.
    assert seen[0] == "X:S000"
    assert seen[-1] == "X:S249"


def test_list_filter_by_exchange(client, seed_asset):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", name="Bitcoin", exchange="BINANCE")
    seed_asset("NASDAQ:AAPL", "AAPL", name="Apple", exchange="NASDAQ", asset_class="EQUITY")
    r = client.get("/v1/assets", params={"exchange": "NASDAQ"})
    assert r.status_code == 200
    syms = [a["asset"]["symbol"] for a in r.json()["assets"]]
    assert syms == ["NASDAQ:AAPL"]


def test_list_filter_by_asset_class(client, seed_asset):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", name="Bitcoin", asset_class="CRYPTO")
    seed_asset("NASDAQ:AAPL", "AAPL", name="Apple", asset_class="EQUITY")
    r = client.get("/v1/assets", params={"asset_class": "EQUITY"})
    assert r.status_code == 200
    syms = [a["asset"]["symbol"] for a in r.json()["assets"]]
    assert syms == ["NASDAQ:AAPL"]


def test_list_filter_by_q_substring(client, seed_asset):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", name="Bitcoin")
    seed_asset("BINANCE:ETHUSDT", "ETH/USDT:USDT", name="Ethereum")
    r = client.get("/v1/assets", params={"q": "bitcoin"})
    assert r.status_code == 200
    syms = [a["asset"]["symbol"] for a in r.json()["assets"]]
    assert syms == ["BINANCE:BTCUSDT"]


def test_list_status_aggregation_active_wins(client, seed_asset, seed_feed):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", name="Bitcoin")
    # Mixed feeds: one active, one pending → asset is active.
    seed_feed("BTC/USDT:USDT", "1h", "BINANCE:BTCUSDT", status="active")
    seed_feed("BTC/USDT:USDT", "15", "BINANCE:BTCUSDT", status="pending")

    r = client.get("/v1/assets")
    body = r.json()
    row = next(a for a in body["assets"] if a["asset"]["symbol"] == "BINANCE:BTCUSDT")
    assert row["status"] == "active"


def test_list_status_pending_when_no_active(client, seed_asset, seed_feed):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", name="Bitcoin")
    seed_feed("BTC/USDT:USDT", "15", "BINANCE:BTCUSDT", status="pending")
    seed_feed("BTC/USDT:USDT", "1h", "BINANCE:BTCUSDT", status="inactive")

    r = client.get("/v1/assets")
    row = next(
        a for a in r.json()["assets"] if a["asset"]["symbol"] == "BINANCE:BTCUSDT"
    )
    assert row["status"] == "pending"


def test_list_status_inactive_only(client, seed_asset, seed_feed):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", name="Bitcoin")
    seed_feed("BTC/USDT:USDT", "1h", "BINANCE:BTCUSDT", status="inactive")
    r = client.get("/v1/assets")
    row = next(
        a for a in r.json()["assets"] if a["asset"]["symbol"] == "BINANCE:BTCUSDT"
    )
    assert row["status"] == "inactive"


def test_list_status_pending_when_no_feeds(client, seed_asset):
    """Asset with no feed rows at all defaults to 'pending'."""
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", name="Bitcoin")
    r = client.get("/v1/assets")
    row = next(
        a for a in r.json()["assets"] if a["asset"]["symbol"] == "BINANCE:BTCUSDT"
    )
    assert row["status"] == "pending"


def test_list_includes_last_bar_ts(client, pg_url, seed_asset, seed_feed, seed_bar):
    """`lastBarTs` surfaces the max bar timestamp across the asset's feeds."""
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", name="Bitcoin")
    seed_feed("BTC/USDT:USDT", "1h", "BINANCE:BTCUSDT", status="active")
    seed_bar("BTC/USDT:USDT", "1h", ts=1_700_000_000)
    seed_bar("BTC/USDT:USDT", "1h", ts=1_700_003_600)
    # seed_bar now writes cache_meta automatically; the JOIN has what it needs.
    r = client.get("/v1/assets")
    row = next(
        a for a in r.json()["assets"] if a["asset"]["symbol"] == "BINANCE:BTCUSDT"
    )
    assert row["lastBarTs"] == 1_700_003_600


def test_list_includes_added_at(client, seed_asset):
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", name="Bitcoin", added_at=1_700_000_000)
    r = client.get("/v1/assets")
    row = next(
        a for a in r.json()["assets"] if a["asset"]["symbol"] == "BINANCE:BTCUSDT"
    )
    assert row["addedAt"] == 1_700_000_000


# ---------------------------------------------------------------------------
# POST /v1/assets — create
# ---------------------------------------------------------------------------


_VALID_BODY = {
    "symbol": "BINANCE:DOGEUSDT",
    "storageSymbol": "DOGE/USDT:USDT",
    "name": "Dogecoin / Tether USD",
    "exchange": "BINANCE",
    "currency": "USD",
    "assetClass": "CRYPTO",
    "assetSubClass": "PERP",
    "timeframes": ["15m", "1h"],
    "tvSymbol": "BINANCE:DOGEUSDTPERP",
}


def test_post_creates_201(client):
    r = client.post("/v1/assets", json=_VALID_BODY)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True
    assert body["asset"]["asset"]["symbol"] == "BINANCE:DOGEUSDT"
    assert body["asset"]["status"] == "pending"
    assert body["pollEtaSeconds"] >= 1


def test_post_creates_feeds_rows(client, pg_url):
    """One feeds row per requested timeframe, all 'pending'. REST `15m`/`30m`
    are translated to storage `15`/`30` on the boundary (see PR #11)."""
    client.post("/v1/assets", json=_VALID_BODY).raise_for_status()
    import psycopg
    with psycopg.connect(pg_url) as conn:
        rows = conn.execute(
            "SELECT timeframe, status FROM feeds WHERE storage_symbol = %s ORDER BY timeframe",
            ("DOGE/USDT:USDT",),
        ).fetchall()
    statuses = {tf: status for tf, status in rows}
    assert set(statuses.keys()) == {"15", "1h"}  # "15m" → "15" via _timeframes.py
    assert all(s == "pending" for s in statuses.values())


def test_post_default_timeframes(client, pg_url):
    body = {k: v for k, v in _VALID_BODY.items() if k != "timeframes"}
    r = client.post("/v1/assets", json=body)
    assert r.status_code == 201, r.text
    import psycopg
    with psycopg.connect(pg_url) as conn:
        rows = conn.execute(
            "SELECT timeframe FROM feeds WHERE storage_symbol = %s",
            ("DOGE/USDT:USDT",),
        ).fetchall()
    assert [r[0] for r in rows] == ["1h"]


def test_post_default_tv_symbol(client, pg_url):
    body = {k: v for k, v in _VALID_BODY.items() if k != "tvSymbol"}
    body["timeframes"] = ["1h"]
    r = client.post("/v1/assets", json=body)
    assert r.status_code == 201, r.text
    import psycopg
    with psycopg.connect(pg_url) as conn:
        rows = conn.execute(
            "SELECT provider_symbol FROM feeds WHERE storage_symbol = %s",
            ("DOGE/USDT:USDT",),
        ).fetchall()
    assert all(r[0] == "BINANCE:DOGEUSDT" for r in rows)  # default = asset.symbol


def test_post_pending_to_active(client, pg_url):
    """POST creates a 'pending' feed; data-svc calling mark_active() flips
    it to 'active' and the next GET shows that."""
    r = client.post("/v1/assets", json=_VALID_BODY)
    assert r.status_code == 201, r.text

    # Simulate what data-svc does after a successful bar insert.
    from data_svc.db.feeds import FeedsRepo
    feeds_repo = FeedsRepo(pg_url)
    feeds_repo.mark_active("DOGE/USDT:USDT", "1h")

    r = client.get("/v1/assets", params={"q": "doge"})
    row = next(a for a in r.json()["assets"] if a["asset"]["symbol"] == "BINANCE:DOGEUSDT")
    # 1h is active, 15m is still pending → priority active wins.
    assert row["status"] == "active"


def test_post_duplicate_409(client):
    r1 = client.post("/v1/assets", json=_VALID_BODY)
    assert r1.status_code == 201
    r2 = client.post("/v1/assets", json=_VALID_BODY)
    assert r2.status_code == 409
    body = r2.json()
    assert body["error"] == "already_exists"
    assert body["existing"]["asset"]["symbol"] == "BINANCE:DOGEUSDT"


def test_post_malformed_symbol_422(client):
    bad_lowercase = dict(_VALID_BODY, symbol="binance:doge")
    assert client.post("/v1/assets", json=bad_lowercase).status_code == 422
    bad_nocolon = dict(_VALID_BODY, symbol="BINANCEDOGE")
    assert client.post("/v1/assets", json=bad_nocolon).status_code == 422


def test_post_invalid_asset_class_422(client):
    body = dict(_VALID_BODY, assetClass="BOND")
    r = client.post("/v1/assets", json=body)
    assert r.status_code == 422


def test_post_auth_admin_token_required(admin_client):
    """With REST_ADMIN_TOKEN=admin set: bare POST → 401, with `Bearer admin` → 201."""
    r = admin_client.post("/v1/assets", json=_VALID_BODY)
    assert r.status_code == 401

    r = admin_client.post(
        "/v1/assets",
        json=_VALID_BODY,
        headers={"Authorization": "Bearer admin"},
    )
    assert r.status_code == 201, r.text


def test_post_auth_rejects_non_admin_token(admin_client):
    """When REST_ADMIN_TOKEN is set, REST_AUTH_TOKEN is NOT a fallback —
    only the admin token unlocks writes."""
    r = admin_client.post(
        "/v1/assets",
        json=_VALID_BODY,
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 401


def test_post_falls_back_to_auth_when_no_admin_token(authed_client):
    """REST_ADMIN_TOKEN unset + REST_AUTH_TOKEN=secret → POST requires
    the secret token; this is the fallback path in auth.require_bearer_admin."""
    r = authed_client.post("/v1/assets", json=_VALID_BODY)
    assert r.status_code == 401  # missing token

    r = authed_client.post(
        "/v1/assets",
        json=_VALID_BODY,
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 201, r.text


def test_post_open_mode_when_both_unset(client):
    """Neither token set → POST is open. Matches the GET behaviour."""
    r = client.post("/v1/assets", json=_VALID_BODY)
    assert r.status_code == 201


def test_get_visible_after_post(client):
    """End-to-end: POST then GET /v1/assets surfaces the new row + GET
    /v1/profile/{symbol} also finds it."""
    client.post("/v1/assets", json=_VALID_BODY).raise_for_status()

    r = client.get("/v1/profile/BINANCE:DOGEUSDT")
    assert r.status_code == 200, r.text
    assert r.json()["symbol"] == "BINANCE:DOGEUSDT"

    r = client.get("/v1/assets", params={"q": "doge"})
    assert r.status_code == 200
    syms = [a["asset"]["symbol"] for a in r.json()["assets"]]
    assert "BINANCE:DOGEUSDT" in syms
