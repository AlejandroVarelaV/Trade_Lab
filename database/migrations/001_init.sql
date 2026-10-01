-- 001_init.sql — TradeLab base schema (spec v2, section 7).
--
-- Runs inside one transaction managed by database/migrate.py: no BEGIN/COMMIT here.
--
-- Mutability policy (enforced by triggers at the bottom, not by convention):
--   * Append-only (no UPDATE, no DELETE): proposals, scenario_adjustments, fills, api_costs.
--   * approvals: append-only, except that a reject's reason may be filled in once
--     (the reason is picked with a second Telegram tap).
--   * scenarios: never edited; the only allowed change is setting active_to once
--     (deactivation). Changing a parameter means inserting a new scenario row.
--   * runs: mutable only while status = 'running'; frozen once finished.
--   * Snapshots (positions_daily, equity_daily) and reference data (instruments,
--     bars_daily, fx_daily) are mutable.

-- ─── Reference data ────────────────────────────────────────────────────────

CREATE TABLE instruments (
    id           SERIAL      PRIMARY KEY,
    symbol       TEXT        NOT NULL,
    asset_class  TEXT        NOT NULL CHECK (asset_class IN ('etf', 'stock', 'crypto')),
    active_from  DATE        NOT NULL,
    active_to    DATE,
    note         TEXT,                       -- why the universe changed
    CHECK (active_to IS NULL OR active_to > active_from),
    UNIQUE (symbol, active_from)
);
-- A symbol can be in the universe only once at a time.
CREATE UNIQUE INDEX instruments_one_active ON instruments (symbol) WHERE active_to IS NULL;

CREATE TABLE bars_daily (
    symbol  TEXT          NOT NULL,
    date    DATE          NOT NULL,
    open    NUMERIC(20,8) NOT NULL,
    high    NUMERIC(20,8) NOT NULL,
    low     NUMERIC(20,8) NOT NULL,
    close   NUMERIC(20,8) NOT NULL,
    volume  NUMERIC(28,8) NOT NULL,          -- fractional for crypto
    source  TEXT          NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE fx_daily (
    date    DATE          PRIMARY KEY,
    eurusd  NUMERIC(12,6) NOT NULL CHECK (eurusd > 0),   -- USD per 1 EUR (ECB reference)
    source  TEXT          NOT NULL
);

-- ─── Decision runs ─────────────────────────────────────────────────────────

CREATE TABLE runs (
    id              BIGSERIAL   PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    model           TEXT        NOT NULL,
    prompt_version  TEXT        NOT NULL,
    input_snapshot  JSONB,
    raw_response    JSONB,
    attempts        INT         NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
    status          TEXT        NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'ok', 'validation_failed', 'error')),
    cost_usd        NUMERIC(12,6)
);

