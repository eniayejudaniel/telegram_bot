import unittest
import os
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from prediction_engine import FEATURE_NAMES, LogisticModel, build_combination, build_optimized_combination
from bet9ja_virtual_bot import forward_gate, passes_market_pattern, target_round
from bet9ja_virtual_betslip import (
    Bet9jaBrowser,
    PreSubmissionError,
    activate_league,
    classify_ticket_response,
    frame_matches_league,
    parse_prediction_message,
    sanitize_ticket_request,
    wager_session_expired,
    build_direct_ticket,
)


class PredictionSystemTests(unittest.TestCase):
    @patch("bet9ja_virtual_betslip._live_market_outcome", return_value={
        "_oid": 8583446854, "_fid": 12, "fieldname": "u", "value": "1.22"
    })
    def test_direct_ticket_uses_rgs_units_and_market_identifiers(self, _outcome):
        details = [{
            "match_id": 123, "timestamp": int(time.time()) + 60,
            "home_id": 1, "away_id": 2, "home": "Home", "away": "Away",
            "league": "Bundesliga", "market": "under_3_5", "odds": 1.22,
            "season": 99, "round": 5,
        }]
        payload = build_direct_ticket(details, 100, "customer")
        selection = payload[0]["selections"][0]
        self.assertEqual(selection["odds"], 12200)
        self.assertEqual(
            selection["id"],
            "18/13?total=3.5",
        )
        self.assertEqual(
            selection["eventId"],
            "123",
        )
        self.assertEqual(payload[0]["bets"][0]["stake"]["value"], "1000000")

    @patch("bet9ja_virtual_betslip._live_market_outcome", return_value={
        "_oid": 8583446854, "_fid": 11, "fieldname": "o", "value": "1.30"
    })
    def test_direct_ticket_rejects_changed_odds(self, _outcome):
        details = [{
            "match_id": 123, "timestamp": int(time.time()) + 60,
            "home_id": 1, "away_id": 2, "home": "Home", "away": "Away",
            "league": "English League", "market": "over_1_5", "odds": 1.22,
            "season": 99, "round": 5,
        }]
        with self.assertRaisesRegex(PreSubmissionError, "Odds changed"):
            build_direct_ticket(details, 100, "customer")

    def test_prediction_parser_rejects_cross_league_ticket(self):
        message = (
            "Bundesliga: A vs B — under_3_5 @ 1.22\n"
            "Euro League: C vs D — 12 @ 1.25\n"
            "Combined odds: 1.52"
        )
        with self.assertRaisesRegex(ValueError, "must all belong to one league"):
            parse_prediction_message(message)

    def test_prediction_parser_rejects_inconsistent_combined_odds(self):
        message = (
            "Bundesliga: A vs B — under_3_5 @ 1.22\n"
            "Bundesliga: C vs D — 12 @ 1.25\n"
            "Combined odds: 1.99"
        )
        with self.assertRaisesRegex(ValueError, "combined odds do not match"):
            parse_prediction_message(message)

    def test_animated_league_tab_uses_dom_click_fallback(self):
        control = MagicMock()
        control.is_visible.return_value = True
        control.click.side_effect = RuntimeError("element is not stable")
        locator = MagicMock()
        locator.count.return_value = 1
        locator.nth.return_value = control
        frame = MagicMock()
        frame.get_by_text.return_value = locator
        page = MagicMock()
        page.main_frame = MagicMock()
        page.frames = [page.main_frame, frame]

        activate_league(page, "English League")

        control.click.assert_called_once_with(timeout=2_000)
        control.evaluate.assert_called_once_with("element => element.click()")

    def test_browser_failure_before_click_is_retryable(self):
        browser = Bet9jaBrowser.__new__(Bet9jaBrowser)
        browser.page = MagicMock()
        browser.page.frames = []
        browser.ticket_timeout_seconds = 75
        browser.keep_warm = MagicMock(side_effect=RuntimeError("browser pipe closed"))
        browser._diagnostic = MagicMock()
        browser.diagnostics_path = SimpleNamespace(
            with_name=lambda _name: SimpleNamespace(resolve=lambda: "failure.png")
        )

        with self.assertRaisesRegex(PreSubmissionError, "browser pipe closed"):
            browser.prepare("message", stake=100, submit=True)

    def test_browser_restart_closes_before_starting(self):
        browser = Bet9jaBrowser.__new__(Bet9jaBrowser)
        calls = []
        browser._diagnostic = MagicMock()
        browser.close = MagicMock(side_effect=lambda: calls.append("close"))
        browser.start = MagicMock(side_effect=lambda: calls.append("start") or browser)

        self.assertIs(browser.restart(), browser)
        self.assertEqual(calls, ["close", "start"])

    def test_expired_browser_age_forces_credential_session_refresh(self):
        browser = Bet9jaBrowser.__new__(Bet9jaBrowser)
        browser._authenticated_at = time.monotonic() - 4_000
        browser._session_invalid = False
        browser.start = MagicMock(return_value=browser)
        browser.restart = MagicMock(return_value=browser)
        browser._diagnostic = MagicMock()

        with patch.dict(os.environ, {"BET9JA_SESSION_REFRESH_SECONDS": "3600"}):
            self.assertTrue(browser.keep_warm())

        browser.restart.assert_called_once_with(force_relogin=True)

    def test_virtual_frame_is_bound_to_requested_league(self):
        english = SimpleNamespace(
            url="https://vflive-vs001.akamaized.net/vfel/desktop/index"
        )
        bundesliga = SimpleNamespace(
            url="https://vflive-vs001.akamaized.net/vfb/desktop/index"
        )
        self.assertTrue(frame_matches_league(english, "English League"))
        self.assertFalse(frame_matches_league(bundesliga, "English League"))

    def test_ticket_response_accepts_ticket_identifier(self):
        result = classify_ticket_response(200, {"ticket": {"id": "ABC123"}})
        self.assertEqual(result["state"], "accepted")
        self.assertEqual(result["ticket_id"], "ABC123")

    def test_ticket_response_accepts_explicit_success(self):
        result = classify_ticket_response(201, {"success": True, "status": "placed"})
        self.assertEqual(result["state"], "accepted")

    def test_ticket_response_accepts_native_rgs_success_contract(self):
        result = classify_ticket_response(
            200, {"statusCode": 0, "data": [[{"code": 0, "ticketId": "T-1"}]]}
        )
        self.assertEqual(result["state"], "accepted")
        self.assertEqual(result["ticket_id"], "T-1")

    def test_ticket_response_does_not_reject_zero_error_code_key(self):
        result = classify_ticket_response(
            200, {"statusCode": 0, "data": [[{"code": 0, "errorCode": 0}]]}
        )
        self.assertEqual(result["state"], "accepted")

    def test_ticket_response_rejects_nonzero_native_result_code(self):
        result = classify_ticket_response(
            200, {"statusCode": 0, "data": [[{"code": 703}]]}
        )
        self.assertEqual(result["state"], "rejected")

    def test_ticket_response_does_not_accept_unrelated_generic_id(self):
        result = classify_ticket_response(200, {"id": "request-id"})
        self.assertEqual(result["state"], "uncertain")

    def test_ticket_request_capture_removes_customer_identity(self):
        payload = [{
            "version": "2.2",
            "sender": {
                "bookmakerId": 9478,
                "endCustomer": {"id": "private", "username": "private"},
            },
            "selections": [{"id": "selection-1"}],
            "bets": [{"stake": {"type": "unit", "value": "100"}}],
        }]
        sanitized = sanitize_ticket_request(payload)
        self.assertEqual(sanitized[0]["selections"][0]["id"], "selection-1")
        self.assertEqual(sanitized[0]["bets"][0]["stake"]["value"], "100")
        self.assertNotIn("id", sanitized[0]["sender"]["endCustomer"])
        self.assertTrue(sanitized[0]["sender"]["endCustomer"]["present"])

    def test_ticket_response_rejects_business_error_with_http_200(self):
        result = classify_ticket_response(200, {"success": False, "message": "Insufficient funds"})
        self.assertEqual(result["state"], "rejected")

    def test_wager_session_expiry_contract_is_detected(self):
        payload = {
            "data": [[{
                "code": 21004,
                "message": "REJECTED_WG_SESSION_EXPIRED: Buy in (reserve) failed",
            }]]
        }
        self.assertTrue(wager_session_expired(payload))
        self.assertFalse(wager_session_expired({"data": [[{"code": 0}]]}))

    def test_ticket_response_keeps_unrecognized_200_uncertain(self):
        result = classify_ticket_response(200, {"message": "request received"})
        self.assertEqual(result["state"], "uncertain")

    def test_combination_uses_one_market_per_match(self):
        candidates = [
            {"match_id": 1, "market": "12", "probability": 0.84, "odds": 1.25},
            {"match_id": 1, "market": "over_1_5", "probability": 0.82, "odds": 1.30},
            {"match_id": 2, "market": "under_3_5", "probability": 0.80, "odds": 1.35},
            {"match_id": 3, "market": "12", "probability": 0.78, "odds": 1.28},
        ]
        result = build_combination(candidates, legs=3)
        self.assertEqual([leg["match_id"] for leg in result["legs"]], [1, 2, 3])
        self.assertAlmostEqual(result["combined_odds"], 1.25 * 1.35 * 1.28)

    def test_probability_is_bounded(self):
        model = LogisticModel([0.0, 1000.0], [0.0], [1.0])
        self.assertGreater(model.predict([10]), 0.999)
        self.assertLess(model.predict([-10]), 0.001)

    def test_target_round_moves_at_actual_kickoff(self):
        competition = {"competition_id": 9, "start_datetime": 1000, "end_datetime": 7720}
        self.assertEqual(target_round(competition, 1060), (9, 1))
        self.assertEqual(target_round(competition, 1119), (9, 1))
        self.assertEqual(target_round(competition, 1120), (9, 2))
        self.assertEqual(target_round(competition, 1339), (9, 2))
        self.assertEqual(target_round(competition, 1340), (9, 3))

    def test_target_odds_accumulator(self):
        candidates = [
            {"match_id": 1, "market": "12", "probability": .84, "odds": 1.25},
            {"match_id": 2, "market": "over_1_5", "probability": .83, "odds": 1.25},
            {"match_id": 3, "market": "under_3_5", "probability": .82, "odds": 1.30},
        ]
        result = build_combination(candidates, legs=None, target_odds=2.0)
        self.assertEqual(len(result["legs"]), 3)
        self.assertGreaterEqual(result["combined_odds"], 2.0)

    def test_ten_odds_ticket_is_suppressed_when_short(self):
        candidates = [
            {"match_id": i, "market": "over_1_5", "probability": .80, "odds": 1.25}
            for i in range(1, 6)
        ]
        result = build_combination(candidates, legs=None, target_odds=10.0, max_legs=20)
        self.assertEqual(result["legs"], [])
        self.assertEqual(result["combined_odds"], 0.0)

    def test_duplicate_match_market_can_be_removed_before_build(self):
        candidates = [
            {"match_id": i, "market": "over_1_5", "probability": .80, "odds": 1.60}
            for i in range(1, 7)
        ]
        existing = {(1, "over_1_5"), (2, "over_1_5")}
        fresh = [c for c in candidates if (c["match_id"], c["market"]) not in existing]
        result = build_combination(fresh, legs=None, target_odds=10.0, max_legs=20)
        self.assertEqual(result["legs"], [])

    def test_optimizer_prefers_the_highest_joint_probability(self):
        candidates = [
            {"match_id": 1, "market": "over_1_5", "probability": .90, "odds": 1.50},
            {"match_id": 2, "market": "under_3_5", "probability": .88, "odds": 1.40},
            {"match_id": 3, "market": "12", "probability": .83, "odds": 1.60},
        ]
        result = build_optimized_combination(candidates, .82, 2.0)
        self.assertEqual([leg["match_id"] for leg in result["legs"]], [1, 2])
        self.assertAlmostEqual(result["estimated_joint_probability"], .90 * .88)

    def test_optimizer_rejects_a_low_joint_probability_ticket(self):
        candidates = [
            {"match_id": 1, "market": "12", "probability": .82, "odds": 1.50},
            {"match_id": 2, "market": "under_3_5", "probability": .82, "odds": 1.40},
        ]
        result = build_optimized_combination(
            candidates, .82, 2.0, joint_probability_floors={2: .70, 3: .50}
        )
        self.assertEqual(result["legs"], [])

    def test_optimizer_enforces_maximum_ticket_odds(self):
        candidates = [
            {"match_id": 1, "market": "under_3_5", "probability": .84, "odds": 1.40},
            {"match_id": 2, "market": "under_3_5", "probability": .84, "odds": 1.40},
        ]
        result = build_optimized_combination(candidates, .82, 1.50, 2, 2, {2: .67}, 1.70)
        self.assertEqual(result["legs"], [])

    def test_optimizer_can_require_different_leagues(self):
        candidates = [
            {"match_id": 1, "league": "A", "market": "12", "probability": .84, "odds": 1.24},
            {"match_id": 2, "league": "A", "market": "12", "probability": .84, "odds": 1.24},
        ]
        result = build_optimized_combination(candidates, .82, 1.50, 2, 2, {2: .67}, 1.55, True)
        self.assertEqual(result["legs"], [])

    def test_optimizer_can_require_same_league_with_mixed_markets(self):
        candidates = [
            {"match_id": 1, "league": "Bundesliga", "market": "12", "probability": .84, "odds": 1.25},
            {"match_id": 2, "league": "Bundesliga", "market": "under_3_5", "probability": .83, "odds": 1.22},
            {"match_id": 3, "league": "Euro League", "market": "under_3_5", "probability": .95, "odds": 1.22},
        ]
        result = build_optimized_combination(
            candidates, .82, 1.50, 2, 2, {2: .67}, 1.55,
            require_same_league=True,
        )
        self.assertEqual({"Bundesliga"}, {leg["league"] for leg in result["legs"]})
        self.assertEqual({"12", "under_3_5"}, {leg["market"] for leg in result["legs"]})

    def test_market_pattern_filters(self):
        features = dict.fromkeys(FEATURE_NAMES, 0.0)
        features.update(expected_goals=2.30, home_market_rate=.80, away_market_rate=.76,
                        h2h_rate=.85, ppg_diff=.35, elo_diff=.05)
        vector = [features[name] for name in FEATURE_NAMES]
        self.assertTrue(passes_market_pattern("under_3_5", vector))
        self.assertTrue(passes_market_pattern("12", vector))
        features["expected_goals"] = 2.50
        vector = [features[name] for name in FEATURE_NAMES]
        self.assertFalse(passes_market_pattern("under_3_5", vector))
        self.assertTrue(passes_market_pattern("over_1_5", vector))
        features["home_market_rate"] = .70
        vector = [features[name] for name in FEATURE_NAMES]
        self.assertFalse(passes_market_pattern("over_1_5", vector))

    def test_forward_gate_requires_sample_and_positive_roi(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE combinations(strategy TEXT,status TEXT,hit INTEGER,combined_odds REAL)")
        old = os.environ.get("FORWARD_GATE_MIN_TICKETS")
        os.environ["FORWARD_GATE_MIN_TICKETS"] = "3"
        try:
            db.executemany("INSERT INTO combinations VALUES('v2','settled',?,?)",
                           [(1, 2.1), (0, 2.0)])
            self.assertFalse(forward_gate(db, "v2")["open"])
            db.execute("INSERT INTO combinations VALUES('v2','settled',1,2.1)")
            self.assertTrue(forward_gate(db, "v2")["open"])
        finally:
            if old is None:
                os.environ.pop("FORWARD_GATE_MIN_TICKETS", None)
            else:
                os.environ["FORWARD_GATE_MIN_TICKETS"] = old


if __name__ == "__main__":
    unittest.main()
