from __future__ import annotations

import asyncio
import logging
from typing import Any

from .alerts import TelegramNotifier
from .config import load_config
from .fetcher import EbayFetchClient
from .models import AlertCandidate, Listing
from .parser import merge_detail_into_listing, parse_detail_html, parse_search_html
from .pricing import build_baselines, evaluate_listing, rank_alerts
from .storage import Storage
from .telegram_preferences import TelegramPreferenceBot

LOGGER = logging.getLogger("ebay_monitor")


async def fetch_keyword_pages(
    client: EbayFetchClient,
    keyword: str,
    *,
    buying_option: str,
    limit: int,
    max_pages: int,
) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    for page in range(max_pages):
        try:
            html_text, source_url = await client.search_html(
                keyword,
                buying_option=buying_option,
                limit=limit,
                page=page + 1,
            )
        except Exception as exc:
            LOGGER.warning(
                "Skipping failed search page: keyword=%r option=%s page=%s error=%s",
                keyword,
                buying_option,
                page + 1,
                exc,
            )
            break
        results.append((html_text, source_url))
        if "pagination__next" not in html_text and "pagination__item" not in html_text:
            break
    return results


async def fetch_all_listings(
    config: Any,
    client: EbayFetchClient,
    *,
    keywords: list[str] | None = None,
) -> tuple[list[Listing], dict[str, set[str]]]:
    monitored_keywords = keywords if keywords is not None else config.search.keywords
    tasks = [
        fetch_keyword_pages(
            client,
            keyword,
            buying_option=buying_option,
            limit=config.search.limit_per_page,
            max_pages=config.search.max_pages_per_keyword,
        )
        for keyword in monitored_keywords
        for buying_option in config.search.buying_options
    ]
    running_tasks = [asyncio.create_task(task) for task in tasks]
    done, pending = await asyncio.wait(
        running_tasks,
        timeout=max(1, config.fetch.cycle_timeout_seconds),
    )
    if pending:
        LOGGER.warning(
            "Fetch cycle reached its %ss deadline; keeping completed pages and cancelling %s searches.",
            config.fetch.cycle_timeout_seconds,
            len(pending),
        )
        for task in pending:
            task.cancel()
        # curl_cffi may take time to unwind a native request after cancellation.
        # Give it one event-loop turn, but do not let cancellation defeat the
        # cycle-level deadline.
        await asyncio.sleep(0)
    pages = [task.result() if task in done else [] for task in running_tasks]
    successful_page_count = sum(len(result) for result in pages)
    if successful_page_count == 0:
        raise RuntimeError("All eBay search-page requests failed; refusing to build empty baselines.")
    expected_first_pages = len(tasks)
    if successful_page_count < expected_first_pages:
        LOGGER.warning(
            "Fetch cycle is partial: received %s pages for %s keyword/option searches.",
            successful_page_count,
            expected_first_pages,
        )
    by_id: dict[str, Listing] = {}
    listing_keywords: dict[str, set[str]] = {}
    task_index = 0
    for keyword in monitored_keywords:
        for buying_option in config.search.buying_options:
            for html_text, _source_url in pages[task_index]:
                for listing in parse_search_html(
                    html_text,
                    config.parser,
                    config.search,
                    buying_option_hint=buying_option,
                ):
                    by_id[listing.item_id] = listing
                    listing_keywords.setdefault(listing.item_id, set()).add(keyword)
            task_index += 1
    return list(by_id.values()), listing_keywords


