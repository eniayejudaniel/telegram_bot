"""Leakage-safe pattern study for the 12 (no draw) market."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from analyze_100_season_markets import load_all, wilson
from virtual_prediction_system import FEATURE_NAMES, LogisticModel, RollingFeatures


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "vfb_market_data" / "market_12_deep_analysis.json"


def actual_12(row: dict) -> int:
    return int(row["home_goals"] != row["away_goals"])


def longest_miss(items: list[dict]) -> int:
    current = maximum = 0
    for item in items:
        current = 0 if item["actual"] else current + 1
        maximum = max(maximum, current)
    return maximum


def bucket(items: list[dict]) -> dict:
    hits = sum(x["actual"] for x in items)
    return {"n": len(items), "hits": hits, "hit_rate": hits / len(items),
            "ci95": wilson(hits, len(items)),
            "mean_probability": sum(x["probability"] for x in items) / len(items),
            "calibration_gap": sum(x["probability"] - x["actual"] for x in items) / len(items),
            "longest_miss_streak": longest_miss(items)}


def tickets(items: list[dict], threshold: float, cross_league: bool) -> dict:
    selected = sorted((x for x in items if x["probability"] >= threshold),
                      key=lambda x: x["row"]["timestamp"])
    results = []
    if cross_league:
        used = set()
        for i, first in enumerate(selected):
            if i in used:
                continue
            choice = None
            for j in range(i + 1, min(i + 40, len(selected))):
                if j in used or selected[j]["row"]["league"] == first["row"]["league"]:
                    continue
                if abs(selected[j]["row"]["timestamp"] - first["row"]["timestamp"]) <= 300:
                    choice = j; break
            if choice is not None:
                used.update((i, choice)); results.append(int(first["actual"] and selected[choice]["actual"]))
    else:
        groups = defaultdict(list)
        for item in selected:
            row = item["row"]; groups[(row["league"], row["season"], row["round"])].append(item)
        for group in groups.values():
            group.sort(key=lambda x: x["probability"], reverse=True)
            results.extend(int(group[i]["actual"] and group[i+1]["actual"]) for i in range(0, len(group)-1, 2))
    if not results:
        return {"tickets": 0}
    wins = sum(results); hit_rate = wins / len(results)
    return {"tickets": len(results), "wins": wins, "hit_rate": hit_rate,
            "ci95": wilson(wins, len(results)), "roi_at_1_50": hit_rate*1.50-1,
            "roi_at_1_55": hit_rate*1.55-1}


def main() -> None:
    rows = load_all(); state = RollingFeatures(); samples = []
    for row in rows:
        features = state.vector(row, "12")
        samples.append((features, actual_12(row), row)); state.update(row)
    cut = int(len(samples)*.75)
    model = LogisticModel.fit([(x,y) for x,y,_ in samples[:cut]])
    test = [{"features": x, "actual": y, "row": row, "probability": model.predict(x)}
            for x,y,row in samples[cut:]]
    report = {"design": {"matches": len(rows), "train": cut, "test": len(test),
                          "chronological_holdout": True, "market": "12"},
              "thresholds": {}, "leagues": {}, "time_stability": {},
              "feature_patterns": {}, "same_league_tickets": {},
              "cross_league_tickets": {}, "rules_at_0_82": {}, "draw_scores_at_0_82": {}}
    for integer in range(76, 87):
        threshold = integer/100
        chosen = [x for x in test if x["probability"] >= threshold]
        if chosen:
            report["thresholds"][f"{threshold:.2f}"] = bucket(chosen)
            report["same_league_tickets"][f"{threshold:.2f}"] = tickets(test, threshold, False)
            report["cross_league_tickets"][f"{threshold:.2f}"] = tickets(test, threshold, True)
    for league in sorted({x["row"]["league"] for x in test}):
        sample = [x for x in test if x["row"]["league"] == league]
        report["leagues"][league] = {"all": bucket(sample), "thresholds": {}}
        for threshold in (.80,.81,.82,.83,.84,.85):
            chosen = [x for x in sample if x["probability"] >= threshold]
            if chosen:
                report["leagues"][league]["thresholds"][f"{threshold:.2f}"] = bucket(chosen)
    width = len(test)//4
    for i in range(4):
        block = test[i*width:(i+1)*width if i<3 else len(test)]
        chosen = [x for x in block if x["probability"] >= .82]
        report["time_stability"][f"quarter_{i+1}"] = bucket(chosen) if chosen else {"n": 0}
    for j,name in enumerate(FEATURE_NAMES):
        ordered = sorted(test,key=lambda x:x["features"][j]); size=len(ordered)//5; groups=[]
        for i in range(5):
            group=ordered[i*size:(i+1)*size if i<4 else len(ordered)]
            groups.append({"n":len(group),"min":group[0]["features"][j],"max":group[-1]["features"][j],
                           "hit_rate":sum(x["actual"] for x in group)/len(group)})
        report["feature_patterns"][name]=groups
    ix={name:FEATURE_NAMES.index(name) for name in FEATURE_NAMES}; base=[x for x in test if x["probability"]>=.82]
    rules={
        "probability_only":lambda x:True,
        "both_draw_rates_le_0_25":lambda x:x["features"][ix["home_draw_rate"]]<=.25 and x["features"][ix["away_draw_rate"]]<=.25,
        "h2h_12_ge_0_80":lambda x:x["features"][ix["h2h_rate"]]>=.80,
        "abs_ppg_diff_ge_0_30":lambda x:abs(x["features"][ix["ppg_diff"]])>=.30,
        "abs_elo_diff_ge_0_15":lambda x:abs(x["features"][ix["elo_diff"]])>=.15,
        "low_draw_and_h2h":lambda x:(x["features"][ix["home_draw_rate"]]<=.25 and x["features"][ix["away_draw_rate"]]<=.25 and x["features"][ix["h2h_rate"]]>=.80),
        "h2h_and_ppg_gap":lambda x:(x["features"][ix["h2h_rate"]]>=.80 and abs(x["features"][ix["ppg_diff"]])>=.30),
        "recommended_composite":lambda x:(x["features"][ix["h2h_rate"]]>=.80 and (abs(x["features"][ix["ppg_diff"]])>=.30 or abs(x["features"][ix["elo_diff"]])>=.15)),
    }
    for name,predicate in rules.items():
        chosen=[x for x in base if predicate(x)]
        if chosen:
            report["rules_at_0_82"][name]=bucket(chosen)
            report["rules_at_0_82"][name]["cross_league_tickets"] = tickets(chosen, 0, True)
    draws=[x for x in base if not x["actual"]]
    for score in range(0,5):
        report["draw_scores_at_0_82"][f"{score}-{score}"]=sum(
            x["row"]["home_goals"]==score and x["row"]["away_goals"]==score for x in draws)/len(draws)
    report["draw_scores_at_0_82"]["5+-5+"]=sum(
        x["row"]["home_goals"]>=5 and x["row"]["away_goals"]>=5 for x in draws)/len(draws)
    OUT.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2))


if __name__ == "__main__":
    main()
