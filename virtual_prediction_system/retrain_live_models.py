"""Retrain and validate models chronologically from captured live matches."""

import json
import os
import sqlite3
from datetime import datetime, timezone

from live_runner import DB_PATH, MODEL_PATH, MODEL_QUALITY_PATH, load_env, observed_rows
from virtual_prediction_system import LogisticModel, feature_dataset


def main() -> None:
    load_env()
    rows = observed_rows(sqlite3.connect(DB_PATH))
    if len(rows) < 2_000:
        raise SystemExit(f"Need at least 2,000 settled live matches; found {len(rows)}")
    thresholds = {
        "over_1_5": float(os.getenv("MIN_PROB_OVER_1_5", "0.85")),
        "under_3_5": float(os.getenv("MIN_PROB_UNDER_3_5", "0.82")),
        "12": float(os.getenv("MIN_PROB_12", "0.82")),
    }
    models, quality = {}, {"generated_at": datetime.now(timezone.utc).isoformat(),
                           "live_matches": len(rows), "markets": {}}
    for market, samples in feature_dataset(rows).items():
        cut = int(len(samples) * 0.75)
        validation_model = LogisticModel.fit([(x, y) for x, y, _ in samples[:cut]])
        test = [(validation_model.predict(x), y) for x, y, _ in samples[cut:]]
        actual = sum(y for _, y in test) / len(test)
        brier = sum((p - y) ** 2 for p, y in test) / len(test)
        baseline_brier = actual * (1 - actual)
        selected = [(p, y) for p, y in test if p >= thresholds[market]]
        selected_probability = sum(p for p, _ in selected) / len(selected) if selected else 0.0
        selected_hit_rate = sum(y for _, y in selected) / len(selected) if selected else 0.0
        enabled = (
            len(selected) >= 200
            and selected_hit_rate >= thresholds[market]
            and abs(selected_probability - selected_hit_rate) <= 0.04
            and brier <= baseline_brier * 1.03
        )
        quality["markets"][market] = {
            "enabled": enabled, "threshold": thresholds[market], "train": cut,
            "test": len(test), "actual_rate": actual,
            "mean_probability": sum(p for p, _ in test) / len(test),
            "brier": brier, "baseline_brier": baseline_brier,
            "selected": len(selected), "selected_mean_probability": selected_probability,
            "selected_hit_rate": selected_hit_rate,
        }
        models[market] = LogisticModel.fit([(x, y) for x, y, _ in samples])

    if not any(item["enabled"] for item in quality["markets"].values()):
        raise SystemExit("No market passed validation; production models were not replaced")
    model_tmp = MODEL_PATH.with_suffix(".json.tmp")
    quality_tmp = MODEL_QUALITY_PATH.with_suffix(".json.tmp")
    model_tmp.write_text(json.dumps({k: vars(v) for k, v in models.items()}, indent=2), encoding="utf-8")
    quality_tmp.write_text(json.dumps(quality, indent=2), encoding="utf-8")
    model_tmp.replace(MODEL_PATH)
    quality_tmp.replace(MODEL_QUALITY_PATH)
    print(json.dumps(quality, indent=2))


if __name__ == "__main__":
    main()
