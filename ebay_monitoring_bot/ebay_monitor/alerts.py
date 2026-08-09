from __future__ import annotations

import html
import sys

import httpx

from .models import AlertCandidate, Component, ComponentValuation
from .parser import time_remaining


def money(value: float) -> str:
    return f"GBP {value:,.2f}"


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def component_label(component: Component) -> str:
    variant = "Pokemon Center " if component.variant == "pokemon_center" else ""
    language = f"{component.language} " if component.language != "unknown" else ""
    set_name = "" if component.set_name == "unknown_set" else f" - {component.set_name}"
    product_type = component.product_type.replace("_", " ")
    identity = f" [{component.identity}]" if component.identity else ""
    return f"{component.quantity}x {language}{variant}{product_type}{set_name}{identity}"


def valuation_line(valuation: ComponentValuation) -> str:
    label = component_label(valuation.component)
    if valuation.baseline is None:
        return f"- {html.escape(label)}: no baseline"
    sample = valuation.baseline.sample_size
    return (
        f"- {html.escape(label)}: {money(valuation.unit_value)} each "
        f"x {valuation.component.quantity} = {money(valuation.total_value)} "
        f"(n={sample})"
    )


def format_alert(alert: AlertCandidate) -> str:
    listing = alert.listing
    title = html.escape(listing.title)
    link = html.escape(listing.url, quote=True)
    seller_feedback = "N/A"
    if listing.seller_feedback_count is not None or listing.seller_feedback_percentage is not None:
        count = listing.seller_feedback_count if listing.seller_feedback_count is not None else "?"
        percentage = (
            f"{listing.seller_feedback_percentage:.1f}%"
            if listing.seller_feedback_percentage is not None
            else "?%"
        )
        seller_feedback = f"{count} / {percentage}"

    breakdown = "\n".join(valuation_line(valuation) for valuation in alert.valuations)
    if not breakdown:
        breakdown = "- Contents could not be parsed confidently."

    listing_type = listing.buying_option
    if listing.buying_option == "AUCTION":
        listing_type = f"AUCTION, {time_remaining(listing.end_time)} remaining"

    reasons = ", ".join(alert.reasons) if alert.reasons else "threshold match"
    return (
        f"<b>eBay deal alert</b> [{html.escape(alert.confidence)}]\n"
        f"<a href=\"{link}\">{title}</a>\n\n"
        f"<b>Price</b>: {money(listing.total_price)} "
        f"(item {money(listing.item_price)} + postage {money(listing.postage)})\n"
        f"<b>Units</b>: {listing.total_units} total, {money(listing.price_per_unit)} per unit\n"
        f"<b>Baseline</b>: {money(alert.baseline_total)}\n"
        f"<b>Below market</b>: {money(alert.opportunity_gbp)} / {pct(alert.below_market_pct)}\n"
        f"<b>Priority</b>: {alert.priority_score:.2f} ({html.escape(reasons)})\n\n"
        f"<b>Contents</b>\n{breakdown}\n\n"
        f"<b>Seller</b>: {html.escape(listing.seller_username or 'N/A')} "
        f"({html.escape(seller_feedback)})\n"
        f"<b>Location</b>: {html.escape(listing.location_bucket)}"
        f"{' / ' + html.escape(listing.seller_location) if listing.seller_location else ''}\n"
        f"<b>Listing</b>: {html.escape(listing_type)}\n"
        f"<b>Coupon</b>: {html.escape(listing.coupon or 'N/A')}"
    )


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, dry_run: bool = False) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self._client = httpx.AsyncClient(timeout=30)

    async def close(self) -> None:
        await self._client.aclose()

    async def send_alert(self, alert: AlertCandidate, *, chat_id: str | None = None) -> None:
        text = format_alert(alert)
        if self.dry_run:
            safe_text = text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
                sys.stdout.encoding or "utf-8"
            )
            print(safe_text)
            print()
            return
        destination = chat_id or self.chat_id
        if not self.bot_token or not destination:
            raise ValueError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before sending alerts.")

        response = await self._client.post(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            json={
                "chat_id": destination,
                "text": text[:3900],
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
        )
        response.raise_for_status()

    async def send_text(self, text: str, *, chat_id: str | None = None) -> None:
        destination = chat_id or self.chat_id
        if self.dry_run:
            print(text)
            return
        if not self.bot_token or not destination:
            raise ValueError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before sending messages.")
        response = await self._client.post(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            json={
                "chat_id": destination,
                "text": text[:3900],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()
