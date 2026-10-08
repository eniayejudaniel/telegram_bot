"""Read-only performance audit for a shadow-test database snapshot."""

import collections
import json
import sqlite3
import statistics
import sys


path = sys.argv[1] if len(sys.argv) > 1 else "data/shadow_audit.sqlite3"
db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
db.row_factory = sqlite3.Row
tickets = db.execute("SELECT * FROM combinations WHERE combined_odds>=10 ORDER BY id").fetchall()
settled = [row for row in tickets if row["status"] == "settled"]
pending = [row for row in tickets if row["status"] == "pending"]
won = sum(int(row["hit"] or 0) for row in settled)
profits = [(row["combined_odds"] - 1) if row["hit"] else -1 for row in settled]
print("tickets", len(tickets), "settled", len(settled), "pending", len(pending), "won", won)
if settled:
    legs = [len(json.loads(row["match_ids"])) for row in settled]
    print("hit_rate", won / len(settled), "profit", sum(profits), "roi", statistics.mean(profits))
    print("avg_odds", statistics.mean(row["combined_odds"] for row in settled),
          "avg_legs", statistics.mean(legs), "leg_range", (min(legs), max(legs)))
    streak = maximum = 0
    market = collections.defaultdict(lambda: [0, 0])
    league = collections.defaultdict(lambda: [0, 0])
    failures = collections.Counter()
    for ticket in settled:
        streak = 0 if ticket["hit"] else streak + 1
        maximum = max(maximum, streak)
        misses = 0
        for match_id, market_name in zip(json.loads(ticket["match_ids"]), json.loads(ticket["markets"])):
            prediction = db.execute(
                "SELECT league,hit FROM predictions WHERE match_id=? AND market=?",
                (match_id, market_name),
            ).fetchone()
            if prediction:
                market[market_name][0] += 1; market[market_name][1] += prediction["hit"]
                league[prediction["league"]][0] += 1; league[prediction["league"]][1] += prediction["hit"]
                misses += 1 - prediction["hit"]
        failures[misses] += 1
    print("max_losing_streak", maximum)
    print("markets", {k: {"n": v[0], "hits": v[1], "rate": v[1] / v[0]} for k, v in market.items()})
    print("leagues", {k: {"n": v[0], "hits": v[1], "rate": v[1] / v[0]} for k, v in league.items()})
    print("failed_legs_per_ticket", dict(sorted(failures.items())))
    print("recent")
    for row in settled[-20:]:
        print(row["id"], round(row["combined_odds"], 2), len(json.loads(row["match_ids"])), row["hit"], row["created_at"])
