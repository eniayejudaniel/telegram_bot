from __future__ import annotations

from collections import defaultdict
from statistics import median

from .config import PricingSettings
from .models import AlertCandidate, Baseline, ComponentValuation, Listing


def build_baselines(listings: list[Listing], settings: PricingSettings) -> dict[str, Baseline]:
    samples: dict[str, list[float]] = defaultdict(list)
    for listing in listings:
        if listing.contents_uncertain or listing.is_mixed_lot or len(listing.components) != 1:
            continue
        component = listing.components[0]
        # Active auction bids are incomplete prices; fixed-price listings supply
        # the market baseline used to evaluate both BIN and auction candidates.
        if listing.buying_option == "AUCTION":
            continue
        if component.confidence < 0.7 or component.set_name == "unknown_set":
            continue
        unit_price = listing.total_price / max(1, component.quantity)
        if unit_price > 0:
            samples[component.group_key].append(unit_price)

    baselines: dict[str, Baseline] = {}
    for key, prices in samples.items():
        if not prices:
            continue
        baselines[key] = Baseline(
            group_key=key,
            median_price=round(float(median(prices)), 2),
            sample_size=len(prices),
            low_confidence=len(prices) < settings.min_baseline_samples,
        )
    return baselines


def evaluate_listing(
    listing: Listing,
    baselines: dict[str, Baseline],
    settings: PricingSettings,
) -> AlertCandidate | None:
    if listing.contents_uncertain and not settings.uncertain_listing_alerts:
        return None
    valuations: list[ComponentValuation] = []
    baseline_total = 0.0
    missing_baseline = False
    low_confidence = listing.contents_uncertain

    for component in listing.components:
        baseline = baselines.get(component.group_key)
        if not baseline:
            missing_baseline = True
            valuations.append(
                ComponentValuation(component=component, baseline=None, unit_value=0.0, total_value=0.0)
            )
            continue
        low_confidence = low_confidence or baseline.low_confidence
        total_value = baseline.median_price * max(1, component.quantity)
        baseline_total += total_value
        valuations.append(
            ComponentValuation(
                component=component,
                baseline=baseline,
                unit_value=baseline.median_price,
                total_value=round(total_value, 2),
            )
        )

    if baseline_total <= 0:
        if settings.uncertain_listing_alerts and listing.contents_uncertain:
            return AlertCandidate(
                listing=listing,
                valuations=valuations,
                baseline_total=0.0,
                opportunity_gbp=0.0,
                below_market_pct=0.0,
                confidence="contents uncertain",
                priority_score=0.0,
                reasons=["Contents uncertain; no reliable baseline yet."],
            )
        return None

    opportunity = round(baseline_total - listing.total_price, 2)
    below_pct = opportunity / baseline_total if baseline_total else 0.0
    if below_pct < settings.alert_threshold_below_median and not listing.contents_uncertain:
        return None

    priority = opportunity
    reasons: list[str] = []
    if listing.is_mixed_lot:
        priority += settings.priority_weight_mixed_lot
        reasons.append("mixed lot priority")
    if listing.has_exclusive_variant:
        priority += settings.priority_weight_exclusive
        reasons.append("exclusive variant priority")
    if missing_baseline:
        low_confidence = True
        reasons.append("some components missing baselines")

    confidence = "low confidence" if low_confidence else "high confidence"
    if listing.contents_uncertain:
        confidence = "contents uncertain"

    return AlertCandidate(
        listing=listing,
        valuations=valuations,
        baseline_total=round(baseline_total, 2),
        opportunity_gbp=opportunity,
        below_market_pct=round(below_pct, 4),
        confidence=confidence,
        priority_score=round(priority, 2),
        reasons=reasons,
    )


def rank_alerts(alerts: list[AlertCandidate]) -> list[AlertCandidate]:
    return sorted(alerts, key=lambda alert: alert.priority_score, reverse=True)
