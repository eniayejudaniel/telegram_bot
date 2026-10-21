"""Paper-only sequence ledger retained for recovered service compatibility."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from modeling import LogisticModel, features
from storage import connect_readonly, load_rounds


SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_signals (
    round_id INTEGER PRIMARY KEY,
    probability REAL NOT NULL,
    threshold REAL NOT NULL,
    paper_stake REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    multiplier REAL,
    pnl REAL
);
CREATE TABLE IF NOT EXISTS bankroll (
    id INTEGER PRIMARY KEY CHECK(id=1), amount REAL NOT NULL
);
"""


def main(args) -> None:
    source = connect_readonly(args.source)
    path = Path(args.ledger); path.parent.mkdir(parents=True, exist_ok=True)
    ledger = sqlite3.connect(path); ledger.executescript(SCHEMA)
    ledger.execute("INSERT OR IGNORE INTO bankroll(id,amount) VALUES(1,?)",
                   (args.initial_paper_bankroll,)); ledger.commit()
    registry = Path(args.source).parent / "ml" / "production.json"
    while True:
        rows = load_rounds(source); actual = dict(rows)
        for round_id, threshold, stake in ledger.execute(
            "SELECT round_id,threshold,paper_stake FROM paper_signals WHERE status='pending'"
        ).fetchall():
            if round_id in actual:
                value = actual[round_id]
                pnl = stake * (threshold - 1) if value >= threshold else -stake
                ledger.execute("UPDATE paper_signals SET status='settled',multiplier=?,pnl=? WHERE round_id=?",
                               (value, pnl, round_id))
                ledger.execute("UPDATE bankroll SET amount=amount+? WHERE id=1", (pnl,))
        if registry.exists() and len(rows) >= 50:
            model = LogisticModel.load(registry)
            probability = model.predict(features([value for _, value in rows]))
            break_even = 1 / model.target
            if probability >= break_even + 0.03:
                bankroll = ledger.execute("SELECT amount FROM bankroll WHERE id=1").fetchone()[0]
                stake = min(bankroll * 0.01, 10.0)
                ledger.execute(
                    "INSERT OR IGNORE INTO paper_signals(round_id,probability,threshold,paper_stake) VALUES(?,?,?,?)",
                    (rows[-1][0] + 1, probability, model.target, stake),
                )
        ledger.commit()
        print(json.dumps({"status": "paper_only", "rounds": len(rows)}), flush=True)
        time.sleep(args.interval)


def parser():
    result = argparse.ArgumentParser()
    result.add_argument("--source", required=True)
    result.add_argument("--ledger", required=True)
    result.add_argument("--initial-paper-bankroll", type=float, default=2000)
    result.add_argument("--interval", type=float, default=.5)
    return result


if __name__ == "__main__":
    main(parser().parse_args())

