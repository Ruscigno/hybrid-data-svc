-- Phase 2 of the Yahoo provider (ADR 0001): generalise the feeds table with a
-- provider dimension. Idempotent (IF EXISTS/IF NOT EXISTS) so re-running is safe.
-- NOT auto-applied by the deploy pipeline — manual apply (same as 004).

-- feeds ----------------------------------------------------------------------
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'tradingview';

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name='feeds' AND column_name='tv_symbol') THEN
    ALTER TABLE feeds RENAME COLUMN tv_symbol TO provider_symbol;
  END IF;
END $$;

ALTER TABLE feeds DROP CONSTRAINT IF EXISTS feeds_pkey;
ALTER TABLE feeds ADD PRIMARY KEY (storage_symbol, timeframe, provider);
