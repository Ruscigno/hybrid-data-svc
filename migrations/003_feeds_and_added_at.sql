-- Phase 3 (catalog management): runtime-mutable feed list + asset added_at.
--
-- 1) `feeds` table — the writer (data-svc) reads polling targets from this
--    table at the top of every cycle, replacing the env-only FEEDS source.
--    POST /v1/assets inserts rows here with status='pending'; the writer
--    flips them to 'active' on first successful bar insert.
-- 2) `assets.added_at` — surfaced in GET /v1/assets so callers can sort by
--    onboarding time. Backfilled from `updated_at` for existing rows.
--
-- Both changes ship in one migration: they're independent SQL but
-- semantically introduced together as Phase 3 of the REST API spec.
-- Idempotent (IF NOT EXISTS) so the file is safe to re-run.

CREATE TABLE IF NOT EXISTS feeds (
    storage_symbol  TEXT NOT NULL,                -- joins to bars.symbol and assets.storage_symbol
    timeframe       TEXT NOT NULL,                -- '15' / '30' / '1h' / '4h' / '1D' / '1W'
    tv_symbol       TEXT NOT NULL,                -- TradingView chart identifier for the tv CLI
    status          TEXT NOT NULL DEFAULT 'pending',  -- 'active' | 'pending' | 'inactive'
    updated_at      BIGINT NOT NULL,              -- unix seconds
    PRIMARY KEY (storage_symbol, timeframe)
);

CREATE INDEX IF NOT EXISTS idx_feeds_status ON feeds (status);

-- Backfill assets.added_at from updated_at; then enforce NOT NULL.
ALTER TABLE assets ADD COLUMN IF NOT EXISTS added_at BIGINT;
UPDATE assets SET added_at = updated_at WHERE added_at IS NULL;
ALTER TABLE assets ALTER COLUMN added_at SET NOT NULL;
