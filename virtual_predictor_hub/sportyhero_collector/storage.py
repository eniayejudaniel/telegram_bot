"""SQLite persistence compatible with the recovered SportyHero snapshots."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping


SCHEMA = """
CREATE TABLE IF NOT EXISTS multipliers (
    round_id INTEGER PRIMARY KEY,
    multiplier REAL NOT NULL CHECK(multiplier >= 1.0),
    multiplier_text TEXT NOT NULL,
    is_new INTEGER NOT NULL,
    round_created_at TEXT,
    first_observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_multiplier ON multipliers(multiplier);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript(SCHEMA)
    return db


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    absolute = Path(path).resolve().as_posix()
    return sqlite3.connect(f"file:{absolute}?mode=ro", uri=True, timeout=30)


def normalize(item: Mapping) -> tuple[int, float, str, str | None]:
    round_id = int(item.get("id", item.get("round_id")))
    raw = item.get("houseCoefficient", item.get("multiplier"))
    if raw is None:
        raw = item.get("houseCoefficientStr", item.get("multiplier_text"))
    multiplier = float(raw)
    if round_id <= 0 or multiplier < 1 or multiplier != multiplier:
        raise ValueError("invalid SportyHero round")
    text = str(item.get("houseCoefficientStr") or f"{multiplier:.2f}")
    created = item.get("createdAt", item.get("round_created_at"))
    return round_id, multiplier, text, str(created) if created else None


def insert_rounds(
    db: sqlite3.Connection, items: Iterable[Mapping], *, is_new: bool = True
) -> int:
    observed = datetime.now(timezone.utc).isoformat(timespec="seconds")
    inserted = 0
    for item in sorted(items, key=lambda value: int(value.get("id", value.get("round_id")))):
        round_id, multiplier, text, created = normalize(item)
        existing = db.execute(
            "SELECT multiplier FROM multipliers WHERE round_id=?", (round_id,)
        ).fetchone()
        if existing:
            if abs(float(existing[0]) - multiplier) > 1e-9:
                raise ValueError(f"conflicting multiplier for round {round_id}")
            continue
        db.execute(
            "INSERT INTO multipliers(round_id,multiplier,multiplier_text,is_new,"
            "round_created_at,first_observed_at) VALUES(?,?,?,?,?,?)",
            (round_id, multiplier, text, int(is_new), created, observed),
        )
        inserted += 1
    db.commit()
    return inserted


def load_rounds(db: sqlite3.Connection) -> list[tuple[int, float]]:
    return [
        (int(round_id), float(multiplier))
        for round_id, multiplier in db.execute(
            "SELECT round_id,multiplier FROM multipliers ORDER BY round_id"
        )
    ]

