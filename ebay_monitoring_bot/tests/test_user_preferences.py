from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from ebay_monitor.storage import Storage
from ebay_monitor.telegram_preferences import TelegramPreferenceBot


class UserPreferenceStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "test.sqlite3")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def test_subscriptions_are_persisted_and_routed(self) -> None:
        self.storage.upsert_telegram_user("101", username="alice", first_name="Alice")
        self.storage.upsert_telegram_user("202", username="bob", first_name="Bob")
        self.storage.replace_user_keywords("101", {"pokemon booster box", "pokemon tin"})
        self.storage.replace_user_keywords("202", {"pokemon tin"})

        self.assertEqual(
            self.storage.get_user_keywords("101"),
            {"pokemon booster box", "pokemon tin"},
        )
        self.assertEqual(
            self.storage.active_keyword_routes(),
            {
                "pokemon booster box": {"101"},
                "pokemon tin": {"101", "202"},
            },
        )

    def test_stop_deactivates_routes_without_deleting_choices(self) -> None:
        self.storage.upsert_telegram_user("101")
        self.storage.replace_user_keywords("101", {"one piece booster box"})
        self.storage.set_user_active("101", False)
        self.assertEqual(self.storage.active_keyword_routes(), {})
        self.assertEqual(
            self.storage.get_user_keywords("101"),
            {"one piece booster box"},
        )
        self.storage.upsert_telegram_user("101", username="still-paused")
        self.assertEqual(self.storage.active_keyword_routes(), {})

    def test_alert_history_is_per_user(self) -> None:
        self.assertTrue(self.storage.should_alert_user("101", "item", 100))
        self.storage.mark_user_alerted("101", "item", 100)
        self.assertFalse(self.storage.should_alert_user("101", "item", 100))
        self.assertTrue(self.storage.should_alert_user("202", "item", 100))
        self.assertTrue(self.storage.should_alert_user("101", "item", 90))

    def test_marketplace_subscriptions_route_keywords_independently(self) -> None:
        self.storage.upsert_telegram_user("101")
        self.storage.upsert_telegram_user("202")
        self.storage.replace_user_keywords("101", {"pokemon tin"})
        self.storage.replace_user_keywords("202", {"pokemon tin"})
        self.storage.replace_user_marketplaces("101", {"EBAY_GB", "EBAY_US"})
        self.storage.replace_user_marketplaces("202", {"EBAY_US"})

        self.assertEqual(
            self.storage.active_marketplace_keyword_routes(),
            {
                ("EBAY_GB", "pokemon tin"): {"101"},
                ("EBAY_US", "pokemon tin"): {"101", "202"},
            },
        )

    def test_existing_users_default_to_uk_marketplace(self) -> None:
        self.storage.upsert_telegram_user("legacy")
        self.storage.replace_user_keywords("legacy", {"pokemon tin"})
        self.assertEqual(self.storage.get_user_marketplaces("legacy"), {"EBAY_GB"})


class KeywordKeyboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_notification_only_runs_for_first_user_interaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            storage = Storage(Path(temp) / "test.sqlite3")
            bot = TelegramPreferenceBot(
                "test-token",
                ["pokemon tin"],
                discord_new_user_webhook_url="https://discord.invalid/webhook",
            )
            bot.notify_new_user = AsyncMock()
            bot.show_main_menu = AsyncMock()
            message = {
                "text": "/start",
                "chat": {
                    "id": 101,
                    "type": "private",
                    "username": "alice",
                    "first_name": "Alice",
                },
            }
            try:
                await bot.handle_message(message, storage)
                await bot.handle_message(message, storage)
                bot.notify_new_user.assert_awaited_once_with(
                    "101", username="alice", first_name="Alice"
                )
            finally:
                await bot.close()
                storage.close()

    async def test_keyboard_marks_selected_keywords_and_has_done(self) -> None:
        bot = TelegramPreferenceBot(
            "test-token",
            ["pokemon tin", "one piece booster box"],
            marketplaces={"EBAY_GB": "United Kingdom", "EBAY_US": "United States"},
        )
        try:
            keyboard = bot.selection_keyboard({"pokemon tin"}, {"EBAY_US"})["inline_keyboard"]
            texts = [button["text"] for row in keyboard for button in row]
            callbacks = [button["callback_data"] for row in keyboard for button in row]
            self.assertIn("✅ Pokemon Tin", texts)
            self.assertIn("▫️ One Piece Booster Box", texts)
            self.assertIn("kw_done", callbacks)
            self.assertIn("▫️ United Kingdom", texts)
            self.assertIn("✅ United States", texts)
            self.assertIn("mp:1", callbacks)
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
