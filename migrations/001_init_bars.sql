-- Mirror of the legacy SQLite schema, ported to Postgres.
-- Keyed by (symbol, timeframe, ts) where ts = bar open unix timestamp (seconds, UTC).

CREATE TABLE IF NOT EXISTS bars (
    symbol      TEXT             NOT NULL,
    timeframe   TEXT             NOT NULL,
    ts          BIGINT           NOT NULL,    -- bar open unix timestamp (seconds, UTC)
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      DOUBLE PRECISION NOT NULL,
    fetched_at  BIGINT           NOT NULL,    -- unix timestamp when this row was written
    PRIMARY KEY (symbol, timeframe, ts)
);

CREATE INDEX IF NOT EXISTS idx_bars_symbol_tf_ts
    ON bars (symbol, timeframe, ts DESC);

CREATE TABLE IF NOT EXISTS cache_meta (
    symbol           TEXT   NOT NULL,
    timeframe        TEXT   NOT NULL,
    last_bar_ts      BIGINT NOT NULL,
    bar_count        BIGINT NOT NULL,
    last_fetched_at  BIGINT NOT NULL,
    PRIMARY KEY (symbol, timeframe)
);
