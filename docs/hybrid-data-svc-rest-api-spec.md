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

---

## Phase 3 — Catalog management (proposed, 2026-05-28)

### Motivation

Phase 1 + 2 shipped (`/v1/quote`, `/v1/historical`, `/v1/search`, `/v1/profile`, `/healthz`) and Ghostfolio integration was validated end-to-end on 2026-05-28. The remaining gap blocking real portfolio coverage is the **hand-curated catalog**: `data_svc/assets.yaml` (5 crypto symbols today) plus a YAML edit + `FEEDS` env edit + PR + deploy whenever a new symbol is needed. The `data-svc` reads `FEEDS` only at process startup, so inserting into the `assets` table alone doesn't start polling.

To scale to a real portfolio (Binance + OKX + IBKR + Nomad + B3 = hundreds of symbols), we need two new endpoints plus a small change to how `data-svc` discovers what to poll.

### Endpoints

#### 1. `GET /v1/assets` — list the catalog

Optional query params: `exchange`, `asset_class`, `q` (substring on `symbol` or `name`), `cursor`, `limit` (default 100, max 500).

**Response 200:**

```json
{
  "assets": [
    {
      "symbol": "BINANCE:BTCUSDT",
      "storage_symbol": "BTC/USDT:USDT",
      "name": "Bitcoin / Tether USD",
      "exchange": "BINANCE",
      "currency": "USD",
      "asset_class": "CRYPTO",
      "asset_subclass": "PERP",
      "isin": null,
      "country": null,
      "status": "active",
      "added_at": 1748390400,
      "last_bar_ts": 1779987600
    }
  ],
  "next_cursor": "BINANCE:XRPUSDT"
}
```

`status` (new field on the `assets` table):
- `active` — feed is being polled and writing bars.
- `pending` — in the catalog, polling not yet started (typically a brief window after POST, < 60s if Option A below is implemented).
- `inactive` — explicitly disabled by an operator. Stays in catalog but no polling.

Backed by `SELECT ... FROM assets WHERE <filters> ORDER BY symbol LIMIT ... OFFSET ...` in `AssetsRepo` (`data_svc/db/assets.py`). Same auth as other `/v1/*` endpoints (`Authorization: Bearer ${REST_AUTH_TOKEN}` when set).

#### 2. `POST /v1/assets` — register a new asset and start polling

**Body:**

```json
{
  "symbol": "NASDAQ:AAPL",
  "storage_symbol": "AAPL",
  "name": "Apple Inc.",
  "exchange": "NASDAQ",
  "currency": "USD",
  "asset_class": "EQUITY",
  "asset_subclass": "STOCK",
  "isin": "US0378331005",
  "country": "US",
  "timeframes": ["1h", "1D"]
}
```

Required fields: `symbol`, `storage_symbol`, `name`, `exchange`, `currency`, `asset_class`. Other fields optional. `timeframes` defaults to `["1h"]`.

**Response 201 Created:**

```json
{ "symbol": "NASDAQ:AAPL", "status": "pending", "poll_eta_seconds": 30 }
```

**Response 409 Conflict** (already in catalog):

```json
{ "error": "exists", "existing": { /* same shape as GET /v1/assets entry */ } }
```

**Response 422 Unprocessable Entity** when the TradingView identifier is malformed (must match `^[A-Z0-9]+:[A-Z0-9._-]+$`) or `asset_class` is not one of `EQUITY|CRYPTO|ETF|FUND`. Validation that TradingView Desktop *recognizes* the symbol can be deferred (asynchronous) — the polling will simply fail to fetch bars and `status` stays `pending` indefinitely; surface that via `GET /v1/assets`.

**Auth recommendation**: this endpoint mutates catalog state, so consider a second bearer `REST_ADMIN_TOKEN` (separate from `REST_AUTH_TOKEN`). When `REST_ADMIN_TOKEN` is set, `POST /v1/assets` (and any future mutating endpoints) require *it*; reads keep using `REST_AUTH_TOKEN`. When unset, fall back to `REST_AUTH_TOKEN`. Lets ops grant read-only credentials to consumers like Ghostfolio without granting catalog-modify rights.

