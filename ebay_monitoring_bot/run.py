#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio

from ebay_monitor.monitor import run_monitor


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor eBay UK listings and alert on underpriced TCG deals.")
    parser.add_argument("--config", default="config.yml", help="Path to config YAML.")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts instead of sending Telegram messages.")
    args = parser.parse_args()

    asyncio.run(run_monitor(args.config, once=args.once, dry_run_override=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

