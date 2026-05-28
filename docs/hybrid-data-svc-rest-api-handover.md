# `hybrid-data-svc` REST API — handover

> Companion to [hybrid-data-svc-rest-api-spec.md](hybrid-data-svc-rest-api-spec.md) (the original spec) and [openapi.yaml](openapi.yaml) (the live contract).
> Audience: an operator who needs to **consume** the API or **run/debug** the service.

---

## 1. What was built (90-second tour)

A new container, **`bar-rest`**, sits in front of the existing internal gRPC services and exposes 5 REST endpoints over HTTP on port `8001` (host-mapped per `.env`):

| Endpoint | Purpose | Backed by |
|---|---|---|
| `GET /healthz` | Liveness probe — `{status, grpc_reachable, db_reachable}` | `BarService.Ping` (gRPC) |
| `GET /v1/quote/{symbol}` | Latest closed-bar price + currency | `AssetService.GetProfile` + `BarService.GetRecentBars` (gRPC) |
| `GET /v1/historical/{symbol}` | OHLCV range query | `AssetService.GetProfile` + `BarService.GetBarsInRange` (gRPC) |
| `GET /v1/search` | Substring search over the asset catalog | `AssetService.Search` (gRPC) |
| `GET /v1/profile/{symbol}` | Full asset metadata (name, currency, ISIN, country, etc.) | `AssetService.GetProfile` (gRPC) |

Architecture is **thin-gateway**: every REST route is a translation layer (HTTP ↔ gRPC), no business logic in the REST process. The gRPC server (`bar-grpc`) owns Postgres access and the asset catalog. The same `BarService` continues to serve gRPC clients (trading bots, audits) unchanged.

**Source of truth split:**
- `docs/openapi.yaml` — REST contract. Pydantic models are generated from it (`make codegen`); a drift test asserts the running app matches.
- `data_svc/grpc_server/proto/bars.proto` — gRPC contract. Stubs regenerated via `buf generate` (`make proto`); CI fails the build if a regen produces a diff.

**Asset catalog**: hand-curated [`data_svc/assets.yaml`](../data_svc/assets.yaml) loaded into the `assets` Postgres table at `bar-grpc` startup. Bridges the TradingView identifier (REST surface, e.g. `BINANCE:BTCUSDT`) to the ccxt storage key (internal, e.g. `BTC/USDT:USDT`) and carries name/currency/ISIN/country.

**Auth**: optional bearer via `REST_AUTH_TOKEN`. Unset = open mode. Set = every `/v1/*` request requires `Authorization: Bearer <token>`; `/healthz` always open.

**CI/CD** (self-hosted Woodpecker at `localhost:8000`, tunneled as `ci.tickerbeats.com`):
- `.woodpecker/pr.yml` — 12 gates on every PR + push to main (lint, codegen drift, tests, docker build, secret scan).
- `.woodpecker/deploy.yml` — manual-trigger only; runs `git reset --hard origin/main`, rebuilds the three project services with `--remove-orphans`, then polls `/healthz` until it returns 200.

---

## 2. How to use (operations)

### 2.1 Consuming the REST API

The container publishes on the host port configured by `REST_HOST_PORT` (defaults to `8001`; this host uses `8003` because `tnr-backend` already owns `8001`). On the shared Docker network, consumers always use the **internal** port `8001`.

**Examples** (replace `:8003` with your `REST_HOST_PORT`):

```bash
# Healthcheck
curl -sf http://localhost:8003/healthz | jq .
#   {"status":"ok","grpc_reachable":true,"db_reachable":true}

# Latest price (URL-encode `:` as %3A)
curl -sf 'http://localhost:8003/v1/quote/BINANCE%3ABTCUSDT?timeframe=1h' | jq .

# Historical OHLCV over a range (epoch seconds, inclusive)
curl -sf "http://localhost:8003/v1/historical/BINANCE%3ABTCUSDT?from=$(date -v-1d +%s)&to=$(date +%s)&interval=1h" | jq .

# Search by substring (matches symbol OR name)
curl -sf 'http://localhost:8003/v1/search?q=bitcoin&limit=5' | jq .

# Full asset metadata
curl -sf 'http://localhost:8003/v1/profile/BINANCE%3ABTCUSDT' | jq .

# Swagger UI for interactive exploration:
open http://localhost:8003/docs
```

