from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .storage import Storage

LOGGER = logging.getLogger("ebay_monitor")


class TelegramPreferenceBot:
    def __init__(
        self,
        token: str,
        keywords: list[str],
        *,
        discord_new_user_webhook_url: str = "",
    ) -> None:
        self.token = token
        self.keywords = list(dict.fromkeys(keywords))
        self._client = httpx.AsyncClient(timeout=35)
        self._offset = 0
        self._drafts: dict[str, set[str]] = {}
        self.discord_new_user_webhook_url = discord_new_user_webhook_url

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def api_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    async def _request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(f"{self.api_url}/{method}", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Telegram {method} returned HTTP {exc.response.status_code}."
            ) from None
        except httpx.HTTPError:
            raise RuntimeError(f"Telegram {method} request failed.") from None
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description', 'unknown error')}")
        return data

    async def send_message(self, chat_id: str | int, text: str, *, reply_markup: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": str(chat_id), "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self._request("sendMessage", payload)

    async def notify_new_user(
        self,
        chat_id: str,
        *,
        username: str,
        first_name: str,
    ) -> None:
        if not self.discord_new_user_webhook_url:
            return
        fields = [
            {"name": "Telegram user", "value": html.escape(first_name) or "Unknown", "inline": True},
            {"name": "Username", "value": f"@{username}" if username else "Not provided", "inline": True},
            {"name": "Chat ID", "value": chat_id, "inline": False},
        ]
        payload = {
            "username": "TCG Sourcing Bot Monitor",
            "embeds": [{
                "title": "New Telegram user",
                "color": 0x2ECC71,
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }],
            "allowed_mentions": {"parse": []},
        }
        try:
            response = await self._client.post(self.discord_new_user_webhook_url, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            LOGGER.warning(
                "Discord new-user notification returned HTTP %s.",
                exc.response.status_code,
            )
        except httpx.HTTPError:
            LOGGER.warning("Discord new-user notification request failed.")

    def selection_keyboard(self, selected: set[str]) -> dict[str, Any]:
        buttons = [
            {"text": f"{'✅' if keyword in selected else '▫️'} {keyword.title()}", "callback_data": f"kw:{index}"}
            for index, keyword in enumerate(self.keywords)
        ]
        rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        rows.extend([
            [{"text": "Select all", "callback_data": "kw_all"}, {"text": "Clear", "callback_data": "kw_clear"}],
            [{"text": "✅ Save alerts", "callback_data": "kw_done"}, {"text": "Cancel", "callback_data": "kw_cancel"}],
        ])
        return {"inline_keyboard": rows}

    def main_keyboard(self, active: bool, has_keywords: bool) -> dict[str, Any]:
        state = ({"text": "⏸ Pause alerts", "callback_data": "menu_pause"}
                 if active and has_keywords else
                 {"text": "▶️ Resume alerts", "callback_data": "menu_resume"})
        return {"inline_keyboard": [
            [{"text": "🎯 Manage alerts", "callback_data": "menu_keywords"}],
            [{"text": "📋 My status", "callback_data": "menu_status"}, state],
            [{"text": "ℹ️ Help", "callback_data": "menu_help"}],
        ]}

    def selection_text(self, selected: set[str]) -> str:
        return ("<b>Choose your eBay alerts</b>\n\nTap products to turn them on or off, then save.\n"
                f"Selected: <b>{len(selected)}</b>")

    def status_text(self, chat_id: str, storage: Storage) -> str:
        keywords = sorted(storage.get_user_keywords(chat_id))
        active = storage.is_user_active(chat_id) and bool(keywords)
        body = "\n".join(f"• {html.escape(k.title())}" for k in keywords) if keywords else "No alert products selected."
        return f"<b>Your alert status: {'Active' if active else 'Paused'}</b>\n\n{body}\n\nListings are checked every 10 minutes."

    async def show_main_menu(self, chat_id: str, storage: Storage, *, welcome: bool = False) -> None:
        keywords = storage.get_user_keywords(chat_id)
        active = storage.is_user_active(chat_id)
        text = ("<b>Welcome to TCG Sourcing Bot</b>\n\nChoose the Pokémon and One Piece products you want monitored. "
                "I’ll send underpriced eBay UK listings with a direct link.\n\n" if welcome else "<b>TCG Sourcing Bot</b>\n\n")
        text += "Your alerts are active." if active and keywords else "Configure alerts to get started."
        await self.send_message(chat_id, text, reply_markup=self.main_keyboard(active, bool(keywords)))

    async def show_selector(self, chat_id: str, storage: Storage, *, message_id: int | None = None) -> None:
        selected = set(storage.get_user_keywords(chat_id))
        self._drafts[chat_id] = selected
        markup = self.selection_keyboard(selected)
        if message_id is None:
            await self.send_message(chat_id, self.selection_text(selected), reply_markup=markup)
        else:
            await self._request("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                "text": self.selection_text(selected), "parse_mode": "HTML", "reply_markup": markup})

    async def handle_message(self, message: dict[str, Any], storage: Storage) -> None:
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or chat.get("id") is None:
            return
        chat_id = str(chat["id"])
        command = str(message.get("text") or "").split(maxsplit=1)[0].casefold()
        username = str(chat.get("username") or "")
        first_name = str(chat.get("first_name") or "")
        is_new_user = storage.upsert_telegram_user(chat_id, username=username,
            first_name=first_name,
            active=True if command in {"/start", "/keywords", "/alerts", "/resume"} else None)
        if is_new_user:
            await self.notify_new_user(
                chat_id,
                username=username,
                first_name=first_name,
            )
        if command == "/start":
            await self.show_main_menu(chat_id, storage, welcome=True)
        elif command in {"/keywords", "/alerts"}:
            await self.show_selector(chat_id, storage)
        elif command == "/status":
            await self.send_message(chat_id, self.status_text(chat_id, storage),
                reply_markup=self.main_keyboard(storage.is_user_active(chat_id), bool(storage.get_user_keywords(chat_id))))
        elif command == "/stop":
            storage.set_user_active(chat_id, False)
            await self.send_message(chat_id, "Alerts paused. Your selections are saved.",
                reply_markup=self.main_keyboard(False, bool(storage.get_user_keywords(chat_id))))
        elif command == "/resume":
            if storage.get_user_keywords(chat_id):
                storage.set_user_active(chat_id, True)
                await self.send_message(chat_id, "Alerts resumed. I’ll check every 10 minutes.")
            else:
                await self.show_selector(chat_id, storage)
        elif command == "/help":
            await self.send_message(chat_id, "<b>TCG Sourcing Bot</b>\n/start — main menu\n/keywords — choose products\n/status — view settings\n/stop — pause\n/resume — resume")
        else:
            await self.show_main_menu(chat_id, storage)

    async def _edit_status(self, chat_id: str, message_id: int, storage: Storage) -> None:
        await self._request("editMessageText", {"chat_id": chat_id, "message_id": message_id,
            "text": self.status_text(chat_id, storage), "parse_mode": "HTML",
            "reply_markup": self.main_keyboard(storage.is_user_active(chat_id), bool(storage.get_user_keywords(chat_id)))})

    async def handle_callback(self, query: dict[str, Any], storage: Storage) -> None:
        query_id = str(query.get("id") or "")
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or chat.get("id") is None:
            if query_id:
                await self._request("answerCallbackQuery", {"callback_query_id": query_id})
            return
        chat_id, message_id = str(chat["id"]), int(message.get("message_id") or 0)
        data = str(query.get("data") or "")
        if data == "menu_keywords":
            await self.show_selector(chat_id, storage, message_id=message_id)
        elif data == "menu_status":
            await self._edit_status(chat_id, message_id, storage)
        elif data in {"menu_pause", "menu_resume"}:
            has_keywords = bool(storage.get_user_keywords(chat_id))
            if data == "menu_resume" and not has_keywords:
                await self.show_selector(chat_id, storage, message_id=message_id)
            else:
                storage.set_user_active(chat_id, data == "menu_resume")
                await self._edit_status(chat_id, message_id, storage)
        elif data == "menu_help":
            await self._request("answerCallbackQuery", {"callback_query_id": query_id, "text": "Choose products in Manage alerts, then save."})
            return
        elif data == "kw_cancel":
            self._drafts.pop(chat_id, None)
            await self._edit_status(chat_id, message_id, storage)
            await self._request("answerCallbackQuery", {"callback_query_id": query_id, "text": "No changes saved"})
            return
        else:
            selected = self._drafts.setdefault(chat_id, set(storage.get_user_keywords(chat_id)))
            if data.startswith("kw:"):
                try:
                    keyword = self.keywords[int(data.split(":", 1)[1])]
                except (ValueError, IndexError):
                    keyword = ""
                if keyword:
                    selected.symmetric_difference_update({keyword})
            elif data == "kw_all": selected.update(self.keywords)
            elif data == "kw_clear": selected.clear()
            elif data == "kw_done":
                if not selected:
                    await self._request("answerCallbackQuery", {"callback_query_id": query_id, "text": "Select at least one product first.", "show_alert": True})
                    return
                storage.replace_user_keywords(chat_id, selected)
                summary = "\n".join(f"• {html.escape(v.title())}" for v in sorted(selected))
                await self._request("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                    "text": f"<b>Alerts saved ({len(selected)})</b>\n{summary}\n\nI’ll check eBay every 10 minutes.",
                    "parse_mode": "HTML", "reply_markup": self.main_keyboard(True, True)})
                self._drafts.pop(chat_id, None)
                await self._request("answerCallbackQuery", {"callback_query_id": query_id, "text": "Saved"})
                return
            if message_id:
                await self._request("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                    "text": self.selection_text(selected), "parse_mode": "HTML", "reply_markup": self.selection_keyboard(selected)})
        if query_id:
            await self._request("answerCallbackQuery", {"callback_query_id": query_id})

    async def poll_forever(self, storage: Storage) -> None:
        if not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required for user preference polling.")
        LOGGER.info("Telegram preference polling started.")
        try:
            await self._request("setMyCommands", {"commands": [
                {"command": "start", "description": "Open the main menu"},
                {"command": "keywords", "description": "Choose alert products"},
                {"command": "status", "description": "View your alert status"},
                {"command": "stop", "description": "Pause alerts"},
                {"command": "resume", "description": "Resume saved alerts"},
                {"command": "help", "description": "How the bot works"},
            ]})
        except Exception:
            LOGGER.exception("Could not register Telegram command menu.")
        while True:
            try:
                data = await self._request("getUpdates", {"offset": self._offset, "timeout": 25,
                    "allowed_updates": ["message", "callback_query"]})
                for update in data.get("result") or []:
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    if update.get("message"):
                        await self.handle_message(update["message"], storage)
                    elif update.get("callback_query"):
                        await self.handle_callback(update["callback_query"], storage)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Telegram preference polling failed; retrying in 5 seconds.")
                await asyncio.sleep(5)
