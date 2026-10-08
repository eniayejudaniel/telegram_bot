"""Fit production candidates from the complete deduplicated research archive."""

import json
from pathlib import Path

from analyze_100_season_markets import load_all
from prediction_engine import LogisticModel, feature_dataset


ROOT = Path(__file__).resolve().parent
models = {}
for market, samples in feature_dataset(load_all()).items():
    models[market] = LogisticModel.fit([(features, actual) for features, actual, _ in samples])
(ROOT / "vfb_market_data" / "prediction_models.json").write_text(
    json.dumps({market: vars(model) for market, model in models.items()}, indent=2), encoding="utf-8"
)
