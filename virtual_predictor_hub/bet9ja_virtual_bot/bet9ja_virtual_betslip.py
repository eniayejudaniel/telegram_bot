#!/usr/bin/env python3
"""Prepare a Bet9ja virtual-football betslip from a bot prediction message.

Bot integration:
    from bet9ja_virtual_betslip import prepare_bet
    result = prepare_bet(message=telegram_text, stake=100)

The default is preparation-only: selections and stake are entered, but the
Place Bet button is not clicked. Real submission requires BOTH submit=True and
BET9JA_ALLOW_REAL_BET=true. Do not enable those during shadow testing.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SITE_URL = "https://virtual.bet9ja.com/virtual/?s=new"
DEFAULT_ENV_FILE = Path(__file__).parent / ".env"
DEFAULT_PROFILE_DIR = Path(__file__).parent / "data" / "bet9ja_playwright_profile"
SUPPORTED_LEAGUES = {
    "bundesliga": "Bundesliga",
    "euro league": "Euro League",
    "english league": "English League",
}
LEAGUE_FRAME_PATHS = {
    "Bundesliga": "/vfb/",
    "Euro League": "/vflm/",
    "English League": "/vfel/",
}
MARKET_LABELS = {
    "under_3_5": ("under 3.5", "under_3_5", "u 3.5"),
    "over_1_5": ("over 1.5", "over_1_5", "o 1.5"),
    "12": ("12", "1 or 2", "home or away", "no draw"),
}
MARKET_DATA_NAME_PARTS = {
    "under_3_5": "/18/13?total=3.5",
    "over_1_5": "/18/12?total=1.5",
    "12": "/10/10",
}
DIRECT_MARKETS = {
    "under_3_5": {"market": 18, "outcome": 13, "specifier": "total=3.5",
                    "market_name": "Total 3.5", "outcome_name": "Under 3.5"},
    "over_1_5": {"market": 18, "outcome": 12, "specifier": "total=1.5",
                   "market_name": "Total 1.5", "outcome_name": "Over 1.5"},
    "12": {"market": 10, "outcome": 10, "specifier": "",
           "market_name": "Double chance", "outcome_name": "12"},
}
LEAGUE_PRODUCTS = {
    "Bundesliga": "VFB", "Euro League": "VFLM", "English League": "VFEL",
}
RGS_BASE = "https://rgspluslive-vs001.akamaized.net/betting-rest"
RGS_KEY = "0GIZYGXQGoqvBBsY"
ODDS_FEED_BASE = (
    "https://vglslive-vs001.akamaized.net/vfl/feeds/?/bet9ja/en/"
    "Europe:Berlin/gismo"
)
ODDS_TOLERANCE = 0.011
SELECTION_PATTERN = re.compile(
    r"^\s*([^:\r\n]+):\s*(.+?)\s+vs\s+(.+?)\s+[—–-]\s*"
    r"(under[_ ]3[._ ]5|over[_ ]1[._ ]5|12)\s+@\s+"
    r"(\d+(?:\.\d+)?)(?:\s+[—–-].*)?$",
    re.IGNORECASE,
)


class PreSubmissionError(RuntimeError):
    """A failure that occurred before Place Bet was clicked and is safe to retry."""


class SubmissionUncertainError(RuntimeError):
    """A ticket may have reached Bet9ja and must never be retried automatically."""


def wager_session_expired(payload: Any) -> bool:
    """Recognize Bet9ja's expired wagering-session rejection contract."""
    values: list[Any] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        else:
            values.append(value)

    visit(payload)
    text = " ".join(str(value).lower() for value in values)
    return any(str(value) == "21004" for value in values) or any(
        marker in text
        for marker in ("session_expired", "session expired", "buy in (reserve) failed")
    )


def load_env(file_path: str | Path = DEFAULT_ENV_FILE) -> None:
    """Load an .env file without overwriting existing process variables."""
    path = Path(file_path)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def clean(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def classify_ticket_response(status: int, payload: Any) -> dict[str, Any]:
    """Classify Bet9ja's ticket API response without relying on UI wording."""
    flattened: dict[str, list[Any]] = {}

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, clean(child_key).lower().replace("_", ""))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif key:
            flattened.setdefault(key, []).append(value)

    visit(payload)
    textual_values = [
        clean(value).lower()
        for values in flattened.values()
        for value in values
        if isinstance(value, str)
    ]
    searchable = " ".join(textual_values)
    rejection_terms = (
        "rejected", "declined", "failed", "failure", "insufficient funds",
        "invalid stake", "odds changed", "selection closed", "event closed",
        "not accepted", "error",
    )
    success_terms = ("accepted", "success", "successful", "placed", "created")
    status_values = [clean(value).lower() for value in flattened.get("status", [])]
    success_values = flattened.get("success", []) + flattened.get("ok", [])
    rgs_status_codes = flattened.get("statuscode", [])
    rgs_result_codes = flattened.get("code", [])
    identifier_keys = (
        "ticketid", "ticketcode", "bookingcode", "couponid", "betslipid",
    )
    ticket_id = next(
        (
            clean(value)
            for key in identifier_keys
            for value in flattened.get(key, [])
            if clean(value)
        ),
        None,
    )
    if ticket_id is None and isinstance(payload, dict):
        ticket = payload.get("ticket")
        if isinstance(ticket, dict) and clean(ticket.get("id")):
            ticket_id = clean(ticket["id"])
    rejected = (
        status >= 400
        or any(value is False for value in success_values)
        or any(str(value) not in {"0", ""} for value in rgs_status_codes)
        or any(str(value) not in {"0", ""} for value in rgs_result_codes)
        or any(term in searchable for term in rejection_terms)
    )
    accepted = (
        200 <= status < 300
        and not rejected
        and (
            ticket_id is not None
            or any(value is True for value in success_values)
            or (
                any(str(value) == "0" for value in rgs_status_codes)
                and any(str(value) == "0" for value in rgs_result_codes)
            )
            or any(any(term in value for term in success_terms) for value in status_values)
        )
    )
    return {
        "state": "rejected" if rejected else "accepted" if accepted else "uncertain",
        "http_status": status,
        "ticket_id": ticket_id,
    }


def sanitize_ticket_request(payload: Any) -> Any:
    """Retain the RGS ticket contract while removing customer identity fields."""
    if not isinstance(payload, (dict, list)):
        return payload
    clone = json.loads(json.dumps(payload))
    tickets = clone if isinstance(clone, list) else [clone]
    for ticket in tickets:
        if not isinstance(ticket, dict):
            continue
        sender = ticket.get("sender")
        if isinstance(sender, dict) and "endCustomer" in sender:
            customer = sender.get("endCustomer")
            sender["endCustomer"] = {
                "present": bool(customer),
                "fields": sorted(customer) if isinstance(customer, dict) else [],
            }
    return clone


