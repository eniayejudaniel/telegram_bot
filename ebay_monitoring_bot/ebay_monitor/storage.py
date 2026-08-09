from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .models import Baseline, Listing


class Storage:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.initialize()

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen_listings (
                item_id TEXT PRIMARY KEY,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                last_total_price REAL NOT NULL,
                last_alert_price REAL,
                alert_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS listing_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL,
                seen_at REAL NOT NULL,
                title TEXT NOT NULL,
                total_price REAL NOT NULL,
                buying_option TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS baseline_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_key TEXT NOT NULL,
                median_price REAL NOT NULL,
                sample_size INTEGER NOT NULL,
                low_confidence INTEGER NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS telegram_users (
                chat_id TEXT PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_keyword_subscriptions (
                chat_id TEXT NOT NULL,
                keyword TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (chat_id, keyword),
                FOREIGN KEY (chat_id) REFERENCES telegram_users(chat_id)
            );

            CREATE TABLE IF NOT EXISTS user_alert_history (
                chat_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                last_alert_price REAL NOT NULL,
                alert_count INTEGER NOT NULL DEFAULT 1,
                last_alerted_at REAL NOT NULL,
                PRIMARY KEY (chat_id, item_id)
            );
            """
        )
        self.connection.commit()

    def record_listing(self, listing: Listing) -> None:
        now = time.time()
        self.connection.execute(
            """
            INSERT INTO seen_listings (
                item_id, first_seen, last_seen, title, url, last_total_price
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                last_seen=excluded.last_seen,
                title=excluded.title,
                url=excluded.url,
                last_total_price=excluded.last_total_price
            """,
            (
                listing.item_id,
                now,
                now,
                listing.title,
                listing.url,
                listing.total_price,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO listing_snapshots (
                item_id, seen_at, title, total_price, buying_option, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                listing.item_id,
                now,
                listing.title,
                listing.total_price,
                listing.buying_option,
                json.dumps(listing.raw, ensure_ascii=False),
            ),
        )
        self.connection.commit()

    def record_baselines(self, baselines: dict[str, Baseline]) -> None:
        now = time.time()
        rows = [
            (baseline.group_key, baseline.median_price, baseline.sample_size, int(baseline.low_confidence), now)
            for baseline in baselines.values()
        ]
        self.connection.executemany(
            """
            INSERT INTO baseline_history (
                group_key, median_price, sample_size, low_confidence, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.connection.commit()

    def should_alert(self, item_id: str, total_price: float) -> bool:
        row = self.connection.execute(
            "SELECT last_alert_price FROM seen_listings WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None or row["last_alert_price"] is None:
            return True
        return total_price < float(row["last_alert_price"]) - 0.01

    def mark_alerted(self, item_id: str, total_price: float) -> None:
        self.connection.execute(
            """
            UPDATE seen_listings
            SET last_alert_price = ?, alert_count = alert_count + 1
            WHERE item_id = ?
            """,
            (total_price, item_id),
        )
        self.connection.commit()

    def upsert_telegram_user(
        self,
        chat_id: str | int,
        *,
        username: str = "",
        first_name: str = "",
        active: bool | None = None,
    ) -> None:
        now = time.time()
        existing = self.connection.execute(
            "SELECT 1 FROM telegram_users WHERE chat_id = ?",
            (str(chat_id),),
        ).fetchone()
        if existing:
            if active is None:
                self.connection.execute(
                    """
                    UPDATE telegram_users
                    SET username = ?, first_name = ?, updated_at = ?
                    WHERE chat_id = ?
                    """,
                    (username, first_name, now, str(chat_id)),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE telegram_users
                    SET username = ?, first_name = ?, active = ?, updated_at = ?
                    WHERE chat_id = ?
                    """,
                    (username, first_name, int(active), now, str(chat_id)),
                )
            self.connection.commit()
            return
        self.connection.execute(
            """
            INSERT INTO telegram_users (
                chat_id, username, first_name, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(chat_id), username, first_name, int(active if active is not None else True), now, now),
        )
        self.connection.commit()

    def set_user_active(self, chat_id: str | int, active: bool) -> None:
        self.connection.execute(
            "UPDATE telegram_users SET active = ?, updated_at = ? WHERE chat_id = ?",
            (int(active), time.time(), str(chat_id)),
        )
        self.connection.commit()

    def is_user_active(self, chat_id: str | int) -> bool:
        row = self.connection.execute(
            "SELECT active FROM telegram_users WHERE chat_id = ?",
            (str(chat_id),),
        ).fetchone()
        return bool(row["active"]) if row is not None else False

    def replace_user_keywords(self, chat_id: str | int, keywords: set[str]) -> None:
        chat_id_text = str(chat_id)
        now = time.time()
        with self.connection:
            self.connection.execute(
                "DELETE FROM user_keyword_subscriptions WHERE chat_id = ?",
                (chat_id_text,),
            )
            self.connection.executemany(
                """
                INSERT INTO user_keyword_subscriptions (chat_id, keyword, created_at)
                VALUES (?, ?, ?)
                """,
                [(chat_id_text, keyword, now) for keyword in sorted(keywords)],
            )
            self.connection.execute(
                "UPDATE telegram_users SET active = 1, updated_at = ? WHERE chat_id = ?",
                (now, chat_id_text),
            )

    def get_user_keywords(self, chat_id: str | int) -> set[str]:
        rows = self.connection.execute(
            "SELECT keyword FROM user_keyword_subscriptions WHERE chat_id = ? ORDER BY keyword",
            (str(chat_id),),
        ).fetchall()
        return {str(row["keyword"]) for row in rows}

    def active_keyword_routes(self) -> dict[str, set[str]]:
        rows = self.connection.execute(
            """
            SELECT s.keyword, s.chat_id
            FROM user_keyword_subscriptions AS s
            JOIN telegram_users AS u ON u.chat_id = s.chat_id
            WHERE u.active = 1
            """
        ).fetchall()
        routes: dict[str, set[str]] = {}
        for row in rows:
            routes.setdefault(str(row["keyword"]), set()).add(str(row["chat_id"]))
        return routes

    def should_alert_user(self, chat_id: str | int, item_id: str, total_price: float) -> bool:
        row = self.connection.execute(
            """
            SELECT last_alert_price FROM user_alert_history
            WHERE chat_id = ? AND item_id = ?
            """,
            (str(chat_id), item_id),
        ).fetchone()
        return row is None or total_price < float(row["last_alert_price"]) - 0.01

    def mark_user_alerted(self, chat_id: str | int, item_id: str, total_price: float) -> None:
        self.connection.execute(
            """
            INSERT INTO user_alert_history (
                chat_id, item_id, last_alert_price, alert_count, last_alerted_at
            ) VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(chat_id, item_id) DO UPDATE SET
                last_alert_price=excluded.last_alert_price,
                alert_count=user_alert_history.alert_count + 1,
                last_alerted_at=excluded.last_alerted_at
            """,
            (str(chat_id), item_id, total_price, time.time()),
        )
        self.connection.commit()
