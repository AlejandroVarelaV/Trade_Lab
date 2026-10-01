"""Load and resolve config/scenarios.yaml (SPEC §1.1).

Resolution turns the YAML into a flat list of strategy scenarios (grid plus the
explicit list) and one benchmark per (capital, cost profile) pair in use. Each
gets a config_hash: the sha256 of its fully resolved parameters, so a change to
any parameter it depends on yields a new hash (and therefore a new scenario row).
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tradelab.costs import CostProfile, cost_profile_from_config

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "scenarios.yaml"
RISK_PROFILE_NAMES = ("conservative", "base", "aggressive")   # DB CHECK on scenarios
RISK_KEYS = ("max_weight", "max_crypto_weight", "max_positions", "max_new_per_day",
             "stop_min_pct", "stop_max_pct", "kill_switch_drawdown")


class ConfigError(ValueError):
    """scenarios.yaml is invalid."""


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    kind: str                     # 'strategy' (A, B) or 'benchmark' (C)
    capital_eur: float
    risk_profile: str | None
    cost_profile: str
    is_primary: bool
    resolved: dict = field(compare=False, hash=False)   # what config_hash covers

    @property
    def config_hash(self) -> str:
        blob = json.dumps(self.resolved, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    @property
    def portfolios(self) -> tuple[str, ...]:
        return ("A", "B") if self.kind == "strategy" else ("C",)


@dataclass(frozen=True)
class Config:
    scenarios: list[ScenarioSpec]
    cost_profiles: dict[str, CostProfile]
    risk_profiles: dict[str, dict]
    benchmark_weights: dict[str, float]
    min_trade_eur: float
    path: str

    @property
    def strategies(self) -> list[ScenarioSpec]:
        return [s for s in self.scenarios if s.kind == "strategy"]

    @property
    def benchmarks(self) -> list[ScenarioSpec]:
        return [s for s in self.scenarios if s.kind == "benchmark"]

    @property
    def primary(self) -> ScenarioSpec:
        return next(s for s in self.scenarios if s.is_primary)


def config_path() -> Path:
    return Path(os.environ.get("TRADELAB_SCENARIOS", DEFAULT_PATH))


def _capital_label(capital: float) -> str:
    return str(int(capital)) if float(capital).is_integer() else str(capital).replace(".", "p")


def load(path: str | Path | None = None) -> Config:
    path = Path(path or config_path())
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return resolve(raw, str(path))


def resolve(raw: dict, path: str = "<dict>") -> Config:
    if raw.get("version") != 1:
        raise ConfigError("scenarios.yaml: version must be 1")

    risk_profiles = raw.get("risk_profiles") or {}
    for name, params in risk_profiles.items():
        if name not in RISK_PROFILE_NAMES:
            raise ConfigError(f"risk profile {name!r} not one of {RISK_PROFILE_NAMES}")
        missing = [k for k in RISK_KEYS if k not in (params or {})]
        if missing:
            raise ConfigError(f"risk profile {name}: missing {missing}")

    try:
        cost_profiles = {n: cost_profile_from_config(n, p)
                         for n, p in (raw.get("cost_profiles") or {}).items()}
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc

    rules = raw.get("rules") or {}
    min_trade = float(rules.get("min_trade_eur", 10))
    bench = raw.get("benchmark") or {}
    weights = {str(k): float(v) for k, v in (bench.get("weights") or {}).items()}
    if not weights or any(w <= 0 for w in weights.values()) or sum(weights.values()) > 1 + 1e-9:
        raise ConfigError("benchmark.weights must be positive and sum to at most 1")
    if bench.get("rebalance") != "monthly":
        raise ConfigError("benchmark.rebalance: only 'monthly' is supported")

    # Strategy scenarios: grid × explicit list.
    entries: list[dict] = []
    grid = raw.get("grid")
    if grid:
        for cap, risk, cost in itertools.product(
                grid["capital_eur"], grid["risk_profile"], grid["cost_profile"]):
            entries.append({"capital_eur": cap, "risk_profile": risk, "cost_profile": cost})
    entries.extend(raw.get("scenarios") or [])

    primary_name = raw.get("primary")
    strategies: dict[str, ScenarioSpec] = {}
    for e in entries:
        try:
            cap = float(e["capital_eur"])
            risk, cost = str(e["risk_profile"]), str(e["cost_profile"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"bad scenario entry {e!r}") from exc
        if cap <= 0:
            raise ConfigError(f"scenario {e!r}: capital_eur must be > 0")
        if risk not in risk_profiles:
            raise ConfigError(f"scenario {e!r}: unknown risk profile {risk!r}")
        if cost not in cost_profiles:
            raise ConfigError(f"scenario {e!r}: unknown cost profile {cost!r}")
        name = e.get("name") or f"c{_capital_label(cap)}_{risk}_{cost}"
        if name in strategies:
            raise ConfigError(f"duplicate scenario name {name!r}")
        strategies[name] = ScenarioSpec(
            name=name, kind="strategy", capital_eur=cap, risk_profile=risk,
            cost_profile=cost, is_primary=(name == primary_name),
            resolved={
                "kind": "strategy",
                "capital_eur": cap,
                "is_primary": name == primary_name,
                "risk_profile": {"name": risk, **risk_profiles[risk]},
                "cost_profile": {"name": cost, **raw["cost_profiles"][cost]},
                "rules": {"min_trade_eur": min_trade},
            },
        )
    if primary_name not in strategies:
        raise ConfigError(f"primary scenario {primary_name!r} is not defined")

    benchmarks: dict[str, ScenarioSpec] = {}
    for s in strategies.values():
        name = f"bench_c{_capital_label(s.capital_eur)}_{s.cost_profile}"
        if name in benchmarks:
            continue
        benchmarks[name] = ScenarioSpec(
            name=name, kind="benchmark", capital_eur=s.capital_eur, risk_profile=None,
            cost_profile=s.cost_profile, is_primary=False,
            resolved={
                "kind": "benchmark",
                "capital_eur": s.capital_eur,
                "cost_profile": {"name": s.cost_profile, **raw["cost_profiles"][s.cost_profile]},
                "benchmark": {"weights": weights, "rebalance": "monthly"},
                "rules": {"min_trade_eur": min_trade},
            },
        )

    return Config(
        scenarios=[*strategies.values(), *benchmarks.values()],
        cost_profiles=cost_profiles,
        risk_profiles=risk_profiles,
        benchmark_weights=weights,
        min_trade_eur=min_trade,
        path=path,
    )
