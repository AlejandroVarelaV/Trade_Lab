"""config/scenarios.yaml resolution and config_hash (SPEC §1.1)."""
import copy

import pytest
import yaml

from tradelab import config

RAW = yaml.safe_load(config.DEFAULT_PATH.read_text())


def test_default_grid():
    cfg = config.load()
    names = sorted(s.name for s in cfg.strategies)
    assert len(names) == 10            # 3 capitals × 3 risk × myinvestor + 1 zero_commission
    assert "c200_base_zero_commission" in names
    assert cfg.primary.name == "c200_base_myinvestor"
    assert (cfg.primary.capital_eur, cfg.primary.risk_profile, cfg.primary.cost_profile) == \
        (200, "base", "myinvestor")
    assert sum(s.is_primary for s in cfg.scenarios) == 1
    # One C per (capital, cost profile) pair in use.
    assert sorted(b.name for b in cfg.benchmarks) == [
        "bench_c1000_myinvestor", "bench_c200_myinvestor", "bench_c200_zero_commission",
        "bench_c500_myinvestor"]
    assert cfg.benchmark_weights == {"SPY": 0.8, "BTC/USD": 0.2}


def test_config_hash_is_stable_and_unique():
    a, b = config.resolve(copy.deepcopy(RAW)), config.resolve(copy.deepcopy(RAW))
    assert [s.config_hash for s in a.scenarios] == [s.config_hash for s in b.scenarios]
    assert len({s.config_hash for s in a.scenarios}) == len(a.scenarios)


def test_config_hash_changes_only_for_affected_scenarios():
    before = {s.name: s.config_hash for s in config.resolve(copy.deepcopy(RAW)).scenarios}
    raw = copy.deepcopy(RAW)
    raw["risk_profiles"]["aggressive"]["max_weight"] = 0.30
    after = {s.name: s.config_hash for s in config.resolve(raw).scenarios}
    changed = sorted(n for n in before if before[n] != after[n])
    assert changed == ["c1000_aggressive_myinvestor", "c200_aggressive_myinvestor",
                       "c500_aggressive_myinvestor"]


def test_cost_change_also_changes_benchmark_hash():
    before = {s.name: s.config_hash for s in config.resolve(copy.deepcopy(RAW)).scenarios}
    raw = copy.deepcopy(RAW)
    raw["cost_profiles"]["zero_commission"]["slippage_bps"]["crypto"] = 6
    after = {s.name: s.config_hash for s in config.resolve(raw).scenarios}
    assert sorted(n for n in before if before[n] != after[n]) == [
        "bench_c200_zero_commission", "c200_base_zero_commission"]


@pytest.mark.parametrize("mutate, match", [
    (lambda r: r["scenarios"].append({"capital_eur": 200, "risk_profile": "yolo",
                                      "cost_profile": "myinvestor"}), "unknown risk profile"),
    (lambda r: r["scenarios"].append({"capital_eur": 200, "risk_profile": "base",
                                      "cost_profile": "ibkr"}), "unknown cost profile"),
    (lambda r: r["scenarios"].append({"capital_eur": 200, "risk_profile": "base",
                                      "cost_profile": "myinvestor"}), "duplicate"),
    (lambda r: r.update(primary="nope"), "primary"),
    (lambda r: r["benchmark"].update(weights={"SPY": 0.9, "BTC/USD": 0.2}), "sum"),
    (lambda r: r["cost_profiles"]["myinvestor"]["commission"].pop("etf"), "commission.etf"),
])
def test_invalid_config_is_rejected(mutate, match):
    raw = copy.deepcopy(RAW)
    mutate(raw)
    with pytest.raises(config.ConfigError, match=match):
        config.resolve(raw)
