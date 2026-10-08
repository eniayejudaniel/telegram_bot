"""Leakage-safe predictors for Bet9ja virtual football target markets.

The module uses only Python's standard library. It trains three regularized
logistic models from rolling, pre-match features and can build a combination
while enforcing at most one selection per match.
"""

from __future__ import annotations

import argparse
import collections
import csv
import itertools
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


MARKETS: dict[str, Callable[[dict], bool]] = {
    "over_1_5": lambda row: row["total_goals"] >= 2,
    "under_3_5": lambda row: row["total_goals"] <= 3,
    "12": lambda row: row["home_goals"] != row["away_goals"],
}
FEATURE_NAMES = [
    "league_prior", "home_ppg", "away_ppg", "ppg_diff", "home_gf",
    "home_ga", "away_gf", "away_ga", "expected_goals", "home_draw_rate",
    "away_draw_rate", "home_market_rate", "away_market_rate", "h2h_rate",
    "table_ppg_diff", "elo_diff",
]


def clamp(value: float, low: float = 1e-6, high: float = 1 - 1e-6) -> float:
    return max(low, min(high, value))


def sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1 / (1 + z)
    z = math.exp(value)
    return z / (1 + z)


def mean(values, default: float) -> float:
    values = list(values)
    return sum(values) / len(values) if values else default


@dataclass
class TeamState:
    results: collections.deque = field(default_factory=lambda: collections.deque(maxlen=20))
    goals_for: collections.deque = field(default_factory=lambda: collections.deque(maxlen=20))
    goals_against: collections.deque = field(default_factory=lambda: collections.deque(maxlen=20))
    draws: collections.deque = field(default_factory=lambda: collections.deque(maxlen=20))
    market_hits: dict = field(default_factory=lambda: {m: collections.deque(maxlen=20) for m in MARKETS})
    elo: float = 1500.0


class RollingFeatures:
    def __init__(self) -> None:
        self.teams: dict[tuple[str, int], TeamState] = {}
        self.h2h: dict[tuple[str, int, int, str], collections.deque] = {}
        self.league_hits = {m: collections.defaultdict(lambda: [1.0, 1.0]) for m in MARKETS}
        self.tables: dict[tuple[str, int, int], list[float]] = collections.defaultdict(lambda: [0.0, 0.0])

    def team(self, league: str, team_id: int) -> TeamState:
        return self.teams.setdefault((league, team_id), TeamState())

    def vector(self, row: dict, market: str) -> list[float]:
        league, home_id, away_id = row["league"], row["home_id"], row["away_id"]
        home, away = self.team(league, home_id), self.team(league, away_id)
        wins, total = self.league_hits[market][league]
        prior = wins / total
        home_ppg = mean(home.results, 1.35)
        away_ppg = mean(away.results, 1.35)
        home_gf, home_ga = mean(home.goals_for, 1.3), mean(home.goals_against, 1.3)
        away_gf, away_ga = mean(away.goals_for, 1.3), mean(away.goals_against, 1.3)
        pair = tuple(sorted((home_id, away_id)))
        h2h_rate = mean(self.h2h.get((league, *pair, market), ()), prior)
        ht = self.tables[(league, row["season"], home_id)]
        at = self.tables[(league, row["season"], away_id)]
        table_home = ht[0] / ht[1] if ht[1] else 1.35
        table_away = at[0] / at[1] if at[1] else 1.35
        return [
            prior, home_ppg, away_ppg, home_ppg - away_ppg, home_gf, home_ga,
            away_gf, away_ga, (home_gf + away_ga + away_gf + home_ga) / 2,
            mean(home.draws, 0.24), mean(away.draws, 0.24),
            mean(home.market_hits[market], prior), mean(away.market_hits[market], prior),
            h2h_rate, table_home - table_away, (home.elo - away.elo) / 400,
        ]

    def update(self, row: dict) -> None:
        league, home_id, away_id = row["league"], row["home_id"], row["away_id"]
        hg, ag = row["home_goals"], row["away_goals"]
        home, away = self.team(league, home_id), self.team(league, away_id)
        hp = 3 if hg > ag else 1 if hg == ag else 0
        ap = 3 if ag > hg else 1 if hg == ag else 0
        for state, points, gf, ga in ((home, hp, hg, ag), (away, ap, ag, hg)):
            state.results.append(points); state.goals_for.append(gf); state.goals_against.append(ga)
            state.draws.append(1.0 if hg == ag else 0.0)
        expected = 1 / (1 + 10 ** ((away.elo - home.elo) / 400))
        actual = 1.0 if hg > ag else 0.5 if hg == ag else 0.0
        change = 18 * (actual - expected); home.elo += change; away.elo -= change
        self.tables[(league, row["season"], home_id)][0] += hp
        self.tables[(league, row["season"], home_id)][1] += 1
        self.tables[(league, row["season"], away_id)][0] += ap
        self.tables[(league, row["season"], away_id)][1] += 1
        pair = tuple(sorted((home_id, away_id)))
        for market, test in MARKETS.items():
            hit = float(test(row))
            home.market_hits[market].append(hit); away.market_hits[market].append(hit)
            history = self.h2h.setdefault((league, *pair, market), collections.deque(maxlen=10))
            history.append(hit)
            self.league_hits[market][league][0] += hit
            self.league_hits[market][league][1] += 1


