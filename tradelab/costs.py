"""Cost profiles (SPEC §5): commission, FX fee (with a monthly free allowance),
slippage + spread, ETP fee, fractional shares, free orders and interest on cash.

Everything here is pure arithmetic, so it is unit-tested without a database.
The fill engine (ledger.py) is the only caller that moves cash.
"""
from __future__ import annotations

from dataclasses import dataclass

ASSET_CLASSES = ("stock", "etf", "crypto")
CURRENCIES = ("EUR", "USD")
DAYS_PER_YEAR = 365   # ETP fee and cash interest accrue on calendar days, weekends included


@dataclass(frozen=True)
class FeeSchedule:
    """commission = clamp(pct × notional + per_share × qty, min, max), in `currency`.

    The maximum is `max_fee` (absolute) and/or `max_pct` of the trade value; if
    both are set the lower one applies. The maximum is applied last, so it wins
    over the minimum on a small trade (as IBKR's 1% cap does).
    """
    currency: str = "EUR"
    pct: float = 0.0
    per_share: float = 0.0
    min_fee: float = 0.0
    max_fee: float | None = None    # None = no absolute maximum
    max_pct: float | None = None    # None = no maximum as % of trade value

    def commission(self, notional: float, qty: float) -> float:
        """Fee in this schedule's currency; notional is in the same currency."""
        notional, qty = abs(notional), abs(qty)
        fee = max(self.pct * notional + self.per_share * qty, self.min_fee)
        if self.max_fee is not None:
            fee = min(fee, self.max_fee)
        if self.max_pct is not None:
            fee = min(fee, self.max_pct * notional)
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
    spread_bps: dict[str, float]
    fractional: dict[str, bool]
    free_orders_per_month: int = 0
    free_order_classes: tuple[str, ...] = ()
    fx_free_eur_per_month: float = 0.0
    cash_interest_annual_pct: float = 0.0

    def converts(self, asset_class: str) -> bool:
        """True if an order in this class is an EUR<->USD conversion (it has an FX fee)."""
        return self.fx_fee_pct[asset_class] > 0

    def trade_cost(self, asset_class: str, notional_eur: float, *, fx_rate: float,
                   price_usd: float | None = None, free: bool = False,
                   fx_used_eur: float = 0.0) -> TradeCost:
        """Fees for one order of `notional_eur` (buy or sell; sign is ignored).

        fx_rate is the fill's ECB EUR/USD rate (USD per EUR): it converts a USD
        fee schedule to EUR. price_usd (the execution price) gives the share
        count for per-share fees. A free order pays no commission but still
        pays the FX fee. The FX fee applies once per order: a buy converts
        EUR→USD, a sell converts the proceeds USD→EUR. fx_used_eur is the
        EUR already converted this month in the ledger: the FX fee applies only
        to the part of this order above what's left of fx_free_eur_per_month.
        """
        notional = abs(notional_eur)
        sched = self.commission[asset_class]
        if sched.per_share and not price_usd:
            raise ValueError(f"{self.name}: per-share commission needs price_usd")
        qty = notional * fx_rate / price_usd if price_usd else 0.0
        if free:
            commission = 0.0
        elif sched.currency == "USD":
            commission = sched.commission(notional * fx_rate, qty) / fx_rate
        else:
            commission = sched.commission(notional, qty)
        allowance_left = max(self.fx_free_eur_per_month - fx_used_eur, 0.0)
        return TradeCost(commission_eur=commission,
                         fx_fee_eur=self.fx_fee_pct[asset_class] * max(notional - allowance_left, 0.0))

    def is_free_eligible(self, asset_class: str) -> bool:
        return self.free_orders_per_month > 0 and asset_class in self.free_order_classes

    def exec_price(self, asset_class: str, ref_price: float, side: str) -> float:
        """Reference price moved against us by slippage plus spread."""
        cost = (self.slippage_bps[asset_class] + self.spread_bps[asset_class]) / 10_000
        return ref_price * (1 + cost) if side == "buy" else ref_price * (1 - cost)

    def etp_daily_fee(self, asset_class: str, market_value_eur: float) -> float:
        """One calendar day of the ETP fee on a position (crypto only)."""
        if asset_class != "crypto" or self.etp_annual_fee_pct == 0:
            return 0.0
        return market_value_eur * self.etp_annual_fee_pct / DAYS_PER_YEAR

    def daily_interest(self, cash_eur: float) -> float:
        """One calendar day of interest on cash; nothing on a negative balance."""
        if cash_eur <= 0 or self.cash_interest_annual_pct == 0:
            return 0.0
        return cash_eur * self.cash_interest_annual_pct / DAYS_PER_YEAR

    def max_affordable_notional(self, asset_class: str, cash_eur: float, *, fx_rate: float,
                                price_usd: float | None = None, free: bool = False,
                                fx_used_eur: float = 0.0) -> float:
        """Largest buy notional n with n + fees(n) <= cash (0 if none)."""
        def fees(n: float) -> float:
            return self.trade_cost(asset_class, n, fx_rate=fx_rate, price_usd=price_usd,
                                   free=free, fx_used_eur=fx_used_eur).total_eur

        def fits(n: float) -> bool:
            return n + fees(n) <= cash_eur

        if cash_eur <= 0 or cash_eur - fees(0.0) <= 0:
            return 0.0
        lo, hi = 0.0, cash_eur
        for _ in range(100):   # fees are monotone in n, so bisection converges
            mid = (lo + hi) / 2
            if fits(mid):
                lo = mid
            else:
                hi = mid
        return lo


