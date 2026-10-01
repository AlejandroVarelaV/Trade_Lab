"""The append-only and immutability rules of section 7 hold in the database."""
import psycopg2
import pytest

pytestmark = pytest.mark.db


def _one(cur, sql, args=()):
    cur.execute(sql, args)
    return cur.fetchone()[0]


@pytest.fixture()
def cur(migrated_db):
    with migrated_db.cursor() as c:
        yield c


@pytest.fixture()
def proposal_id(cur):
    run_id = _one(cur, "INSERT INTO runs (model, prompt_version) VALUES ('m', 'v1') RETURNING id")
    return _one(cur, """
        INSERT INTO proposals (run_id, symbol, action, target_weight, stop_loss_pct,
                               horizon_days, confidence, rationale, original, validator_status)
        VALUES (%s, 'NVDA', 'open', 0.08, 0.07, 10, 0.55, 'RSI 71', '{}', 'accepted')
        RETURNING id""", (run_id,))


@pytest.fixture()
def scenario_id(cur):
    return _one(cur, """
        INSERT INTO scenarios (name, capital_eur, risk_profile, cost_profile, config_hash, is_primary)
        VALUES ('primary', 200, 'base', 'eu_small_account', 'h', true) RETURNING id""")


def test_proposals_cannot_be_updated_or_deleted(cur, proposal_id):
    with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
        cur.execute("UPDATE proposals SET target_weight = 0.5 WHERE id = %s", (proposal_id,))
    with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
        cur.execute("DELETE FROM proposals WHERE id = %s", (proposal_id,))


def test_open_without_stop_is_rejected(cur, proposal_id):
    run_id = _one(cur, "SELECT run_id FROM proposals WHERE id = %s", (proposal_id,))
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("""
            INSERT INTO proposals (run_id, symbol, action, target_weight, rationale, original, validator_status)
            VALUES (%s, 'AAPL', 'open', 0.05, 'x', '{}', 'accepted')""", (run_id,))


def test_reject_reason_can_be_set_once_but_decision_is_frozen(cur, proposal_id):
    cur.execute("INSERT INTO approvals (proposal_id, decision) VALUES (%s, 'reject')", (proposal_id,))
    cur.execute("UPDATE approvals SET reason = 'too_risky' WHERE proposal_id = %s", (proposal_id,))
    with pytest.raises(psycopg2.errors.RaiseException):
        cur.execute("UPDATE approvals SET reason = 'other' WHERE proposal_id = %s", (proposal_id,))
    with pytest.raises(psycopg2.errors.RaiseException):
        cur.execute("UPDATE approvals SET decision = 'approve', reason = NULL WHERE proposal_id = %s",
                    (proposal_id,))


def test_scenarios_only_deactivate(cur, scenario_id):
    with pytest.raises(psycopg2.errors.RaiseException, match="never edited"):
        cur.execute("UPDATE scenarios SET capital_eur = 500 WHERE id = %s", (scenario_id,))
    cur.execute("UPDATE scenarios SET active_to = now() + interval '1 second' WHERE id = %s",
                (scenario_id,))
    with pytest.raises(psycopg2.errors.RaiseException):
        cur.execute("UPDATE scenarios SET active_to = now() + interval '1 day' WHERE id = %s",
                    (scenario_id,))


def test_finished_runs_are_frozen(cur):
    run_id = _one(cur, "INSERT INTO runs (model, prompt_version) VALUES ('m', 'v1') RETURNING id")
    cur.execute("UPDATE runs SET status = 'ok', attempts = 1 WHERE id = %s", (run_id,))
    with pytest.raises(psycopg2.errors.RaiseException, match="frozen"):
        cur.execute("UPDATE runs SET status = 'error' WHERE id = %s", (run_id,))


def test_fill_cannot_predate_its_proposal(cur, proposal_id, scenario_id):
    insert = """
        INSERT INTO fills (scenario_id, portfolio, proposal_id, fill_reason, symbol, side, qty,
                           price_usd, fx_rate, fees_eur, filled_at, fill_source)
        VALUES (%s, 'A', %s, 'proposal', 'NVDA', 'buy', 0.1, 120, 1.08, 1, %s, 'internal')"""
    with pytest.raises(psycopg2.errors.RaiseException, match="not after proposal"):
        cur.execute(insert, (scenario_id, proposal_id, "2000-01-01T00:00:00Z"))
    cur.execute(insert, (scenario_id, proposal_id, "2999-01-01T00:00:00Z"))
    with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
        cur.execute("DELETE FROM fills")