@dataclass
class LogisticModel:
    weights: list[float]
    means: list[float]
    scales: list[float]

    def predict(self, features: list[float]) -> float:
        standardized = [(x - m) / s for x, m, s in zip(features, self.means, self.scales)]
        return sigmoid(self.weights[0] + sum(w * x for w, x in zip(self.weights[1:], standardized)))

    @classmethod
    def fit(cls, samples: list[tuple[list[float], int]], epochs: int = 35) -> "LogisticModel":
        width = len(samples[0][0])
        means = [sum(x[0][j] for x in samples) / len(samples) for j in range(width)]
        scales = []
        for j, centre in enumerate(means):
            variance = sum((x[0][j] - centre) ** 2 for x in samples) / len(samples)
            scales.append(max(math.sqrt(variance), 1e-6))
        normalized = [([(v - means[j]) / scales[j] for j, v in enumerate(x)], y) for x, y in samples]
        weights = [0.0] * (width + 1)
        rng = random.Random(42)
        for epoch in range(epochs):
            rng.shuffle(normalized)
            rate = 0.035 / (1 + epoch * 0.12)
            for x, y in normalized:
                prediction = sigmoid(weights[0] + sum(w * v for w, v in zip(weights[1:], x)))
                error = prediction - y
                weights[0] -= rate * error
                for j, value in enumerate(x, 1):
                    weights[j] -= rate * (error * value + 0.0008 * weights[j])
        return cls(weights, means, scales)


