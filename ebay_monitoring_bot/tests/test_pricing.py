from __future__ import annotations

import unittest

from ebay_monitor.config import PricingSettings
from ebay_monitor.models import Baseline, Component, Listing
from ebay_monitor.pricing import build_baselines, evaluate_listing


def listing(
    product_type: str,
    buying_option: str = "FIXED_PRICE",
    *,
    identity: str = "",
) -> Listing:
    return Listing(
        item_id="1",
        title="test",
        url="https://example.test",
        item_price=10,
        postage=0,
        currency="GBP",
        buying_option=buying_option,
        components=[
            Component(
                "pokemon",
                "english",
                product_type,
                "Test Set",
                confidence=0.9,
                identity=identity,
            )
        ],
    )


class PricingSafetyTests(unittest.TestCase):
    def test_single_cards_build_identity_specific_baselines(self) -> None:
        first = listing("single_card", identity="op10-118|psa-10")
        second = listing("single_card", identity="op10-118|psa-10")
        other = listing("single_card", identity="op10-119|psa-10")
        baselines = build_baselines([first, second, other], PricingSettings())
        self.assertEqual(baselines[first.components[0].group_key].sample_size, 2)
        self.assertEqual(baselines[other.components[0].group_key].sample_size, 1)

    def test_auctions_use_bin_baseline_without_joining_baseline_samples(self) -> None:
        item = listing("booster_box", "AUCTION")
        self.assertEqual(build_baselines([item], PricingSettings()), {})
        baseline = Baseline(item.components[0].group_key, 100, 10, False)
        alert = evaluate_listing(item, {baseline.group_key: baseline}, PricingSettings())
        self.assertIsNotNone(alert)

    def test_low_sample_baseline_alerts_with_low_confidence(self) -> None:
        item = listing("booster_box")
        baseline = Baseline(item.components[0].group_key, 100, 2, True)
        alert = evaluate_listing(item, {baseline.group_key: baseline}, PricingSettings())
        self.assertIsNotNone(alert)
        assert alert is not None
        self.assertEqual(alert.confidence, "low confidence")

    def test_uncertain_listing_cannot_bypass_threshold_when_disabled(self) -> None:
        item = listing("booster_box")
        item.contents_uncertain = True
        baseline = Baseline(item.components[0].group_key, 10.05, 10, False)
        self.assertIsNone(
            evaluate_listing(
                item,
                {baseline.group_key: baseline},
                PricingSettings(uncertain_listing_alerts=False),
            )
        )


if __name__ == "__main__":
    unittest.main()
