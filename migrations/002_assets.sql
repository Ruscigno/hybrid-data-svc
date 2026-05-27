-- Asset catalog. Bridges the TradingView identity (used by the REST API,
-- e.g. "BINANCE:BTCUSDT") to the ccxt storage identity (used by bars.symbol,
-- e.g. "BTC/USDT:USDT"), and carries human-readable metadata for /v1/search
-- and /v1/profile.
--
-- Rows are populated at REST gateway startup from data_svc/assets.yaml
-- (upsert-only — see data_svc/services/assets_loader.py).
--
-- pg_trgm is required for the substring search on (symbol || name).
-- Creating it here works because Postgres runs /docker-entrypoint-initdb.d/*.sql
-- as the bootstrap superuser, which has CREATE EXTENSION privileges.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS assets (
    symbol          TEXT PRIMARY KEY,    -- TradingView id, e.g. "BINANCE:BTCUSDT"
    storage_symbol  TEXT NOT NULL,       -- ccxt key matching bars.symbol, e.g. "BTC/USDT:USDT"
    name            TEXT NOT NULL,       -- human-readable
    exchange        TEXT NOT NULL,
    currency        TEXT NOT NULL,       -- ISO 4217 quote currency
    asset_class     TEXT NOT NULL,       -- 'EQUITY' | 'CRYPTO' | 'ETF' | 'FUND'
    asset_subclass  TEXT,                -- nullable; omitted from REST response if NULL
    isin            TEXT,                -- nullable; ISO 6166
    country         TEXT,                -- nullable; ISO 3166-1 alpha-2
    updated_at      BIGINT NOT NULL      -- unix seconds, set by assets_loader
);

-- /v1/quote and /v1/historical translate TV id → storage id via this index.
CREATE INDEX IF NOT EXISTS idx_assets_storage_symbol
    ON assets (storage_symbol);

-- /v1/search uses a substring match across (symbol || ' ' || name). pg_trgm's
-- GIN-with-gin_trgm_ops makes lower-cased ILIKE '%q%' index-backed.
CREATE INDEX IF NOT EXISTS idx_assets_search_trgm
    ON assets USING gin ((lower(symbol) || ' ' || lower(name)) gin_trgm_ops);
