-- =========================================================================
-- Divergence Detector · DB initialization
-- Run once at container startup. Idempotent.
-- =========================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -------------------------------------------------------------------------
-- Reference: instruments + users
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instruments (
  id            SERIAL PRIMARY KEY,
  symbol        TEXT UNIQUE NOT NULL,
  asset_class   TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  active        BOOLEAN DEFAULT TRUE,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
  id            BIGSERIAL PRIMARY KEY,
  email         TEXT UNIQUE NOT NULL,
  display_name  TEXT,
  api_key       TEXT UNIQUE NOT NULL DEFAULT encode(gen_random_bytes(24), 'hex'),
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- -------------------------------------------------------------------------
-- Time-series: implied + realized metrics
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS implied_metrics (
  ts            TIMESTAMPTZ NOT NULL,
  instrument_id INT NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
  metric_type   TEXT NOT NULL,
  value         DOUBLE PRECISION NOT NULL,
  source        TEXT,
  PRIMARY KEY (ts, instrument_id, metric_type)
);
SELECT create_hypertable('implied_metrics', 'ts', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS realized_metrics (
  ts                  TIMESTAMPTZ NOT NULL,
  instrument_id       INT NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
  t_cost_bps          DOUBLE PRECISION,
  intraday_vol_bps    DOUBLE PRECISION,
  composite_width_bps DOUBLE PRECISION,
  benchmark_size      NUMERIC,
  PRIMARY KEY (ts, instrument_id)
);
SELECT create_hypertable('realized_metrics', 'ts', if_not_exists => TRUE);

-- -------------------------------------------------------------------------
-- Commentary: LLM-classified text + embeddings
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS commentary (
  id              BIGSERIAL PRIMARY KEY,
  ts              TIMESTAMPTZ NOT NULL,
  source          TEXT,
  raw_text        TEXT NOT NULL,
  stress_score    DOUBLE PRECISION,
  sentiment_score DOUBLE PRECISION,
  instrument_ids  INT[],
  model_version   TEXT,
  embedding       VECTOR(1536)
);
CREATE INDEX IF NOT EXISTS commentary_ts_idx ON commentary (ts DESC);
CREATE INDEX IF NOT EXISTS commentary_embed_idx
  ON commentary USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 50);

-- -------------------------------------------------------------------------
-- Divergence events: the product artifact
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS divergence_events (
  id               BIGSERIAL PRIMARY KEY,
  ts               TIMESTAMPTZ NOT NULL,
  instrument_id    INT NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
  implied_z        DOUBLE PRECISION NOT NULL,
  realized_z       DOUBLE PRECISION NOT NULL,
  divergence_score DOUBLE PRECISION NOT NULL,
  regime_label     TEXT NOT NULL,
  lookback_days    INT NOT NULL DEFAULT 252,
  feature_vector   VECTOR(32),
  created_at       TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (ts, instrument_id, lookback_days)
);
CREATE INDEX IF NOT EXISTS de_instr_ts_idx ON divergence_events (instrument_id, ts DESC);
CREATE INDEX IF NOT EXISTS de_abs_score_idx
  ON divergence_events (ABS(divergence_score) DESC)
  WHERE ABS(divergence_score) > 2;
CREATE INDEX IF NOT EXISTS de_feature_idx
  ON divergence_events USING ivfflat (feature_vector vector_cosine_ops)
  WITH (lists = 50);

-- Curated labels for historical analog naming ("covid_mar_2020" etc.)
CREATE TABLE IF NOT EXISTS event_labels (
  event_id    BIGINT PRIMARY KEY REFERENCES divergence_events(id) ON DELETE CASCADE,
  label       TEXT NOT NULL,
  description TEXT,
  created_by  BIGINT REFERENCES users(id),
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- -------------------------------------------------------------------------
-- Narratives (LLM output, cached per event + prompt version)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS narratives (
  id                  BIGSERIAL PRIMARY KEY,
  divergence_event_id BIGINT NOT NULL REFERENCES divergence_events(id) ON DELETE CASCADE,
  generated_at        TIMESTAMPTZ DEFAULT NOW(),
  model               TEXT NOT NULL,
  prompt_version      TEXT NOT NULL,
  body                TEXT NOT NULL,
  historical_analogs  JSONB,
  latency_ms          INT,
  UNIQUE (divergence_event_id, prompt_version)
);

-- -------------------------------------------------------------------------
-- Subscriptions + delivery log
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subscriptions (
  id              BIGSERIAL PRIMARY KEY,
  user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name            TEXT NOT NULL,
  asset_classes   TEXT[],
  instrument_ids  INT[],
  min_abs_score   DOUBLE PRECISION NOT NULL DEFAULT 2.0,
  direction       TEXT NOT NULL DEFAULT 'either',
  regime_labels   TEXT[],
  channel         TEXT NOT NULL,
  webhook_url     TEXT,
  cooldown_min    INT NOT NULL DEFAULT 60,
  active          BOOLEAN DEFAULT TRUE,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  CHECK (direction IN ('positive', 'negative', 'either')),
  CHECK (channel IN ('email', 'webhook', 'websocket'))
);
CREATE INDEX IF NOT EXISTS sub_active_idx ON subscriptions (user_id) WHERE active;

CREATE TABLE IF NOT EXISTS alert_deliveries (
  id                  BIGSERIAL PRIMARY KEY,
  subscription_id     BIGINT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
  divergence_event_id BIGINT NOT NULL REFERENCES divergence_events(id) ON DELETE CASCADE,
  delivered_at        TIMESTAMPTZ DEFAULT NOW(),
  channel             TEXT NOT NULL,
  status              TEXT NOT NULL,
  error               TEXT,
  UNIQUE (subscription_id, divergence_event_id)
);
CREATE INDEX IF NOT EXISTS ad_sub_time_idx
  ON alert_deliveries (subscription_id, delivered_at DESC);

-- -------------------------------------------------------------------------
-- Metric weights (per asset class · tunable without redeploy)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metric_weights (
  asset_class  TEXT NOT NULL,
  metric_type  TEXT NOT NULL,
  side         TEXT NOT NULL CHECK (side IN ('implied', 'realized')),
  weight       DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  PRIMARY KEY (asset_class, metric_type, side)
);
