"""Spec acceptance criterion #6 — Ghostfolio MANUAL scraperConfiguration.

The spec at docs/hybrid-data-svc-rest-api-spec.md §"Acceptance criteria"
includes:

  - A Ghostfolio MANUAL `scraperConfiguration` with `selector: "$.price"`
    against this endpoint successfully populates the asset's market price
    on the next hourly cron.

Ghostfolio's MANUAL data source scraper does three observable things:

  1. HTTP GET the configured URL (optionally with a static `Authorization`
     header — which we expose via REST_AUTH_TOKEN).
  2. Apply the configured JSONPath selector to the response body
     (Ghostfolio uses `jsonpath-plus` server-side; we use the Python port
     `jsonpath-ng` which evaluates the same standard syntax).
  3. Assert the extracted value is a number, then write it to its
     `Order.unitPrice` column for that asset on the cron tick.

We can't exercise step 3 without a Ghostfolio instance, but steps 1+2
(the engineering contract) are deterministic — if the JSON shape matches
and `$.price` extracts a non-null number, Ghostfolio will succeed on the
next tick. These tests assert that contract.
"""

from __future__ import annotations

import time

from jsonpath_ng.ext import parse


GHOSTFOLIO_PRICE_SELECTOR = parse("$.price")


def test_quote_response_satisfies_ghostfolio_price_selector(client, seed_asset, seed_bar):
    """JSON returned by /v1/quote yields a numeric price under `$.price`."""
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", currency="USD")
    seed_bar("BTC/USDT:USDT", "1D", int(time.time()) - 60, close=42_000.5)

    r = client.get("/v1/quote/BINANCE:BTCUSDT")
    assert r.status_code == 200, r.text

    matches = [m.value for m in GHOSTFOLIO_PRICE_SELECTOR.find(r.json())]
    assert len(matches) == 1, f"Ghostfolio's `$.price` must extract exactly one value (got {matches!r})"
    price = matches[0]
    assert isinstance(price, (int, float)), f"`$.price` must be numeric (got {type(price).__name__})"
    assert price == 42_000.5


def test_quote_with_bearer_satisfies_ghostfolio_price_selector(authed_client, seed_asset, seed_bar):
    """Same selector contract under the `Authorization: Bearer <token>`
    header that Ghostfolio sends when the user configures one."""
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", currency="USD")
    seed_bar("BTC/USDT:USDT", "1D", int(time.time()) - 60, close=88_888.0)

    r = authed_client.get(
        "/v1/quote/BINANCE:BTCUSDT",
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 200, r.text

    matches = [m.value for m in GHOSTFOLIO_PRICE_SELECTOR.find(r.json())]
    assert matches == [88_888.0]


def test_quote_stale_bar_still_extracts_a_price(client, seed_asset, seed_bar):
    """Per spec §Notes for /v1/quote: setting `stale: true` is preferred
    over erroring so Ghostfolio still displays the price. The price field
    must remain present and numeric even when stale=true."""
    seed_asset("BINANCE:BTCUSDT", "BTC/USDT:USDT", currency="USD")
    seed_bar("BTC/USDT:USDT", "1D", int(time.time()) - 100_000, close=12_345.0)

    r = client.get("/v1/quote/BINANCE:BTCUSDT?max_age_seconds=60")
    assert r.status_code == 200
    body = r.json()
    assert body["stale"] is True

    matches = [m.value for m in GHOSTFOLIO_PRICE_SELECTOR.find(body)]
    assert matches == [12_345.0]
