-- 002_phase1.sql — Phase 1: universe seed, data quality guards, benchmark
-- scenarios, orders, fee accruals, features and job runs.
--
-- Runs inside one transaction managed by database/migrate.py: no BEGIN/COMMIT here.

-- ─── Universe v1 (SPEC §3.1) ───────────────────────────────────────────────

INSERT INTO instruments (symbol, asset_class, active_from, note) VALUES
    ('SPY', 'etf', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('QQQ', 'etf', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('IWM', 'etf', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('TLT', 'etf', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('GLD', 'etf', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('XLE', 'etf', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('XLF', 'etf', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('XLK', 'etf', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('XLV', 'etf', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('AAPL', 'stock', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('MSFT', 'stock', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('NVDA', 'stock', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('AMZN', 'stock', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('GOOGL', 'stock', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('META', 'stock', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('BTC/USD', 'crypto', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('ETH/USD', 'crypto', '2026-09-29', 'v1 universe (SPEC v2 §3.1)'),
    ('SOL/USD', 'crypto', '2026-09-29', 'v1 universe (SPEC v2 §3.1)');

-- ─── Data quality: NUMERIC accepts 'NaN', so forbid it explicitly ──────────
-- A missing value is a missing row, never a NaN or a forward-filled copy.

ALTER TABLE bars_daily ADD CONSTRAINT bars_daily_no_nan CHECK (
    open <> 'NaN' AND high <> 'NaN' AND low <> 'NaN' AND close <> 'NaN' AND volume <> 'NaN'
    AND open > 0 AND high > 0 AND low > 0 AND close > 0 AND volume >= 0);
ALTER TABLE bars_daily ADD COLUMN ingested_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE fx_daily   ADD CONSTRAINT fx_daily_no_nan CHECK (eurusd <> 'NaN');
ALTER TABLE fx_daily   ADD COLUMN ingested_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- US trading sessions (Alpaca /v2/calendar): drives expected bars and fill windows.
CREATE TABLE market_calendar (
    date      DATE        PRIMARY KEY,
    open_at   TIMESTAMPTZ NOT NULL,
    close_at  TIMESTAMPTZ NOT NULL,
    CHECK (close_at > open_at)
);

-- ─── Benchmark scenarios ───────────────────────────────────────────────────
-- Portfolio C is one ledger per (capital, cost profile) pair, so it lives in its
-- own scenario row (kind = 'benchmark', no risk profile). Strategy scenarios
-- hold the A and B ledgers.

ALTER TABLE scenarios ADD COLUMN kind TEXT NOT NULL DEFAULT 'strategy'
    CHECK (kind IN ('strategy', 'benchmark'));
ALTER TABLE scenarios ALTER COLUMN risk_profile DROP NOT NULL;
ALTER TABLE scenarios ADD CONSTRAINT scenarios_kind_risk
    CHECK ((kind = 'strategy') = (risk_profile IS NOT NULL));
ALTER TABLE scenarios ADD CONSTRAINT scenarios_primary_is_strategy
    CHECK (NOT is_primary OR kind = 'strategy');

CREATE OR REPLACE FUNCTION scenarios_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.active_to IS NULL AND NEW.active_to IS NOT NULL
       AND (NEW.id, NEW.name, NEW.kind, NEW.capital_eur, NEW.risk_profile, NEW.cost_profile,
            NEW.config_hash, NEW.active_from, NEW.is_primary)
           IS NOT DISTINCT FROM
           (OLD.id, OLD.name, OLD.kind, OLD.capital_eur, OLD.risk_profile, OLD.cost_profile,
            OLD.config_hash, OLD.active_from, OLD.is_primary) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'scenarios are never edited: only deactivation (setting active_to once) is allowed';
END $$;

-- ─── Orders: a decision waiting for its fill window ────────────────────────
-- Written when the decision is made; resolved once (filled or skipped).

CREATE TABLE orders (
    id             BIGSERIAL    PRIMARY KEY,
    scenario_id    INT          NOT NULL REFERENCES scenarios(id),
    portfolio      TEXT         NOT NULL CHECK (portfolio IN ('A', 'B', 'C')),
    reason         TEXT         NOT NULL CHECK (reason IN ('proposal', 'stop', 'rebalance')),
    proposal_id    BIGINT       REFERENCES proposals(id),
    symbol         TEXT         NOT NULL,
    target_weight  NUMERIC(8,6) NOT NULL CHECK (target_weight BETWEEN 0 AND 1),
    decided_at     TIMESTAMPTZ  NOT NULL,
    fill_window    TIMESTAMPTZ  NOT NULL,   -- earliest moment it may fill (SPEC §3.2)
    status         TEXT         NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'filled', 'skipped')),
    status_note    TEXT,                    -- e.g. below_min_size, cash_limited
    resolved_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CHECK (fill_window > decided_at),
    CHECK (reason = 'rebalance' OR proposal_id IS NOT NULL),
    CHECK ((status = 'pending') = (resolved_at IS NULL))
);
CREATE INDEX orders_pending ON orders (fill_window) WHERE status = 'pending';
CREATE INDEX orders_ledger ON orders (scenario_id, portfolio, decided_at);

CREATE FUNCTION orders_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.status = 'pending' AND NEW.status <> 'pending'
       AND (NEW.id, NEW.scenario_id, NEW.portfolio, NEW.reason, NEW.proposal_id, NEW.symbol,
            NEW.target_weight, NEW.decided_at, NEW.fill_window, NEW.created_at)
           IS NOT DISTINCT FROM
           (OLD.id, OLD.scenario_id, OLD.portfolio, OLD.reason, OLD.proposal_id, OLD.symbol,
            OLD.target_weight, OLD.decided_at, OLD.fill_window, OLD.created_at) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'orders are append-only: only resolving a pending order once is allowed';
END $$;
CREATE TRIGGER orders_resolve_once BEFORE UPDATE OR DELETE ON orders
    FOR EACH ROW EXECUTE FUNCTION orders_guard();

-- Fills: link to their order and keep the cost breakdown for hand reconciliation.
-- fees_eur = commission_eur + fx_fee_eur; price_usd already includes slippage.
ALTER TABLE fills ADD COLUMN order_id       BIGINT REFERENCES orders(id);
ALTER TABLE fills ADD COLUMN ref_price_usd  NUMERIC(20,8) CHECK (ref_price_usd > 0);
ALTER TABLE fills ADD COLUMN commission_eur NUMERIC(12,4) CHECK (commission_eur >= 0);
ALTER TABLE fills ADD COLUMN fx_fee_eur     NUMERIC(12,4) CHECK (fx_fee_eur >= 0);
ALTER TABLE fills ADD CONSTRAINT fills_fee_breakdown CHECK (
    commission_eur IS NULL OR fees_eur = commission_eur + fx_fee_eur);

-- Daily ETP fee accruals (crypto, SPEC §5): a derived snapshot like equity_daily.
CREATE TABLE accruals_daily (
    scenario_id  INT           NOT NULL REFERENCES scenarios(id),
    portfolio    TEXT          NOT NULL CHECK (portfolio IN ('A', 'B', 'C')),
    date         DATE          NOT NULL,
    symbol       TEXT          NOT NULL,
    kind         TEXT          NOT NULL CHECK (kind IN ('etp_fee')),
    base_eur     NUMERIC(14,4) NOT NULL,   -- market value the fee accrued on
    amount_eur   NUMERIC(14,6) NOT NULL CHECK (amount_eur >= 0),
    PRIMARY KEY (scenario_id, portfolio, date, symbol, kind)
);

-- ─── Features (SPEC §3.3), recomputed from bars every run ──────────────────
-- NULL means "not computable" (not enough history, or a gap in the window).

CREATE TABLE features_daily (
    symbol      TEXT             NOT NULL,
    date        DATE             NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    ret_1d      DOUBLE PRECISION,
    ret_5d      DOUBLE PRECISION,
    ret_21d     DOUBLE PRECISION,
    ret_63d     DOUBLE PRECISION,
    vol_21d     DOUBLE PRECISION,   -- annualized: sqrt(252) stocks/ETFs, sqrt(365) crypto
    rsi_14      DOUBLE PRECISION,   -- Wilder
    dist_ma50   DOUBLE PRECISION,   -- close / SMA50 - 1
    dist_ma200  DOUBLE PRECISION,   -- close / SMA200 - 1
    volume_z    DOUBLE PRECISION,   -- stocks/ETFs only
    PRIMARY KEY (symbol, date)
);

-- ─── Job runs (evidence for "N consecutive daily runs") ────────────────────

CREATE TABLE job_runs (
    id           BIGSERIAL   PRIMARY KEY,
    job          TEXT        NOT NULL,
    run_at       TIMESTAMPTZ NOT NULL,   -- logical run time (now, or the replayed time)
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    status       TEXT        NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'ok', 'warn', 'error')),
    summary      JSONB,
    git_sha      TEXT
);
CREATE INDEX job_runs_job ON job_runs (job, run_at);
