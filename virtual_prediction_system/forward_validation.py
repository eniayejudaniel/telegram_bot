"""Expanding-window forward-style validation over historical matches."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

from virtual_prediction_system import LogisticModel, feature_dataset, load_rows, mean


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "vfb_market_data" / "virtual_league_hit_rates.csv"
OUT = ROOT / "vfb_market_data"
THRESHOLD = 0.76


def longest_miss(sequence: list[int]) -> int:
    current = maximum = 0
    for hit in sequence:
        current = 0 if hit else current + 1
        maximum = max(maximum, current)
    return maximum


def main() -> None:
    rows = load_rows(DATA)
    datasets = feature_dataset(rows)
    initial_train = 12_000
    block = 6_000
    predictions = []

    for market, samples in datasets.items():
        start = initial_train
        fold = 1
        while start < len(samples):
            end = min(start + block, len(samples))
            model = LogisticModel.fit([(x, y) for x, y, _ in samples[:start]], epochs=22)
            for features, actual, row in samples[start:end]:
                predictions.append(
                    {
                        "fold": fold,
                        "market": market,
                        "league": row["league"],
                        "season": row["season"],
                        "match_id": row["match_id"],
                        "timestamp": row["timestamp"],
                        "probability": model.predict(features),
                        "actual": actual,
                    }
                )
            start = end
            fold += 1

    predictions.sort(key=lambda row: (row["timestamp"], row["match_id"], row["market"]))
    with (OUT / "forward_predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(predictions[0]))
        writer.writeheader(); writer.writerows(predictions)

    report = {"design": {"initial_train": initial_train, "block": block, "threshold": THRESHOLD}, "markets": {}}
    for market in sorted({row["market"] for row in predictions}):
        sample = [row for row in predictions if row["market"] == market]
        selected = [row for row in sample if row["probability"] >= THRESHOLD]
        report["markets"][market] = {
            "predictions": len(sample),
            "base_hit_rate": mean((row["actual"] for row in sample), 0),
            "brier": mean(((row["probability"] - row["actual"]) ** 2 for row in sample), 0),
            "selected": len(selected),
            "coverage": len(selected) / len(sample),
            "selected_hit_rate": mean((row["actual"] for row in selected), 0),
            "longest_selected_miss_streak": longest_miss([row["actual"] for row in selected]),
        }

    # Select the highest-probability qualifying market per match, then form
    # consecutive three-leg slips from distinct matches.
    best = {}
    for row in predictions:
        if row["probability"] < THRESHOLD:
            continue
        key = (row["timestamp"], row["match_id"])
        if key not in best or row["probability"] > best[key]["probability"]:
            best[key] = row
    selections = [best[key] for key in sorted(best)]
    slips = [selections[index:index + 3] for index in range(0, len(selections) - 2, 3)]
    report["three_leg_simulation"] = {
        "selections": len(selections),
        "slips": len(slips),
        "hit_rate": mean((int(all(leg["actual"] for leg in slip)) for slip in slips), 0),
        "mean_estimated_probability": mean((math.prod(leg["probability"] for leg in slip) for slip in slips), 0),
    }
    (OUT / "forward_validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
