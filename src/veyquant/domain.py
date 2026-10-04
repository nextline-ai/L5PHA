from dataclasses import dataclass
from decimal import Decimal


def valid_money(*values: Decimal) -> bool:
    return all(isinstance(v, Decimal) and v.is_finite() and v >= 0 for v in values)


@dataclass(frozen=True)
class Event:
    id: str
    symbol: str
    currency: str
    observed_at: int
    material: bool


@dataclass(frozen=True)
class Evidence:
    id: str
    source: str
    as_of: int
    collected_at: int
    symbol: str
    currency: str
    unit: str
    summary: str
    status: str = "available"  # available, missing, failed, conflicting


@dataclass(frozen=True)
class Proposal:
    id: str
    event_id: str
    symbol: str
    currency: str
    quantity: int
    limit_price: Decimal
    created_at: int
    expires_at: int
    policy_version: str
    strategy_version: str
    evidence_ids: tuple[str, ...]
    summary: str
    counterargument: str
    uncertainty: str
    side: str = "BUY"


@dataclass(frozen=True)
class Quote:
    symbol: str
    currency: str
    price: Decimal
    as_of: int


@dataclass(frozen=True)
class AccountSnapshot:
    """Single-currency paper account; all values include manual activity.

    cash is total cash BEFORE reservations. exposure is filled holdings only.
    External reservations exclude this store's paper reservations (no double count).
    Real broker normalization and mixed currency valuation are not implemented.
    """

    currency: str
    cash: Decimal
    exposure: Decimal
    symbol_exposure: dict[str, Decimal]
    external_reserved: Decimal
    external_symbol_reserved: dict[str, Decimal]
    as_of: int
    reconciled: bool


@dataclass(frozen=True)
class RiskPolicy:
    version: str
    currency: str
    allowed_symbols: frozenset[str]
    max_order: Decimal
    max_exposure: Decimal
    max_symbol_exposure: Decimal
    max_price_drift: Decimal
    max_age_seconds: int = 30

    def __post_init__(self):
        if (
            not valid_money(
                self.max_order, self.max_exposure, self.max_symbol_exposure, self.max_price_drift
            )
            or not 0 <= self.max_price_drift <= 1
        ):
            raise ValueError("invalid risk policy")
        if self.max_age_seconds <= 0 or not self.version or not self.allowed_symbols:
            raise ValueError("invalid risk policy")


@dataclass(frozen=True)
class RiskResult:
    accepted: bool
    reason: str
    proposal_id: str