**With bearer auth enabled**: set `REST_AUTH_TOKEN=<your-token>` in `.env`, restart `bar-rest`, then add `-H 'Authorization: Bearer <your-token>'` to every `/v1/*` request. `/healthz` stays open.

### 2.2 Configuring Ghostfolio's MANUAL data source

Ghostfolio's MANUAL data source polls a URL hourly and extracts a JSONPath. For each asset:

| Field | Value |
|---|---|
| **URL** | `http://hybrid-data-svc-bar-rest:8001/v1/quote/<SYMBOL_URL_ENCODED>` (use `8001` — internal docker network port — when Ghostfolio runs in the same docker network) |
| **Selector** | `$.price` |
| **HTTP Header** (optional, only if `REST_AUTH_TOKEN` is set) | `Authorization: Bearer <your-token>` |

The contract that `$.price` returns a numeric value is gated in CI by `tests/rest/test_ghostfolio_contract.py` — it won't regress silently. Sample response:

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

### 2.3 Adding a new feed (operator workflow)

Two equivalent paths — pick by whether you want a permanent record in version control.

**Runtime path — `POST /v1/assets` (no restart, no PR)**. The Phase 3 catalog-management endpoint registers the asset + writes one `feeds` row per requested timeframe with `status='pending'`. `data-svc` picks the new feed up on the next poll cycle (≤30s typical) and flips it to `active` after the first successful bar insert.

```bash
curl -sf -X POST -H "Authorization: Bearer $REST_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "EXCHANGE:NEWSYMBOL",
    "storageSymbol": "NEW/SYMBOL:QUOTE",
    "name": "Human-readable Name",
    "exchange": "EXCHANGE",
    "currency": "USD",
    "assetClass": "CRYPTO",
    "assetSubClass": "PERP",
    "timeframes": ["15m", "1h"],
    "tvSymbol": "EXCHANGE:NEWSYMBOL"
  }' \
  http://localhost:8001/v1/assets | jq .
```

After ~30 seconds: `curl -sf -H "Authorization: Bearer $REST_AUTH_TOKEN" 'http://localhost:8001/v1/assets?q=newsymbol' | jq '.assets[]|{status,lastBarTs}'` — `status` should be `active` and `lastBarTs > 0`. Re-POSTing the same `symbol` returns 409 with the existing row in the body.

**Greenfield path — YAML + env**. Use this for symbols you want committed to source control (e.g. a fresh deploy needs them seeded automatically):

1. Add the feed to `FEEDS` in `.env`:
   ```
   FEEDS=...,NEW/SYMBOL:QUOTE@1h@EXCHANGE:NEWSYMBOL
   ```
2. Add the matching catalog entry to [`data_svc/assets.yaml`](../data_svc/assets.yaml):
   ```yaml
   - symbol: EXCHANGE:NEWSYMBOL          # TV identifier (REST surface)
     storage_symbol: NEW/SYMBOL:QUOTE    # ccxt key (must match FEEDS)
     name: Human-readable Name
     exchange: EXCHANGE
     currency: USD
     asset_class: CRYPTO                 # EQUITY | CRYPTO | ETF | FUND
     asset_subclass: PERP                # free-form, optional
   ```
3. Commit, PR, merge.
4. Deploy (next section). On startup, `assets_loader` upserts the YAML rows and `feeds_loader` seeds the `feeds` table from `FEEDS` env (idempotent — no-ops if rows already exist).

Until either path has placed an `assets` row for the symbol, `/v1/quote/EXCHANGE:NEWSYMBOL` returns 404 `unknown_symbol` because `AssetsRepo.resolve()` can't bridge the TV id to a storage symbol. This is intentional — keeps the catalog explicit.

### 2.4 Triggering a deploy

**From the runner host** (e.g. `sander@mac`), the cleanest path is the local Woodpecker API. UI works too; both flow through the same manual trigger.

```bash
# Load the token (no echoing!)
TOKEN=$(awk -F= '/^WOODPECKER_API_TOKEN=/{sub(/^WOODPECKER_API_TOKEN=/,""); print; exit}' /Users/sander/projects/hybrid-data-svc/.env)

# Trigger a deploy of main
curl -sS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"branch":"main","variables":{"DEPLOY_MAIN":"true"}}' \
  http://localhost:8000/api/repos/14/pipelines \
  | jq '{number, status, commit}'
```

**Manual-build variables:**

