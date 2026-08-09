from __future__ import annotations

import unittest

from ebay_monitor.config import ParserSettings, SearchSettings
from ebay_monitor.parser import parse_listing, parse_search_html, time_remaining


class ParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = ParserSettings(
            assumed_case_units={
                "elite_trainer_box": 10,
                "pokemon_center_etb": 4,
                "booster_box": 6,
                "booster_bundle": 10,
            },
            set_aliases={
                "pokemon": {
                    "prismatic evolutions": "Prismatic Evolutions",
                    "pokemon 151": "Pokemon 151",
                },
                "one_piece": {
                    "romance dawn": "Romance Dawn",
                },
            },
        )
        self.search = SearchSettings(keywords=["pokemon"], excluded_title_terms=["empty box"])

    def item(self, title: str) -> dict:
        return {
            "itemId": "v1|123|0",
            "title": title,
            "itemWebUrl": "https://www.ebay.co.uk/itm/123",
            "price": {"value": "100", "currency": "GBP"},
            "shippingOptions": [{"shippingCost": {"value": "5", "currency": "GBP"}}],
            "buyingOptions": ["FIXED_PRICE"],
            "seller": {"username": "seller", "feedbackScore": 100, "feedbackPercentage": "99.8"},
            "itemLocation": {"country": "GB"},
        }

    def test_pokemon_center_etb_is_separate_variant(self) -> None:
        listing = parse_listing(
            self.item("Pokemon Center Prismatic Evolutions Elite Trainer Box ETB"),
            self.parser,
            self.search,
        )
        self.assertIsNotNone(listing)
        assert listing is not None
        self.assertEqual(len(listing.components), 1)
        self.assertEqual(listing.components[0].variant, "pokemon_center")
        self.assertEqual(listing.components[0].product_type, "elite_trainer_box")

    def test_mixed_lot_quantities(self) -> None:
        listing = parse_listing(
            self.item("Pokemon 151 mixed lot 2 ETBs plus 2 booster bundles"),
            self.parser,
            self.search,
        )
        self.assertIsNotNone(listing)
        assert listing is not None
        quantities = sorted(component.quantity for component in listing.components)
        self.assertEqual(quantities, [2, 2])
        self.assertTrue(listing.is_mixed_lot)

    def test_etb_synonyms_do_not_double_count(self) -> None:
        listing = parse_listing(
            self.item("Pokemon Mega Evolution ETB Elite Trainer Box New Factory Sealed"),
            self.parser,
            self.search,
        )
        self.assertIsNotNone(listing)
        assert listing is not None
        self.assertEqual(len(listing.components), 1)
        self.assertEqual(listing.components[0].quantity, 1)

    def test_explicit_one_etb_synonym_does_not_double_count(self) -> None:
        listing = parse_listing(
            self.item("1 ETB Pokemon Mega Evolution Elite Trainer Box New Factory Sealed"),
            self.parser,
            self.search,
        )
        self.assertIsNotNone(listing)
        assert listing is not None
        self.assertEqual(len(listing.components), 1)
        self.assertEqual(listing.components[0].quantity, 1)

    def test_etb_promo_card_is_single_card_not_sealed_etb(self) -> None:
        listing = parse_listing(
            self.item("Pokemon Tyrunt 070 Mega Evolution ETB Black Star Promo Holo NM"),
            self.parser,
            self.search,
        )
        self.assertIsNotNone(listing)
        assert listing is not None
        self.assertEqual(len(listing.components), 1)
        self.assertEqual(listing.components[0].product_type, "single_card")

    def test_excluded_title(self) -> None:
        listing = parse_listing(self.item("Pokemon 151 empty box only"), self.parser, self.search)
        self.assertIsNone(listing)

    def test_year_is_not_quantity(self) -> None:
        listing = parse_listing(
            self.item("Prismatic Evolutions Pokemon Booster Bundle 2025 German Sealed"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(listing.components[0].quantity, 1)
        self.assertEqual(listing.components[0].language, "german")

    def test_set_code_is_not_quantity(self) -> None:
        listing = parse_listing(
            self.item("One Piece OP09 Booster Box English Sealed"), self.parser, self.search
        )
        assert listing is not None
        self.assertEqual(listing.components[0].quantity, 1)

    def test_pack_count_does_not_multiply_or_duplicate_box(self) -> None:
        listing = parse_listing(
            self.item("Pokemon Evolving Skies Booster Box 36 Packs New Sealed"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(len(listing.components), 1)
        self.assertEqual(listing.components[0].product_type, "booster_box")
        self.assertEqual(listing.components[0].quantity, 1)

    def test_explicit_suffix_quantity_wins_over_set_number(self) -> None:
        listing = parse_listing(
            self.item("pokemon 151 mini tins x3 brand new factory sealed"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(listing.components[0].quantity, 3)

    def test_promo_number_and_year_are_not_quantities(self) -> None:
        listing = parse_listing(
            self.item("PIKACHU PALDEA EVOLVED ETB BLACK STAR PROMO EN 027 2023 PSA 9"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(len(listing.components), 1)
        self.assertEqual(listing.components[0].product_type, "single_card")
        self.assertEqual(listing.components[0].quantity, 1)
        self.assertEqual(listing.components[0].identity, "027|psa-9")

    def test_sealed_svp_promo_is_a_single_card(self) -> None:
        listing = parse_listing(
            self.item("Pokemon Paldean Fates Mimikyu SVP075 ETB Promo Sealed"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(len(listing.components), 1)
        self.assertEqual(listing.components[0].product_type, "single_card")
        self.assertEqual(listing.components[0].quantity, 1)

    def test_acrylic_case_listing_is_conservatively_excluded(self) -> None:
        listing = parse_listing(
            self.item("One Piece Romance Dawn Booster Box Sealed w Acrylic Case"),
            self.parser,
            self.search,
        )
        self.assertIsNone(listing)

    def test_accessory_and_empty_titles_are_excluded(self) -> None:
        titles = (
            "*EMPTY* Pokemon 151 Mini Tin Set Including Art NO PACKS",
            "Pokemon Booster Bundle Protector Case Mega Evolution",
            "One Piece Acrylic Display Case for OP01 Booster Box",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertIsNone(parse_listing(self.item(title), self.parser, self.search))

    def test_explicit_ten_box_case_is_not_double_counted(self) -> None:
        listing = parse_listing(
            self.item("Pokemon Prismatic Evolutions Elite Trainer Box ETB 10-Box Case"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(len(listing.components), 1)
        self.assertEqual(listing.components[0].quantity, 10)

    def test_display_pack_count_is_not_box_quantity(self) -> None:
        titles = (
            "One Piece OP09 Emperors in the New World 24 Booster Display EN",
            "One Piece Two Legends Booster Display (24 Envelopes) JAP",
        )
        for title in titles:
            with self.subTest(title=title):
                listing = parse_listing(self.item(title), self.parser, self.search)
                assert listing is not None
                self.assertEqual(listing.components[0].quantity, 1)

    def test_etb_card_contents_are_not_etb_quantity(self) -> None:
        listing = parse_listing(
            self.item("Pokemon Mega Evolution Elite Trainer Box 65 Cards English 2025"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(listing.components[0].quantity, 1)

    def test_from_case_means_one_box(self) -> None:
        titles = (
            "One Piece OP10 Booster Box 1 Sealed Box From Fresh Case",
            "One Piece OP10 Booster Box Sealed From Case",
            "One Piece OP11 Booster Box Case Fresh Sealed",
        )
        for title in titles:
            with self.subTest(title=title):
                listing = parse_listing(self.item(title), self.parser, self.search)
                assert listing is not None
                self.assertEqual(listing.components[0].quantity, 1)

    def test_explicit_quantity_beats_accessory_case(self) -> None:
        listing = parse_listing(
            self.item("2x One Piece OP-01 Booster Box Japanese Sealed CIB w PET Case"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(listing.components[0].quantity, 2)

    def test_uv_acrylic_display_case_is_excluded(self) -> None:
        listing = parse_listing(
            self.item("One Piece OP01 Booster Box Acrylic Display Case UV Protection Magnet"),
            self.parser,
            self.search,
        )
        self.assertIsNone(listing)

    def test_bundle_pack_count_is_contents_not_quantity(self) -> None:
        listing = parse_listing(
            self.item("Pokemon Mega Evolution Booster Bundle 6 Booster Packs"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(listing.components[0].quantity, 1)

    def test_codes_sleeves_and_magnetic_cases_are_excluded(self) -> None:
        titles = (
            "156x Mega Evolution TCG Live Codes Includes 2 ETB Bundle Codes",
            "Pokemon Pack 65 Protective Bags Eevee ETB Sleeve",
            "One Piece OP02 Booster Box Acrylic Display Case Magnetic Lid",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertIsNone(parse_listing(self.item(title), self.parser, self.search))

    def test_generic_etb_promo_is_single_card(self) -> None:
        listing = parse_listing(
            self.item("Magneton 159 Pokemon Surging Sparks ETB Promo Sealed"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(listing.components[0].product_type, "single_card")

    def test_first_one_piece_code_wins_over_later_range(self) -> None:
        parser = ParserSettings(
            assumed_case_units=self.parser.assumed_case_units,
            set_aliases={
                **self.parser.set_aliases,
                "one_piece": {
                    **self.parser.set_aliases["one_piece"],
                    "op09": "Emperors in the New World",
                    "op01": "Romance Dawn",
                },
            },
        )
        listing = parse_listing(
            self.item("Japanese OP-09 One Piece Booster Box OP-01-15"), parser, self.search
        )
        assert listing is not None
        self.assertEqual(listing.components[0].set_name, "Emperors in the New World")

    def test_trailing_quantity_after_descriptors(self) -> None:
        listing = parse_listing(
            self.item("Pokemon Paldean Fates Booster Bundle Factory Sealed X2"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(listing.components[0].quantity, 2)

    def test_tin_card_count_is_contents(self) -> None:
        listing = parse_listing(
            self.item("Pokemon Crown Zenith Moltres Tin 10 Cards Box"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(listing.components[0].quantity, 1)

    def test_double_packs_and_standalone_acrylic_displays_are_excluded(self) -> None:
        titles = (
            "One Piece DP-01 Case OP-04 Double Packs x8",
            "One Piece OP1 Sealed Booster Box Acrylic Display Case",
            "One Piece OP2 Booster Box Protective Acrylic Case Premium Display",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertIsNone(parse_listing(self.item(title), self.parser, self.search))

    def test_acrylic_magnetic_case_listing_is_excluded(self) -> None:
        listing = parse_listing(
            self.item("Pokemon Journey Together Booster Bundle with Acrylic Magnetic Case"),
            self.parser,
            self.search,
        )
        self.assertIsNone(listing)

    def test_pokemon_center_suffix_does_not_add_standard_etb(self) -> None:
        listing = parse_listing(
            self.item("Pokemon Temporal Forces Elite Trainer Box Pokemon Center ETB"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(len(listing.components), 1)
        self.assertEqual(listing.components[0].variant, "pokemon_center")

    def test_leading_x_quantity_is_parsed(self) -> None:
        listing = parse_listing(
            self.item("x2 Perfect Order Pokemon Center Elite Trainer Box"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(listing.components[0].quantity, 2)

    def test_number_word_in_set_title_is_not_quantity(self) -> None:
        listing = parse_listing(
            self.item("One Piece The Azure Seas Seven Booster Box"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(listing.components[0].quantity, 1)

    def test_half_box_and_unsealed_products_are_excluded(self) -> None:
        titles = (
            "Pokemon Perfect Order Half Booster Box Factory Sealed",
            "Pokemon Pitch Black 18 Pack Display Box Sealed",
            "Pokemon Chaos Rising Elite Trainer Box New and Unsealed",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertIsNone(parse_listing(self.item(title), self.parser, self.search))

    def test_remaining_accessory_and_bulk_patterns_are_excluded(self) -> None:
        titles = (
            "Magnetic Acrylic Display Case One Piece OP-01 Booster Box",
            "One Piece Booster Box Acrylic Display Protector Case All Sizes",
            "Pokemon Greninja ETB Sleeves Coin Dice Set",
            "Pokemon Bulk With 2 Tins 350 Cards",
            "Pokemon Prismatic Evolutions Mini Tin Tin Only",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertIsNone(parse_listing(self.item(title), self.parser, self.search))

    def test_holo_promo_before_etb_is_single_card(self) -> None:
        listing = parse_listing(
            self.item("Pokemon Eevee 173 Holo Promo Prismatic Evolutions ETB"),
            self.parser,
            self.search,
        )
        assert listing is not None
        self.assertEqual(listing.components[0].product_type, "single_card")

    def test_relative_auction_time_is_displayed(self) -> None:
        self.assertEqual(time_remaining("2d 4h"), "2d 4h")
        self.assertEqual(time_remaining("3h 12m"), "3h 12m")

    def test_parse_current_ebay_search_card_markup(self) -> None:
        html = """
        <ul class="srp-results srp-grid clearfix">
          <li class="s-card" data-listingid="188700394539">
            <a class="s-card__link" href="https://www.ebay.co.uk/itm/188700394539?_skw=pokemon+151">
              <div class="s-card__title">
                <span class="su-styled-text primary default">Pokemon 151 mixed lot 2 ETBs plus 2 booster bundles</span>
                <span class="clipped">Opens in a new window or tab</span>
              </div>
            </a>
            <span class="su-styled-text primary bold large-1 s-card__price">Ł120.00</span>
            <span class="su-styled-text secondary large">Buy It Now</span>
            <span class="su-styled-text secondary large">+Ł5.50 delivery</span>
            <span class="su-styled-text secondary large">from United Kingdom</span>
          </li>
        </ul>
        """
        listings = parse_search_html(html, self.parser, self.search, buying_option_hint="bin")
        self.assertEqual(len(listings), 1)
        listing = listings[0]
        self.assertEqual(listing.item_id, "188700394539")
        self.assertEqual(listing.item_price, 120.0)
        self.assertEqual(listing.postage, 5.5)
        self.assertEqual(listing.location_bucket, "UK")
        self.assertEqual(listing.buying_option, "BIN")
        self.assertTrue(listing.is_mixed_lot)


if __name__ == "__main__":
    unittest.main()
