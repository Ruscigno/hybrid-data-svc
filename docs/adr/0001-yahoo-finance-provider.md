# ADR 0001 — Yahoo Finance market-data provider

- **Status:** Proposed
- **Date:** 2026-06-18
- **Deciders:** @Ruscigno (scope), informed by the `yahoo-rate-probe` experiment
- **Supersedes / superseded by:** —
- **Implementation:** phased; see [Rollout](#rollout). Spec → plan follow this ADR.

> First ADR in this repo; establishes `docs/adr/` (one record per significant,
> hard-to-reverse decision). Format is MADR-ish, adapted to capture the bundle of
> interlocking decisions that make up one feature.

---

## Context and problem statement

`hybrid-data-svc` acquires OHLCV bars by polling **TradingView Desktop over CDP**. That
path is **single-writer by design** — TradingView chart access is serialized — so the
number of symbols one host can track is tightly bounded. We want to track **thousands of US
equities** on ≥ 4h-ish timeframes, where to-the-second freshness is not required.

The `yahoo-rate-probe` experiment (this branch) established the empirical ground truth:

- Yahoo's public chart endpoint (`query1/2.finance.yahoo.com/v8/finance/chart`) is viable
  for high-volume polling **from a single residential IP**, but its edge **TLS-fingerprints
  clients** and returns `429` to non-browser stacks (`httpx`/`requests`/`curl`) on the first
  request. **`curl_cffi` browser impersonation is mandatory.**
- AIMD search found **no throttling up to 20 req/s** (the probe's safety cap); a 24h soak at
  20 rps / "all symbols every 15 min" is in progress to rule out an hourly/daily quota.
- Capacity is therefore *far* beyond the equities universe we want: `N_max ≈ R × 600`, so
  20 rps ⇒ ~12,000 symbols revisitable every 10 min. Tracking a few thousand stocks on a
  15-min cadence is comfortably within budget.

This ADR records how Yahoo becomes a **first-class second provider** behind the existing
gRPC + REST surface, without disturbing the TradingView path.

## Decision drivers (requirements)

From the design conversation (these are fixed inputs, not decisions):

1. **Universe:** top **1,500 by market cap per exchange** (NYSE, NASDAQ, NYSE American),
   **4,500 overall cap**. Pool includes **common stocks and ETFs** (ETFs ranked by net
   assets). Well within the probe's ~12k-symbol capacity.
2. **Cadence & shape:** fetch **1-min data every 15 min**; **aggregate** to all timeframes:
   `1min, 5min, 15min, 30min, 1h, 2h, 4h, 8h, 1d, 3d, 1w, 1mo`.
3. **Backfill:** as far back as Yahoo serves 1-min (commonly ~30 days).
4. **Rate limit owned by the Yahoo interface**, requests-per-minute configured in `.env`.
   Single instance — no multi-instance coordination required.
5. **Serve through the existing endpoints.** When the caller does not specify a provider,
   **TradingView (real-time) takes precedence** over Yahoo.

## Current architecture (what we build against)

- `bars(symbol, timeframe, ts, o,h,l,c,v, fetched_at)` PK `(symbol, timeframe, ts)` — **no
  provider column**. `cache_meta(symbol, timeframe, …)` likewise. (`migrations/001`.)
- `feeds(storage_symbol, timeframe, tv_symbol, status, …)` PK `(storage_symbol, timeframe)`
  — **TV-specific**; the writer reads polling targets here. (`migrations/003`.)
- `assets(symbol[TV id], storage_symbol[ccxt], name, exchange, currency, asset_class, …)`
  bridges TV identity ↔ storage identity; already supports `asset_class='EQUITY'`.
  (`migrations/002`.)
- Writer (`data_svc/fetcher.py`, `__main__.py`): **single-threaded, serialized**, one
  `(symbol, timeframe)` per call, **no aggregation** (TV returns per-timeframe bars).
- Serving (`data_svc/grpc_server/service.py`): `GetRecentBars`/`GetBarsInRange` are straight
  Postgres reads, **provider-agnostic**. REST maps through `data_svc/rest/_timeframes.py`.
- Config (`data_svc/config.py`): plain dataclass `from_env()` (not pydantic-settings);
  env vars are `UPPER_SNAKE`.
- Leak guards (`db/cache.py`): `PLAUSIBLE_RANGES` (crypto price bands), drift, overlap — all
  defend against **TV chart-switch races**, which Yahoo (per-symbol HTTP) does not have.

---

## Decisions

### D1 — Multi-provider storage: add a `provider` column (not a separate table)

**Options**
- **(a) `provider` column** on `bars`/`cache_meta`/`feeds`; PKs extended with `provider`.
- (b) Separate `bars_yahoo` table; serving UNIONs + merges.
- (c) Separate database for Yahoo.

**Decision: (a).** One unified schema and one serving path; precedence becomes a `WHERE` /
resolver concern rather than a UNION in every query. The `assets` table already anticipates
multiple sources, so a `provider` dimension is the honest model.

**Consequences**
- (+) Single read path; precedence/override is a filter, not a merge.
- (+) Existing rows backfill cleanly to `provider='tradingview'` (default).
- (−) Touches the **core `bars` PK** and every read query — the highest-risk change; done
  first and in isolation (Phase 1) so it's provable with existing TV data before any Yahoo
  code exists.

### D2 — Aggregate everything from 1-min; accept ~30-day history

**Options:** (a) pure 1-min aggregation; (b) hybrid (intraday from 1-min, daily/weekly/
monthly from Yahoo's native daily endpoint for years of history); (c) native per-timeframe.

**Decision: (a) pure 1-min aggregation.** Simplest single ingest path, matching the stated
intent.

**Consequences**
- (+) One fetch shape, one code path; aggregation is a pure function over 1-min bars.
- (−) **All** timeframes inherit the ~30-day 1-min horizon: monthly ≈ 1–2 bars, weekly ≈ 4,
   3-day ≈ 10, daily ≈ 30. Acceptable now; a **native-daily backfill** can be added later
   (revisit this ADR) if long history is needed for backtests.
- Daily-and-up bars are bucketed by **US/Eastern trading session** (not UTC calendar) so a
  `1d` bar = one trading day. `8h`/`3d` are non-standard and defined by fixed-anchor
  bucketing (detail deferred to the implementation spec).

### D3 — Serving precedence: per-series, TradingView wins, explicit override

**Options:** (a) per-series (if TV has the symbol+tf, serve TV entirely, else Yahoo);
(b) per-bar merge (TV wins on conflicting `ts`, fill from Yahoo); (c) most-recent-write wins.

**Decision: (a) per-series TV precedence**, with an explicit `provider` override that skips
the resolver.

**Consequences**
- (+) One source per response — predictable, no seams where two sources meet, reproducible
  for backtests.
- (+) Resolver is a cheap existence check (`cache_meta` lookup for `provider='tradingview'`).
- (−) No automatic gap-filling of TV from Yahoo; callers wanting Yahoo ask for it explicitly.
- API: optional `provider` field on `GetRecentBars`/`GetBarsInRange` (gRPC) and `?provider=`
  (REST). Resolver lives in the **service layer** — no precedence logic duplicated across
  transports (repo principle: gRPC/REST are transports over a shared service layer).

### D4 — Separate, concurrent Yahoo writer process

**Options:** (a) a new writer process/container; (b) a thread/loop inside the existing
`data-svc` writer.

**Decision: (a) separate process** (`data_svc/yahoo_writer.py`, its own entrypoint +
compose service).

**Rationale:** the TV writer is **serialized and CDP-bound**; the Yahoo writer is
**concurrent and HTTP-bound** with its own rate limiter. Mixing them into one serialized loop
would bottleneck Yahoo behind TV chart switches and entangle two very different failure
modes.

**Consequences**
- (+) Independent scaling, deploy, and failure isolation; the rate limiter is wholly owned by
  the Yahoo process (matches driver #4, single instance).
- (−) A second long-running service to operate (compose entry, healthcheck, logs).

### D5 — Yahoo feed = one row per symbol (1-min base); derived timeframes are global config

Because we **fetch 1-min and aggregate**, a Yahoo feed is **one `feeds` row per symbol** with
base `timeframe='1'`, `provider='yahoo'`, `provider_symbol=<yahoo ticker>`. The set of
derived timeframes (the 12) is **global config**, not per-feed rows.

**Consequences**
- (+) Onboarding a stock = one feed row; no 12× row fan-out per symbol.
- (+) `cache_meta` still tracks each derived `(symbol, timeframe, 'yahoo')` independently.
- (−) Per-symbol timeframe customization isn't expressible (acceptable — the universe is
   uniform). Revisit if per-symbol timeframe subsets are ever needed.

### D6 — Yahoo client owns the rate limit (curl_cffi + in-process rpm limiter)

A `data_svc/providers/yahoo/` module is the **only** thing that talks to Yahoo:
`client.py` = curl_cffi browser-impersonation session(s) + an **in-process token-bucket rate
limiter** sized by `YAHOO_RPM` (`.env`). Single instance is authoritative (driver #4); no
distributed coordination. Browser TLS impersonation is **mandatory** (probe finding).

**Consequences**
- (+) Rate policy lives in one place; the writer just submits work.
- (+) Directly reuses what the probe proved (impersonation, pacing, 429 handling).
- (−) Correctness of the global limit depends on the single-instance assumption; running two
   Yahoo writers would double the effective rate (documented constraint).

### D7 — Universe = top-1,500 per exchange (stocks + ETFs) by size, via the NASDAQ screener API

The Nasdaq Trader **symbol directory** (used by the probe's `build_symbols.py`) has **no
market-cap field**, so ranking needs a different source. A `build_catalog.py` generator uses
the **NASDAQ screener API** — the stock screener (`api.nasdaq.com/api/screener/stocks`) **and**
the ETF screener (`/api/screener/etf`), each returning symbol + exchange + a size field
(market cap for stocks, net assets for ETFs). It merges both per exchange, takes the **top
1,500 by size per exchange** (NASDAQ, NYSE, AMEX) as a **combined stock+ETF ranking**, caps at
**4,500**, validates each against Yahoo, and seeds `assets` (`asset_class='EQUITY'` or
`'ETF'`) + one `feeds` row each (`provider='yahoo'`, `status='pending'`).

**Consequences**
- (+) Real size-based ranking across stocks and ETFs; re-runnable to refresh membership as
   caps/AUM drift.
- (−) Two **undocumented endpoints** (curl_cffi again; tolerate schema/availability changes).
   Ranking mixes market cap (stocks) and net assets (ETFs) — both "size" proxies but not
   identical; acceptable for a coverage cut, not a precise ordering. Membership is a
   **point-in-time snapshot**; refresh cadence is operational (e.g. re-run monthly).

### D8 — Stock-specific lightweight validation (do not reuse the crypto/TV guards)

The TV path's `PLAUSIBLE_RANGES` / drift / overlap guards exist to defend against **chart-
switch races** and are crypto-tuned. Yahoo fetches one symbol per HTTP call — no race. The
Yahoo path gets a **separate, lightweight** validation: well-formed OHLCV, positive prices,
strictly increasing timestamps, and gap logging. (Targeted improvement, not forcing the wrong
guard onto a different source.)

---

## Schema changes (`migrations/004_provider.sql`)

| Table | Change |
|---|---|
| `bars` | `+ provider TEXT NOT NULL DEFAULT 'tradingview'`; PK → `(symbol, timeframe, provider, ts)`; index updated to lead with `provider`. |
| `cache_meta` | `+ provider`; PK → `(symbol, timeframe, provider)`. |
| `feeds` | `+ provider TEXT NOT NULL DEFAULT 'tradingview'`; rename `tv_symbol` → `provider_symbol`; PK → `(storage_symbol, timeframe, provider)`. |

Existing rows backfill to `'tradingview'` so the TV path is byte-for-byte unaffected. Migration
is **not** auto-applied by the deploy pipeline (see README §Deployment) — applied manually
before rollout.

## Timeframe vocabulary additions

REST token → storage code (canonical), extending `data_svc/rest/_timeframes.py` + the
generated Timeframe enum + `openapi.yaml`:

| TF | REST | Storage |
|---|---|---|
| 1 min | `1m` | `1` |
| 5 min | `5m` | `5` |
| 15 min | `15m` | `15` |
| 30 min | `30m` | `30` |
| 1 hour | `1h` | `1h` |
| 2 hour | `2h` | `2h` |
| 4 hour | `4h` | `4h` |
| **8 hour** | `8h` | `8h` *(new)* |
| 1 day | `1D` | `1D` |
| **3 day** | `3d` | `3D` *(new)* |
| 1 week | `1W` | `1W` |
| **1 month** | `1mo` | `1M` *(new)* |

> `1mo`/`1M` is deliberately **not** `1m`/`1M-ambiguous` — the repo has prior timeframe-
> translation bugs (`BUG_tv_non_json.md`, the `15m`→`15` fix), so monthly uses the explicit
> `1mo` REST token.

## Component map

```
data_svc/
  providers/yahoo/
    client.py         # curl_cffi sessions + in-process rpm rate limiter (YAHOO_RPM)
    parse.py          # chart JSON -> 1-min OHLCV DataFrame
    build_catalog.py  # NASDAQ screener -> top-1000/exchange -> validate -> seed assets+feeds
  aggregate.py        # pure: 1-min DataFrame -> each target timeframe (session-aware)
  yahoo_writer.py     # separate process: every 15 min, fetch 1-min + aggregate + store
  db/cache.py         # +provider filter on reads/writes; lightweight Yahoo validation path
  grpc_server/service.py  # +provider field; per-series TV-precedence resolver
  rest/...            # +?provider=; new timeframe tokens
migrations/004_provider.sql
```

## Config additions (`.env`, `DataSvcConfig.from_env()`)

| Var | Purpose | Default |
|---|---|---|
| `YAHOO_ENABLED` | master switch for the Yahoo writer | `false` |
| `YAHOO_RPM` | requests/minute the Yahoo interface enforces | _required when enabled_ |
| `YAHOO_POLL_INTERVAL` | seconds between cycles | `900` |
| `YAHOO_TIMEFRAMES` | derived timeframe set | the 12 above |
| `YAHOO_WORKERS` | client concurrency (≥ rpm × latency) | `12` |
| `YAHOO_IMPERSONATE` | curl_cffi fingerprint | `chrome` |

## Rollout

Tracer-bullet first; each phase is independently shippable and testable.

1. **Foundation** — migration 004 + backfill TV bars to `provider='tradingview'` + serving
   precedence/override + `provider` API field. *Provable with existing data; no Yahoo code.*
2. **Yahoo vertical (1 symbol)** — provider module + writer + aggregation, AAPL end-to-end.
3. **Backfill** — ~30-day 1-min on onboarding, chunked ≤ 8 days/request.
4. **Catalog at scale** — screener generator + ramp to 4,500.
5. **Ops/polish** — compose service, status surfacing, metrics, docs.

## Consequences (overall)

**Positive**
- Scales tracked-symbol count ~10× beyond the serialized TV path, at near-zero marginal cost.
- TV path is untouched (default `'tradingview'`, separate writer); precedence keeps real-time
  data authoritative.
- The expensive, risky change (provider column) lands first, in isolation, against known-good
  TV data.

**Negative / risks**
- ~30-day history ceiling for all timeframes (D2) — revisit for long-history backtests.
- Two undocumented Yahoo endpoints (chart + screener) — both need curl_cffi and tolerance for
  upstream change; a 24h soak is validating the chart endpoint's sustained limits.
- Single-instance rate-limit correctness (D6) — must not run two Yahoo writers.
- A core-PK migration on `bars` that the deploy pipeline won't auto-apply (manual step).

## Open questions

- **Catalog refresh cadence** — manual re-run vs scheduled; default manual for now.
- **Aggregation anchors for `8h`/`3d`** — exact fixed-anchor definition deferred to the
  implementation spec.
- **Soak outcome** — if the 24h soak surfaces a daily cap below our projected ~432k req/day
  (4,500 symbols × 96 cycles), `YAHOO_RPM` / cadence get tuned down; the architecture is
  unaffected. (At 20 rps a 4,500-symbol burst drains in ~3.75 min, well inside the 15-min
  cycle.)
