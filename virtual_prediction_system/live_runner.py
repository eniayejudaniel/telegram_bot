"""Live shadow-test runner for Bet9ja/Sportradar virtual football."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from virtual_prediction_system import (
    FEATURE_NAMES,
    LogisticModel,
    MARKETS,
    RollingFeatures,
    build_combination,
    build_optimized_combination,
    load_rows,
)


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
MODEL_PATH = ROOT / "vfb_market_data" / "prediction_models.json"
MODEL_QUALITY_PATH = ROOT / "vfb_market_data" / "model_quality.json"
HISTORY_PATH = ROOT / "vfb_market_data" / "virtual_league_hit_rates.csv"
DB_PATH = DATA_DIR / "shadow_test.sqlite3"
BASE = "https://vglslive-vs001.akamaized.net/vfl/feeds/?/bet9ja/en/Europe:Berlin/gismo"
LEAGUES = {
    "Bundesliga": "vfb",
    "Euro League": "vflm",
    "English League": "vfel",
}


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def get_json(url: str, attempts: int = 3) -> dict:
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, timeout=25)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            last_error = error
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Request failed: {url}: {last_error}")


def walk_matches(value: object, season: int) -> list[dict]:
    found = []
    if isinstance(value, dict):
        if value.get("_doc") == "match" and value.get("_seasonid") == season:
            found.append(value)
        else:
            for child in value.values():
                found.extend(walk_matches(child, season))
    elif isinstance(value, list):
        for child in value:
            found.extend(walk_matches(child, season))
    return found


def target_round(competition: dict, server_time: int) -> tuple[int, int] | None:
    start, end = int(competition["start_datetime"]), int(competition["end_datetime"])
    if not start <= server_time < end:
        return None
    elapsed = server_time - start
    if elapsed < 60:
        return int(competition["competition_id"]), 1
    round_index = min(29, max(0, (elapsed - 60) // 220))
    phase = (elapsed - 60) % 220
    if phase >= 45:
        round_index += 1
    if round_index >= 30:
        return None
    return int(competition["competition_id"]), int(round_index + 1)


def next_fixtures(league: str, product: str) -> list[dict]:
    url = f"https://vflive-vs001.akamaized.net/{product}/mobile/competitions?clientid=1205&lang=en"
    data = get_json(url)
    server_time = max(
        [int(item.get("server_datetime", 0)) for item in data.get("past_competitions", [])]
        + [int(time.time())]
    )
    competitions = data.get("next_competitions", []) + data.get("past_competitions", [])
    choice = next((target_round(item, server_time) for item in competitions if target_round(item, server_time)), None)
    if choice is None and data.get("next_competitions"):
        choice = (int(data["next_competitions"][0]["competition_id"]), 1)
    if choice is None:
        return []
    season, round_no = choice
    feed = get_json(f"{BASE}/vfl_event_fullfeed/{season}/{round_no}")
    fixtures = []
    for match in walk_matches(feed, season):
        # The public full-feed may contain a populated result object even while
        # status is "Not started". Never inspect it for prediction/settlement.
        if match.get("status", {}).get("name") == "Ended":
            continue
        fixtures.append(
            {
                "league": league,
                "season": season,
                "round": round_no,
                "match_id": int(match["_id"]),
                "timestamp": int((match.get("_dt") or match.get("time") or {}).get("uts", 0)),
                "home_id": int(match["teams"]["home"]["uid"]),
                "home": match["teams"]["home"]["name"],
                "away_id": int(match["teams"]["away"]["uid"]),
                "away": match["teams"]["away"]["name"],
            }
        )
    return fixtures


def market_odds(match_id: int) -> tuple[dict[str, float], dict | None]:
    data = get_json(f"{BASE}/match_odds2/{match_id}")
    try:
        match = data["doc"][0]["data"]
    except (KeyError, IndexError, TypeError):
        return {}, None
    prices = {}
    for item in match.get("odds", []):
        try:
            price = float(item["value"])
        except (KeyError, TypeError, ValueError):
            continue
        field = str(item.get("fieldname", ""))
        if item.get("_otid") == 5 and item.get("specialoddsvalue") == "1.5" and field == "o":
            prices["over_1_5"] = price
        elif item.get("_otid") == 5 and item.get("specialoddsvalue") == "3.5" and field == "u":
            prices["under_3_5"] = price
        elif item.get("_otid") == 8 and item.get("_stid") == 27 and field == "12":
            prices["12"] = price
    return prices, match


class Telegram:
    def __init__(self, token: str, db: sqlite3.Connection, admin_user_ids: set[int], chat_id: str = "") -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.db = db
        self.admin_user_ids = admin_user_ids
        for admin_id in admin_user_ids:
            self._subscribe(admin_id, admin_id, "configured_admin", is_admin=True)
        if chat_id:
            try:
                legacy_id = int(chat_id)
                self._subscribe(legacy_id, legacy_id, "legacy", is_admin=legacy_id in admin_user_ids)
            except ValueError:
                logging.warning("Ignoring invalid TELEGRAM_CHAT_ID")

    def _send_to(self, chat_id: int, text: str) -> bool:
        try:
            response = requests.post(
                f"{self.base}/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                timeout=20,
            )
            response.raise_for_status()
            return True
        except requests.RequestException:
            logging.exception("Telegram delivery failed for chat_id=%s", chat_id)
            return False

    def _subscribe(self, user_id: int, chat_id: int, username: str, is_admin: bool = False) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO telegram_subscribers(user_id,chat_id,username,is_admin,active,created_at,updated_at) "
            "VALUES(?,?,?,?,1,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "chat_id=excluded.chat_id,username=excluded.username,is_admin=MAX(is_admin,excluded.is_admin),"
            "active=1,updated_at=excluded.updated_at",
            (user_id, chat_id, username, int(is_admin), now, now),
        )
        self.db.commit()

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_user_ids

    def _known_subscriber(self, user_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM telegram_subscribers WHERE user_id=?", (user_id,)
        ).fetchone() is not None

    def _valid_invite(self, supplied: str) -> bool:
        configured = os.getenv("TELEGRAM_INVITE_CODE", "")
        if not configured or not supplied or supplied != configured:
            return False
        digest = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
        try:
            self.db.execute(
                "INSERT INTO telegram_redeemed_invites(code_hash,redeemed_at) VALUES(?,?)",
                (digest, datetime.now(timezone.utc).isoformat()),
            )
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def process_updates(self) -> None:
        row = self.db.execute("SELECT value FROM bot_state WHERE key='telegram_update_offset'").fetchone()
        offset = int(row[0]) if row else 0
        data = get_json(f"{self.base}/getUpdates?timeout=1&offset={offset}")
        for update in data.get("result", []):
            offset = max(offset, int(update["update_id"]) + 1)
            message = update.get("message") or {}
            text = str(message.get("text", "")).strip()
            if not text.startswith("/"):
                continue
            sender = message.get("from", {})
            chat = message.get("chat", {})
            if not sender.get("id") or not chat.get("id"):
                continue
            user_id, chat_id = int(sender["id"]), int(chat["id"])
            username = str(sender.get("username") or sender.get("first_name") or "")
            command, *arguments = text.split(maxsplit=1)
            command = command.split("@", 1)[0].lower()
            if command == "/start":
                invite = arguments[0].strip() if arguments else ""
                if self._is_admin(user_id) or self._known_subscriber(user_id) or self._valid_invite(invite):
                    self._subscribe(user_id, chat_id, username, self._is_admin(user_id))
                    self._send_to(chat_id, "Access approved. You will now receive prediction alerts. Use /stop to unsubscribe.")
                else:
                    self._send_to(chat_id, f"Access denied. Ask the administrator for a valid invitation. Your Telegram ID is {user_id}.")
            elif command == "/stop":
                self.db.execute("UPDATE telegram_subscribers SET active=0,updated_at=? WHERE user_id=?",
                                (datetime.now(timezone.utc).isoformat(), user_id))
                self.db.commit()
                self._send_to(chat_id, "Alerts have been disabled. An authorized /start can enable them again.")
            elif command == "/subscribers" and self._is_admin(user_id):
                rows = self.db.execute(
                    "SELECT user_id,username,is_admin FROM telegram_subscribers WHERE active=1 ORDER BY is_admin DESC,user_id"
                ).fetchall()
                lines = [f"Active subscribers: {len(rows)}"]
                lines.extend(f"{uid} — @{name or 'unknown'}{' (admin)' if admin else ''}" for uid, name, admin in rows)
                self._send_to(chat_id, "\n".join(lines))
            elif command == "/remove" and self._is_admin(user_id):
                try:
                    target = int(arguments[0].strip())
                except (IndexError, ValueError):
                    self._send_to(chat_id, "Usage: /remove <telegram_user_id>")
                else:
                    if target in self.admin_user_ids:
                        self._send_to(chat_id, "Administrators cannot be removed through Telegram.")
                    else:
                        self.db.execute("DELETE FROM telegram_subscribers WHERE user_id=?", (target,))
                        self.db.commit()
                        self._send_to(chat_id, f"Subscriber {target} was removed.")
        self.db.execute(
            "INSERT INTO bot_state(key,value) VALUES('telegram_update_offset',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),)
        )
        self.db.commit()

    def send(self, text: str) -> None:
        recipients = self.db.execute(
            "SELECT chat_id FROM telegram_subscribers WHERE active=1"
        ).fetchall()
        if not recipients:
            logging.warning("No authorized Telegram subscribers")
        for (chat_id,) in recipients:
            self._send_to(int(chat_id), text)


def connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, league TEXT NOT NULL,
            season INTEGER NOT NULL, round INTEGER NOT NULL, match_id INTEGER NOT NULL,
            home TEXT NOT NULL, away TEXT NOT NULL, market TEXT NOT NULL,
            probability REAL NOT NULL, odds REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            home_goals INTEGER, away_goals INTEGER, hit INTEGER,
            strategy TEXT NOT NULL DEFAULT 'legacy',
            UNIQUE(match_id, market)
        );
        CREATE TABLE IF NOT EXISTS combinations (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, match_ids TEXT NOT NULL,
            markets TEXT NOT NULL, combined_odds REAL NOT NULL,
            estimated_probability REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            hit INTEGER, settled_at TEXT, strategy TEXT NOT NULL DEFAULT 'legacy'
        );
        CREATE TABLE IF NOT EXISTS observed_matches (
            match_id INTEGER PRIMARY KEY, league TEXT NOT NULL, season INTEGER NOT NULL,
            round INTEGER NOT NULL, timestamp INTEGER NOT NULL, home_id INTEGER NOT NULL,
            home TEXT NOT NULL, away_id INTEGER NOT NULL, away TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', home_goals INTEGER, away_goals INTEGER
        );
        CREATE TABLE IF NOT EXISTS telegram_subscribers (
            user_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, username TEXT,
            is_admin INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS telegram_redeemed_invites (
            code_hash TEXT PRIMARY KEY, redeemed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(combinations)")}
    for column, definition in (("status", "TEXT NOT NULL DEFAULT 'pending'"),
                               ("hit", "INTEGER"), ("settled_at", "TEXT"),
                               ("strategy", "TEXT NOT NULL DEFAULT 'legacy'")):
        if column not in columns:
            db.execute(f"ALTER TABLE combinations ADD COLUMN {column} {definition}")
    prediction_columns = {row[1] for row in db.execute("PRAGMA table_info(predictions)")}
    if "strategy" not in prediction_columns:
        db.execute("ALTER TABLE predictions ADD COLUMN strategy TEXT NOT NULL DEFAULT 'legacy'")
    db.commit()
    return db


def load_models() -> dict[str, LogisticModel]:
    raw = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
    return {market: LogisticModel(**values) for market, values in raw.items()}


def enabled_markets() -> set[str]:
    configured = {value.strip() for value in os.getenv("ENABLED_MARKETS", "").split(",") if value.strip()}
    if configured:
        return configured & set(MARKETS)
    if not MODEL_QUALITY_PATH.exists():
        return set(MARKETS)
    quality = json.loads(MODEL_QUALITY_PATH.read_text(encoding="utf-8"))
    return {market for market, values in quality.get("markets", {}).items() if values.get("enabled")}


def observed_rows(db: sqlite3.Connection) -> list[dict]:
    keys = ("league", "season", "round", "match_id", "timestamp", "home_id", "home",
            "away_id", "away", "home_goals", "away_goals")
    rows = []
    for values in db.execute(
        "SELECT league,season,round,match_id,timestamp,home_id,home,away_id,away,home_goals,away_goals "
        "FROM observed_matches WHERE status='settled' ORDER BY timestamp,match_id"
    ):
        row = dict(zip(keys, values)); row["total_goals"] = row["home_goals"] + row["away_goals"]
        rows.append(row)
    return rows


def rebuild_state(db: sqlite3.Connection) -> RollingFeatures:
    state = RollingFeatures()
    rows = observed_rows(db)
    if len(rows) < 500:
        rows = load_rows(HISTORY_PATH)
    for row in rows:
        state.update(row)
    return state


def forward_gate(db: sqlite3.Connection, strategy: str) -> dict:
    minimum = int(os.getenv("FORWARD_GATE_MIN_TICKETS", "100"))
    total, wins, profit = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(hit),0),"
        "COALESCE(SUM(CASE WHEN hit=1 THEN combined_odds-1 ELSE -1 END),0) "
        "FROM combinations WHERE strategy=? AND status='settled'", (strategy,)
    ).fetchone()
    roi = profit / total if total else 0.0
    return {"open": total >= minimum and profit > 0, "total": total, "wins": wins,
            "profit": profit, "roi": roi, "minimum": minimum}


def settle(db: sqlite3.Connection, state: RollingFeatures | None = None) -> int:
    settled = 0
    pending = db.execute("SELECT id, match_id, market FROM predictions WHERE status='pending'").fetchall()
    for prediction_id, match_id, market in pending:
        try:
            _, match = market_odds(match_id)
        except RuntimeError:
            continue
        if not match or not match.get("result") or match.get("status", {}).get("name") != "Ended":
            continue
        hg, ag = int(match["result"]["home"]), int(match["result"]["away"])
        row = {"home_goals": hg, "away_goals": ag, "total_goals": hg + ag}
        hit = int(MARKETS[market](row))
        db.execute(
            "UPDATE predictions SET status='settled',home_goals=?,away_goals=?,hit=? WHERE id=?",
            (hg, ag, hit, prediction_id),
        )
        settled += 1
    db.commit()
    observations = db.execute(
        "SELECT match_id,league,season,round,timestamp,home_id,home,away_id,away FROM observed_matches WHERE status='pending'"
    ).fetchall()
    for values in observations:
        match_id, league, season, round_no, timestamp, home_id, home, away_id, away = values
        try:
            _, match = market_odds(match_id)
        except RuntimeError:
            continue
        if not match or not match.get("result") or match.get("status", {}).get("name") != "Ended":
            continue
        hg, ag = int(match["result"]["home"]), int(match["result"]["away"])
        db.execute(
            "UPDATE observed_matches SET status='settled',home_goals=?,away_goals=? WHERE match_id=?",
            (hg, ag, match_id),
        )
        if state is not None:
            state.update({"league": league, "season": season, "round": round_no,
                          "match_id": match_id, "timestamp": timestamp,
                          "home_id": home_id, "home": home, "away_id": away_id,
                          "away": away, "home_goals": hg, "away_goals": ag,
                          "total_goals": hg + ag})
    db.commit()
    return settled


def settle_combinations(db: sqlite3.Connection) -> list[dict]:
    completed = []
    pending = db.execute(
        "SELECT id,match_ids,markets,combined_odds,estimated_probability,strategy FROM combinations WHERE status='pending'"
    ).fetchall()
    for combination_id, match_ids_json, markets_json, odds, probability, strategy in pending:
        match_ids, markets = json.loads(match_ids_json), json.loads(markets_json)
        legs = []
        for match_id, market in zip(match_ids, markets):
            row = db.execute(
                "SELECT home,away,status,home_goals,away_goals,hit,odds FROM predictions WHERE match_id=? AND market=?",
                (match_id, market),
            ).fetchone()
            if row:
                legs.append((match_id, market, *row))
        if len(legs) != len(match_ids) or any(leg[4] != "settled" for leg in legs):
            continue
        hit = int(all(leg[7] for leg in legs))
        settled_at = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE combinations SET status='settled',hit=?,settled_at=? WHERE id=?",
            (hit, settled_at, combination_id),
        )
        completed.append({"id": combination_id, "hit": hit, "odds": odds,
                          "probability": probability, "legs": legs, "strategy": strategy})
    db.commit()
    return completed


def summary(db: sqlite3.Connection) -> str:
    target = float(os.getenv("TARGET_COMBINED_ODDS", "2.00"))
    strategy = os.getenv("STRATEGY_NAME", "conservative_2odds_v1")
    pending = db.execute("SELECT COUNT(*) FROM predictions WHERE status='pending'").fetchone()[0]
    lines = ["Shadow-test performance", f"Pending selections: {pending}", "", "By market:"]
    rows = db.execute(
        "SELECT market,COUNT(*),SUM(hit),SUM(CASE WHEN hit=1 THEN odds-1 ELSE -1 END) "
        "FROM predictions WHERE status='settled' AND strategy=? GROUP BY market", (strategy,)
    ).fetchall()
    for market, total, hits, profit in rows:
        lines.append(f"{market}: {hits}/{total} ({100 * hits / total:.2f}%), flat ROI {100 * profit / total:+.2f}%")
    lines.extend(["", "By league:"])
    for league, total, hits in db.execute(
        "SELECT league,COUNT(*),SUM(hit) FROM predictions WHERE status='settled' AND strategy=? GROUP BY league",
        (strategy,),
    ):
        lines.append(f"{league}: {hits}/{total} ({100 * hits / total:.2f}%)")
    combo = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(hit),0),COALESCE(SUM(CASE WHEN hit=1 THEN combined_odds-1 ELSE -1 END),0) "
        "FROM combinations WHERE status='settled' AND combined_odds>=? AND strategy=?",
        (target, strategy),
    ).fetchone()
    gate = forward_gate(db, strategy)
    lines.extend(["", f"Strategy: {strategy}",
                  f"Forward gate: {'OPEN' if gate['open'] else 'CLOSED'} ({gate['total']}/{gate['minimum']} settled, ROI {gate['roi']:+.2%})",
                  f"Accas ≥{target:.2f}: {combo[1]}/{combo[0]} won"
                  + (f" ({100 * combo[1] / combo[0]:.2f}%), flat ROI {100 * combo[2] / combo[0]:+.2f}%" if combo[0] else "")])
    return "\n".join(lines)


def combination_result_message(combo: dict) -> str:
    heading = "✅ ACCA WON" if combo["hit"] else "❌ ACCA LOST"
    lines = ["SHADOW TEST RESULT — NO STAKE", f"{heading} — #{combo['id']}",
             f"Strategy: {combo['strategy']}", f"Total odds: {combo['odds']:.2f}"]
    for _, market, home, away, _, hg, ag, hit, _ in combo["legs"]:
        icon = "✅" if hit else "❌"
        lines.append(f"{icon} {home} {hg}-{ag} {away} — {market}")
    return "\n".join(lines)


def passes_market_pattern(market: str, features: list[float]) -> bool:
    values = dict(zip(FEATURE_NAMES, features))
    if market == "over_1_5":
        return (values["expected_goals"] >= 2.40
                and values["home_market_rate"] >= 0.75
                and values["away_market_rate"] >= 0.75
                and values["h2h_rate"] >= 0.75)
    if market == "under_3_5":
        return (values["expected_goals"] <= 2.35
                and values["home_market_rate"] >= 0.75
                and values["away_market_rate"] >= 0.75)
    if market == "12":
        return (values["h2h_rate"] >= 0.80
                and (abs(values["ppg_diff"]) >= 0.30 or abs(values["elo_diff"]) >= 0.15))
    return False


def run_once(db: sqlite3.Connection, telegram: Telegram, models: dict, state: RollingFeatures) -> int:
    thresholds = {
        "over_1_5": float(os.getenv("MIN_PROB_OVER_1_5", "0.76")),
        "under_3_5": float(os.getenv("MIN_PROB_UNDER_3_5", "0.76")),
        "12": float(os.getenv("MIN_PROB_12", "0.78")),
    }
    candidates = []
    allowed_markets = enabled_markets()
    minimum_selection_odds = float(os.getenv("MIN_SELECTION_ODDS", "1.00"))
    maximum_selection_odds = float(os.getenv("MAX_SELECTION_ODDS", "99.00"))
    for league, product in LEAGUES.items():
        for fixture in next_fixtures(league, product):
            db.execute(
                "INSERT OR IGNORE INTO observed_matches(match_id,league,season,round,timestamp,home_id,home,away_id,away) VALUES(?,?,?,?,?,?,?,?,?)",
                (fixture["match_id"], fixture["league"], fixture["season"], fixture["round"],
                 fixture["timestamp"], fixture["home_id"], fixture["home"],
                 fixture["away_id"], fixture["away"]),
            )
            prices, _ = market_odds(fixture["match_id"])
            for market, model in models.items():
                if market not in prices or market not in allowed_markets:
                    continue
                if not minimum_selection_odds <= prices[market] <= maximum_selection_odds:
                    continue
                features = state.vector(fixture, market)
                probability = model.predict(features)
                required = thresholds[market]
                if league == "Euro League":
                    required = max(required, float(os.getenv("MIN_PROB_EURO_LEAGUE", "0.85")))
                if probability >= required and passes_market_pattern(market, features):
                    candidates.append({**fixture, "market": market, "probability": probability, "odds": prices[market]})
    db.commit()
    # A ticket must be built exclusively from new selections. Previously the
    # builder could count an existing leg toward the target, then SQLite would
    # ignore that duplicate and the remaining sub-target subset was alerted.
    existing = {
        (int(match_id), market)
        for match_id, market in db.execute("SELECT match_id,market FROM predictions")
    }
    candidates = [
        item for item in candidates
        if (int(item["match_id"]), item["market"]) not in existing
    ]
    target = float(os.getenv("TARGET_COMBINED_ODDS", "2.00"))
    strategy = os.getenv("STRATEGY_NAME", "conservative_2odds_v1")
    min_legs = int(os.getenv("MIN_COMBINATION_LEGS", "2"))
    # Build each market independently so a ticket never mixes Under 3.5 and 12.
    options = []
    for market in sorted(allowed_markets):
        option = build_optimized_combination(
            [item for item in candidates if item["market"] == market],
            minimum_probability=thresholds[market], target_odds=target,
            min_legs=min_legs, max_legs=int(os.getenv("MAX_COMBINATION_LEGS", "2")),
            joint_probability_floors={
                2: float(os.getenv("MIN_JOINT_PROB_2_LEGS", "0.67")),
                3: float(os.getenv("MIN_JOINT_PROB_3_LEGS", "0.45")),
            },
            maximum_odds=float(os.getenv("MAX_COMBINED_ODDS", "1.55")),
            require_distinct_leagues=os.getenv("REQUIRE_DISTINCT_LEAGUES", "true").lower() == "true",
        )
        if option["legs"]:
            options.append(option)
    combination = max(options, key=lambda value: value["estimated_joint_probability"], default={"legs": []})
    inserted = []
    inserted_ids = []
    now = datetime.now(timezone.utc).isoformat()
    for item in combination["legs"]:
        cursor = db.execute(
            "INSERT OR IGNORE INTO predictions(created_at,league,season,round,match_id,home,away,market,probability,odds,strategy) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (now, item["league"], item["season"], item["round"], item["match_id"], item["home"], item["away"], item["market"], item["probability"], item["odds"], strategy),
        )
        if cursor.rowcount:
            inserted.append(item)
            inserted_ids.append(cursor.lastrowid)
    if inserted:
        combined_odds = math_prod(item["odds"] for item in inserted)
        joint = math_prod(item["probability"] for item in inserted)
        # Final invariant: the legs actually written to SQLite—not the proposed
        # candidate list—must independently satisfy the configured ticket rule.
        if (len(inserted) < min_legs or combined_odds + 1e-9 < target
                or combined_odds - 1e-9 > float(os.getenv("MAX_COMBINED_ODDS", "1.55"))):
            db.executemany("DELETE FROM predictions WHERE id=?", [(value,) for value in inserted_ids])
            db.commit()
            logging.error(
                "suppressed invalid ticket: inserted_legs=%s combined_odds=%.4f target=%.2f",
                len(inserted), combined_odds, target,
            )
            return 0
        db.execute(
            "INSERT INTO combinations(created_at,match_ids,markets,combined_odds,estimated_probability,strategy) VALUES(?,?,?,?,?,?)",
            (now, json.dumps([x["match_id"] for x in inserted]), json.dumps([x["market"] for x in inserted]), combined_odds, joint, strategy),
        )
        db.commit()
        gate = forward_gate(db, strategy)
        ticket_market = inserted[0]["market"]
        lines = ["SHADOW TEST — NO STAKE", f"New {ticket_market} validation ACCA ({target:.2f}–{float(os.getenv('MAX_COMBINED_ODDS', '999')):.2f})",
                 f"Strategy: {strategy}"]
        for item in inserted:
            lines.append(f"{item['league']}: {item['home']} vs {item['away']} — {item['market']} @ {item['odds']:.2f} — model {item['probability']:.1%}")
        lines.append(f"Combined odds: {combined_odds:.2f}")
        lines.append(f"Raw model joint estimate: {joint:.1%} (historically overconfident)")
        lines.append(f"Forward gate: {gate['total']}/{gate['minimum']} settled — {'OPEN' if gate['open'] else 'CLOSED'}")
        if gate["open"] or os.getenv("REPORT_SHADOW_TICKETS", "false").lower() == "true":
            telegram.send("\n".join(lines))
        else:
            logging.info("ticket %s stored silently; forward gate %s/%s ROI=%+.2f%%",
                         combined_odds, gate["total"], gate["minimum"], 100 * gate["roi"])
    return len(inserted)


def math_prod(values) -> float:
    result = 1.0
    for value in values:
        result *= value
    return result


def main() -> None:
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing")
    db, models = connect_db(), load_models()
    state = rebuild_state(db)
    admin_user_ids = {
        int(value.strip()) for value in os.getenv("TELEGRAM_ADMIN_USER_IDS", "").split(",")
        if value.strip().isdigit()
    }
    if not admin_user_ids:
        raise SystemExit("TELEGRAM_ADMIN_USER_IDS must contain at least one numeric Telegram user ID")
    telegram = Telegram(token, db, admin_user_ids, os.getenv("TELEGRAM_CHAT_ID", ""))
    poll_seconds = max(10, int(os.getenv("POLL_SECONDS", "15")))
    logging.info("Live shadow runner started; send /start to the Telegram bot if chat ID is blank")
    last_summary_day = None
    while True:
        try:
            telegram.process_updates()
            settled = settle(db, state)
            target = float(os.getenv("TARGET_COMBINED_ODDS", "2.00"))
            strategy = os.getenv("STRATEGY_NAME", "conservative_2odds_v1")
            gate = forward_gate(db, strategy)
            for combo in settle_combinations(db):
                report_shadow = os.getenv("REPORT_SHADOW_TICKETS", "false").lower() == "true"
                if combo["odds"] >= target and combo["strategy"] == strategy and (gate["open"] or report_shadow):
                    telegram.send(combination_result_message(combo))
            created = run_once(db, telegram, models, state)
            logging.info("cycle complete: predictions=%s settled=%s", created, settled)
            day = datetime.now(timezone.utc).date()
            if day != last_summary_day and datetime.now(timezone.utc).hour == 20:
                telegram.send(summary(db)); last_summary_day = day
        except Exception:
            logging.exception("poll cycle failed")
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
