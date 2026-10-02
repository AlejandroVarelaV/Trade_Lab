"""config/scenarios.yaml resolution and config_hash (SPEC §1.1)."""
import copy

import pytest
import yaml

from tradelab import config

RAW = yaml.safe_load(config.DEFAULT_PATH.read_text())


def test_default_grid():
    cfg = config.load()
    costs = ["ibkr", "trade_republic", "revolut_standard", "myinvestor", "zero_commission"]
    assert sorted(cfg.cost_profiles) == sorted(costs)
    names = sorted(s.name for s in cfg.strategies)
    assert len(names) == 45            # 3 capitals × 3 risk × 5 cost profiles
    assert names == sorted(f"c{c}_{r}_{k}" for c in (200, 500, 1000)
                           for r in ("conservative", "base", "aggressive") for k in costs)
    assert cfg.primary.name == "c200_base_ibkr"
    assert (cfg.primary.capital_eur, cfg.primary.risk_profile, cfg.primary.cost_profile) == \
        (200, "base", "ibkr")
    assert sum(s.is_primary for s in cfg.scenarios) == 1
    # One C per (capital, cost profile) pair: 15.
    assert sorted(b.name for b in cfg.benchmarks) == sorted(
        f"bench_c{c}_{k}" for c in (200, 500, 1000) for k in costs)
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
    assert len(changed) == 15 and all("_aggressive_" in n for n in changed)
    assert not any(n.startswith("bench_") for n in changed)


def test_cost_change_also_changes_benchmark_hash():
    before = {s.name: s.config_hash for s in config.resolve(copy.deepcopy(RAW)).scenarios}
    raw = copy.deepcopy(RAW)
    raw["cost_profiles"]["zero_commission"]["slippage_bps"]["crypto"] = 6
    after = {s.name: s.config_hash for s in config.resolve(raw).scenarios}
    changed = sorted(n for n in before if before[n] != after[n])
    assert len(changed) == 12 and all(n.endswith("_zero_commission") for n in changed)
    assert sum(n.startswith("bench_") for n in changed) == 3


@pytest.mark.parametrize("mutate, match", [
    (lambda r: r["scenarios"].append({"capital_eur": 200, "risk_profile": "yolo",
                                      "cost_profile": "myinvestor"}), "unknown risk profile"),
    (lambda r: r["scenarios"].append({"capital_eur": 200, "risk_profile": "base",
                                      "cost_profile": "degiro"}), "unknown cost profile"),
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
