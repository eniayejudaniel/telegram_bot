"""Leakage-safe pattern study for the Under 3.5 market."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

from analyze_100_season_markets import load_all, wilson
from virtual_prediction_system import FEATURE_NAMES, LogisticModel, RollingFeatures


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "vfb_market_data" / "under35_deep_analysis.json"


def under35(row: dict) -> int:
    return int(row["total_goals"] <= 3)


def rate(items: list[dict]) -> float | None:
    return sum(item["actual"] for item in items) / len(items) if items else None


def longest_miss(items: list[dict]) -> int:
    current = maximum = 0
    for item in items:
        current = 0 if item["actual"] else current + 1
        maximum = max(maximum, current)
    return maximum


def bucket_report(items: list[dict]) -> dict:
    hits = sum(item["actual"] for item in items)
    return {
        "n": len(items), "hits": hits, "hit_rate": hits / len(items),
        "ci95": wilson(hits, len(items)),
        "mean_probability": sum(item["probability"] for item in items) / len(items),
        "calibration_gap": sum(item["probability"] - item["actual"] for item in items) / len(items),
        "longest_miss_streak": longest_miss(items),
    }


def pair_tickets(items: list[dict], threshold: float) -> dict:
    groups = defaultdict(list)
    for item in items:
        if item["probability"] >= threshold:
            row = item["row"]
            groups[(row["league"], row["season"], row["round"])].append(item)
    tickets = []
    for group in groups.values():
        group.sort(key=lambda item: item["probability"], reverse=True)
        for index in range(0, len(group) - 1, 2):
            a, b = group[index:index + 2]
            tickets.append({"actual": int(a["actual"] and b["actual"]),
                            "estimated": a["probability"] * b["probability"],
                            "league": a["row"]["league"]})
    if not tickets:
        return {"tickets": 0}
    hits = sum(t["actual"] for t in tickets)
    result = {
        "tickets": len(tickets), "wins": hits, "hit_rate": hits / len(tickets),
        "ci95": wilson(hits, len(tickets)),
        "mean_estimated_probability": sum(t["estimated"] for t in tickets) / len(tickets),
        "roi_at_1_50": hits / len(tickets) * 1.50 - 1,
        "roi_at_1_55": hits / len(tickets) * 1.55 - 1,
        "by_league": {},
    }
    for league in sorted({t["league"] for t in tickets}):
        sample = [t for t in tickets if t["league"] == league]
        league_hits = sum(t["actual"] for t in sample)
        result["by_league"][league] = {
            "tickets": len(sample), "hit_rate": league_hits / len(sample),
            "ci95": wilson(league_hits, len(sample)),
            "roi_at_1_50": league_hits / len(sample) * 1.50 - 1,
            "roi_at_1_55": league_hits / len(sample) * 1.55 - 1,
        }
    return result


def cross_league_tickets(items: list[dict], threshold: float) -> dict:
    pool = sorted((item for item in items if item["probability"] >= threshold),
                  key=lambda item: item["row"]["timestamp"])
    used, tickets = set(), []
    for index, first in enumerate(pool):
        if index in used:
            continue
        choice = None
        for other in range(index + 1, min(index + 40, len(pool))):
            if other in used or pool[other]["row"]["league"] == first["row"]["league"]:
                continue
            if abs(pool[other]["row"]["timestamp"] - first["row"]["timestamp"]) <= 300:
                choice = other; break
        if choice is not None:
            used.update((index, choice)); second = pool[choice]
            tickets.append(int(first["actual"] and second["actual"]))
    if not tickets:
        return {"tickets": 0}
    hits = sum(tickets); hit_rate = hits / len(tickets)
    return {"tickets": len(tickets), "wins": hits, "hit_rate": hit_rate,
            "ci95": wilson(hits, len(tickets)), "roi_at_1_50": hit_rate * 1.50 - 1,
            "roi_at_1_55": hit_rate * 1.55 - 1}


def main() -> None:
    rows = load_all()
    state = RollingFeatures(); samples = []
    for row in rows:
        samples.append((state.vector(row, "under_3_5"), under35(row), row))
        state.update(row)
    cut = int(len(samples) * .75)
    model = LogisticModel.fit([(x, y) for x, y, _ in samples[:cut]])
    test = [{"features": x, "actual": y, "row": row, "probability": model.predict(x)}
            for x, y, row in samples[cut:]]
    report = {
        "design": {"matches": len(rows), "train": cut, "test": len(test),
                   "chronological_holdout": True, "market": "under_3_5"},
        "score_distribution": {}, "thresholds": {}, "leagues": {},
        "time_stability": {}, "feature_patterns": {}, "two_leg_simulations": {},
        "cross_league_two_leg": {}, "rules_at_0_82": {}, "miss_goal_distribution_at_0_82": {},
    }
    for goals in range(0, 8):
        count = sum((r["total_goals"] == goals if goals < 7 else r["total_goals"] >= 7) for r in rows)
        report["score_distribution"][str(goals) if goals < 7 else "7+"] = count / len(rows)
    for integer in range(76, 87):
        threshold = integer / 100
        chosen = [item for item in test if item["probability"] >= threshold]
        if chosen:
            report["thresholds"][f"{threshold:.2f}"] = bucket_report(chosen)
            report["two_leg_simulations"][f"{threshold:.2f}"] = pair_tickets(test, threshold)
            report["cross_league_two_leg"][f"{threshold:.2f}"] = cross_league_tickets(test, threshold)
    for league in sorted({item["row"]["league"] for item in test}):
        league_items = [item for item in test if item["row"]["league"] == league]
        report["leagues"][league] = {"all": bucket_report(league_items), "thresholds": {}}
        for threshold in (.80, .81, .82, .83, .84, .85):
            chosen = [item for item in league_items if item["probability"] >= threshold]
            if chosen:
                report["leagues"][league]["thresholds"][f"{threshold:.2f}"] = bucket_report(chosen)
    width = len(test) // 4
    for index in range(4):
        block = test[index * width:(index + 1) * width if index < 3 else len(test)]
        chosen = [item for item in block if item["probability"] >= .82]
        report["time_stability"][f"quarter_{index + 1}"] = bucket_report(chosen) if chosen else {"n": 0}
    for feature_index, name in enumerate(FEATURE_NAMES):
        ordered = sorted(test, key=lambda item: item["features"][feature_index])
        size = len(ordered) // 5
        buckets = []
        for index in range(5):
            bucket = ordered[index * size:(index + 1) * size if index < 4 else len(ordered)]
            buckets.append({
                "n": len(bucket), "min": bucket[0]["features"][feature_index],
                "max": bucket[-1]["features"][feature_index], "hit_rate": rate(bucket),
                "mean_model_probability": sum(x["probability"] for x in bucket) / len(bucket),
            })
        report["feature_patterns"][name] = buckets
    indices = {name: FEATURE_NAMES.index(name) for name in FEATURE_NAMES}
    rules = {
        "probability_only": lambda item: True,
        "expected_goals_le_2_35": lambda item: item["features"][indices["expected_goals"]] <= 2.35,
        "home_gf_le_0_90": lambda item: item["features"][indices["home_gf"]] <= .90,
        "h2h_ge_0_80": lambda item: item["features"][indices["h2h_rate"]] >= .80,
        "both_team_rates_ge_0_75": lambda item: (
            item["features"][indices["home_market_rate"]] >= .75
            and item["features"][indices["away_market_rate"]] >= .75),
        "low_xg_and_h2h": lambda item: (
            item["features"][indices["expected_goals"]] <= 2.35
            and item["features"][indices["h2h_rate"]] >= .80),
        "low_xg_and_home_gf": lambda item: (
            item["features"][indices["expected_goals"]] <= 2.35
            and item["features"][indices["home_gf"]] <= .90),
        "low_xg_and_team_rates": lambda item: (
            item["features"][indices["expected_goals"]] <= 2.35
            and item["features"][indices["home_market_rate"]] >= .75
            and item["features"][indices["away_market_rate"]] >= .75),
        "recommended_composite": lambda item: (
            item["features"][indices["expected_goals"]] <= 2.35
            and (item["features"][indices["home_gf"]] <= .90
                 or (item["features"][indices["home_market_rate"]] >= .75
                     and item["features"][indices["away_market_rate"]] >= .75))),
    }
    base = [item for item in test if item["probability"] >= .82]
    for name, predicate in rules.items():
        chosen = [item for item in base if predicate(item)]
        if chosen:
            report["rules_at_0_82"][name] = bucket_report(chosen)
            report["rules_at_0_82"][name]["cross_league_tickets"] = cross_league_tickets(chosen, 0)
    misses = [item for item in base if not item["actual"]]
    for goals in range(4, 8):
        report["miss_goal_distribution_at_0_82"][str(goals) if goals < 7 else "7+"] = sum(
            item["row"]["total_goals"] == goals if goals < 7 else item["row"]["total_goals"] >= 7
            for item in misses
        ) / len(misses)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