def _get_json(url: str, timeout: float = 6) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _live_market_outcome(match_id: int, market: str) -> dict[str, Any]:
    data = _get_json(f"{ODDS_FEED_BASE}/match_odds2/{match_id}")
    try:
        match = data["doc"][0]["data"]
    except (KeyError, IndexError, TypeError) as error:
        raise PreSubmissionError(f"No live odds document for match {match_id}") from error
    if match.get("status", {}).get("name") == "Ended":
        raise PreSubmissionError(f"Match {match_id} has already ended")
    for item in match.get("odds", []):
        field = str(item.get("fieldname", ""))
        if (
            market == "over_1_5" and item.get("_otid") == 5
            and item.get("specialoddsvalue") == "1.5" and field == "o"
        ) or (
            market == "under_3_5" and item.get("_otid") == 5
            and item.get("specialoddsvalue") == "3.5" and field == "u"
        ) or (
            market == "12" and item.get("_otid") == 8
            and item.get("_stid") == 27 and field == "12"
        ):
            if item.get("status") not in (None, "open"):
                raise PreSubmissionError(f"Market {market} is closed for match {match_id}")
            return item
    raise PreSubmissionError(f"Market {market} is unavailable for match {match_id}")


def _live_market_price(match_id: int, market: str) -> float:
    return float(_live_market_outcome(match_id, market)["value"])


def build_direct_ticket(
    details: list[dict[str, Any]], stake: float, customer_id: str
) -> list[dict[str, Any]]:
    """Build Bet9ja's validated RGS 2.2 accumulator request."""
    if not details:
        raise PreSubmissionError("Direct ticket details are missing")
    selections = []
    for index, item in enumerate(details):
        required = ("match_id", "timestamp", "season", "round", "home_id",
                    "away_id", "home", "away", "league", "market", "odds")
        missing = [key for key in required if item.get(key) is None]
        if missing:
            raise PreSubmissionError(
                f"Direct ticket leg is missing fields: {', '.join(missing)}"
            )
        kickoff = int(item["timestamp"])
        if kickoff > time.time() + 90:
            raise PreSubmissionError(
                f"Betting window is not open yet for match {item['match_id']}"
            )
        if kickoff <= time.time() + 5:
            raise PreSubmissionError(
                f"Betting window is closed or too close for match {item['match_id']}"
            )
        live_outcome = _live_market_outcome(int(item["match_id"]), item["market"])
        live_odds = float(live_outcome["value"])
        if abs(live_odds - float(item["odds"])) > ODDS_TOLERANCE:
            raise PreSubmissionError(
                f"Odds changed for match {item['match_id']}: "
                f"expected {float(item['odds']):.2f}, live {live_odds:.2f}"
            )
        mapping = DIRECT_MARKETS[item["market"]]
        # The retail widget sends its native matchId to the launcher, which
        # maps the selected market/outcome to the UOF market identifier.
        event_id = str(int(item["match_id"]))
        base = f"{mapping['market']}/{mapping['outcome']}"
        if mapping["specifier"]:
            base += f"?{mapping['specifier']}"
        selections.append({
            "id": base, "eventId": event_id, "odds": round(live_odds * 10000),
            "convertedOdds": f"{live_odds:.2f}", "convertedOddsType": "dec",
            "productType": LEAGUE_PRODUCTS[item["league"]], "selectionIndex": index,
            "metaData": {
                "lang": "EN-EN", "marketName": mapping["market_name"],
                "outcomeName": mapping["outcome_name"], "outrightType": None,
                "marketId": None, "product": LEAGUE_PRODUCTS[item["league"]],
                "status": "open", "tournament": {
                    "name": "Match", "sequence": str(item.get("round", "")),
                    "event": {"competitors": [
                        {"id": str(item["home_id"]), "name": item["home"],
                         "abbr": item["home"]},
                        {"id": str(item["away_id"]), "name": item["away"],
                         "abbr": item["away"]}],
                        "eventStart": str(kickoff * 1000),
                        "name": f"{item['home']} vs {item['away']}",
                        "sequence": str(item["match_id"])}}
            },
        })
    refs = [{"selectionIndex": index, "banker": False}
            for index in range(len(selections))]
    return [{
        "version": "2.2", "timestampUtc": str(round(time.time() * 1000)),
        "sender": {"bookmakerId": 9478, "channel": "internet", "currency": "ngn",
                   "shopId": None, "terminalId": None, "limitId": 1424,
                   "endCustomer": {"id": customer_id}},
        "selections": selections,
        "bets": [{"selectionRefs": refs, "selectedSystems": [len(selections)],
                  "stake": {"type": "unit", "value": str(round(stake * 10000))}}],
    }]


