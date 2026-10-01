"""Sync config/scenarios.yaml into the `scenarios` table (SPEC §1.1, §7).

Scenarios are never edited. For each resolved scenario (strategy or benchmark):
  - same name and same config_hash as an active row → kept;
  - same name, different hash → old row deactivated, new row inserted;
  - new name → inserted (its ledgers start from this run);
  - active row whose name is gone from the YAML → deactivated.
"""
from __future__ import annotations

from datetime import datetime

from tradelab.config import Config


def sync(cur, cfg: Config, t: datetime) -> dict:
    """Bring the active scenarios in line with cfg at time t. Returns a report."""
    cur.execute("SELECT symbol FROM instruments WHERE active_to IS NULL")
    universe = {r[0] for r in cur.fetchall()}
    unknown = sorted(set(cfg.benchmark_weights) - universe)
    if unknown:
        raise ValueError(f"benchmark symbols not in the universe: {unknown}")

    cur.execute("SELECT id, name, config_hash, active_from FROM scenarios WHERE active_to IS NULL")
    active = {name: (sid, h, af) for sid, name, h, af in cur.fetchall()}
    wanted = {s.name: s for s in cfg.scenarios}

    to_deactivate = [n for n, (_, h, _) in active.items()
                     if n not in wanted or wanted[n].config_hash != h]
    to_create = [s for s in cfg.scenarios
                 if s.name not in active or s.name in to_deactivate]

    for name in to_deactivate:
        sid, _, active_from = active[name]
        if active_from >= t:
            raise ValueError(f"cannot deactivate {name} at {t}: it starts at {active_from}")
        cur.execute("UPDATE scenarios SET active_to = %s WHERE id = %s", (t, sid))
    for s in to_create:
        cur.execute("""
            INSERT INTO scenarios (name, kind, capital_eur, risk_profile, cost_profile,
                                   config_hash, active_from, is_primary)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (s.name, s.kind, s.capital_eur, s.risk_profile, s.cost_profile,
             s.config_hash, t, s.is_primary))
    return {
        "created": [s.name for s in to_create],
        "deactivated": to_deactivate,
        "active": len(wanted),
    }
