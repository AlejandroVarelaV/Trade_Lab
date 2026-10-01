"""Cost profiles (SPEC §5): commission, FX fee, slippage and the ETP fee.

Everything here is pure arithmetic in EUR, so it is unit-tested without a
database. The fill engine (ledger.py) is the only caller that moves cash.
"""
from __future__ import annotations

from dataclasses import dataclass

ASSET_CLASSES = ("stock", "etf", "crypto")
ETP_DAYS_PER_YEAR = 365   # the ETP fee accrues on calendar days, weekends included


@dataclass(frozen=True)
class FeeSchedule:
    pct: float
    min_fee: float
    max_fee: float | None   # None = no maximum

    def commission(self, notional_eur: float) -> float:
        """min(max(pct × notional, min_fee), max_fee)."""
        fee = max(self.pct * abs(notional_eur), self.min_fee)
        if self.max_fee is not None:
            fee = min(fee, self.max_fee)
        return fee


@dataclass(frozen=True)
class TradeCost:
    commission_eur: float
    fx_fee_eur: float

    @property
    def total_eur(self) -> float:
        return self.commission_eur + self.fx_fee_eur


@dataclass(frozen=True)
class CostProfile:
    name: str
    commission: dict[str, FeeSchedule]
    fx_fee_pct: dict[str, float]
    etp_annual_fee_pct: float
    slippage_bps: dict[str, float]

    def trade_cost(self, asset_class: str, notional_eur: float) -> TradeCost:
        """Fees for one order of `notional_eur` (buy or sell; sign is ignored).

        The FX fee applies once per order: a buy converts EUR→USD, a sell
        converts the proceeds USD→EUR.
        """
        notional = abs(notional_eur)
        return TradeCost(
            commission_eur=self.commission[asset_class].commission(notional),
            fx_fee_eur=self.fx_fee_pct[asset_class] * notional,
        )

    def exec_price(self, asset_class: str, ref_price: float, side: str) -> float:
        """Reference price moved against us by the slippage."""
        slip = self.slippage_bps[asset_class] / 10_000
        return ref_price * (1 + slip) if side == "buy" else ref_price * (1 - slip)

    def etp_daily_fee(self, asset_class: str, market_value_eur: float) -> float:
        """One calendar day of the ETP fee on a position (crypto only)."""
        if asset_class != "crypto" or self.etp_annual_fee_pct == 0:
            return 0.0
        return market_value_eur * self.etp_annual_fee_pct / ETP_DAYS_PER_YEAR

    def max_affordable_notional(self, asset_class: str, cash_eur: float) -> float:
        """Largest buy notional n with n + fees(n) <= cash (0 if none)."""
        if cash_eur <= 0 or cash_eur - self.trade_cost(asset_class, 0.0).total_eur <= 0:
            return 0.0
        lo, hi = 0.0, cash_eur
        for _ in range(100):   # fees are monotone in n, so bisection converges
            mid = (lo + hi) / 2
            if mid + self.trade_cost(asset_class, mid).total_eur <= cash_eur:
                lo = mid
            else:
                hi = mid
        return lo


def cost_profile_from_config(name: str, raw: dict) -> CostProfile:
    """Build a CostProfile from its scenarios.yaml block (validated)."""
    def per_class(key: str) -> dict[str, float]:
        block = raw.get(key) or {}
        missing = [c for c in ASSET_CLASSES if c not in block]
        if missing:
            raise ValueError(f"cost profile {name}: {key} missing {missing}")
        return {c: float(block[c]) for c in ASSET_CLASSES}

    commission = {}
    for cls in ASSET_CLASSES:
        sched = (raw.get("commission") or {}).get(cls)
        if sched is None:
            raise ValueError(f"cost profile {name}: commission.{cls} missing")
        max_fee = sched.get("max")
        commission[cls] = FeeSchedule(
            pct=float(sched["pct"]),
            min_fee=float(sched.get("min") or 0.0),
            max_fee=None if max_fee is None else float(max_fee),
        )
    profile = CostProfile(
        name=name,
        commission=commission,
        fx_fee_pct=per_class("fx_fee_pct"),
        etp_annual_fee_pct=float(raw.get("etp_annual_fee_pct", 0.0)),
        slippage_bps=per_class("slippage_bps"),
    )
    for cls, s in profile.commission.items():
        if s.pct < 0 or s.min_fee < 0 or (s.max_fee is not None and s.max_fee < s.min_fee):
            raise ValueError(f"cost profile {name}: invalid commission for {cls}")
    return profile
