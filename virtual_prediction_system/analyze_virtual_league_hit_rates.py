"""Compare raw market hit rates across Bet9ja virtual football leagues."""

from __future__ import annotations

import concurrent.futures
import csv
import itertools
import json
import math
import statistics
from pathlib import Path

import requests


BASE = "https://vglslive-vs001.akamaized.net/vfl/feeds/?/bet9ja/en/Europe:Berlin/gismo"
COMPETITIONS = "https://vflive-vs001.akamaized.net/{product}/mobile/competitions?clientid=1205&lang=en"
LEAGUES = {
    "Bundesliga": {"product": "vfb", "utid": 34616},
    "Euro League": {"product": "vflm", "utid": 14149},
    "English League": {"product": "vfel", "utid": 31867},
}
OUT = Path("vfb_market_data")


def get_json(url: str) -> dict:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def walk_matches(value: object, season: int) -> list[dict]:
    found: list[dict] = []
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


def wilson(successes: int, total: int) -> list[float]:
    z = 1.96
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [centre - radius, centre + radius]


def longest_streak(rows: list[dict], predicate) -> tuple[int, int]:
    wins = losses = max_wins = max_losses = 0
    for row in sorted(rows, key=lambda item: item["timestamp"]):
        if predicate(row):
            wins += 1
            losses = 0
        else:
            losses += 1
            wins = 0
        max_wins = max(max_wins, wins)
        max_losses = max(max_losses, losses)
    return max_wins, max_losses


def collect_league(name: str, config: dict) -> tuple[list[dict], dict]:
    product, utid = config["product"], config["utid"]
    competitions = get_json(COMPETITIONS.format(product=product))
    season = int(competitions["past_competitions"][0]["competition_id"])
    feed = get_json(f"{BASE}/vfl_event_fullfeed/{season}/30")
    reference = walk_matches(feed, season)
    teams = sorted({int(team["uid"]) for match in reference for team in match["teams"].values()})
    if len(teams) != 16:
        raise RuntimeError(f"{name}: expected 16 teams, found {len(teams)}")

    def fetch_pair(pair: tuple[int, int]) -> list[dict]:
        a, b = pair
        data = get_json(f"{BASE}/stats_uniquetournament_team_versusrecent/{utid}/{a}/{b}/100")
        try:
            matches = data["doc"][0]["data"]["matches"]
        except (KeyError, IndexError, TypeError):
            return []
        return [m for m in matches if m.get("_utid") == utid and m.get("result")]

    matches: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for batch in pool.map(fetch_pair, itertools.combinations(teams, 2)):
            matches.extend(batch)
    matches = list({int(match["_id"]): match for match in matches}.values())

    rows = []
    for match in matches:
        result = match["result"]
        hg, ag = int(result["home"]), int(result["away"])
        timestamp = int((match.get("time") or match.get("_dt") or {}).get("uts", 0))
        rows.append(
            {
                "league": name,
                "season": int(match["_seasonid"]),
                "round": int(match.get("round", 0)),
                "match_id": int(match["_id"]),
                "timestamp": timestamp,
                "home_id": int(match["teams"]["home"]["uid"]),
                "home": match["teams"]["home"]["name"],
                "away_id": int(match["teams"]["away"]["uid"]),
                "away": match["teams"]["away"]["name"],
                "home_goals": hg,
                "away_goals": ag,
                "total_goals": hg + ag,
            }
        )

    predicates = {
        "over_1_5": lambda row: row["total_goals"] >= 2,
        "under_3_5": lambda row: row["total_goals"] <= 3,
        "12": lambda row: row["home_goals"] != row["away_goals"],
    }
    result = {
        "matches": len(rows),
        "season_ids": len({row["season"] for row in rows}),
        "season_equivalents": len(rows) / 240,
        "markets": {},
    }
    for market, predicate in predicates.items():
        hits = sum(predicate(row) for row in rows)
        win_streak, loss_streak = longest_streak(rows, predicate)
        by_season = {}
        for season_id in {row["season"] for row in rows}:
            sample = [row for row in rows if row["season"] == season_id]
            by_season[season_id] = sum(predicate(row) for row in sample) / len(sample)
        result["markets"][market] = {
            "hits": hits,
            "rate": hits / len(rows),
            "ci95": wilson(hits, len(rows)),
            "season_rate_sd": statistics.pstdev(by_season.values()),
            "longest_hit_streak": win_streak,
            "longest_miss_streak": loss_streak,
        }
    return rows, result


def main() -> None:
    OUT.mkdir(exist_ok=True)
    all_rows = []
    summary = {}
    for name, config in LEAGUES.items():
        rows, result = collect_league(name, config)
        all_rows.extend(rows)
        summary[name] = result
    with (OUT / "virtual_league_hit_rates.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(all_rows[0]))
        writer.writeheader()
        writer.writerows(sorted(all_rows, key=lambda row: (row["league"], row["timestamp"])))
    (OUT / "virtual_league_hit_rates.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