CREATE TABLE proposals (
    id               BIGSERIAL   PRIMARY KEY,
    run_id           BIGINT      NOT NULL REFERENCES runs(id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    symbol           TEXT        NOT NULL,
    action           TEXT        NOT NULL CHECK (action IN ('open', 'increase', 'reduce', 'close')),
    target_weight    NUMERIC(8,6) NOT NULL CHECK (target_weight BETWEEN 0 AND 1),
    stop_loss_pct    NUMERIC(8,6) CHECK (stop_loss_pct > 0 AND stop_loss_pct < 1),
    horizon_days     INT         CHECK (horizon_days > 0),
    confidence       NUMERIC(4,3) CHECK (confidence BETWEEN 0 AND 1),
    rationale        TEXT        NOT NULL,
    original         JSONB       NOT NULL,   -- proposal exactly as Claude returned it
    validator_status TEXT        NOT NULL CHECK (validator_status IN ('accepted', 'clipped', 'dropped')),
    validator_notes  TEXT,
    UNIQUE (run_id, symbol),
    -- Every open or increase must carry a stop (section 4).
    CHECK (action NOT IN ('open', 'increase') OR stop_loss_pct IS NOT NULL)
);

CREATE TABLE approvals (
    proposal_id  BIGINT      PRIMARY KEY REFERENCES proposals(id),
    decision     TEXT        NOT NULL CHECK (decision IN ('approve', 'reject', 'timeout')),
    reason       TEXT        CHECK (reason IN ('disagree', 'too_risky', 'no_time_to_check', 'other')),
    decided_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (decision = 'reject' OR reason IS NULL)
);

CREATE TABLE api_costs (
    id             BIGSERIAL   PRIMARY KEY,
    date           DATE        NOT NULL,
    run_id         BIGINT      REFERENCES runs(id),
    model          TEXT,
    input_tokens   INT         NOT NULL CHECK (input_tokens >= 0),
    output_tokens  INT         NOT NULL CHECK (output_tokens >= 0),
    usd            NUMERIC(12,6) NOT NULL CHECK (usd >= 0),
    eur            NUMERIC(12,6) NOT NULL CHECK (eur >= 0),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX api_costs_date ON api_costs (date);

-- ─── Scenarios and ledgers ─────────────────────────────────────────────────

CREATE TABLE scenarios (
    id            SERIAL        PRIMARY KEY,
    name          TEXT          NOT NULL,
    capital_eur   NUMERIC(12,2) NOT NULL CHECK (capital_eur > 0),
    risk_profile  TEXT          NOT NULL CHECK (risk_profile IN ('conservative', 'base', 'aggressive')),
    cost_profile  TEXT          NOT NULL,
    config_hash   TEXT          NOT NULL,   -- sha256 of the scenario's resolved config
    active_from   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    active_to     TIMESTAMPTZ,
    is_primary    BOOLEAN       NOT NULL DEFAULT false,
    CHECK (active_to IS NULL OR active_to > active_from)
);
CREATE UNIQUE INDEX scenarios_one_active_per_name ON scenarios (name) WHERE active_to IS NULL;
CREATE UNIQUE INDEX scenarios_one_active_primary ON scenarios (is_primary)
    WHERE is_primary AND active_to IS NULL;

CREATE TABLE scenario_adjustments (
    id               BIGSERIAL    PRIMARY KEY,
    scenario_id      INT          NOT NULL REFERENCES scenarios(id),
    proposal_id      BIGINT       NOT NULL REFERENCES proposals(id),
    adjusted_weight  NUMERIC(8,6) NOT NULL CHECK (adjusted_weight BETWEEN 0 AND 1),
    rule_fired       TEXT         NOT NULL,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX scenario_adjustments_proposal ON scenario_adjustments (proposal_id);

CREATE TABLE fills (
    id               BIGSERIAL     PRIMARY KEY,
    scenario_id      INT           NOT NULL REFERENCES scenarios(id),
    portfolio        TEXT          NOT NULL CHECK (portfolio IN ('A', 'B', 'C')),
    proposal_id      BIGINT        REFERENCES proposals(id),   -- NULL for benchmark rebalances
    fill_reason      TEXT          NOT NULL CHECK (fill_reason IN ('proposal', 'stop', 'rebalance')),
    symbol           TEXT          NOT NULL,
    side             TEXT          NOT NULL CHECK (side IN ('buy', 'sell')),
    qty              NUMERIC(28,10) NOT NULL CHECK (qty > 0),
    price_usd        NUMERIC(20,8) NOT NULL CHECK (price_usd > 0),
    fx_rate          NUMERIC(12,6) NOT NULL CHECK (fx_rate > 0),  -- eurusd used for conversion
    fees_eur         NUMERIC(12,4) NOT NULL CHECK (fees_eur >= 0),
    filled_at        TIMESTAMPTZ   NOT NULL,
    fill_source      TEXT          NOT NULL CHECK (fill_source IN ('internal', 'alpaca')),
    broker_order_id  TEXT,                                     -- Alpaca order id (mirror of B)
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CHECK (fill_reason = 'rebalance' OR proposal_id IS NOT NULL),
    CHECK (fill_source = 'internal' OR broker_order_id IS NOT NULL)
);
CREATE INDEX fills_ledger ON fills (scenario_id, portfolio, filled_at);

CREATE TABLE positions_daily (
    scenario_id       INT           NOT NULL REFERENCES scenarios(id),
    portfolio         TEXT          NOT NULL CHECK (portfolio IN ('A', 'B', 'C')),
    date              DATE          NOT NULL,
    symbol            TEXT          NOT NULL,
    qty               NUMERIC(28,10) NOT NULL,
    avg_price_eur     NUMERIC(20,8) NOT NULL,
    market_value_eur  NUMERIC(14,4) NOT NULL,
    stop_price        NUMERIC(20,8),
    PRIMARY KEY (scenario_id, portfolio, date, symbol)
);

CREATE TABLE equity_daily (
    scenario_id  INT           NOT NULL REFERENCES scenarios(id),
    portfolio    TEXT          NOT NULL CHECK (portfolio IN ('A', 'B', 'C')),
    date         DATE          NOT NULL,
    equity_eur   NUMERIC(14,4) NOT NULL,
    cash_eur     NUMERIC(14,4) NOT NULL,
    drawdown     NUMERIC(8,6)  NOT NULL CHECK (drawdown BETWEEN 0 AND 1),
    PRIMARY KEY (scenario_id, portfolio, date)
);

-- ─── Mutability guards ─────────────────────────────────────────────────────

CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % not allowed', TG_TABLE_NAME, TG_OP;
END $$;

CREATE TRIGGER proposals_append_only BEFORE UPDATE OR DELETE ON proposals
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER scenario_adjustments_append_only BEFORE UPDATE OR DELETE ON scenario_adjustments
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER fills_append_only BEFORE UPDATE OR DELETE ON fills
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER api_costs_append_only BEFORE UPDATE OR DELETE ON api_costs
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE FUNCTION approvals_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.decision = 'reject' AND OLD.reason IS NULL AND NEW.reason IS NOT NULL
       AND (NEW.proposal_id, NEW.decision, NEW.decided_at)
           IS NOT DISTINCT FROM (OLD.proposal_id, OLD.decision, OLD.decided_at) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'approvals is append-only (only a reject''s missing reason may be set once)';
END $$;
CREATE TRIGGER approvals_append_only BEFORE UPDATE OR DELETE ON approvals
    FOR EACH ROW EXECUTE FUNCTION approvals_guard();

CREATE FUNCTION scenarios_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.active_to IS NULL AND NEW.active_to IS NOT NULL
       AND (NEW.id, NEW.name, NEW.capital_eur, NEW.risk_profile, NEW.cost_profile,
            NEW.config_hash, NEW.active_from, NEW.is_primary)
           IS NOT DISTINCT FROM
           (OLD.id, OLD.name, OLD.capital_eur, OLD.risk_profile, OLD.cost_profile,
            OLD.config_hash, OLD.active_from, OLD.is_primary) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'scenarios are never edited: only deactivation (setting active_to once) is allowed';
END $$;
CREATE TRIGGER scenarios_immutable BEFORE UPDATE OR DELETE ON scenarios
    FOR EACH ROW EXECUTE FUNCTION scenarios_guard();

CREATE FUNCTION runs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.status = 'running'
       AND NEW.id = OLD.id AND NEW.started_at = OLD.started_at THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'runs are frozen once finished (run % has status %)', OLD.id, OLD.status;
END $$;
CREATE TRIGGER runs_frozen_when_finished BEFORE UPDATE OR DELETE ON runs
    FOR EACH ROW EXECUTE FUNCTION runs_guard();

-- A proposal-driven fill can never predate its proposal: the proposal timestamp
-- is the proof that the decision came before the outcome.
CREATE FUNCTION fills_after_proposal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    proposed_at TIMESTAMPTZ;
BEGIN
    IF NEW.proposal_id IS NOT NULL THEN
        SELECT created_at INTO proposed_at FROM proposals WHERE id = NEW.proposal_id;
        IF NEW.filled_at <= proposed_at THEN
            RAISE EXCEPTION 'fill at % is not after proposal % created at %',
                NEW.filled_at, NEW.proposal_id, proposed_at;
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER fills_not_before_proposal BEFORE INSERT ON fills
    FOR EACH ROW EXECUTE FUNCTION fills_after_proposal();
