"""Collect and analyse Bet9ja/Sportradar VFB over/under and double-chance data."""

from __future__ import annotations

import collections
import concurrent.futures
import csv
import json
import math
import statistics
import time
from pathlib import Path

import requests


COMPETITIONS_URL = (
    "https://vflive-vs001.akamaized.net/vfb/mobile/competitions"
    "?clientid=1205&lang=en"
)
FEED_URL = (
    "https://vglslive-vs001.akamaized.net/vfl/feeds/?/bet9ja/en/Europe:Berlin/"
    "gismo/vfl_event_fullfeed/{season}/{round_no}"
)
ODDS_URL = (
    "https://vglslive-vs001.akamaized.net/vfl/feeds/?/bet9ja/en/Europe:Berlin/"
    "gismo/match_odds2/{match_id}"
)
H2H_URL = (
    "https://vglslive-vs001.akamaized.net/vfl/feeds/?/bet9ja/en/Europe:Berlin/"
    "gismo/stats_uniquetournament_team_versusrecent/34616/{team_a}/{team_b}/100"
)
DATA_DIR = Path("vfb_market_data")


def get_json(url: str, attempts: int = 3) -> dict:
    for attempt in range(attempts):
        try:
            response = requests.get(url, timeout=25)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, json.JSONDecodeError):
            if attempt + 1 == attempts:
                raise
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError("unreachable")


def extract_matches(document: object, season: int) -> list[dict]:
    matches: list[dict] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            if (
                value.get("_doc") == "match"
                and value.get("_seasonid") == season
                and value.get("result")
                and value.get("status", {}).get("name") == "Ended"
            ):
                matches.append(value)
                return
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document)
    return matches


def probe_season(season: int) -> int | None:
    try:
        data = get_json(FEED_URL.format(season=season, round_no=30), attempts=1)
        return season if extract_matches(data, season) else None
    except Exception:
        return None


def discover_seasons(target: int = 30) -> list[int]:
    published = get_json(COMPETITIONS_URL)["past_competitions"]
    found = {int(item["competition_id"]) for item in published}
    oldest = min(found)
    # Season IDs share a global allocator with other products. Observed VFB gaps are
    # normally 20-40, so scan a bounded historical range rather than guessing gaps.
    lower = oldest - max(900, (target - len(found)) * 45)
    candidates = list(range(lower, oldest))
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for result in pool.map(probe_season, candidates):
            if result is not None:
                found.add(result)
    return sorted(found, reverse=True)[:target]


def fetch_h2h(pair: tuple[int, int]) -> list[dict]:
    team_a, team_b = pair
    data = get_json(H2H_URL.format(team_a=team_a, team_b=team_b))
    try:
        matches = data["doc"][0]["data"]["matches"]
    except (KeyError, IndexError, TypeError):
        return []
    return [
        match
        for match in matches
        if match.get("_doc") == "match"
        and match.get("_utid") == 34616
        and match.get("result")
    ]


def fetch_round(job: tuple[int, int]) -> list[dict]:
    season, round_no = job
    data = get_json(FEED_URL.format(season=season, round_no=round_no))
    return extract_matches(data, season)


