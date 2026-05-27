# `hybrid-data-svc` — REST API Spec (proposed)

> **Status:** proposal — open for team review.
> **Author:** Sander (consumer: self-hosted Ghostfolio instance for personal portfolio tracking).
> **Goal:** expose an HTTP REST gateway in front of the existing `BarService` gRPC, so consumers that don't speak gRPC (Ghostfolio, Grafana, ad-hoc curl, etc.) can read prices without a separate client.

---

## Motivation

`hybrid-data-svc` is currently gRPC-only (`BarService.GetRecentBars`). My self-hosted Ghostfolio instance can consume prices from any URL that returns JSON, via its built-in `MANUAL` data source + `scraperConfiguration` (URL + JSON selector). Adding a thin REST layer in front of the existing gRPC service lets Ghostfolio (and other tooling) consume the same closed-bar feed without writing a gRPC client per consumer.

The REST layer should be a **thin gateway** — no business logic, just call into the existing `BarService` and serialize the response. Suggested stack: **FastAPI + grpcio** (reuses the existing `bars.proto`), deployed as a new `bar-rest` container in the existing `docker-compose.yml`.

---

## Scope

### Phase 1 (minimum to unblock Ghostfolio)

| # | Endpoint | Purpose |
|---|---|---|
| 1 | `GET /v1/quote/{symbol}` | Latest closed price for a symbol |
| 2 | `GET /healthz` | Liveness probe |

### Phase 2 (nice-to-haves, ship after Phase 1 is stable)

| # | Endpoint | Purpose |
|---|---|---|
| 3 | `GET /v1/historical/{symbol}` | OHLCV bars over a date range |
| 4 | `GET /v1/search` | Symbol autocomplete |
| 5 | `GET /v1/profile/{symbol}` | Asset metadata (name, currency, exchange) |

---

## Conventions

- **Base path**: `/v1/...` (versioned).
- **Symbol format**: TradingView chart identifiers, **uppercase**, `:` separator. Examples:
  - Crypto: `BINANCE:BTCUSDT`, `OKX:ETHUSDT`
  - US equities: `NASDAQ:AAPL`, `NYSE:VOO`
  - B3 equities (if covered): `BMFBOVESPA:PETR4`
  - In path params, URL-encode `:` as `%3A` (e.g. `/v1/quote/BINANCE%3ABTCUSDT`).
- **Timestamps**: integer seconds since Unix epoch, UTC.
- **Numbers**: JSON numbers (float for prices, int for timestamps and volume).
- **Currency**: ISO 4217 string (`USD`, `BRL`, etc.).
- **Errors**: JSON body `{"error": "<code>", "message": "<human>"}` with appropriate HTTP status (404 unknown symbol, 503 feed stale beyond grace, 401 unauth, 500 internal).
- **CORS**: allow all origins (`Access-Control-Allow-Origin: *`). Service is intended for trusted networks; CORS just simplifies browser debugging.

---

## Auth

Optional Bearer token, configurable via env var `REST_AUTH_TOKEN`.

- If `REST_AUTH_TOKEN` is **unset or empty**: open (suitable for dev / private docker network).
- If set: every request must carry `Authorization: Bearer <token>`. Missing/invalid → `401 Unauthorized`.

Same token applies to all endpoints (no per-endpoint scopes).

---

## Endpoints

### `GET /v1/quote/{symbol}` — latest price (Phase 1)

Returns the most recent closed bar's close price, plus metadata.

**Path params:**

- `symbol` (string, required) — URL-encoded TradingView identifier.

**Query params (all optional):**

- `timeframe` (string, default `1D`) — which timeframe's last close to return. Useful when the same symbol is fed in multiple timeframes. Allowed: `1m,3m,5m,15m,30m,1h,2h,4h,1D,1W`.
- `max_age_seconds` (int, default `3600`) — if the latest bar is older than this, the `stale` flag is set to `true`. Does not change HTTP status unless the feed is fully unavailable.

**Response `200 OK`:**

```json
{
  "symbol": "BINANCE:BTCUSDT",
  "timeframe": "1D",
  "price": 95123.45,
  "currency": "USD",
  "ts": 1748390400,
  "stale": false
}
```

**Response `404 Not Found`** (symbol not in feed list):

```json
{ "error": "unknown_symbol", "message": "BINANCE:BTCUSDT is not in the configured feed list" }
```

**Response `503 Service Unavailable`** (no bars stored yet for this symbol):

```json
{ "error": "no_data", "message": "No bars available for BINANCE:BTCUSDT@1D" }
```

