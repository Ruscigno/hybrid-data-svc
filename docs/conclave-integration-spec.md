# Conclave integration — engineering spec

> **Audience.** Developer working on `hybrid-data-svc`.
> **Driver.** [Conclave ADR-0009](https://github.com/Ruscigno/conviction/blob/main/docs/adr/0009-hybrid-data-svc-as-market-data-backend.md) (proposed → accepted on 2026-05-13 after founder sign-off on all five open items). Conclave is a multi-agent equity-research swarm that will consume bars from this service via gRPC. Conclave does **not** maintain its own market-data store; this service is authoritative.
> **Status.** Approved scope. Three features to land; suggested order is F-08-follow-up → F-11 → F-12 (each can ship in its own PR).

## 0. TL;DR

Three changes are required so Conclave can use this service for equity-research meetings:

| # | Feature | Why | Where it lives |
|---|---|---|---|
| **F-08-follow-up** | Load the V1 25-ticker equity universe into `FEEDS` and generalize `PLAUSIBLE_RANGES` so it works for equities. | Conclave's V1 universe is US equities (`AAPL`, `SPY`, …), not crypto. The service can chart any TradingView symbol; today only crypto is wired up. | `data_svc/db/cache.py`, `.env.example`, `docker-compose.yml` (new `equities` profile or merged `FEEDS`). |
| **F-11** | Add optional `as_of_ts` field to `GetRecentBarsRequest`; when set, the server filters `WHERE ts ≤ as_of_ts` before returning. Default (unset/0) keeps existing behavior. | Conclave's look-ahead discipline is non-negotiable; the meeting graph passes a meeting timestamp and the data layer must refuse rows from after that point. Server-side filter is more efficient than the client-side fallback. | `data_svc/grpc_server/proto/bars.proto`, `data_svc/grpc_server/service.py`, `data_svc/db/cache.py`. |
| **F-12** | Bearer-token gRPC auth via a server-side interceptor. Token from env (`AUTH_TOKEN`); empty/unset disables auth (loopback-dev mode). | Conclave deploys to Cloud Run and will reach this service over a non-loopback network. Plaintext + no-auth is fine for localhost dev but unacceptable in production. | `data_svc/grpc_server/__main__.py` (new), `data_svc/grpc_server/auth.py` (new). |

No breaking changes to existing consumers (the `trading-bot-multi` client keeps working unchanged for all three).

---

## 1. Background

`hybrid-data-svc` was built as the data layer for `hybrid-quant-bot`, a single-symbol crypto trading bot. The proto surface (`GetRecentBars`, `HealthCheck`) reflects that one use case: "give me the most recent N bars for this pair so my trading loop can decide what to do *now*."

Conclave's use case is different in three ways:

1. **Equities, not crypto.** The 25-ticker universe per championship is US equities (`AAPL`, `MSFT`, `SPY`, etc.). TradingView Desktop can chart any of them via `NASDAQ:AAPL`-style symbols; the service's polling architecture is symbol-agnostic, but `PLAUSIBLE_RANGES` and the multi-feed config currently assume crypto.
2. **Historical-as-of reads, not "most recent."** A Conclave meeting timestamped 2026-04-15 must read prices as they were on 2026-04-15 — never anything newer. The bot reads "most recent" only because it trades in real time; Conclave's agents need to reason about an arbitrary `as_of` in the past or present.
3. **Reachable from Cloud Run.** Conclave's backend deploys to GCP Cloud Run. The gRPC connection will cross a network — bearer-token auth becomes non-optional.

The rest of this spec details each change.

---

## 2. Feature F-08-follow-up — Equity feeds

### 2.1 Scope

- Make the equity 25-ticker universe loadable via `FEEDS`.
- Generalize `PLAUSIBLE_RANGES` so adding equities doesn't require Python edits per symbol.
- Keep the existing crypto feeds working unchanged (the bot still uses them).

### 2.2 What stays the same

- The polling loop, the TradingView CDP pathway, the cache-validation flow ([`data_svc/db/cache.py`](../data_svc/db/cache.py)), and the bar storage schema (`bars(symbol, timeframe, ts, open, high, low, close, volume, fetched_at)`) all stay as-is.
- The `FEEDS` env-var format (`db_symbol@timeframe@tv_symbol`, comma-separated) stays as-is.
- The 30-second poll interval stays as-is at the loop level (the loop already adapts to bar-close cadence, so polling a daily bar every 30 s is harmless — almost every call is a no-op).

### 2.3 What changes

#### 2.3.1 `PLAUSIBLE_RANGES` becomes data-driven

Today, [`data_svc/db/cache.py`](../data_svc/db/cache.py) hardcodes a Python dict of 5 crypto symbols. Adding 25 equities by editing the dict is fine but doesn't scale.

**Proposed shape.** Move the dict to a YAML file, e.g. `data/plausibility.yaml`, that the service reads at startup:

```yaml
# Format: db_symbol -> [min_close, max_close]. Symbols not listed are
# accepted (no plausibility check). Use generous bands — the goal is to
# catch chart-switch races, not to validate price moves.
crypto:
  "BTC/USDT:USDT": [1_000.0, 1_000_000.0]
  "ETH/USDT:USDT": [100.0, 50_000.0]
  "BNB/USDT:USDT": [20.0, 10_000.0]
  "SOL/USDT:USDT": [5.0, 10_000.0]
  "XRP/USDT:USDT": [0.05, 100.0]
equities:
  "NASDAQ:AAPL":  [10.0, 1_000.0]
  "NASDAQ:MSFT":  [50.0, 2_000.0]
  "NYSE:JPM":     [20.0, 1_000.0]
  "NYSE:SPY":     [50.0, 2_000.0]
  # ... 21 more, populated when Conclave finalizes its 25-ticker universe
```

`cache.py` loads the file at import time and flattens both sections into one lookup dict. Bands are intentionally generous — these are leak guards (catch a TV chart-switch race where SPY-tagged rows actually came from a crypto chart), not price validators.

**Backward compatibility.** If `data/plausibility.yaml` is absent, fall back to the existing hardcoded crypto dict so the current bot deployment doesn't break.

#### 2.3.2 `INSERT_DRIFT_REJECT` adjustment for equity stock splits

The drift guard rejects new bars whose close diverges > 50% from the most recent cached close. For equities, a 2-for-1 split produces a 50% drop on the split day — false-positive risk.

**Proposed move.**
- Keep the 50% threshold (it's a sane upper bound for "this can't be the same symbol").
- On rejection, log loudly (existing behavior is correct) and **stop accepting more bars** until a human reviews. A genuine cross-symbol leak is data corruption; a stock split is rare and worth a manual `python -m data_svc.backfill --symbol NYSE:SPY --invalidate-from <split_date>`.
- Optional follow-up: add a `--allow-drift-once` CLI flag to `data_svc/backfill.py` for the operator to whitelist a known split. Not required for V1.

#### 2.3.3 Multi-feed config example

Add a worked example to `.env.example`:

```bash
# Conclave V1 equity universe — example with 4 of the 25 tickers.
# Symbol format: <db_key>@<timeframe>@<tradingview_symbol>
# db_key matches what Conclave's adapter sends (NASDAQ-prefixed for
# Nasdaq tickers, NYSE-prefixed for NYSE; matches what TradingView uses).
FEEDS=NASDAQ:AAPL@1D@NASDAQ:AAPL,NASDAQ:MSFT@1D@NASDAQ:MSFT,NYSE:JPM@1D@NYSE:JPM,NYSE:SPY@1D@NYSE:SPY
```

The TradingView Desktop chart tab must show each symbol in turn for the polling loop to capture it. The existing tab-pin / focus logic in [`data_svc/tab_pin.py`](../data_svc/tab_pin.py) handles rotation.

### 2.4 Non-goals (out of scope for F-08-follow-up)

- **Market-hours awareness.** Conclave doesn't need it for V1 — daily bars are end-of-day anyway; polling during off-hours just hits the cache.
- **A separate equities docker-compose profile.** One service instance can carry both crypto and equity feeds via the same `FEEDS` list. Don't split the deployment unless polling throughput becomes a problem.
- **Per-symbol polling frequency.** Single `POLL_INTERVAL_SECONDS` is fine.

### 2.5 Acceptance criteria

- [ ] `data/plausibility.yaml` exists and is loaded by `data_svc/db/cache.py` at startup.
- [ ] Adding 25 equity entries to `FEEDS` + `data/plausibility.yaml` does not require Python code changes.
- [ ] Existing crypto feeds (`BTC/USDT:USDT`, etc.) continue to work unchanged with the existing bot client.
- [ ] `data/plausibility.yaml` absent → service falls back to hardcoded crypto dict, no crash.
- [ ] A live test: load `NYSE:SPY` into `FEEDS`, point TradingView Desktop at it, run for 1 hour, verify `bars` table has rows with `symbol = 'NYSE:SPY'` and close prices in a reasonable range.

---

## 3. Feature F-11 — `as_of_ts` parameter on `GetRecentBars`

### 3.1 Scope

Add an optional `as_of_ts` field to `GetRecentBarsRequest`. When set (> 0), the service returns the most recent `count` bars with `ts ≤ as_of_ts`. When unset (the proto3 default of 0), behavior is unchanged.

### 3.2 Proto change

Edit [`data_svc/grpc_server/proto/bars.proto`](../data_svc/grpc_server/proto/bars.proto):

```protobuf
message GetRecentBarsRequest {
    string symbol    = 1;   // ccxt or NASDAQ:TICKER style — must match a row in PLAUSIBLE_RANGES.
    string timeframe = 2;   // "15", "30", "1h", "4h", "1D", "1W"
    int32  count     = 3;   // bars to return (server may cap at 5000)

    // Optional upper bound on bar-open timestamp (unix epoch seconds, UTC,
    // inclusive). When > 0, the server returns the most recent `count` bars
    // whose ts <= as_of_ts. When 0 or unset, no filter (current behavior).
    //
    // Use case: historical-as-of reads. A consumer reasoning about how the
    // market looked on a past date passes that date's end-of-day timestamp
    // and gets back only bars at or before that moment.
    int64  as_of_ts  = 4;
}
```

**Why optional with default 0.** proto3 scalar fields default to zero and the existing bot client doesn't set the field — it will continue to get unfiltered "most recent" reads. No client recompilation forced.

### 3.3 Service change

Edit [`data_svc/grpc_server/service.py`](../data_svc/grpc_server/service.py) `GetRecentBars`:

```python
def GetRecentBars(self, request, context):  # noqa: N802
    symbol = request.symbol
    timeframe = request.timeframe
    count = max(1, min(int(request.count or 300), _MAX_BARS))
    as_of_ts = int(request.as_of_ts) if request.as_of_ts > 0 else None

    df = self._cache.read_bars(symbol, timeframe, count, as_of_ts=as_of_ts)
    bars = [
        _pb.Bar(
            ts=int(row["time"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
        for _, row in df.iterrows()
    ]
    return _pb.BarsResponse(bars=bars)
```

### 3.4 Cache change

Edit [`data_svc/db/cache.py`](../data_svc/db/cache.py) `read_bars` (and the internal `_read_bars`):

```python
def read_bars(
    self,
    symbol: str,
    timeframe: str,
    count: int,
    as_of_ts: Optional[int] = None,
) -> pd.DataFrame:
    return self._read_bars(symbol, timeframe, count, as_of_ts)


def _read_bars(
    self,
    symbol: str,
    timeframe: str,
    count: int,
    as_of_ts: Optional[int] = None,
) -> pd.DataFrame:
    sql = """
        SELECT ts, open, high, low, close, volume
          FROM bars
         WHERE symbol = %s AND timeframe = %s
    """
    params: list[object] = [symbol, timeframe]
    if as_of_ts is not None:
        sql += " AND ts <= %s"
        params.append(int(as_of_ts))
    sql += " ORDER BY ts DESC LIMIT %s"
    params.append(int(count))

    with self._pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    return df.iloc[::-1].reset_index(drop=True)
```

The existing index `idx_bars_symbol_tf_ts` (on `(symbol, timeframe, ts DESC)`, per [`migrations/001_init_bars.sql`](../migrations/001_init_bars.sql)) already covers the new WHERE clause efficiently — no migration needed.

### 3.5 Edge cases — required behavior

| Scenario | Expected behavior |
|---|---|
| `as_of_ts = 0` or field unset | No filter; existing "most recent N bars" behavior. |
| `as_of_ts` between two cached bars | Returns the most recent N bars whose `ts ≤ as_of_ts`. |
| `as_of_ts` before the oldest cached bar | Returns empty `BarsResponse{bars=[]}`. Consumer handles. |
| `as_of_ts` far in the future | Equivalent to unset — returns most recent N bars. |
| `as_of_ts` is negative | Treat as `None` (no filter). Don't 400. |

### 3.6 Test cases

In whichever test framework you use (or a new one if there isn't one yet):

1. **Existing behavior preserved.** `GetRecentBars(symbol='BTC/USDT:USDT', timeframe='1h', count=10)` (no `as_of_ts`) returns the same response as before this change.
2. **Historical read.** Seed a known small fixture (e.g., 100 bars at 1-hour spacing starting 2024-01-01); request `count=20, as_of_ts=<midpoint>`; verify all returned bars have `ts ≤ as_of_ts` and the response has at most 20 bars.
3. **Before-cache read.** With the same fixture, request `as_of_ts=<before-oldest>`; verify empty response.
4. **Future read.** Request `as_of_ts=<far future>`; verify it's equivalent to unset.

### 3.7 Acceptance criteria

- [ ] Proto regenerated; `bars_pb2.py` reflects the new field.
- [ ] Service accepts `as_of_ts` and filters when > 0.
- [ ] `EXPLAIN ANALYZE` on a representative query (one symbol, timeframe, as_of_ts midway) uses `idx_bars_symbol_tf_ts`.
- [ ] All four edge cases above pass.
- [ ] The existing bot client (no `as_of_ts` set) sees no behavior change.

---

## 4. Feature F-12 — Bearer-token gRPC auth

### 4.1 Scope

Add a server-side gRPC interceptor that validates a shared bearer token. If the `AUTH_TOKEN` env var is unset, the interceptor is a no-op (loopback dev mode). When set, every RPC must carry `Authorization: Bearer <token>` in its metadata.

### 4.2 Auth scheme

- **Single shared static token.** Rotation = restart with a new env var value. Conclave will hold the token in GCP Secret Manager (`hybrid-data-svc-auth-token`) and inject it into its Cloud Run runtime; this service holds it in its own env (founder's choice of source — `.env`, Compose secret, or Secret Manager if running on GCP).
- **Header format:** `Authorization: Bearer <token>` (HTTP/2 metadata, lowercase key per gRPC convention).
- **Constant-time compare** (`hmac.compare_digest`) to avoid timing leaks.

mTLS was considered and rejected for V1: more setup overhead, no operational benefit at this trust level (single trusted consumer, founder-owned both ends).

### 4.3 Implementation

#### New file: `data_svc/grpc_server/auth.py`

```python
"""Bearer-token gRPC server interceptor. Loopback-dev mode when token unset."""
from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

import grpc

logger = logging.getLogger(__name__)


def _unauth(message: str) -> grpc.RpcMethodHandler:
    def reject(_request, context: grpc.ServicerContext):
        context.abort(grpc.StatusCode.UNAUTHENTICATED, message)
    return grpc.unary_unary_rpc_method_handler(reject)


class BearerTokenInterceptor(grpc.ServerInterceptor):
    """Validates `Authorization: Bearer <token>` against AUTH_TOKEN env.

    When AUTH_TOKEN is unset or empty, the interceptor is a no-op — every
    RPC passes through. Use this for localhost-only dev; never deploy with
    an unset token to a non-loopback bind.
    """

    def __init__(self, expected_token: Optional[str]) -> None:
        self._expected = expected_token or ""
        if not self._expected:
            logger.warning(
                "[auth] AUTH_TOKEN unset — gRPC server running WITHOUT auth. "
                "OK for localhost dev; never deploy to a public network like this."
            )

    def intercept_service(self, continuation, handler_call_details):
        if not self._expected:
            return continuation(handler_call_details)

        metadata = dict(handler_call_details.invocation_metadata or [])
        auth = metadata.get("authorization", "")
        if not auth.startswith("Bearer "):
            return _unauth("missing bearer token")
        token = auth[len("Bearer "):]
        if not hmac.compare_digest(token, self._expected):
            return _unauth("invalid bearer token")
        return continuation(handler_call_details)


def build_interceptor() -> BearerTokenInterceptor:
    return BearerTokenInterceptor(os.getenv("AUTH_TOKEN"))
```

#### Wire it in `data_svc/grpc_server/__main__.py`

Wherever the server is constructed today (something like `grpc.server(thread_pool)`), pass the interceptor:

```python
from .auth import build_interceptor

server = grpc.server(
    futures.ThreadPoolExecutor(max_workers=8),
    interceptors=[build_interceptor()],
)
```

### 4.4 `.env.example` additions

```bash
# -----------------------------------------------------------------------------
# Auth — bearer token for gRPC. Leave UNSET to disable auth (localhost dev).
# -----------------------------------------------------------------------------
# Generate a strong token with: openssl rand -hex 32
# When set, every gRPC call must carry: Authorization: Bearer <token>
# Conclave (Cloud Run consumer) reads its copy from Secret Manager.
# AUTH_TOKEN=
```

### 4.5 docker-compose change

The `bar-grpc` service in [`docker-compose.yml`](../docker-compose.yml) already passes `env_file: [.env]`, so once `AUTH_TOKEN` is in `.env` it flows through. No compose edit required unless you want to make the dependency explicit:

```yaml
  bar-grpc:
    # ... existing config ...
    environment:
      AUTH_TOKEN: ${AUTH_TOKEN:-}  # explicit pass-through; empty default keeps dev mode
```

### 4.6 Conclave-side expectations (informational — not your work, just so you know what to expect)

Conclave's MCP adapter (CON-023) will use `grpc.metadata_call_credentials` to attach the token on every call:

```python
def _bearer_call_creds(token: str) -> grpc.CallCredentials:
    def _attach(_ctx, callback):
        callback([("authorization", f"Bearer {token}")], None)
    return grpc.metadata_call_credentials(_attach)
```

For Cloud Run → this-service connectivity, Conclave will use `grpc.secure_channel` with channel credentials (TLS-terminating proxy or VPC connector). The auth token rides on top of the TLS — your interceptor doesn't need to know about TLS, only the metadata.

### 4.7 Acceptance criteria

- [ ] `AUTH_TOKEN` unset → service starts, all RPCs succeed, warning logged at startup.
- [ ] `AUTH_TOKEN=secret123` → call without metadata → `UNAUTHENTICATED`. Call with `Authorization: Bearer wrong` → `UNAUTHENTICATED`. Call with `Authorization: Bearer secret123` → succeeds.
- [ ] Token comparison is constant-time (`hmac.compare_digest`) — verifiable by code review, no test needed.
- [ ] Token rotation procedure documented in [README.md](../README.md): "to rotate, set new `AUTH_TOKEN`, `docker compose up -d --force-recreate bar-grpc`, update consumer secret."

---

## 5. Out of scope for this engineering pass

Listed explicitly so scope creep is easy to reject.

- **`adj_close` field on bars.** Conclave V1 uses raw `close` and documents the limitation; Stage-3 unlock writes its own ADR (a new spec doc lands here at that time).
- **Corporate actions feed.** Deferred to Stage 3 per Conclave's ADR-0007. No work here for V1.
- **Symbol coverage RPC** (batch). Conclave uses the existing `HealthCheck` per-symbol as a coverage probe; sufficient at 25-ticker scale.
- **Fundamentals / sentiment / macro feeds.** Conclave declared these "nice-to-have for V1" and degrades gracefully without them.
- **mTLS for gRPC auth.** Bearer token is enough at V1's trust level.
- **Market-hours-aware polling cadence.** Polling continues at the current 30-second loop; cache hits are cheap.
- **Conclave's response to upstream outages.** Conclave's adapter handles `UNAVAILABLE` / `DEADLINE_EXCEEDED` and halts meetings cleanly — not your problem.
- **TradingView Desktop uptime monitoring.** Conclave will surface `UPSTREAM_UNAVAILABLE` via Sentry when this service is down; the founder watches that signal. No work needed here.

---

## 6. Rollout sequencing

Suggested order (each ships independently — no cross-PR dependencies):

1. **F-08-follow-up first.** Smallest blast radius. Lets you verify TradingView equity charting works for the 25-ticker universe before committing to the rest. Once one equity feed (e.g., `NYSE:SPY`) is producing daily bars cleanly in Postgres, you've validated the architecture.
2. **F-11 second.** Backward-compatible proto extension; existing bot client unaffected. Conclave's CON-023 adapter ships with client-side filtering as a fallback and switches to server-side once this lands.
3. **F-12 third.** Required only when Conclave deploys to Cloud Run. Can ship in parallel with F-11 if convenient; the interceptor doesn't touch the bar pathway.

---

## 7. Open questions (for the founder)

None that block implementation. The full 25-ticker universe list is the only outstanding detail — once you pick it, drop it into `FEEDS` and `data/plausibility.yaml`. The features themselves can be coded against any equity ticker.

---

## 8. Traceability

- Upstream decision: [Conclave ADR-0009](https://github.com/Ruscigno/conviction/blob/main/docs/adr/0009-hybrid-data-svc-as-market-data-backend.md).
- Conclave-side interface contract: [docs/market-data-requirements.md](https://github.com/Ruscigno/conviction/blob/main/docs/market-data-requirements.md) in the `conviction` repo.
- Architectural commitment: [Conclave ADR-0008](https://github.com/Ruscigno/conviction/blob/main/docs/adr/0008-founder-provided-market-data.md) (Conclave consumes from this service, stores no market data of its own).

When this spec is fully implemented, update Conclave's ADR-0009 status from `proposed` to `accepted` and update [docs/market-data-requirements.md](https://github.com/Ruscigno/conviction/blob/main/docs/market-data-requirements.md) Q1/Q2/Q3/Q6/Q7 with the answers ("yes, equity feeds loaded", "yes, `as_of_ts` supported", "raw close for V1", "bearer-token auth", "Sentry-driven monitoring on Conclave side").