def cost_profile_from_config(name: str, raw: dict) -> CostProfile:
    """Build a CostProfile from its scenarios.yaml block (validated)."""
    def per_class(key: str, cast=float) -> dict:
        block = raw.get(key) or {}
        missing = [c for c in ASSET_CLASSES if c not in block]
        if missing:
            raise ValueError(f"cost profile {name}: {key} missing {missing}")
        return {c: cast(block[c]) for c in ASSET_CLASSES}

    def opt(v):
        return None if v is None else float(v)

    commission = {}
    for cls in ASSET_CLASSES:
        sched = (raw.get("commission") or {}).get(cls)
        if sched is None:
            raise ValueError(f"cost profile {name}: commission.{cls} missing")
        unknown = set(sched) - {"currency", "pct", "per_share", "min", "max", "max_pct"}
        if unknown:
            raise ValueError(f"cost profile {name}: commission.{cls} unknown keys {sorted(unknown)}")
        commission[cls] = FeeSchedule(
            currency=str(sched.get("currency", "EUR")),
            pct=float(sched.get("pct") or 0.0),
            per_share=float(sched.get("per_share") or 0.0),
            min_fee=float(sched.get("min") or 0.0),
            max_fee=opt(sched.get("max")),
            max_pct=opt(sched.get("max_pct")),
        )

    def strict_bool(v):
        if not isinstance(v, bool):
            raise ValueError(f"cost profile {name}: fractional values must be true/false")
        return v

    free_classes = tuple(raw.get("free_order_classes") or ())
    profile = CostProfile(
        name=name,
        commission=commission,
        fx_fee_pct=per_class("fx_fee_pct"),
        etp_annual_fee_pct=float(raw.get("etp_annual_fee_pct", 0.0)),
        slippage_bps=per_class("slippage_bps"),
        spread_bps=per_class("spread_bps"),
        fractional=per_class("fractional", strict_bool),
        free_orders_per_month=int(raw.get("free_orders_per_month", 0)),
        free_order_classes=free_classes,
        fx_free_eur_per_month=float(raw.get("fx_free_eur_per_month", 0.0)),
        cash_interest_annual_pct=float(raw.get("cash_interest_annual_pct", 0.0)),
    )
    for cls, s in profile.commission.items():
        if (s.currency not in CURRENCIES or s.pct < 0 or s.per_share < 0 or s.min_fee < 0
                or (s.max_fee is not None and s.max_fee < s.min_fee)
                or (s.max_pct is not None and s.max_pct <= 0)):
            raise ValueError(f"cost profile {name}: invalid commission for {cls}")
    if profile.free_orders_per_month < 0 or any(c not in ASSET_CLASSES for c in free_classes):
        raise ValueError(f"cost profile {name}: invalid free orders")
    if profile.free_orders_per_month and not free_classes:
        raise ValueError(f"cost profile {name}: free_orders_per_month needs free_order_classes")
    if profile.fx_free_eur_per_month < 0:
        raise ValueError(f"cost profile {name}: fx_free_eur_per_month must be >= 0")
    if profile.cash_interest_annual_pct < 0:
        raise ValueError(f"cost profile {name}: cash_interest_annual_pct must be >= 0")
    return profile