**Notes:**

- `currency` is the **quote currency** of the symbol (e.g. `USD` for `BTCUSDT`, `BRL` for `BMFBOVESPA:PETR4`). The service may have to infer this from a mapping table; document the mapping in the README.
- `ts` is the bar's open timestamp (matches `bars.ts` in the existing schema).
- Setting `stale: true` is preferred over erroring — Ghostfolio will still display the price but the consumer can decide what to do.

---

### `GET /healthz` — liveness (Phase 1)

Minimal health check. Returns 200 if the gateway can reach the gRPC service and the Postgres connection is alive.

```json
{ "status": "ok", "grpc_reachable": true, "db_reachable": true }
```

---

### `GET /v1/historical/{symbol}` — OHLCV history (Phase 2)

**Path params:**

- `symbol` (string, required) — URL-encoded TradingView identifier.

**Query params:**

- `from` (int, required) — epoch seconds, inclusive.
- `to` (int, required) — epoch seconds, inclusive.
- `interval` (string, default `1D`) — same allowed list as `timeframe` above.

**Response `200 OK`:**

```json
{
  "symbol": "BINANCE:BTCUSDT",
  "interval": "1D",
  "bars": [
    { "ts": 1748304000, "open": 94000.0, "high": 95500.0, "low": 93800.0, "close": 95123.0, "volume": 12345.67 }
  ]
}
```

**Notes:**

- Cap response at a reasonable number of bars (suggest 5000, matching the current gRPC service cap). If `from`/`to` would exceed the cap, return only the most recent N bars and include a `truncated: true` field.

---

### `GET /v1/search` — autocomplete (Phase 2)

**Query params:**

- `q` (string, required) — substring (case-insensitive) matched against symbol id and asset name.
- `limit` (int, default 10, max 50).

**Response `200 OK`:**

```json
{
  "results": [
    {
      "symbol": "NASDAQ:AAPL",
      "name": "Apple Inc.",
      "exchange": "NASDAQ",
      "type": "stock",
      "currency": "USD"
    }
  ]
}
```

---

### `GET /v1/profile/{symbol}` — metadata (Phase 2)

**Response `200 OK`:**

```json
{
  "symbol": "NASDAQ:AAPL",
  "name": "Apple Inc.",
  "exchange": "NASDAQ",
  "currency": "USD",
  "assetClass": "EQUITY",
  "assetSubClass": "STOCK",
  "isin": "US0378331005",
  "country": "US"
}
```

Fields that the gateway cannot fill should be omitted (not returned as `null`).

---

## Deployment

Add to the existing `docker-compose.yml` of `hybrid-data-svc`:

```yaml
  bar-rest:
    build: .
    command: ["python", "-m", "data_svc.rest_server"]
    environment:
      - GRPC_TARGET=bar-grpc:50051
      - DATABASE_URL=postgresql://...   # same as data-svc / bar-grpc
      - REST_AUTH_TOKEN=                # empty = open in dev
    ports:
      - "8001:8001"                     # exposed for Ghostfolio
    depends_on:
      bar-grpc:
        condition: service_started
      postgres:
        condition: service_healthy
```

On a shared Docker network with Ghostfolio, the consumer URL becomes:

```
http://hybrid-data-svc-bar-rest:8001/v1/quote/BINANCE%3ABTCUSDT
```

---

## Acceptance criteria (Phase 1)

- [ ] New service container `bar-rest` builds and starts as part of `docker compose up`.
- [ ] `curl http://localhost:8001/healthz` returns `200`.
- [ ] `curl http://localhost:8001/v1/quote/BINANCE%3ABTCUSDT` returns 200 with the expected JSON shape, given a configured feed.
- [ ] `curl http://localhost:8001/v1/quote/UNKNOWN%3AXYZ` returns 404.
- [ ] When `REST_AUTH_TOKEN` is set, missing/invalid bearer returns 401.
- [ ] A Ghostfolio MANUAL `scraperConfiguration` with `selector: "$.price"` against this endpoint successfully populates the asset's market price on the next hourly cron.

---

## Out of scope (for now)

- WebSocket / SSE push of real-time bars (REST polling is enough for Ghostfolio's hourly cron).
- Brazilian fund prices by CNPJ — no public feed; Ghostfolio handles these via direct MANUAL POST from a separate ingestion script.
- Symbol mapping (e.g. `AAPL` → `NASDAQ:AAPL`) — caller is expected to know the TradingView identifier. Could be added later via `/v1/search`.
