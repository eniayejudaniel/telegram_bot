from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


class KeywordKeyboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyboard_marks_selected_keywords_and_has_done(self) -> None:
        bot = TelegramPreferenceBot("test-token", ["pokemon tin", "one piece booster box"])
        try:
            keyboard = bot.selection_keyboard({"pokemon tin"})["inline_keyboard"]
            texts = [button["text"] for row in keyboard for button in row]
            callbacks = [button["callback_data"] for row in keyboard for button in row]
            self.assertIn("✅ Pokemon Tin", texts)
            self.assertIn("▫️ One Piece Booster Box", texts)
            self.assertIn("kw_done", callbacks)
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
