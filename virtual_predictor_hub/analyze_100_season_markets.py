"""Market-by-market chronological analysis over every unique captured match."""

import csv
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

from prediction_engine import LogisticModel, MARKETS, feature_dataset


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "vfb_market_data"


def load_all() -> list[dict]:
    rows = {}
    for name in ("older_seasons.csv", "virtual_league_hit_rates.csv"):
        path = OUT / name
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                for key in ("season","round","match_id","timestamp","home_id","away_id","home_goals","away_goals","total_goals"):
                    raw[key] = int(raw[key])
                rows[raw["match_id"]] = raw
    db = sqlite3.connect(ROOT / "data" / "shadow_analysis.sqlite3")
    keys = ("league","season","round","match_id","timestamp","home_id","home","away_id","away","home_goals","away_goals")
    for values in db.execute(
        "SELECT league,season,round,match_id,timestamp,home_id,home,away_id,away,home_goals,away_goals "
        "FROM observed_matches WHERE status='settled'"
    ):
        row = dict(zip(keys, values)); row["total_goals"] = row["home_goals"] + row["away_goals"]
        rows[row["match_id"]] = row
    return sorted(rows.values(), key=lambda r: (r["timestamp"], r["match_id"]))


def wilson(hits: int, total: int) -> list[float]:
    z = 1.96; p = hits / total; d = 1 + z*z/total
    c = (p + z*z/(2*total))/d
    r = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total))/d
    return [c-r, c+r]


def main() -> None:
    rows = load_all()
    coverage = {}
    for league in sorted({r["league"] for r in rows}):
        sample = [r for r in rows if r["league"] == league]
        coverage[league] = {"matches": len(sample), "season_equivalents": len(sample)/240,
                            "season_ids": len({r["season"] for r in sample})}
    report = {"coverage": coverage, "matches": len(rows), "markets": {}}
    for market, samples in feature_dataset(rows).items():
        cut = int(len(samples) * .75)
        model = LogisticModel.fit([(x,y) for x,y,_ in samples[:cut]])
        test = [(model.predict(x), y, row) for x,y,row in samples[cut:]]
        market_report = {"train": cut, "test": len(test), "thresholds": {}, "leagues": {}}
        for integer in range(70, 91):
            threshold = integer / 100
            selected = [(p,y,row) for p,y,row in test if p >= threshold]
            if not selected:
                continue
            hits = sum(y for _,y,_ in selected)
            market_report["thresholds"][f"{threshold:.2f}"] = {
                "selected": len(selected), "coverage": len(selected)/len(test),
                "hit_rate": hits/len(selected), "ci95": wilson(hits,len(selected)),
                "mean_probability": sum(p for p,_,_ in selected)/len(selected),
                "calibration_gap": sum(p-y for p,y,_ in selected)/len(selected),
            }
        for league in sorted(coverage):
            league_test = [(p,y,row) for p,y,row in test if row["league"] == league]
            hits = sum(y for _,y,_ in league_test)
            market_report["leagues"][league] = {
                "test": len(league_test), "base_hit_rate": hits/len(league_test),
                "ci95": wilson(hits,len(league_test)),
                "brier": sum((p-y)**2 for p,y,_ in league_test)/len(league_test),
                "thresholds": {},
            }
            for integer in range(78, 86):
                threshold = integer / 100
                selected = [(p,y) for p,y,_ in league_test if p >= threshold]
                if selected:
                    market_report["leagues"][league]["thresholds"][f"{threshold:.2f}"] = {
                        "selected": len(selected),
                        "hit_rate": sum(y for _,y in selected)/len(selected),
                        "mean_probability": sum(p for p,_ in selected)/len(selected),
                    }
        report["markets"][market] = market_report
        print(market, json.dumps(market_report["thresholds"], indent=2), flush=True)
    (OUT / "individual_market_analysis.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"coverage": coverage}, indent=2))


if __name__ == "__main__":
    main()
