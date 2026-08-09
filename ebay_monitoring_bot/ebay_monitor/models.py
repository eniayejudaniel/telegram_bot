from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Component:
    franchise: str
    language: str
    product_type: str
    set_name: str
    quantity: int = 1
    variant: str = "standard"
    confidence: float = 1.0
    notes: tuple[str, ...] = field(default_factory=tuple)
    identity: str = ""

    @property
    def group_key(self) -> str:
        return "|".join(
            [
                self.franchise or "unknown",
                self.language or "unknown",
                self.product_type or "unknown",
                self.variant or "standard",
                self.set_name or "unknown_set",
                self.identity or "standard_item",
            ]
        )


@dataclass
class Listing:
    item_id: str
    title: str
    url: str
    item_price: float
    postage: float
    currency: str
    buying_option: str
    seller_username: str = ""
    seller_feedback_count: int | None = None
    seller_feedback_percentage: float | None = None
    seller_location: str = ""
    location_bucket: str = "Unknown"
    end_time: str = ""
    coupon: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    components: list[Component] = field(default_factory=list)
    contents_uncertain: bool = False
    parser_notes: list[str] = field(default_factory=list)

    @property
    def total_price(self) -> float:
        return round(self.item_price + self.postage, 2)

    @property
    def total_units(self) -> int:
        units = sum(max(1, component.quantity) for component in self.components)
        return max(1, units)

    @property
    def price_per_unit(self) -> float:
        return round(self.total_price / self.total_units, 2)

    @property
    def is_mixed_lot(self) -> bool:
        keys = {component.group_key for component in self.components}
        return len(self.components) > 1 or len(keys) > 1

    @property
    def has_exclusive_variant(self) -> bool:
        return any(component.variant != "standard" for component in self.components)


@dataclass
class Baseline:
    group_key: str
    median_price: float
    sample_size: int
    low_confidence: bool


@dataclass
class ComponentValuation:
    component: Component
    baseline: Baseline | None
    unit_value: float
    total_value: float


@dataclass
class AlertCandidate:
    listing: Listing
    valuations: list[ComponentValuation]
    baseline_total: float
    opportunity_gbp: float
    below_market_pct: float
    confidence: str
    priority_score: float
    reasons: list[str] = field(default_factory=list)
