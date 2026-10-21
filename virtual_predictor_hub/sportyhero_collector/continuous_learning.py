"""Train a candidate and promote only statistically useful paper models."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from modeling import LogisticModel, samples, score
from storage import connect_readonly, load_rounds


def main(args: argparse.Namespace) -> int:
    db = connect_readonly(args.db)
    rows = load_rounds(db)
    db.close()
    dataset = samples(rows)
    if len(dataset) < args.holdout_rounds * 2:
        raise SystemExit("not enough settled rounds for chronological validation")
    registry = Path(args.registry)
    registry.mkdir(parents=True, exist_ok=True)
    last_count_path = registry / "last_training_count.txt"
    prior_count = int(last_count_path.read_text() or 0) if last_count_path.exists() else 0
    if prior_count and len(rows) - prior_count < args.min_new_rounds:
        print(json.dumps({"status": "skipped", "new_rounds": len(rows)-prior_count}))
        return 0
    train, holdout = dataset[:-args.holdout_rounds], dataset[-args.holdout_rounds:]
    candidate = LogisticModel.fit(train)
    quality = score(candidate, holdout)
    calibration_gap = abs(quality["mean_probability"] - quality["actual_rate"])
    reasons = []
    if quality["brier"] >= quality["baseline_brier"] - 0.0005:
        reasons.append("does not improve baseline Brier by 0.0005")
    if calibration_gap > 0.03:
        reasons.append("calibration gap exceeds 3 percentage points")
    production_path = registry / "production.json"
    if production_path.exists():
        production_quality = score(LogisticModel.load(production_path), holdout)
        if quality["brier"] >= production_quality["brier"]:
            reasons.append("does not beat production Brier on the same holdout")
    else:
        production_quality = None
    candidate.save(registry / "candidate.json")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rounds": len(rows), "candidate": quality,
        "production": production_quality, "promoted": not reasons, "reasons": reasons,
        "warning": "Paper research only; crash outcomes are designed to be random.",
    }
    (registry / "quality.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not reasons:
        LogisticModel.fit(dataset).save(production_path)
    last_count_path.write_text(str(len(rows)), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


def parser():
    result = argparse.ArgumentParser()
    result.add_argument("--db", required=True)
    result.add_argument("--registry", required=True)
    result.add_argument("--min-new-rounds", type=int, default=250)
    result.add_argument("--holdout-rounds", type=int, default=2000)
    return result


if __name__ == "__main__":
    raise SystemExit(main(parser().parse_args()))