def fetch_odds(match: dict) -> dict | None:
    try:
        document = get_json(ODDS_URL.format(match_id=match["_id"]))
        odds = document["doc"][0]["data"].get("odds", [])
    except Exception:
        return None

    markets: dict[str, float] = {}
    for item in odds:
        field = str(item.get("fieldname", "")).lower()
        special = str(item.get("specialoddsvalue", ""))
        try:
            price = float(item["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if item.get("_otid") == 5 and special in {"1.5", "2.5", "3.5"}:
            markets[f"{field}_{special}"] = price
        if item.get("_otid") == 8 and item.get("_stid") == 27:
            markets[field.upper()] = price
    return {"match": match, "markets": markets}


def profit(price: float, won: bool) -> float:
    return price - 1 if won else -1.0


def match_timestamp(match: dict) -> int:
    return int((match.get("_dt") or match.get("time") or {}).get("uts", 0))


def wilson(successes: int, total: int) -> tuple[float, float]:
    if total == 0:
        return (math.nan, math.nan)
    z = 1.96
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return centre - radius, centre + radius


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    published = get_json(COMPETITIONS_URL)
    reference_season = int(published["past_competitions"][0]["competition_id"])
    reference = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        for result in pool.map(fetch_round, [(reference_season, r) for r in range(1, 31)]):
            reference.extend(result)
    team_ids = sorted(
        {
            int(team["uid"])
            for match in reference
            for team in match["teams"].values()
        }
    )
    pairs = [
        (team_ids[i], team_ids[j])
        for i in range(len(team_ids))
        for j in range(i + 1, len(team_ids))
    ]
    matches = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for h2h_matches in pool.map(fetch_h2h, pairs):
            matches.extend(h2h_matches)
    matches = list({match["_id"]: match for match in matches}.values())

    # Results are retained much longer than a consistent set of bookmaker market
    # prices. Price only the newest 13 complete seasons and retain all older scores.
    season_times: dict[int, int] = {}
    for match in matches:
        season = int(match["_seasonid"])
        season_times[season] = max(season_times.get(season, 0), match_timestamp(match))
    seasons = [s for s, _ in sorted(season_times.items(), key=lambda item: item[1], reverse=True)]
    priced_seasons = set(seasons[:13])
    odds_matches = [match for match in matches if match["_seasonid"] in priced_seasons]

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        priced = [item for item in pool.map(fetch_odds, odds_matches) if item is not None]

    rows: list[dict] = []
    for item in priced:
        match = item["match"]
        hg, ag = int(match["result"]["home"]), int(match["result"]["away"])
        rows.append(
            {
                "season": match["_seasonid"],
                "round": match["round"],
                "match_id": match["_id"],
                "timestamp": match_timestamp(match),
                "home_id": match["teams"]["home"]["uid"],
                "home": match["teams"]["home"]["name"],
                "away_id": match["teams"]["away"]["uid"],
                "away": match["teams"]["away"]["name"],
                "home_goals": hg,
                "away_goals": ag,
                "total_goals": hg + ag,
                **item["markets"],
            }
        )
    rows.sort(key=lambda row: row["timestamp"])
    columns = sorted({key for row in rows for key in row})
    with (DATA_DIR / "vfb_markets.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    summary: dict = {
        "seasons": len(seasons),
        "season_ids": seasons,
        "matches": len(matches),
        "priced_matches": len(rows),
        "markets": {},
    }
    totals = [int(match["result"]["home"]) + int(match["result"]["away"]) for match in matches]
    summary["outcome_sample"] = {
        "over_1.5_rate": sum(total > 1.5 for total in totals) / len(totals),
        "over_2.5_rate": sum(total > 2.5 for total in totals) / len(totals),
        "over_3.5_rate": sum(total > 3.5 for total in totals) / len(totals),
        "1X_rate": sum(m["result"]["home"] >= m["result"]["away"] for m in matches) / len(matches),
        "12_rate": sum(m["result"]["home"] != m["result"]["away"] for m in matches) / len(matches),
        "X2_rate": sum(m["result"]["away"] >= m["result"]["home"] for m in matches) / len(matches),
    }
    for line in (1.5, 2.5, 3.5):
        over_key, under_key = f"o_{line}", f"u_{line}"
        sample = [row for row in rows if over_key in row and under_key in row]
        over_wins = sum(row["total_goals"] > line for row in sample)
        interval = wilson(over_wins, len(sample))
        summary["markets"][f"over_under_{line}"] = {
            "n": len(sample),
            "over_rate": over_wins / len(sample),
            "over_95ci": interval,
            "mean_over_price": statistics.mean(row[over_key] for row in sample),
            "mean_under_price": statistics.mean(row[under_key] for row in sample),
            "mean_overround": statistics.mean(
                1 / row[over_key] + 1 / row[under_key] - 1 for row in sample
            ),
            "over_roi": statistics.mean(
                profit(row[over_key], row["total_goals"] > line) for row in sample
            ),
            "under_roi": statistics.mean(
                profit(row[under_key], row["total_goals"] < line) for row in sample
            ),
        }

    dc_sample = [row for row in rows if all(key in row for key in ("1X", "12", "X2"))]
    conditions = {
        "1X": lambda row: row["home_goals"] >= row["away_goals"],
        "12": lambda row: row["home_goals"] != row["away_goals"],
        "X2": lambda row: row["away_goals"] >= row["home_goals"],
    }
    dc = {
        "n": len(dc_sample),
        "mean_overround": statistics.mean(
            sum(1 / row[key] for key in conditions) - 2 for row in dc_sample
        ),
    }
    for key, condition in conditions.items():
        wins = sum(condition(row) for row in dc_sample)
        dc[key] = {
            "hit_rate": wins / len(dc_sample),
            "hit_95ci": wilson(wins, len(dc_sample)),
            "mean_price": statistics.mean(row[key] for row in dc_sample),
            "roi": statistics.mean(profit(row[key], condition(row)) for row in dc_sample),
        }
    summary["markets"]["double_chance"] = dc

    (DATA_DIR / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
