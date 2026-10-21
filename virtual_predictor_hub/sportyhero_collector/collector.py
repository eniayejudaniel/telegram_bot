"""Read-only SportyHero multiplier collector.

The collector observes the Vuex round-history buffer. It never calls betting,
order, wallet, cash-out, or account endpoints.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

from playwright.sync_api import Frame, Page, sync_playwright

from storage import connect, insert_rounds


ROOT = Path(__file__).resolve().parent
DEFAULT_URL = "https://www.sportybet.com/ng/games?source=TopRibbon"

READ_BUFFER_JS = """
() => {
  const elements = Array.from(document.querySelectorAll('*'));
  let store = null;
  let router = null;
  for (const element of elements) {
    const app = element.__vue_app__;
    const globals = app && app.config && app.config.globalProperties;
    if (globals && globals.$store) {
      store = globals.$store;
      router = globals.$router;
      break;
    }
    if (element.__vue__ && element.__vue__.$store) {
      store = element.__vue__.$store;
      router = element.__vue__.$router;
      break;
    }
  }
  const hero = store && store.state && store.state.sportyHero;
  if (!hero) {
    if (router) {
      try { router.push('/ng/sportygames/sporty-hero'); } catch (_) {}
    }
    return [];
  }
  const values = hero.pastMultipliers && hero.pastMultipliers.coefficients;
  if (!Array.isArray(values)) return [];
  return values.map(value => ({
    id: value.id,
    houseCoefficient: value.houseCoefficient,
    houseCoefficientStr: value.houseCoefficientStr,
    createdAt: value.createdAt
  }));
}
"""


def read_page(page: Page) -> list[dict]:
    frames: list[Frame] = list(page.frames)
    frames.sort(key=lambda frame: "sportygames" not in frame.url)
    for frame in frames:
        try:
            values = frame.evaluate(READ_BUFFER_JS)
        except Exception:
            continue
        if values:
            return values
    return []


def run(args: argparse.Namespace) -> None:
    db = connect(args.db)
    profile = Path(args.profile)
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile), headless=args.headless,
            args=["--disable-background-timer-throttling"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
        while True:
            try:
                inserted = insert_rounds(db, read_page(page), is_new=True)
                if inserted:
                    latest = db.execute("SELECT MAX(round_id) FROM multipliers").fetchone()[0]
                    logging.info("captured=%s latest_round=%s", inserted, latest)
            except Exception:
                logging.exception("collector cycle failed")
            if args.once:
                break
            time.sleep(args.interval)
        context.close()
    db.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--db", default=str(ROOT / "data" / "sportyhero.sqlite3"))
    result.add_argument("--profile", default=os.getenv(
        "SPORTYHERO_PROFILE", str(ROOT / "data" / "browser-profile")
    ))
    result.add_argument("--url", default=os.getenv("SPORTYHERO_URL", DEFAULT_URL))
    result.add_argument("--interval", type=float, default=1.0)
    result.add_argument("--once", action="store_true")
    result.add_argument("--headless", action="store_true")
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(parser().parse_args())