def comparable(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", clean(value).lower())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def text_contains_team(text: str, team: str) -> bool:
    haystack = comparable(text)
    needle = comparable(team)
    if needle in haystack:
        return True
    words = haystack.split()
    significant = [word for word in needle.split() if len(word) > 2]
    return bool(significant) and all(
        any(difflib.SequenceMatcher(None, wanted, found).ratio() >= 0.78 for found in words)
        for wanted in significant
    )


def parse_prediction_message(message: str) -> dict[str, Any]:
    """Extract supported virtual selections from a Telegram message."""
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Prediction message must be a non-empty string")

    selections: list[dict[str, Any]] = []
    for raw_line in message.splitlines():
        match = SELECTION_PATTERN.match(clean(raw_line))
        if not match:
            continue
        raw_league, home, away, raw_market, raw_odds = match.groups()
        league = SUPPORTED_LEAGUES.get(clean(raw_league).lower())
        if not league:
            raise ValueError(f"Unsupported virtual league: {raw_league}")
        market = raw_market.lower().replace(" ", "_").replace(".", "_")
        if market not in MARKET_LABELS:
            raise ValueError(f"Unsupported market: {raw_market}")
        selections.append(
            {
                "league": league,
                "home": clean(home),
                "away": clean(away),
                "market": market,
                "expected_odds": float(raw_odds),
            }
        )

    if not selections:
        raise ValueError("No supported selections found in the prediction message")
    leagues = {selection["league"] for selection in selections}
    if len(leagues) != 1:
        raise ValueError(
            "Bet9ja virtual accumulator selections must all belong to one league"
        )
    combined = re.search(r"Combined odds:\s*(\d+(?:\.\d+)?)", message, re.I)
    parsed = {
        "selections": selections,
        "advertised_combined_odds": float(combined.group(1)) if combined else None,
    }
    if parsed["advertised_combined_odds"] is not None:
        calculated = 1.0
        for selection in selections:
            calculated *= selection["expected_odds"]
        if abs(round(calculated, 2) - parsed["advertised_combined_odds"]) > 0.011:
            raise ValueError(
                "Advertised combined odds do not match the selection odds: "
                f"advertised={parsed['advertised_combined_odds']:.2f}, "
                f"calculated={calculated:.2f}"
            )
    return parsed


def first_visible(locator: Any) -> Any | None:
    for index in range(locator.count()):
        item = locator.nth(index)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def wait_for_first_visible(locator: Any, timeout_ms: int = 30_000) -> Any | None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        visible = first_visible(locator)
        if visible is not None:
            return visible
        time.sleep(0.5)
    return None


def login(page: Any, username: str | None, password: str | None) -> None:
    if first_visible(
        page.locator('.acc-dd__btn-right, [class*="account-balance" i]')
    ) is not None:
        return
    if not username or not password:
        raise RuntimeError("BET9JA_USERNAME and BET9JA_PASSWORD are required")
    # Bet9ja initially renders both credential fields hidden. The top-bar Log
    # In control must be opened before waiting for a visible input.
    if first_visible(page.locator('input[name="username"], #username')) is None:
        opener = wait_for_first_visible(
            page.locator('.btn-login, [title="Log In"], [title="Login"]'),
            timeout_ms=30_000,
        )
        if opener is None:
            opener = wait_for_first_visible(
                page.get_by_text(re.compile(r"^(log in|login)$", re.I)),
                timeout_ms=5_000,
            )
        if opener is None:
            raise RuntimeError("Bet9ja login opener was not found")
        opener.click()

    username_input = wait_for_first_visible(
        page.locator('input[name="username"], #username')
    )
    password_input = wait_for_first_visible(
        page.locator('input[name="password"], #password')
    )
    if username_input is None or password_input is None:
        raise RuntimeError("Visible Bet9ja login fields were not found")

    username_input.fill(username)
    password_input.fill(password)
    login_button = first_visible(
        page.locator('.btn-primary-l:visible, .login-modal__login-btn:visible')
    )
    if login_button is None:
        exact_buttons = page.get_by_text(re.compile(r"^(log in|login)$", re.I))
        for index in range(exact_buttons.count() - 1, -1, -1):
            candidate = exact_buttons.nth(index)
            if candidate.is_visible() and "btn-login" not in (candidate.get_attribute("class") or ""):
                login_button = candidate
                break
    if login_button is None:
        raise RuntimeError("Bet9ja Log In control was not found")
    login_button.click(no_wait_after=True)
    page.wait_for_timeout(5_000)

    authenticated = first_visible(page.locator('.btn-login, [title="Log In"]')) is None
    if not authenticated:
        page_text = page.locator("body").inner_text()
        auth_error = re.search(r"(?:invalid|incorrect|failed|locked)[^\n]*", page_text, re.I)
        detail = auth_error.group(0) if auth_error else "account header still shows Log In"
        raise RuntimeError(f"Bet9ja login failed: {detail}")


def open_bet9ja(page: Any, attempts: int = 3) -> None:
    """Open Bet9ja and wait until its login shell is interactive."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60_000)
            page.locator("body").wait_for(state="visible", timeout=15_000)
            opener = wait_for_first_visible(
                page.locator(
                    '.btn-login, [title="Log In"], [title="Login"], '
                    '.acc-dd__btn-right, [class*="account-balance" i]'
                ),
                timeout_ms=20_000,
            )
            if opener is None:
                raise RuntimeError("Bet9ja account shell did not become ready")
            return
        except Exception as error:
            last_error = error
            if attempt < attempts:
                page.wait_for_timeout(1_500 * attempt)
                continue
    raise RuntimeError(f"Bet9ja failed to initialize after {attempts} attempts: {last_error}")


def wait_for_virtual_launcher(page: Any, timeout_ms: int = 60_000) -> None:
    """Wait without navigating the React host page during launcher startup."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        frames = page.frames
        for launcher in frames:
            try:
                body = launcher.locator("body").inner_text()
            except Exception:
                body = ""
            desktop_ready = any(
                "vflive-vs001.akamaized.net" in frame.url and "/desktop/" in frame.url
                for frame in frames
            )
            content_ready = "BETSLIP" in body.upper() and (
                "MATCHES" in body.upper() or "MY BETS" in body.upper()
            )
            if content_ready or desktop_ready:
                return
        time.sleep(0.5)
    raise RuntimeError("Authenticated virtual-sports launcher did not become ready")


def reload_virtual_launcher(page: Any) -> None:
    """Restart only the virtual iframe, preserving the authenticated React host."""
    iframe = first_visible(page.locator('iframe[src*="launcherlive-vs001.akamaized.net"]'))
    if iframe is None:
        # The host sometimes detaches the launcher during its login rerender.
        # One host reload is safe; repeated reloads caused React error #200.
        page.reload(wait_until="domcontentloaded", timeout=60_000)
        iframe = wait_for_first_visible(
            page.locator('iframe[src*="launcherlive-vs001.akamaized.net"]'),
            timeout_ms=30_000,
        )
        if iframe is None:
            raise RuntimeError("Virtual launcher iframe was not found after one host reload")
    iframe.evaluate(
        """element => {
            const source = element.src;
            element.src = 'about:blank';
            setTimeout(() => { element.src = source; }, 100);
        }"""
    )


def virtual_frames(page: Any) -> list[Any]:
    return [frame for frame in page.frames if frame != page.main_frame]


def frame_matches_league(frame: Any, league: str) -> bool:
    """Return true only for the bookmaker iframe that owns this league."""
    return LEAGUE_FRAME_PATHS.get(league, "<unsupported>") in str(frame.url).lower()


def activate_league(page: Any, league: str) -> None:
    for frame in virtual_frames(page):
        control = first_visible(frame.get_by_text(league, exact=True))
        if control is None:
            escaped = league.replace('"', '\\"')
            control = first_visible(
                frame.locator(
                    f'[title*="{escaped}" i], [alt*="{escaped}" i], '
                    f'[aria-label*="{escaped}" i]'
                )
            )
        if control is not None:
            try:
                # Animated virtual navigation can remain perpetually "unstable"
                # to Playwright even though the tab is visible and usable. Do
                # not spend a third of the betting window waiting on it.
                control.click(timeout=2_000)
            except Exception:
                # Native DOM click bubbles to the launcher's delegated handler
                # while avoiding Playwright's layout-stability requirement.
                control.evaluate("element => element.click()")
            page.wait_for_timeout(250)
            return
    # Bundesliga is the launcher's default tab and is rendered as a logo with
    # no accessible name in some builds.
    if league == "Bundesliga":
        return
    raise RuntimeError(f"League control not found: {league}")


def find_event_scope(frame: Any, selection: dict[str, Any]) -> Any | None:
    """Return the smallest visible element containing both team names."""
    home_needle = comparable(selection["home"])
    away_needle = comparable(selection["away"])
    # Current Bet9ja builds expose stable match-row classes. Prefer the whole
    # row so the +N expanded-market control remains inside the scope.
    betting_control_selector = (
        'a.sr-oddsButton, [data-name], button, [role="button"], '
        '[class*="odd" i], [class*="selection" i], [class*="market" i]'
    )

    def has_visible_betting_control(scope: Any) -> bool:
        for control in scope.query_selector_all(betting_control_selector):
            try:
                if control.is_visible():
                    return True
            except Exception:
                continue
        return False

    for selector in (".sr-matchRow", ".sr-odds-row"):
        for row in frame.locator(selector).element_handles():
            try:
                text = row.inner_text()
                visible = row.is_visible()
            except Exception:
                continue
            if (
                visible
                and text_contains_team(text, home_needle)
                and text_contains_team(text, away_needle)
                and has_visible_betting_control(row)
            ):
                return row
    handles = frame.locator("body *").element_handles()
    best = None
    best_length = sys.maxsize
    for handle in handles:
        try:
            data = handle.evaluate(
                """element => {
                    const rect = element.getBoundingClientRect();
                    const excluded = element.closest(
                        '[class*="livesettlement" i], [class*="settlement" i], '
                        '[class*="statistic" i], [class*="overlay" i]'
                    );
                    return {
                        text: (element.innerText || '').replace(/\\s+/g, ' ').trim(),
                        visible: rect.width > 0 && rect.height > 0,
                        excluded: Boolean(excluded)
                    };
                }"""
            )
        except Exception:
            continue
        if not data.get("visible") or data.get("excluded"):
            continue
        if (
            text_contains_team(data.get("text", ""), home_needle)
            and text_contains_team(data.get("text", ""), away_needle)
            and has_visible_betting_control(handle)
            and len(data["text"]) < best_length
        ):
            best = handle
            best_length = len(data["text"])
    return best


def find_event_scope_with_market(
    frame: Any, selection: dict[str, Any]
) -> Any | None:
    """Return the visible duplicate match row containing the requested offer.

    The launcher keeps the collapsed row in the DOM when ``+N`` is opened and
    inserts an expanded copy beside it.  Team names alone therefore select the
    wrong row after expansion; the market's stable data-name disambiguates it.
    """
    home_needle = comparable(selection["home"])
    away_needle = comparable(selection["away"])
    market_part = MARKET_DATA_NAME_PARTS[selection["market"]]
    for selector in (".sr-matchRow", ".sr-odds-row"):
        for row in frame.locator(selector).element_handles():
            try:
                text = row.inner_text()
                if not row.is_visible():
                    continue
                if not (
                    text_contains_team(text, home_needle)
                    and text_contains_team(text, away_needle)
                ):
                    continue
                offers = row.query_selector_all(
                    f'a.sr-oddsButton[data-name*="{market_part}"]'
                )
                if any(offer.is_visible() for offer in offers):
                    return row
            except Exception:
                continue
    return None


def click_selection_in_scope(scope: Any, selection: dict[str, Any]) -> str:
    labels = tuple(comparable(label) for label in MARKET_LABELS[selection["market"]])
    expected_odds = f'{selection["expected_odds"]:.2f}'
    data_name_part = MARKET_DATA_NAME_PARTS[selection["market"]]
    mapped = scope.query_selector_all(f'a.sr-oddsButton[data-name*="{data_name_part}"]')
    mismatched_prices: list[float] = []
    for candidate in mapped:
        try:
            if not candidate.is_visible():
                continue
            text = clean(candidate.inner_text())
            prices = re.findall(r"\d+(?:\.\d+)?", text)
            displayed_odds = float(prices[-1]) if prices else None
            if displayed_odds is None:
                continue
            if abs(displayed_odds - selection["expected_odds"]) > ODDS_TOLERANCE:
                mismatched_prices.append(displayed_odds)
                continue
            candidate.click()
            return text
        except RuntimeError:
            raise
        except Exception:
            continue
    candidates = scope.query_selector_all(
        'button, [role="button"], a, [class*="odd" i], '
        '[class*="selection" i], [data-qa], [data-testid]'
    )
    for candidate in candidates:
        try:
            data = candidate.evaluate(
                """element => {
                    const rect = element.getBoundingClientRect();
                    return {
                        text: (element.innerText || element.textContent || '')
                            .replace(/\\s+/g, ' ').trim(),
                        visible: rect.width > 0 && rect.height > 0
                    };
                }"""
            )
        except Exception:
            continue
        if not data.get("visible"):
            continue
        value = comparable(data["text"])
        has_label = any(value == label or label in value for label in labels)
        has_odds = expected_odds in data["text"] or str(selection["expected_odds"]) in data["text"]
        if has_label and has_odds:
            candidate.click()
            return data["text"]
    # Many expanded-market rows put the market label and its clickable price
    # in separate descendants. Find the smallest row containing both, then
    # click the descendant whose complete text is the expected price.
    best_container = None
    best_length = sys.maxsize
    for element in scope.query_selector_all("div, li, tr, section"):
        try:
            text = clean(element.inner_text())
            value = comparable(text)
            if (
                element.is_visible()
                and any(label in value for label in labels)
                and expected_odds in text
                and len(text) < best_length
            ):
                best_container = element
                best_length = len(text)
        except Exception:
            continue
    if best_container is not None:
        for price in best_container.query_selector_all(
            'button, [role="button"], a, [class*="odd" i], [class*="price" i], div, span'
        ):
            try:
                text = clean(price.inner_text())
                if price.is_visible() and text in {expected_odds, str(selection["expected_odds"])}:
                    price.click()
                    return text
            except Exception:
                continue
    mismatch_note = (
        f"; displayed candidates were {sorted(set(mismatched_prices))}"
        if mismatched_prices else ""
    )
    visible_controls = []
    for candidate in scope.query_selector_all(
        'button, [role="button"], a, [data-name], [class*="odd" i], '
        '[class*="selection" i], [class*="market" i]'
    ):
        try:
            if not candidate.is_visible():
                continue
            visible_controls.append(
                {
                    "tag": candidate.evaluate("element => element.tagName"),
                    "class": clean(candidate.get_attribute("class"))[:120],
                    "data_name": clean(candidate.get_attribute("data-name"))[:120],
                    "text": clean(candidate.inner_text())[:120],
                }
            )
            if len(visible_controls) >= 20:
                break
        except Exception:
            continue
    raise RuntimeError(
        f'Market {selection["market"]} was not found for '
        f'{selection["home"]} vs {selection["away"]}; '
        f"the site layout or displayed odds may have changed{mismatch_note}; "
        f"visible controls={json.dumps(visible_controls, ensure_ascii=True)}"
    )


def mark_selection(
    page: Any, selection: dict[str, Any], timeout_seconds: float = 65
) -> dict[str, Any]:
    # The executable market offer is rendered by the Bet9ja launcher frame;
    # the product iframe contains video/results and is not the betting DOM.
    frames = [frame for frame in virtual_frames(page) if "bwg.html" in frame.url]
    fixture_already_visible = any(
        find_event_scope(frame, selection) is not None for frame in frames
    )
    # Do not click the active league tab again between accumulator legs. That
    # destroys and recreates its iframe, costing several seconds and briefly
    # hiding every fixture in the already-open betting window.
    if not fixture_already_visible:
        activate_league(page, selection["league"])
    # A missing fixture normally means the virtual round has rolled over.
    # Return quickly instead of consuming the complete ticket window.
    # A freshly selected league can take several seconds to render its market
    # rows even though the launcher itself is ready.
    # Vflm2 products (notably English League on the OVH Xvfb host) can expose
    # the iframe URL several seconds before React paints its betting table.
    # Allow the first leg to wait for that render; later legs reuse the frame.
    deadline = time.monotonic() + max(1, min(timeout_seconds, 25))
    scope = None
    frame = None
    while time.monotonic() < deadline and scope is None:
        for candidate_frame in frames:
            try:
                candidate_scope = find_event_scope(candidate_frame, selection)
            except Exception:
                continue
            if candidate_scope is not None:
                frame, scope = candidate_frame, candidate_scope
                break
        if scope is None:
            page.wait_for_timeout(250)
    # The launcher occasionally creates the correct product iframe but leaves
    # its document blank under Xvfb. Reload that iframe once, then reacquire it.
    # This is pre-submission recovery and therefore cannot duplicate a wager.
    recovery_frames = frames
    frame_has_betting_table = False
    for candidate_frame in recovery_frames:
        try:
            controls = candidate_frame.locator('a.sr-oddsButton')
            frame_has_betting_table = any(
                controls.nth(index).is_visible()
                for index in range(min(controls.count(), 20))
            )
        except Exception:
            continue
        if frame_has_betting_table:
            break
    if (
        scope is None
        and recovery_frames
        and timeout_seconds > 25
        and not frame_has_betting_table
    ):
        try:
            recovery_frames[0].evaluate("location.reload()")
        except Exception:
            pass
        recovery_deadline = time.monotonic() + max(
            1, min(25, timeout_seconds - 25)
        )
        while time.monotonic() < recovery_deadline and scope is None:
            for candidate_frame in frames:
                try:
                    candidate_scope = find_event_scope(candidate_frame, selection)
                except Exception:
                    continue
                if candidate_scope is not None:
                    frame, scope = candidate_frame, candidate_scope
                    break
            if scope is None:
                page.wait_for_timeout(250)
    if scope is not None and frame is not None:
        try:
            clicked_text = click_selection_in_scope(scope, selection)
        except RuntimeError as initial_error:
            expanded = False
            # This is the launcher's real expansion control. Avoid clicking a
            # generic ancestor whose text happens to reduce to ``+20``.
            for candidate in scope.query_selector_all(
                ".sr-additional-markets a, [class*='additional-markets' i] a"
            ):
                try:
                    text = clean(candidate.inner_text())
                    if re.fullmatch(r"\+\d+", text) and candidate.is_visible():
                        try:
                            candidate.click(timeout=2_000)
                        except Exception:
                            candidate.evaluate("element => element.click()")
                        page.wait_for_timeout(250)
                        expanded = True
                        break
                except Exception:
                    continue
            if not expanded:
                raise initial_error
            # Expansion can replace the fixture node. Resolve the same fixture
            # again instead of searching the whole page and selecting another
            # match's identically named market.
            refreshed_scope = None
            expansion_deadline = time.monotonic() + 5
            while time.monotonic() < expansion_deadline:
                # Normally React mutates this same sr-matchRow by appending an
                # sr-additional-markets-area. Check its retained handle first;
                # fall back to a DOM-wide lookup if the row was replaced.
                try:
                    market_part = MARKET_DATA_NAME_PARTS[selection["market"]]
                    offers = scope.query_selector_all(
                        f'a.sr-oddsButton[data-name*="{market_part}"]'
                    )
                    if any(offer.is_visible() for offer in offers):
                        refreshed_scope = scope
                except Exception:
                    pass
                if refreshed_scope is None:
                    refreshed_scope = find_event_scope_with_market(frame, selection)
                if refreshed_scope is not None:
                    break
                page.wait_for_timeout(100)
            if refreshed_scope is None:
                raise initial_error
            clicked_text = click_selection_in_scope(refreshed_scope, selection)
        page.wait_for_timeout(150)
        return {**selection, "clicked_text": clean(clicked_text)}
    raise RuntimeError(
        f'Fixture not found: {selection["league"]}: '
        f'{selection["home"]} vs {selection["away"]}'
    )


def read_capital(page: Any) -> float:
    """Read the visible cash balance, refusing ambiguous or missing values."""
    selector = (
        '[data-testid*="balance" i], [data-qa*="balance" i], '
        '[class*="cash-balance" i], [class*="account-balance" i], '
        '[class*="user-balance" i], .acc-dd__btn-right'
    )
    values: set[float] = set()
    for frame in [page.main_frame, *virtual_frames(page)]:
        locator = frame.locator(selector)
        for index in range(locator.count()):
            item = locator.nth(index)
            try:
                if not item.is_visible():
                    continue
                text = item.inner_text()
            except Exception:
                continue
            for raw_value in re.findall(r"(?:₦|NGN|N)?\s*([\d,]+(?:\.\d{1,2})?)", text, re.I):
                value = float(raw_value.replace(",", ""))
                if value >= 0:
                    values.add(value)
    if len(values) != 1:
        raise RuntimeError(
            "Could not identify one unambiguous Bet9ja cash balance; "
            f"found {len(values)} candidate values"
        )
    return values.pop()


def calculate_stake(capital: float) -> float:
    """Stake exactly one of ten equal bankroll portions."""
    if capital <= 0:
        raise ValueError("Capital must be a positive number")
    return round(capital / 10, 2)


def fill_stake(page: Any, stake: float) -> None:
    if stake <= 0:
        raise ValueError("Stake must be a positive number")
    selector = (
        'input[placeholder*="stake" i], input[name*="stake" i], '
        'input[data-testid*="stake" i], input[type="number"]'
    )
    for frame in [page.main_frame, *virtual_frames(page)]:
        stake_input = first_visible(frame.locator(selector))
        try:
            frame_text = frame.locator("body").inner_text()
        except Exception:
            frame_text = ""
        is_launcher = "bwg.html" in frame.url or "BETSLIP" in frame_text.upper()
        if stake_input is None and is_launcher:
            # The launcher currently renders the stake field as an unlabelled
            # text input. It is safe to use only after login and only inside
            # the betslip launcher frame, excluding credential fields.
            stake_input = first_visible(
                frame.locator(
                    'input:not([type="hidden"]):not([type="password"]):not([name="username"])'
                )
            )
        if stake_input is not None:
            stake_input.fill(str(stake))
            entered = clean(stake_input.input_value()).replace(",", "")
            try:
                numeric = re.search(r"\d+(?:\.\d+)?", entered)
                if numeric is None:
                    raise ValueError(entered)
                entered_value = float(numeric.group(0))
            except (TypeError, ValueError) as error:
                raise RuntimeError(
                    f"Betslip stake input contains an invalid value: {entered!r}"
                ) from error
            if abs(entered_value - stake) > 0.001:
                raise RuntimeError(
                    f"Betslip stake mismatch: expected {stake}, found {entered_value}"
                )
            return
    raise RuntimeError("Visible betslip stake input was not found")


def find_place_bet_button(page: Any) -> Any | None:
    for frame in [page.main_frame, *virtual_frames(page)]:
        button = first_visible(frame.get_by_text(re.compile(r"^place bet\b", re.I)))
        if button is None:
            button = first_visible(frame.locator("button.sr-bs-footer__button"))
        if button is not None:
            return button
    return None


def betslip_count(page: Any) -> int | None:
    for frame in virtual_frames(page):
        try:
            text = frame.locator("body").inner_text()
        except Exception:
            continue
        match = re.search(r"Betslip\s*\((\d+)\)", text, re.I)
        if match:
            return int(match.group(1))
    return None


def wait_for_betslip_count(page: Any, expected: int, timeout_ms: int = 3_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if betslip_count(page) == expected:
            return True
        page.wait_for_timeout(100)
    return False


def clear_betslip(page: Any) -> None:
    count = betslip_count(page)
    if count in (None, 0):
        return
    for frame in virtual_frames(page):
        control = first_visible(
            frame.get_by_text(re.compile(r"^(clear|clear all|remove all)$", re.I))
        )
        if control is not None:
            control.click()
            page.wait_for_timeout(200)
            break
        remove_buttons = frame.locator(
            'button.sr-bs-remove-btn, button[title="Remove Line" i]'
        )
        while True:
            remove_button = first_visible(remove_buttons)
            if remove_button is None:
                break
            remove_button.click()
            page.wait_for_timeout(100)
    remaining = betslip_count(page)
    if remaining not in (None, 0):
        raise RuntimeError(
            f"Betslip already contains {remaining} selection(s); refusing to mix tickets"
        )


class Bet9jaBrowser:
    """Long-lived, warm Playwright session for time-sensitive ticket entry."""

    def __init__(
        self,
        *,
        headless: bool = False,
        env_file: str | Path = DEFAULT_ENV_FILE,
        profile_dir: str | Path = DEFAULT_PROFILE_DIR,
        ticket_timeout_seconds: float = 75,
        diagnostics_path: str | Path | None = None,
        cdp_url: str | None = None,
    ) -> None:
        self.headless = headless
        self.env_file = Path(env_file)
        self.profile_dir = Path(profile_dir)
        self.ticket_timeout_seconds = ticket_timeout_seconds
        self.cdp_url = cdp_url or os.getenv("BET9JA_CDP_URL", "").strip() or None
        self.diagnostics_path = Path(
            diagnostics_path
            or os.getenv(
                "BET9JA_DIAGNOSTICS_PATH",
                str(Path(profile_dir).parent / "bet9ja_browser_diagnostics.jsonl"),
            )
        )
        self._playwright = None
        self._browser = None
        self.context = None
        self.page = None
        self._owns_browser = True
        self._authenticated_at = 0.0
        self._session_invalid = False

    @property
    def storage_state_path(self) -> Path:
        return self.profile_dir.resolve() / "storage_state.json"

    @staticmethod
    def _safe_url(url: str) -> str:
        """Keep endpoint identity while dropping all potentially sensitive parameters."""
        try:
            parsed = urlsplit(url)
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        except Exception:
            return "<unparseable-url>"

    @classmethod
    def _safe_text(cls, value: Any, limit: int = 500) -> str:
        text = str(value)
        text = re.sub(
            r"https?://[^\s\"']+",
            lambda match: cls._safe_url(match.group(0)),
            text,
        )
        text = re.sub(
            r"(?i)(token|password|authorization|cookie|session|csrf)\s*[:=]\s*[^\s,;]+",
            r"\1=<redacted>",
            text,
        )
        return text[:limit]

    def _diagnostic(self, event: str, **details: Any) -> None:
        self.diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
        if (
            self.diagnostics_path.exists()
            and self.diagnostics_path.stat().st_size > 10 * 1024 * 1024
        ):
            rotated = self.diagnostics_path.with_suffix(
                self.diagnostics_path.suffix + ".1"
            )
            rotated.unlink(missing_ok=True)
            self.diagnostics_path.replace(rotated)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **details,
        }
        with self.diagnostics_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=True) + "\n")

    def _attach_diagnostics(self) -> None:
        def relevant(url: str) -> bool:
            host = urlsplit(url).netloc.lower()
            return "bet9ja" in host or "akamaized.net" in host

        def response_seen(response: Any) -> None:
            # Successful feed/video requests occur several times per second.
            # Keep navigation and HTTP failures without allowing diagnostics to
            # consume disk space and eventually stop the betting service.
            if relevant(response.url) and (
                response.request.resource_type == "document"
                or response.status >= 400
            ):
                self._diagnostic(
                    "response",
                    status=response.status,
                    resource_type=response.request.resource_type,
                    url=self._safe_url(response.url),
                )

        def request_failed(request: Any) -> None:
            if relevant(request.url):
                failure = request.failure
                self._diagnostic(
                    "request_failed",
                    resource_type=request.resource_type,
                    url=self._safe_url(request.url),
                    error=self._safe_text(failure)[:300],
                )

        self.page.on("response", response_seen)
        self.page.on("requestfailed", request_failed)
        self.page.on(
            "framenavigated",
            lambda frame: self._diagnostic(
                "frame_navigated", url=self._safe_url(frame.url)
            ),
        )
        self.page.on(
            "pageerror",
            lambda error: self._diagnostic("page_error", message=self._safe_text(error)),
        )
        self.page.on(
            "console",
            lambda message: self._diagnostic(
                "console",
                level=message.type,
                message=self._safe_text(message.text),
            ) if message.type in {"warning", "error"} else None,
        )

    def start(self) -> "Bet9jaBrowser":
        if self.page is not None and not self.page.is_closed():
            return self
        load_env(self.env_file)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError(
                "Playwright is required. Install it with `pip install playwright` "
                "and run `playwright install chromium`."
            ) from error
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        if self.cdp_url:
            self._owns_browser = False
            self._browser = self._playwright.chromium.connect_over_cdp(self.cdp_url)
            if not self._browser.contexts:
                raise RuntimeError("The attached Chrome browser has no usable context")
            self.context = self._browser.contexts[0]
            self.page = next(
                (page for page in self.context.pages if "virtual.bet9ja.com" in page.url),
                None,
            ) or self.context.new_page()
        else:
            self._owns_browser = True
            self._browser = self._playwright.chromium.launch(headless=self.headless)
            self.context = self._browser.new_context(
                viewport={"width": 1440, "height": 1000},
                storage_state=(
                    str(self.storage_state_path) if self.storage_state_path.exists() else None
                ),
            )
            self.page = self.context.new_page()
        self._attach_diagnostics()
        self._diagnostic("browser_started", headless=self.headless)
        try:
            self._authenticate_and_warm()
        except Exception:
            self._diagnostic("warmup_failed")
            self.close()
            raise
        self._diagnostic("warmup_complete")
        self._authenticated_at = time.monotonic()
        self._session_invalid = False
        return self

    def _authenticate_and_warm(self) -> None:
        if self.cdp_url and first_visible(
            self.page.locator('.acc-dd__btn-right, [class*="account-balance" i]')
        ) is not None:
            self._diagnostic("manual_session_attached")
            wait_for_virtual_launcher(self.page, timeout_ms=15_000)
            self._diagnostic(
                "launcher_ready",
                frames=[self._safe_url(frame.url) for frame in self.page.frames],
            )
            return
        self._diagnostic("shell_open_start")
        open_bet9ja(self.page)
        self._diagnostic("shell_open_complete")
        login(
            self.page,
            os.getenv("BET9JA_USERNAME"),
            os.getenv("BET9JA_PASSWORD"),
        )
        self._diagnostic("login_complete")
        self.context.storage_state(path=str(self.storage_state_path))
        try:
            wait_for_virtual_launcher(self.page)
        except RuntimeError:
            self._diagnostic("launcher_initial_wait_failed", action="reload_iframe")
            reload_virtual_launcher(self.page)
            wait_for_virtual_launcher(self.page, timeout_ms=90_000)
        self._diagnostic(
            "launcher_ready",
            frames=[self._safe_url(frame.url) for frame in self.page.frames],
        )

    def keep_warm(self) -> bool:
        """Fast health check; recover the page only when its launcher disappeared."""
        self.start()
        refresh_seconds = max(
            300.0, float(os.getenv("BET9JA_SESSION_REFRESH_SECONDS", "3600"))
        )
        session_age = time.monotonic() - self._authenticated_at
        if self._session_invalid or session_age >= refresh_seconds:
            self._diagnostic(
                "proactive_session_refresh",
                reason="rejected" if self._session_invalid else "age",
                age_seconds=round(session_age, 1),
            )
            self.restart(force_relogin=True)
            return True
        frames = self.page.frames
        launcher_ready = False
        for frame in frames:
            try:
                frame_text = frame.locator("body").inner_text()
            except Exception:
                frame_text = ""
            if "BETSLIP" in frame_text.upper():
                launcher_ready = True
                break
        # The account balance control can be hidden after the virtual iframe
        # takes focus (notably under Xvfb) even though the authenticated
        # launcher remains fully usable. Do not destroy that healthy session.
        if launcher_ready:
            return True
        self.page.goto("about:blank", wait_until="domcontentloaded")
        self._authenticate_and_warm()
        return True

    def _rgs_post(self, url: str, payload: Any) -> dict[str, Any]:
        launcher = next(
            (frame for frame in self.page.frames if "bwg.html" in frame.url), None
        )
        if launcher is None:
            raise PreSubmissionError("Authenticated Bet9ja launcher is unavailable")
        return launcher.evaluate(
            """async args => {
                const response = await fetch(args.url, {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(args.payload)
                });
                const text = await response.text();
                let body;
                try { body = JSON.parse(text); } catch (_) { body = {raw: text}; }
                return {status: response.status, body};
            }""",
            {"url": url, "payload": payload},
        )

    def _prepare_direct(
        self,
        ticket: dict[str, Any],
        details: list[dict[str, Any]],
        effective_stake: float,
        capital: float | None,
        submit: bool,
        started_at: float,
    ) -> dict[str, Any]:
        if len(details) != len(ticket["selections"]):
            raise PreSubmissionError("Direct ticket leg count does not match the message")
        for parsed, detail in zip(ticket["selections"], details):
            if (
                parsed["league"] != detail.get("league")
                or parsed["market"] != detail.get("market")
                or not text_contains_team(parsed["home"], detail.get("home", ""))
                or not text_contains_team(parsed["away"], detail.get("away", ""))
                or abs(parsed["expected_odds"] - float(detail.get("odds", 0)))
                    > ODDS_TOLERANCE
            ):
                raise PreSubmissionError("Direct ticket details do not match the message")
        launcher = next(
            (frame for frame in self.page.frames if "bwg.html" in frame.url), None
        )
        if launcher is None:
            raise PreSubmissionError("Authenticated Bet9ja launcher is unavailable")
        customer_id = parse_qs(urlsplit(launcher.url).query).get("id", [None])[0]
        if not customer_id:
            raise PreSubmissionError("Bet9ja customer session id is unavailable")
        payload = build_direct_ticket(details, effective_stake, customer_id)
        calculate_url = f"{RGS_BASE}/bookmakers/9478/calculate?key={RGS_KEY}"
        calculation = self._rgs_post(calculate_url, payload)
        calculation_body = calculation.get("body", {})
        calculation_ok = (
            calculation.get("status") == 200
            and calculation_body.get("statusCode") == 0
            and not calculation_body.get("errors")
        )
        self._diagnostic(
            "direct_ticket_calculation",
            ok=calculation_ok,
            http_status=calculation.get("status"),
            status_code=calculation_body.get("statusCode"),
            errors=self._safe_text(calculation_body.get("errors", []), 2000),
            payload=sanitize_ticket_request(payload),
        )
        if not calculation_ok:
            raise PreSubmissionError("Bet9ja rejected direct ticket calculation")
        if not submit:
            return {
                "ok": True, "mode": "direct_calculated_only", "capital": capital,
                "stake": effective_stake,
                "advertised_combined_odds": ticket["advertised_combined_odds"],
                "selections": details, "place_bet_found": False,
                "submission_verified": False,
                "submission_evidence": {"state": "validated", "source": "calculate"},
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
            }
        if os.getenv("BET9JA_ALLOW_REAL_BET") != "true":
            raise SubmissionUncertainError(
                "Submission blocked: set BET9JA_ALLOW_REAL_BET=true as well as requesting submit"
            )
        tickets_url = (
            f"{RGS_BASE}/bookmakers/9478/users/{quote(customer_id, safe='')}/tickets"
            f"?key={RGS_KEY}"
        )
        # This is deliberately issued exactly once. Any transport ambiguity is
        # terminal and must never be retried automatically.
        try:
            result = self._rgs_post(tickets_url, payload)
        except Exception as error:
            raise SubmissionUncertainError(
                "Direct Bet9ja ticket request had an ambiguous transport failure; "
                "it will not be retried automatically"
            ) from error
        evidence = classify_ticket_response(result.get("status", 0), result.get("body"))
        self._diagnostic(
            "direct_ticket_submission_response",
            **evidence,
            payload=self._safe_text(result.get("body"), 4000),
        )
        if evidence["state"] != "accepted":
            raise SubmissionUncertainError(
                "Direct Bet9ja ticket submission was " + evidence["state"]
                + "; it will not be retried automatically"
            )
        return {
            "ok": True, "mode": "submitted", "capital": capital,
            "stake": effective_stake,
            "advertised_combined_odds": ticket["advertised_combined_odds"],
            "selections": details, "place_bet_found": False,
            "submission_verified": True, "submission_evidence": evidence,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }

    def prepare(
        self,
        message: str,
        stake: float | None = None,
        *,
        submit: bool = False,
        screenshot: str | Path | None = None,
        selection_details: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        numeric_stake = float(stake) if stake is not None else None
        if numeric_stake is not None and numeric_stake <= 0:
            raise ValueError("Stake must be a positive number")
        if selection_details is not None:
            try:
                self.keep_warm()
                ticket = parse_prediction_message(message)
                capital = read_capital(self.page) if numeric_stake is None else None
                effective_stake = (
                    calculate_stake(capital) if numeric_stake is None else numeric_stake
                )
                return self._prepare_direct(
                    ticket, selection_details, effective_stake, capital, submit, started_at
                )
            except PreSubmissionError:
                raise
            except SubmissionUncertainError:
                raise
            except Exception as error:
                raise PreSubmissionError(str(error)) from error
        ticket: dict[str, Any] | None = None
        selection: dict[str, Any] | None = None
        marked: list[dict[str, Any]] = []
        deadline = started_at + self.ticket_timeout_seconds
        try:
            self.keep_warm()
            ticket = parse_prediction_message(message)
            clear_betslip(self.page)
            capital = read_capital(self.page) if numeric_stake is None else None
            effective_stake = (
                calculate_stake(capital) if numeric_stake is None else numeric_stake
            )
            for leg_number, selection in enumerate(ticket["selections"], start=1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Ticket preparation exceeded its time budget")
                marked.append(mark_selection(self.page, selection, remaining))
                if not wait_for_betslip_count(self.page, leg_number):
                    raise RuntimeError(
                        "Betslip did not retain the selected leg: "
                        f"expected {leg_number} selection(s), found "
                        f"{betslip_count(self.page)}"
                    )
            actual_legs = betslip_count(self.page)
            expected_legs = len(ticket["selections"])
            if actual_legs != expected_legs:
                raise RuntimeError(
                    f"Betslip integrity failure: expected {expected_legs} legs, "
                    f"but Bet9ja retained {actual_legs}"
                )
            fill_stake(self.page, effective_stake)
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Ticket preparation exceeded its time budget before submission"
                )
            place_bet_button = find_place_bet_button(self.page)
            if place_bet_button is None:
                raise RuntimeError(
                    "Place Bet button was not found after preparing the betslip"
                )
            # Confirm that Playwright considers the control visible, stable,
            # enabled and unobscured without dispatching the irreversible click.
            place_bet_button.click(trial=True, timeout=5_000)
        except Exception as error:
            failure_artifact = self.diagnostics_path.with_name(
                f"bet9ja_pre_submission_failure_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.png"
            )
            try:
                self.page.screenshot(path=str(failure_artifact.resolve()), full_page=True)
            except Exception as screenshot_error:
                self._diagnostic(
                    "selection_screenshot_failed", error=self._safe_text(screenshot_error)
                )
            self._diagnostic(
                "pre_submission_failed",
                selection=selection,
                error=self._safe_text(error, limit=4000),
                screenshot=str(failure_artifact),
                frames=[self._safe_url(frame.url) for frame in self.page.frames],
            )
            try:
                clear_betslip(self.page)
            except Exception as cleanup_error:
                self._diagnostic(
                    "pre_submission_cleanup_failed",
                    error=self._safe_text(cleanup_error),
                )
            raise PreSubmissionError(str(error)) from error
        assert ticket is not None
        if screenshot:
            self.page.screenshot(path=str(Path(screenshot).resolve()), full_page=True)
        submitted = False
        submission_verified = False
        submission_evidence: dict[str, Any] | None = None
        if submit:
            if os.getenv("BET9JA_ALLOW_REAL_BET") != "true":
                raise RuntimeError(
                    "Submission blocked: set BET9JA_ALLOW_REAL_BET=true as well as requesting submit"
                )
            ticket_responses: list[Any] = []

            def capture_ticket_request(request: Any) -> None:
                url = request.url.lower().split("?", 1)[0].rstrip("/")
                if not url.endswith("/tickets") or request.method.upper() == "GET":
                    return
                try:
                    payload = request.post_data_json
                except Exception:
                    payload = {"raw": self._safe_text(request.post_data or "")}
                self._diagnostic(
                    "ticket_submission_request",
                    method=request.method.upper(),
                    url=self._safe_url(request.url),
                    content_type=request.headers.get("content-type"),
                    has_authorization=bool(request.headers.get("authorization")),
                    payload=sanitize_ticket_request(payload),
                )

            def capture_ticket_response(response: Any) -> None:
                url = response.url.lower().split("?", 1)[0].rstrip("/")
                if url.endswith("/tickets") and response.request.method.upper() != "GET":
                    ticket_responses.append(response)

            self.page.on("request", capture_ticket_request)
            self.page.on("response", capture_ticket_response)
            place_bet_button.click()
            submitted = True
            confirmation_deadline = time.monotonic() + 30
            confirmation_terms = (
                "bet placed", "bet accepted", "successfully placed",
                "ticket accepted", "booking code",
            )
            try:
                processed_responses = 0
                while time.monotonic() < confirmation_deadline:
                    while processed_responses < len(ticket_responses):
                        response = ticket_responses[processed_responses]
                        processed_responses += 1
                        try:
                            response.finished()
                            try:
                                payload = response.json()
                            except Exception:
                                payload = {"raw": self._safe_text(response.text())}
                        except Exception as error:
                            payload = {"response_read_error": self._safe_text(error)}
                        submission_evidence = classify_ticket_response(response.status, payload)
                        if wager_session_expired(payload):
                            self._session_invalid = True
                        self._diagnostic(
                            "ticket_submission_response",
                            **submission_evidence,
                            payload=self._safe_text(payload, limit=4000),
                        )
                        if submission_evidence["state"] == "rejected":
                            raise RuntimeError(
                                "Bet9ja rejected the wager"
                                + (
                                    f" (HTTP {submission_evidence['http_status']})"
                                    if submission_evidence.get("http_status") else ""
                                )
                            )
                        if submission_evidence["state"] == "accepted":
                            submission_verified = True
                            break
                    if submission_verified:
                        break
                    visible_text = " ".join(
                        clean(frame.locator("body").inner_text()).lower()
                        for frame in virtual_frames(self.page)
                        if frame.locator("body").count()
                    )
                    if any(term in visible_text for term in confirmation_terms):
                        submission_verified = True
                        submission_evidence = submission_evidence or {
                            "state": "accepted", "source": "ui",
                        }
                        break
                    self.page.wait_for_timeout(250)
            finally:
                self.page.remove_listener("request", capture_ticket_request)
                self.page.remove_listener("response", capture_ticket_response)
            if not submission_verified:
                failure_artifact = self.diagnostics_path.with_name(
                    f"bet9ja_submission_uncertain_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.png"
                )
                try:
                    self.page.screenshot(path=str(failure_artifact.resolve()), full_page=True)
                except Exception as error:
                    self._diagnostic("submission_screenshot_failed", error=self._safe_text(error))
                self._diagnostic(
                    "ticket_submission_uncertain",
                    evidence=submission_evidence,
                    screenshot=str(failure_artifact),
                )
                raise RuntimeError(
                    "Place Bet was clicked, but Bet9ja did not provide an "
                    "acceptance confirmation within 30 seconds; do not retry "
                    "automatically because the wager status is uncertain"
                )
        return {
            "ok": True,
            "mode": "submitted" if submitted else "prepared_only",
            "capital": capital,
            "stake": effective_stake,
            "advertised_combined_odds": ticket["advertised_combined_odds"],
            "selections": marked,
            "place_bet_found": True,
            "submission_verified": submission_verified,
            "submission_evidence": submission_evidence,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }

    def close(self) -> None:
        try:
            if self.context is not None and self._owns_browser:
                self.context.close()
        except Exception:
            pass
        try:
            if self._browser is not None and self._owns_browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        finally:
            self.page = self.context = self._browser = self._playwright = None

    def restart(self, *, force_relogin: bool = False) -> "Bet9jaBrowser":
        """Replace a degraded browser while retaining persisted login state."""
        self._diagnostic(
            "browser_restart_requested", force_relogin=force_relogin
        )
        self.close()
        if force_relogin:
            # The wagering session can expire while the account shell and its
            # cached balance still look authenticated. Removing storage state
            # forces a credential login and obtains a fresh RGS session.
            self.storage_state_path.unlink(missing_ok=True)
        return self.start()

    def __enter__(self) -> "Bet9jaBrowser":
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.close()


def prepare_bet(
    message: str,
    stake: float | None = None,
    *,
    headless: bool = False,
    submit: bool = False,
    env_file: str | Path = DEFAULT_ENV_FILE,
    screenshot: str | Path | None = None,
    profile_dir: str | Path = DEFAULT_PROFILE_DIR,
) -> dict[str, Any]:
    """Mark selections and enter a stake, without submitting by default."""
    browser = Bet9jaBrowser(
        headless=headless,
        env_file=env_file,
        profile_dir=profile_dir,
    )
    try:
        return browser.prepare(
            message,
            stake,
            submit=submit,
            screenshot=screenshot,
        )
    finally:
        browser.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    message_group = parser.add_mutually_exclusive_group(required=True)
    message_group.add_argument("--message", help="Prediction message text")
    message_group.add_argument("--message-file", type=Path, help="Prediction text file")
    parser.add_argument("--stake", type=float, help="Stake amount")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--parse-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    message = (
        args.message_file.read_text(encoding="utf-8")
        if args.message_file
        else args.message
    )
    if args.parse_only:
        result = parse_prediction_message(message)
    else:
        result = prepare_bet(
            message,
            args.stake,
            headless=args.headless,
            submit=args.submit,
            env_file=args.env_file,
            screenshot=args.screenshot,
            profile_dir=args.profile_dir,
        )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
