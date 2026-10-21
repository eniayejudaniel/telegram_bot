"""Continuously score and settle paper predictions; never places bets."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from modeling import LogisticModel, features
from storage import connect_readonly, load_rounds


LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    predicted_round_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    probability REAL NOT NULL,
    threshold REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    actual_multiplier REAL,
    hit INTEGER,
    model_path TEXT NOT NULL
);
"""


def cycle(source: sqlite3.Connection, ledger: sqlite3.Connection, registry: Path) -> dict:
    model_path = registry / "production.json"
    if not model_path.exists():
        return {"status": "no_promoted_model"}
    model = LogisticModel.load(model_path)
    rows = load_rounds(source)
    if len(rows) < 50:
        return {"status": "insufficient_history"}
    by_id = dict(rows)
    settled = 0
    for predicted_id, threshold in ledger.execute(
        "SELECT predicted_round_id,threshold FROM predictions WHERE status='pending'"
    ).fetchall():
        if predicted_id in by_id:
            value = by_id[predicted_id]
            ledger.execute(
                "UPDATE predictions SET status='settled',actual_multiplier=?,hit=? "
                "WHERE predicted_round_id=?",
                (value, int(value >= threshold), predicted_id),
            )
            settled += 1
    next_id = rows[-1][0] + 1
    probability = model.predict(features([value for _, value in rows]))
    ledger.execute(
        "INSERT OR IGNORE INTO predictions(predicted_round_id,created_at,probability,"
        "threshold,model_path) VALUES(?,?,?,?,?)",
        (next_id, datetime.now(timezone.utc).isoformat(), probability,
         model.target, str(model_path)),
    )
    ledger.commit()
    return {"status": "paper", "next_round": next_id, "probability": probability,
            "settled": settled}


def main(args) -> None:
    source = connect_readonly(args.source)
    ledger_path = Path(args.ledger)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = sqlite3.connect(ledger_path)
    ledger.executescript(LEDGER_SCHEMA)
    cycles = 0
    while True:
        result = cycle(source, ledger, Path(args.registry))
        cycles += 1
        if cycles % args.report_every == 0 or result.get("settled"):
            print(json.dumps(result), flush=True)
        time.sleep(args.interval)


def parser():
    result = argparse.ArgumentParser()
    result.add_argument("--source", required=True)
    result.add_argument("--ledger", required=True)
    result.add_argument("--registry", required=True)
    result.add_argument("--interval", type=float, default=1)
    result.add_argument("--report-every", type=int, default=250)
    return result


if __name__ == "__main__":
    main(parser().parse_args())

