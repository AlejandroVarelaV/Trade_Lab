-- 003_cost_engine.sql — interest on cash (SPEC §5, cost profiles v3).
--
-- Runs inside one transaction managed by database/migrate.py: no BEGIN/COMMIT here.

-- Daily interest on positive cash is a derived accrual like the ETP fee. It is
-- stored with symbol 'EUR' (the cash balance) and a positive amount that is
-- credited, while an etp_fee amount is debited.
ALTER TABLE accruals_daily DROP CONSTRAINT accruals_daily_kind_check;
ALTER TABLE accruals_daily ADD CONSTRAINT accruals_daily_kind_check
    CHECK (kind IN ('etp_fee', 'interest'));
ALTER TABLE accruals_daily ADD CONSTRAINT accruals_daily_interest_on_cash
    CHECK (kind <> 'interest' OR symbol = 'EUR');