async def run_cycle(
    config: Any,
    client: EbayFetchClient,
    storage: Storage,
    notifier: TelegramNotifier,
    *,
    multi_user: bool = False,
) -> None:
    routes = storage.active_keyword_routes() if multi_user else {}
    monitored_keywords = [keyword for keyword in config.search.keywords if keyword in routes]
    if multi_user and not monitored_keywords:
        LOGGER.info("No active user keyword subscriptions; skipping eBay cycle.")
        return

    listings, listing_keywords = await fetch_all_listings(
        config,
        client,
        keywords=monitored_keywords if multi_user else None,
    )
    LOGGER.info("Fetched and parsed %s monitored listings.", len(listings))

    for listing in listings:
        storage.record_listing(listing)

    baselines = build_baselines(listings, config.pricing)
    storage.record_baselines(baselines)
    LOGGER.info("Built %s baselines.", len(baselines))

    alerts: list[AlertCandidate] = []
    for listing in listings:
        alert = evaluate_listing(listing, baselines, config.pricing)
        if alert:
            alerts.append(alert)

    sent_counts: dict[str, int] = {}
    total_sent = 0
    for alert in rank_alerts(alerts):
        if multi_user:
            recipients: set[str] = set()
            for keyword in listing_keywords.get(alert.listing.item_id, set()):
                recipients.update(routes.get(keyword, set()))
        else:
            recipients = {str(notifier.chat_id or "dry-run")}

        eligible: list[str] = []
        for chat_id in recipients:
            if sent_counts.get(chat_id, 0) >= config.pricing.max_alerts_per_cycle:
                continue
            if not notifier.dry_run:
                should_send = (
                    storage.should_alert_user(chat_id, alert.listing.item_id, alert.listing.total_price)
                    if multi_user
                    else storage.should_alert(alert.listing.item_id, alert.listing.total_price)
                )
                if not should_send:
                    continue
            eligible.append(chat_id)
        if not eligible:
            continue

        if not notifier.dry_run:
            try:
                detail = parse_detail_html(await client.detail_html(alert.listing.url))
                merge_detail_into_listing(alert.listing, detail, config.parser)
                refreshed_alert = evaluate_listing(alert.listing, baselines, config.pricing)
                if refreshed_alert is None:
                    continue
                alert = refreshed_alert
            except Exception as exc:
                LOGGER.warning("Detail enrichment failed for %s: %s", alert.listing.item_id, exc)
        for chat_id in eligible:
            await notifier.send_alert(alert, chat_id=None if chat_id == "dry-run" else chat_id)
            sent_counts[chat_id] = sent_counts.get(chat_id, 0) + 1
            total_sent += 1
            if not notifier.dry_run:
                if multi_user:
                    storage.mark_user_alerted(
                        chat_id,
                        alert.listing.item_id,
                        alert.listing.total_price,
                    )
                else:
                    storage.mark_alerted(alert.listing.item_id, alert.listing.total_price)
    LOGGER.info(
        "Sent %s alert(s) to %s recipient(s) this cycle%s.",
        total_sent,
        len(sent_counts),
        " (dry run)" if notifier.dry_run else "",
    )


async def run_monitor(
    config_path: str,
    *,
    once: bool = False,
    dry_run_override: bool = False,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs full request URLs at INFO; Telegram Bot API URLs contain the
    # secret token, so never allow those URLs into terminal or service logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    config = load_config(config_path)
    dry_run = dry_run_override or config.dry_run
    storage = Storage(config.database_path)
    client = EbayFetchClient(config.fetch)
    notifier = TelegramNotifier(
        bot_token=config.telegram_bot_token,
        chat_id=config.telegram_chat_id,
        dry_run=dry_run,
    )
    preference_bot: TelegramPreferenceBot | None = None
    preference_task: asyncio.Task[None] | None = None
    try:
        try:
            await client.start()
        except Exception:
            # Session creation normally succeeds before the optional homepage
            # warm-up. Treat a warm-up/proxy outage like a failed cycle so the
            # long-running service remains alive and retries on schedule.
            LOGGER.exception(
                "Initial eBay connection failed; the monitor will keep retrying."
            )
        if not once:
            preference_bot = TelegramPreferenceBot(
                config.telegram_bot_token, config.search.keywords
            )
            preference_task = asyncio.create_task(preference_bot.poll_forever(storage))

        consecutive_cycle_failures = 0
        while True:
            try:
                await run_cycle(
                    config,
                    client,
                    storage,
                    notifier,
                    multi_user=not once,
                )
                if consecutive_cycle_failures >= 3:
                    try:
                        await notifier.send_text(
                            "✅ <b>eBay monitor recovered</b>\nMonitoring cycles are running again."
                        )
                    except Exception:
                        LOGGER.exception("Failed to send monitor recovery notification.")
                consecutive_cycle_failures = 0
            except Exception:
                consecutive_cycle_failures += 1
                LOGGER.exception("Monitoring cycle failed.")
                if consecutive_cycle_failures == 3:
                    try:
                        await notifier.send_text(
                            "🚨 <b>eBay monitor problem</b>\n"
                            "Three consecutive monitoring cycles failed. The service is still running and will keep retrying."
                        )
                    except Exception:
                        LOGGER.exception("Failed to send monitor failure notification.")
            if once:
                break
            await asyncio.sleep(config.poll_interval_seconds)
    finally:
        if preference_task:
            preference_task.cancel()
            await asyncio.gather(preference_task, return_exceptions=True)
        if preference_bot:
            await preference_bot.close()
        storage.close()
        await client.close()
        await notifier.close()
