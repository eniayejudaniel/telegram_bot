"""Extend the score archive beyond 100 complete seasons per selected league."""

from __future__ import annotations

import concurrent.futures
import csv
import time
from pathlib import Path

import requests

from analyze_virtual_league_hit_rates import LEAGUES, walk_matches


ROOT = Path(__file__).resolve().parent
EXISTING = ROOT / "vfb_market_data" / "virtual_league_hit_rates.csv"
ARCHIVE = ROOT / "vfb_market_data" / "older_seasons.csv"
BASE = "https://vglslive-vs001.akamaized.net/vfl/feeds/?/bet9ja/en/Europe:Berlin/gismo"
TARGET_OLDER = 55
WORKERS = 16


def get_json(url: str, attempts: int = 3) -> dict:
    error = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            error = exc
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"request failed: {url}: {error}")


def matches_for(season: int, round_no: int, utid: int) -> list[dict]:
    data = get_json(f"{BASE}/vfl_event_fullfeed/{season}/{round_no}", attempts=2)
    return [m for m in walk_matches(data, season) if int(m.get("_utid", 0)) == utid]


def probe(job: tuple[int, int]) -> int | None:
    season, utid = job
    try:
        return season if len(matches_for(season, 30, utid)) == 8 else None
    except RuntimeError:
        return None


def row_from_match(league: str, match: dict) -> dict:
    result = match["result"]
    hg, ag = int(result["home"]), int(result["away"])
    return {
        "league": league, "season": int(match["_seasonid"]),
        "round": int(match.get("round", 0)), "match_id": int(match["_id"]),
        "timestamp": int((match.get("time") or match.get("_dt") or {}).get("uts", 0)),
        "home_id": int(match["teams"]["home"]["uid"]), "home": match["teams"]["home"]["name"],
        "away_id": int(match["teams"]["away"]["uid"]), "away": match["teams"]["away"]["name"],
        "home_goals": hg, "away_goals": ag, "total_goals": hg + ag,
    }


def write_rows(rows: list[dict]) -> None:
    rows = list({int(row["match_id"]): row for row in rows}.values())
    with ARCHIVE.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(sorted(rows, key=lambda r: (r["league"], int(r["timestamp"]))))


def main() -> None:
    with EXISTING.open(encoding="utf-8") as handle:
        existing = list(csv.DictReader(handle))
    archived = []
    if ARCHIVE.exists():
        with ARCHIVE.open(encoding="utf-8") as handle:
            archived = list(csv.DictReader(handle))
    for league, config in LEAGUES.items():
        have = {int(r["season"]) for r in archived if r["league"] == league}
        need = TARGET_OLDER - len(have)
        if need <= 0:
            continue
        oldest = min(int(r["season"]) for r in existing if r["league"] == league)
        candidates = [(season, config["utid"]) for season in range(oldest - 1900, oldest)]
        found = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for result in pool.map(probe, candidates):
                if result is not None and result not in have:
                    found.append(result)
        seasons = sorted(found, reverse=True)[:need]
        print(f"{league}: discovered {len(seasons)} older seasons", flush=True)
        jobs = [(season, round_no) for season in seasons for round_no in range(1, 31)]
        collected = []
        def fetch(job: tuple[int, int]) -> list[dict]:
            return matches_for(job[0], job[1], config["utid"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for index, batch in enumerate(pool.map(fetch, jobs), 1):
                collected.extend(row_from_match(league, match) for match in batch if match.get("result"))
                if index % 300 == 0:
                    print(f"{league}: fetched {index}/{len(jobs)} rounds", flush=True)
        by_season = {season: 0 for season in seasons}
        for row in collected:
            by_season[int(row["season"])] += 1
        complete = {season for season, count in by_season.items() if count == 240}
        archived.extend(row for row in collected if int(row["season"]) in complete)
        write_rows(archived)
        print(f"{league}: retained {len(complete)} complete seasons", flush=True)


if __name__ == "__main__":
    main()
