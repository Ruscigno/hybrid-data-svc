# ADR 0002 — Absorbing market-data-service into hybrid-data-svc

- **Status:** Accepted
- **Date:** 2026-06-22
- **Deciders:** @Ruscigno + @wmatsushita (Wagner, business partner)
- **Supersedes:** ADR 0001 **§D2** ("pure 1-min aggregation"); extends ADR 0001 otherwise.

## Context

`market-data-service` (Wagner's, `github.com/wmatsushita/market-data-service`) is a
production-grade **Yahoo Finance → OHLCV** service (Redis + InfluxDB; distributed rate
limiting; 1m+1d fetch with derived timeframes; 365-day backfill; corporate actions; proxy
rotation; leader election; Prometheus metrics; a multi-source extension). It is being
**decommissioned**. Rather than rebuild, we **transport its best patterns into
`hybrid-data-svc`** so it becomes the single, better hybrid market service.

A three-agent read-only study of `market-data-service` produced the adopt/keep/defer split
recorded below. This ADR is the design of record for that absorption.

## Decisions

### D2′ (supersedes ADR 0001 D2) — Two-tier fetch: native **1m + 1d**, derive the rest

Fetch **only `1m` and `1d`** from Yahoo. Derive the intraday timeframes
(`5m,15m,30m,1h,2h,4h,8h`) from the stored **1m** series; derive the daily-plus timeframes
(`3d,1w,1mo`) from the stored **1d** series. `1m` and `1D` are **native feeds** — never
aggregated.

- **Why:** Yahoo serves `1m` for only ~30 days but `1d` for *years*. The old D2 (pure-1m)
  capped **every** timeframe at ~30 days (`1mo` ≈ 1 bar). Two-tier gives real multi-year
  daily/weekly/monthly history.
- **Bonus:** it *simplifies* aggregation — deriving `3d/1w/1mo` from already-session daily
  bars is plain calendar grouping; the fragile "1-min → US/Eastern session" logic for
  daily-plus goes away (only the intraday UTC-bucket path remains for 1-min-derived).
- **Consequences:** the aggregation engine takes a **base series per target** (`BASE_TF`
  map); `1D` becomes a native feed, not a derived one; backfill (D10) becomes meaningful.

### D9 — Source abstraction (the `provider` made an interface)

A `Source` interface — `fetch(symbol, interval, period, start, end) -> DataFrame` — with one
implementation per provider (`YahooSource`, later others), resolved from a **registry dict**
keyed on the `provider` string (Phase 1's `bars.provider` column). Mirrors
`market-data-service`'s `OHLCVSource` Protocol + `get_source()` factory; we use a registry
(open/closed) rather than an `if/elif` chain. `FetchJob.source` ≡ our `provider`.

### D10 — Backfill: backward-walking chunks + Postgres watermarks

Walk backward in chunks (`7d` for `1m` to ~365d; `60d` for `1d` to ~5y), tracking the oldest
stored ts + a done-flag per `(symbol, timeframe, provider)` in a **`backfill_progress`**
Postgres table (replacing `market-data-service`'s Redis keys). Runs idle/low-priority.
Terminates on the depth target or when Yahoo returns nothing older (stall watermark).

### D11 — Corporate-action discontinuity detection + repair

Port the pure-pandas utilities verbatim: `find_jumps(df, threshold=20%)` (close-to-close
jumps) + `is_stale_vs_refetch(stored, refetched, rel_err=1%)`. On the daily series, detect
adjustment discontinuities (yfinance's `auto_adjust` flip era), validate by a small refetch,
and **repair** the affected window via `INSERT … ON CONFLICT DO UPDATE`. Always fetch
`auto_adjust=True, prepost=False`.

### D12 — Adaptive 429 auto-throttle in the Yahoo client

On a `429`, cut the effective rpm to **75%** (with a small floor); restore gradually
(`+N` rpm per `K` clean cycles). Honor `Retry-After`. In-process for the single writer — the
rate probe's AIMD, productionized as a runtime control.

### D13 — Declarative schedule / derivation table

A `yahoo_schedules.yaml`: per timeframe → `source` (`yahoo` native vs `aggregate:<base_tf>`),
`interval`, `period`, `cron`, `priority`. Single declarative source of truth for the
native-vs-derived routing (D2′) and cadence. Drives the writer.

### D14 — Observability + boot audit + calendar-aware staleness

Prometheus metrics endpoint with **per-provider** counters (requests/failures), plus
queue/staleness gauges; an **immediate integrity audit on writer startup** (catch gaps
accrued during downtime); and **NYSE-calendar** staleness (`exchange_calendars` `XNYS`)
instead of a raw 24h threshold.

## Keep ours (we're ahead / deliberately different)

- **`curl_cffi` browser-TLS impersonation.** `market-data-service` uses `yfinance` + plain
  `requests` (non-browser TLS) and leans on throttling + proxies to survive bans — it eats
  the edge **TLS-fingerprint 429 wall** that `curl_cffi` sidesteps. Our client stays on
  `curl_cffi`. **This is the one place we're ahead.**
- **Postgres + upsert** (not InfluxDB). The OHLCV model maps 1:1; `ON CONFLICT DO UPDATE`
  replicates InfluxDB's last-write-wins, giving free repair upserts (D11).

## Defer until multi-pod

Redis **distributed** sliding-window limiter, the Redis **job queue** (high/low/DLQ),
**leader election**, and **proxy rotation**. The single-writer + in-process limiter suffices
(the 19h soak sustained 20 rps with zero 429s from one residential IP). The D9 Source and D13
schedule abstractions keep these cheap to add later if we ever run multiple Yahoo writers.

## Rollout impact (revises ADR 0001's phasing)

| Phase | Change from ADR 0001 |
|---|---|
| **2a aggregation** (PR #17) | Rework to the **1m/1d two-tier** (D2′): `1D` native; `3d/1w/1mo` derived from daily. |
| **2b Yahoo client** | `curl_cffi` + **D12 auto-throttle**, implementing the **D9 Source** interface. |
| **2c writer** | Driven by the **D13 schedule table**: fetch `1m`+`1d`, derive the rest, upsert `provider='yahoo'`. |
| **3 backfill** | **D10** chunked backfill + **D11** corp-action repair. |
| **4 catalog + ops** | ADR 0001 D7 catalog + **D14** observability/audit. |