| Variable | Required | Purpose |
|---|---|---|
| `DEPLOY_MAIN` | yes (must equal `true`) | Guard. Without it the pipeline refuses to proceed. |
| `DEPLOY_PATH` | no | Override the host clone location. Default: `/Users/$(id -un)/projects/hybrid-data-svc`. |
| `REST_HOST_PORT` | no | Override the host port the post-deploy healthcheck polls. Default `8001`; auto-read from `.env` on the host if set there. |

**What happens** (steps in order):
1. `guard` — fails if `DEPLOY_MAIN != true`.
2. `deploy` — `cd $DEPLOY_PATH`, refuse if dirty tree, `git fetch + reset --hard origin/main`, `docker compose up -d --build --remove-orphans bar-grpc bar-rest data-svc`.
3. `healthz` — read `REST_HOST_PORT` from `$DEPLOY_PATH/.env`, poll `http://localhost:$REST_HOST_PORT/healthz` every 5s for up to 60s; build red if never 200.

**Watch the build:**

```bash
PIPELINE=<the number returned above>
watch -n 5 "curl -sS http://localhost:8000/api/repos/14/pipelines/$PIPELINE | jq '.workflows[].children[] | {name, state}'"
```

### 2.5 Debugging a failed deploy

When a step fails, fetch its log directly via the local API:

```bash
# Find the failed step id
PIPELINE=63
curl -sS http://localhost:8000/api/repos/14/pipelines/$PIPELINE \
  | jq '.workflows[].children[] | select(.state=="failure")'

# Decode the base64-encoded log
STEP_ID=<the id from above>
curl -sS http://localhost:8000/api/repos/14/logs/$PIPELINE/$STEP_ID \
  | jq -r '.[].data' \
  | while read -r line; do echo "$line" | base64 -d; echo; done
```

**Common failure modes (covered by self-healing or operator action):**

| Symptom | Root cause | Fix |
|---|---|---|
| `Bind for 127.0.0.1:8001 failed: port is already allocated` | Sibling project (e.g. `tnr-backend`) owns the host port | Set `REST_HOST_PORT=8003` (or another free port) in `.env` |
| `No such container: <random>_hybrid-data-svc-...` | Orphan from an interrupted deploy | Already fixed by `--remove-orphans`; if reappears, `docker compose down --remove-orphans` then retrigger |
| `/healthz never returned 200 after 60s` | bar-rest container failed to start, or healthz polling the wrong port | Check `docker compose logs bar-rest`; verify `REST_HOST_PORT` matches the published port (`docker compose ps`) |
| New migration not picked up | `pgdata` volume already initialized — `initdb.d` scripts only run on first init | Apply manually before the deploy: `docker compose exec -T postgres psql -U datasvc -d datasvc -f /docker-entrypoint-initdb.d/00X_*.sql` (see [README §Deployment](../README.md)) |
| Pipeline errored at YAML render time with `unable to parse variable name` | A `${VAR}` somewhere in `.woodpecker/deploy.yml` (including inside a comment) — Woodpecker scans the whole file | Use brace-less `$VAR` form everywhere; for descriptive comments write prose ("dollar-brace VAR"), not literal `${VAR}` |

### 2.6 Day-2 ops cheat sheet

| Action | Command |
|---|---|
| Service status | `docker compose ps` |
| REST log tail | `docker compose logs -f --tail=100 bar-rest` |
| gRPC log tail | `docker compose logs -f --tail=100 bar-grpc` |
| Restart REST only | `docker compose restart bar-rest` |
| Regenerate proto stubs | `make proto` (requires `buf` on PATH) |
| Regenerate Pydantic models | `make codegen` (requires `datamodel-codegen` from `requirements-dev.txt`) |
| Run the test suite | `make test` (or `pytest -q tests/rest/`) |
| List assets in catalog | `docker compose exec -T postgres psql -U datasvc -d datasvc -c "TABLE assets"` |

---

## 3. Where to read more

- [README.md](../README.md) — full project README incl. §Deployment with the manual-build variables.
- [docs/hybrid-data-svc-rest-api-spec.md](hybrid-data-svc-rest-api-spec.md) — original spec.
- [docs/openapi.yaml](openapi.yaml) — REST contract (live, drift-gated).
- [data_svc/grpc_server/proto/bars.proto](../data_svc/grpc_server/proto/bars.proto) — gRPC contract (live, drift-gated).
- [tests/rest/test_ghostfolio_contract.py](../tests/rest/test_ghostfolio_contract.py) — the test that proves a Ghostfolio scraperConfiguration with `$.price` will work end-to-end.
