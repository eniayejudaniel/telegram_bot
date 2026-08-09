#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys

from ebay_monitor.config import load_config
from ebay_monitor.fetcher import EbayFetchClient
from ebay_monitor.parser import parse_search_html


def component_summary(listing: object) -> str:
    components = getattr(listing, "components", [])
    if not components:
        return "UNPARSED"
    return "; ".join(
        f"{component.franchise}/{component.set_name}/{component.product_type} "
        f"x{component.quantity} ({component.language})"
        for component in components
    )


async def run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.no_proxy:
        config.fetch.proxy_url = ""
        # Avoid loading a saved identity that still contains an old proxy URL.
        config.fetch.identity_id = f"{config.fetch.identity_id}_direct_test"
        config.fetch.use_camoufox_on_challenge = False

    client = EbayFetchClient(config.fetch)
    try:
        await client.start()
        html, url = await client.search_html(
            args.keyword,
            buying_option=args.buying_option,
            limit=args.limit,
            page=args.page,
        )
        listings = parse_search_html(
            html,
            config.parser,
            config.search,
            buying_option_hint=args.buying_option,
        )

        print(f"URL: {url}")
        print(f"HTML bytes: {len(html.encode('utf-8')):,}")
        print(f"Parsed listings: {len(listings)}")
        print("-" * 100)
        for index, listing in enumerate(listings[: args.show], 1):
            print(f"{index:02}. {listing.currency} {listing.total_price:.2f} | {listing.title}")
            print(f"    {component_summary(listing)}")
            print(f"    {listing.url}")
    finally:
        await client.close()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Fetch and print one eBay search-results page.")
    parser.add_argument("keyword", nargs="?", default="one piece booster box")
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--buying-option", choices=("bin", "auction"), default="bin")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--show", type=int, default=20)
    parser.add_argument("--no-proxy", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
