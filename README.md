# hybrid-data-svc

Market data service for the [hybrid-quant-bot](https://github.com/Ruscigno/hybrid-quant-bot) trading project.

Polls TradingView Desktop via Chrome DevTools Protocol (CDP), stores closed OHLCV bars in Postgres with overlap-validation + cross-symbol-leak guards, and publishes them to consumers (bots, audits, optimizers) over gRPC.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  TradingView Desktop  (host, CDP :9222)                     │
└────────────────────┬────────────────────────────────────────┘
                     │ tv CLI (Node, via host.docker.internal)
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  data-svc (writer)                                          │
│    • multi-feed polling loop                                │
│    • PLAUSIBLE_RANGES + overlap validation                  │
│    • INSERT ... ON CONFLICT DO NOTHING                      │
└────────────────────┬────────────────────────────────────────┘
                     │ writes
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  Postgres 16                                                │
│    bars(symbol, timeframe, ts, open, high, low, close, vol) │
│    cache_meta(symbol, timeframe, last_bar_ts, …)            │
└────────────────────┬────────────────────────────────────────┘
                     │ reads
        ┌────────────┴───────────────┐
        ▼                            ▼
┌───────────────────┐       ┌────────────────────────┐
│  bar-grpc (reader)│       │  Optuna / audit (psql) │
│    GetRecentBars  │       │    direct SQL on PG    │
│    HealthCheck    │       │                        │
└───────────────────┘       └────────────────────────┘
        │ gRPC :50051
        ▼
   bot-multi (trading repo, gRPC client)
```

**Why a separate service?** The bot is decoupled from data acquisition — multiple bot processes (even on multiple hosts) can consume the same bars without filesystem coupling. Data acquisition is single-writer by design (TradingView chart access is serialized).

## Quickstart

### Prerequisites
- Docker + Docker Compose
- TradingView Desktop running on the host with `--remote-debugging-port=9222`
- A TradingView chart tab open and focused on the symbol you want to track

### First run

```bash
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD (any strong value), FEEDS, TV_SYMBOL, etc.

git submodule update --init --recursive   # pull tradingview-mcp

docker compose up -d --build
docker compose logs -f data-svc bar-grpc
```

After a few seconds:
```bash
# Verify Postgres has data
docker compose exec postgres psql -U datasvc -d datasvc \
    -c "SELECT symbol, timeframe, COUNT(*) FROM bars GROUP BY 1,2 ORDER BY 1,2;"

# Verify gRPC reachable (requires grpcurl on host)
grpcurl -plaintext localhost:50051 list datasvc.v1.BarService
grpcurl -plaintext -d '{"symbol":"BTC/USDT:USDT","timeframe":"1h","min_bars":200}' \
    localhost:50051 datasvc.v1.BarService/HealthCheck

# Verify REST gateway (bar-rest) reachable
curl -sf http://localhost:8001/healthz | jq .
curl -sf 'http://localhost:8001/v1/quote/BINANCE%3ABTCUSDT' | jq .
# Interactive docs: open http://localhost:8001/docs in a browser.
```

### REST API

The `bar-rest` container exposes an HTTP gateway on port `8001` for any HTTP client that doesn't speak gRPC (Ghostfolio, Grafana, ad-hoc curl).

- **Architecture**: pure thin gateway over `BarService` + `AssetService` gRPC (both served by `bar-grpc:50051`). The REST process holds no Postgres connection of its own; every route is a translation from HTTP into a gRPC call and back. Spec §Motivation.
- **gRPC API**: see [data_svc/grpc_server/proto/bars.proto](data_svc/grpc_server/proto/bars.proto). `BarService.{GetRecentBars, GetBarsInRange, HealthCheck, Ping}` + `AssetService.{GetProfile, Search}`. Stubs regenerated via `make proto` (`buf generate`).
- **REST spec**: [docs/openapi.yaml](docs/openapi.yaml) — hand-authored OpenAPI 3.1, source of truth for the REST surface. Pydantic models are generated from it (`make codegen`); a drift test asserts the running app matches.
- **Swagger UI**: `http://localhost:8001/docs` once the stack is up.
- **Asset catalog**: [data_svc/assets.yaml](data_svc/assets.yaml) — loaded into the `assets` Postgres table by the `bar-grpc` service at startup. When you add a new feed to `FEEDS`, add a matching entry here so `/v1/quote`, `/v1/search`, and `/v1/profile` recognize the symbol.
- **Auth**: optional bearer via `REST_AUTH_TOKEN`. Unset = open mode. Set = every `/v1/*` request must carry `Authorization: Bearer <token>`; `/healthz` is always open.
- **Module path**: both `python -m data_svc.rest` and `python -m data_svc.rest_server` (alias matching the spec) work as entry points.
- **Host port override**: defaults to `8001:8001`. Set `REST_HOST_PORT=8003` in `.env` if your host already publishes 8001.
- **Ghostfolio integration**: configure a MANUAL data source with URL `http://hybrid-data-svc-bar-rest:8001/v1/quote/<SYMBOL>` (URL-encode the `:` to `%3A`) and selector `$.price`. The contract is asserted by `tests/rest/test_ghostfolio_contract.py` so it can't regress silently.

## Configuration

All knobs are environment variables. See [.env.example](.env.example) for defaults.

| Variable | Purpose | Default |
|---|---|---|
| `POSTGRES_PASSWORD` | Postgres password (set in `.env`, never commit) | _required_ |
| `FEEDS` | Comma-separated `symbol@timeframe@tv_symbol` tuples | _required_ |
| `BARS_TO_FETCH` | Bars per TV poll | `300` |
| `POLL_INTERVAL_SECONDS` | Max sleep between polls (loop auto-adapts to bar close) | `30` |
| `GRPC_LISTEN` | gRPC server bind | `0.0.0.0:50051` |
| `GRPC_TARGET` | Where bar-rest connects to BarService + AssetService gRPC | `bar-grpc:50051` |
| `REST_LISTEN_HOST` | REST gateway bind host | `0.0.0.0` |
| `REST_LISTEN_PORT` | REST gateway bind port | `8001` |
| `REST_HOST_PORT` | Host port the bar-rest container publishes to | `8001` |
| `REST_AUTH_TOKEN` | Optional bearer for `/v1/*` (unset = open mode) | _(empty)_ |
| `CDP_HOST` | Chrome DevTools host (auto-resolved inside container) | `host.docker.internal` |
| `CDP_PORT` | CDP port | `9222` |

## Deployment

The repo ships **two** Woodpecker pipelines:

- [`.woodpecker/pr.yml`](.woodpecker/pr.yml) — runs on every PR + push to `main`. Lint + codegen drift + tests + image build. **Does not deploy.**
- [`.woodpecker/deploy.yml`](.woodpecker/deploy.yml) — runs **only on a manual trigger** from the Woodpecker UI. Pulls `origin/main`, rebuilds the three project services in place, and polls `/healthz` to confirm readiness.

### Triggering a deploy

1. Open the repo in the Woodpecker UI → **New pipeline** → pick `deploy.yml`.
2. In the build-parameter form, set the variables below and submit.

| Manual build variable | Required | Purpose |
|---|---|---|
| `DEPLOY_MAIN` | **yes** (must equal `true`) | Flag authorizing the deploy of `main` to this host. The `guard` step fails the build if this isn't set to the literal string `true`. Anything else (including the unset case) refuses. |
| `DEPLOY_PATH` | no | Override the host clone location. Default: `$HOME/projects/hybrid-data-svc` (resolved from the runner user's real home via `eval echo "~$(id -un)"`, not the ephemeral per-step `$HOME`). |
| `REST_HOST_PORT` | no | Override the host port the post-deploy healthcheck polls. Default `8001`. Set this if you also overrode `REST_HOST_PORT` in `.env` on the host. |

> **⚠️ Schema migrations are NOT applied by this pipeline.** The Postgres init scripts under
> `./migrations/*.sql` are mounted as `/docker-entrypoint-initdb.d/*.sql` and only run on the
> **very first** initialization of the `pgdata` volume. On a host where the volume already
> exists, any new migration file added by a PR is **ignored silently** by `docker compose up`.
>
> **The `healthz` step still passes with a stale schema** — it only checks gRPC + DB
> reachability (`SELECT 1`), not table/column structure. A schema-drift bug will only
> surface at runtime under real query load.
>
> If the release you're deploying introduces a new migration file (e.g. `003_*.sql`), apply
> it manually **before** triggering this pipeline:
>
> ```bash
> docker compose exec -T postgres psql -U datasvc -d datasvc \
>     -f /docker-entrypoint-initdb.d/003_your_migration.sql
> ```
>
> Audit which migration files are new vs what's currently deployed. Run this from
> inside `$DEPLOY_PATH` on the host, where `HEAD` points at the production checkout:
>
> ```bash
> git fetch origin main && git diff HEAD..origin/main -- migrations/
> ```

### What the deploy does (in order)

1. **guard** — refuses to proceed unless `DEPLOY_MAIN=true`.
2. **deploy** —
   - validates `$DEPLOY_PATH` exists AND is a git working tree (fails fast with a clear message otherwise)
   - aborts if the working tree has uncommitted edits to tracked files
   - `git fetch origin main && git reset --hard origin/main`
   - `docker compose up -d --build bar-grpc bar-rest data-svc`
3. **healthz** — polls `http://localhost:$REST_HOST_PORT/healthz` every 5s for up to 60s; fails the build if it never returns `200`.

Pushes to `main` therefore do *not* auto-deploy; deploys are a deliberate operator action.

## Migrating from SQLite (bars.db)

If you have an existing `bars.db` SQLite file (e.g. from the previous monolithic setup), seed Postgres in one shot:

```bash
docker compose exec data-svc \
    python -m scripts.seed_from_sqlite \
        --src /seed/bars.db \
        --pg-url "$POSTGRES_URL"
```

Mount the legacy `bars.db` into the container before running (see [scripts/seed_from_sqlite.py](scripts/seed_from_sqlite.py)). The script is idempotent — re-runs are safe.

## Public gRPC API

Proto: [data_svc/grpc_server/proto/bars.proto](data_svc/grpc_server/proto/bars.proto)

```protobuf
service BarService {
    rpc GetRecentBars(GetRecentBarsRequest) returns (BarsResponse);
    rpc HealthCheck(HealthRequest) returns (HealthResponse);
}
```

Bot integration: generate Python stubs from this proto, then call `GetRecentBars(symbol="BTC/USDT:USDT", timeframe="1h", count=300)`.

## Cross-symbol leak guards

Three layers defend against TradingView chart-switch races contaminating the cache:

1. **PLAUSIBLE_RANGES** — per-symbol absolute price band (e.g. BTC ∈ [1k, 1M]). Rejects inserts/fetches outside the band.
2. **Drift guard** — rejects new bars whose close is >50% off the most recent cached close for that symbol.
3. **Overlap validation** — on every fetch, the most recent cached bar's close is re-fetched and compared against cache; mismatch >0.001% invalidates the cache for that (symbol, timeframe) and triggers full refetch.

See [data_svc/db/cache.py](data_svc/db/cache.py) for implementation.

## License

MIT — see [LICENSE](LICENSE).
