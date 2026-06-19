-- Phase 1 of the Yahoo provider (ADR 0001): add a provider dimension to bar
-- storage so multiple sources coexist. feeds is handled in a later migration.
-- Idempotent (IF EXISTS/IF NOT EXISTS) so re-running is safe.

-- bars ----------------------------------------------------------------------
ALTER TABLE bars ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'tradingview';
ALTER TABLE bars DROP CONSTRAINT IF EXISTS bars_pkey;
ALTER TABLE bars ADD PRIMARY KEY (symbol, timeframe, provider, ts);
DROP INDEX IF EXISTS idx_bars_symbol_tf_ts;
-- No separate index needed: the PK (symbol, timeframe, provider, ts) already
-- serves ORDER BY ts DESC via a backward index scan.

-- cache_meta ----------------------------------------------------------------
ALTER TABLE cache_meta ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'tradingview';
ALTER TABLE cache_meta DROP CONSTRAINT IF EXISTS cache_meta_pkey;
ALTER TABLE cache_meta ADD PRIMARY KEY (symbol, timeframe, provider);