### Polling activation — the structural change

Today `data-svc` reads `FEEDS` from env at startup (`data_svc/__main__.py:67`). For POST to take effect without restart, the recommended approach is:

#### Option A (recommended) — feeds table as runtime source of truth

1. Add migration `migrations/003_feeds.sql`:

   ```sql
   CREATE TABLE IF NOT EXISTS feeds (
       storage_symbol  TEXT NOT NULL,
       timeframe       TEXT NOT NULL,
       tv_symbol       TEXT NOT NULL,
       status          TEXT NOT NULL DEFAULT 'pending',  -- pending|active|inactive
       updated_at      BIGINT NOT NULL,
       PRIMARY KEY (storage_symbol, timeframe)
   );
   CREATE INDEX feeds_status_idx ON feeds (status);
   ```

2. At `data-svc` startup, seed the `feeds` table from the `FEEDS` env var (one-time idempotent upsert; env stays as the seed mechanism for greenfield deploys).

3. Replace the in-memory `cfg.feeds` iteration in `data_svc/__main__.py` (lines 95–111) with a poll-loop step that **re-reads `SELECT (storage_symbol, timeframe, tv_symbol) FROM feeds WHERE status = 'active' OR status = 'pending'` at the top of every cycle** (or every N cycles, e.g. 1-in-10 with a cheap row-count change check). When a bar is successfully written for a `pending` feed, flip its `status` to `active`.

4. `POST /v1/assets` upserts `assets` and one row in `feeds` per requested timeframe with `status = 'pending'`. `GET /v1/assets` joins `assets` with `feeds` to derive the aggregate `status`.

5. `assets.yaml` remains the seed for the catalog (the loader at `data_svc/services/assets_loader.py:91` already upserts on startup — leave it as-is).

#### Option B (cheaper, less elegant)

Postgres `LISTEN/NOTIFY`: `POST /v1/assets` runs `NOTIFY feeds_changed`; `data-svc` has a background task that reacts by appending to the in-memory `cfg.feeds`. Less invasive but loses observability into "what's the live feed list" without a separate query against in-process state.

#### Option C (rejected)

SIGHUP that re-reads YAML. Keeps YAML as single source of truth but defeats the self-service goal — operators would still have to commit YAML changes for catalog growth.

The team chooses, but the consumer (Ghostfolio glue) is written assuming Option A is in place. With Option B, the `poll_eta_seconds` in the POST response is just shorter; the contract holds.

### Acceptance criteria

- [ ] `GET /v1/assets` returns the catalog paginated with `status` per symbol.
- [ ] `POST /v1/assets` with a valid payload returns `201` + `status: pending`.
- [ ] Within 60s of POST, `GET /v1/profile/{symbol}` returns `200` and (after the first bar closes) `GET /v1/quote/{symbol}` returns a real price; `GET /v1/assets` shows `status: active`.
- [ ] Duplicate POST returns `409` with the existing record in the body.
- [ ] Without `Authorization: Bearer` (when admin token is required) → `401`.
- [ ] `tests/rest/test_ghostfolio_contract.py` (the `$.price` contract gate) stays green.
- [ ] New test `tests/rest/test_catalog_management.py` covers: list pagination, POST round-trip, 409 on dup, 422 on malformed `symbol`, and (with a mock TradingView) the pending → active transition within 60s.

### Out of scope for Phase 3

- `PUT /v1/assets/{symbol}` (edit) and `DELETE /v1/assets/{symbol}` — not needed for our workflow; surface only if a real use case emerges.
- Per-asset alerting / health pings — `status: pending` lingering past 60s is enough signal; operators can query and react.

---

## Out of scope (for now)

- WebSocket / SSE push of real-time bars (REST polling is enough for Ghostfolio's hourly cron).
- Brazilian fund prices by CNPJ — no public feed; Ghostfolio handles these via direct MANUAL POST from a separate ingestion script.
- Symbol mapping (e.g. `AAPL` → `NASDAQ:AAPL`) — caller is expected to know the TradingView identifier. Could be added later via `/v1/search`.