def load_rows(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            for key in ("season", "round", "match_id", "timestamp", "home_id", "away_id", "home_goals", "away_goals", "total_goals"):
                row[key] = int(row[key])
            rows.append(row)
    return sorted(rows, key=lambda row: (row["timestamp"], row["match_id"]))


def feature_dataset(rows: list[dict]) -> dict[str, list[tuple[list[float], int, dict]]]:
    state = RollingFeatures()
    output = {market: [] for market in MARKETS}
    for row in rows:
        for market, test in MARKETS.items():
            output[market].append((state.vector(row, market), int(test(row)), row))
        state.update(row)
    return output


def train_and_backtest(rows: list[dict], split: float = 0.75) -> tuple[dict[str, LogisticModel], dict]:
    datasets = feature_dataset(rows); models = {}; report = {}
    for market, samples in datasets.items():
        cut = int(len(samples) * split)
        model = LogisticModel.fit([(x, y) for x, y, _ in samples[:cut]])
        models[market] = model
        test = [(model.predict(x), y) for x, y, _ in samples[cut:]]
        brier = sum((p - y) ** 2 for p, y in test) / len(test)
        selected = [(p, y) for p, y in test if p >= 0.76]
        report[market] = {
            "train": cut, "test": len(test), "brier": brier,
            "mean_probability": mean((p for p, _ in test), 0),
            "actual_rate": mean((y for _, y in test), 0),
            "selected_at_0_76": len(selected),
            "selected_hit_rate": mean((y for _, y in selected), 0),
        }
    return models, report


def build_combination(
    candidates: list[dict],
    legs: int | None = 3,
    minimum_probability: float = 0.76,
    target_odds: float | None = None,
    min_legs: int = 2,
    max_legs: int = 6,
) -> dict:
    eligible = [c for c in candidates if c["probability"] >= minimum_probability and c.get("odds", 0) > 1]
    eligible.sort(key=lambda c: (c["probability"], c["probability"] * c["odds"]), reverse=True)
    chosen, used_matches = [], set()
    for candidate in eligible:
        if candidate["match_id"] in used_matches:
            continue
        chosen.append(candidate); used_matches.add(candidate["match_id"])
        combined = math.prod(item["odds"] for item in chosen)
        if target_odds is not None and len(chosen) >= min_legs and combined >= target_odds:
            break
        if target_odds is None and legs is not None and len(chosen) == legs:
            break
        if len(chosen) >= max_legs:
            break
    if target_odds is not None:
        combined = math.prod(item["odds"] for item in chosen) if chosen else 0.0
        if len(chosen) < min_legs or combined < target_odds:
            chosen = []
    combined_odds = math.prod(item["odds"] for item in chosen) if chosen else 0.0
    joint_probability = math.prod(item["probability"] for item in chosen) if chosen else 0.0
    return {
        "legs": chosen,
        "combined_odds": combined_odds,
        "estimated_joint_probability": joint_probability,
        "estimated_hit_rate_percent": 100 * joint_probability,
        "warning": "Joint probability assumes different matches are independent; treat it as an approximation.",
    }


def build_optimized_combination(
    candidates: list[dict], minimum_probability: float, target_odds: float,
    min_legs: int = 2, max_legs: int = 3,
    joint_probability_floors: dict[int, float] | None = None,
    maximum_odds: float | None = None,
    require_distinct_leagues: bool = False,
    require_same_league: bool = False,
) -> dict:
    """Return the highest-probability ticket satisfying every hard constraint."""
    floors = joint_probability_floors or {2: 0.60, 3: 0.45}
    eligible = [c for c in candidates if c["probability"] >= minimum_probability and c.get("odds", 0) > 1]
    best = None
    for size in range(min_legs, max_legs + 1):
        for legs in itertools.combinations(eligible, size):
            if len({leg["match_id"] for leg in legs}) != size:
                continue
            if require_same_league and len({leg["league"] for leg in legs}) != 1:
                continue
            if require_distinct_leagues and len({leg["league"] for leg in legs}) != size:
                continue
            odds = math.prod(leg["odds"] for leg in legs)
            joint = math.prod(leg["probability"] for leg in legs)
            if (odds + 1e-9 < target_odds
                    or (maximum_odds is not None and odds - 1e-9 > maximum_odds)
                    or joint + 1e-9 < floors.get(size, 0)):
                continue
            score = (joint, -size, odds)
            if best is None or score > best[0]:
                best = (score, list(legs), odds, joint)
    return {
        "legs": best[1] if best else [],
        "combined_odds": best[2] if best else 0.0,
        "estimated_joint_probability": best[3] if best else 0.0,
        "estimated_hit_rate_percent": 100 * best[3] if best else 0.0,
        "warning": "Joint probability is uncalibrated; shadow results are authoritative.",
    }


def save_models(models: dict[str, LogisticModel], path: Path) -> None:
    path.write_text(json.dumps({k: vars(v) for k, v in models.items()}, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("vfb_market_data/virtual_league_hit_rates.csv"))
    parser.add_argument("--models", type=Path, default=Path("vfb_market_data/prediction_models.json"))
    args = parser.parse_args()
    rows = load_rows(args.data)
    models, report = train_and_backtest(rows)
    save_models(models, args.models)
    report_path = args.models.with_name("prediction_backtest.json")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
