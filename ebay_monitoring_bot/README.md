# eBay Monitoring Bot

Monitors eBay UK and eBay US for Pokemon and One Piece listings, builds marketplace-specific active-listing medians, and sends Telegram alerts when listings look underpriced.

This version fetches eBay directly:

- `curl_cffi` is the primary high-volume fetcher.
- Camoufox is the optional browser session bootstrap/repair layer.
- The bot warms eBay's homepage first, reuses cookies, and persists a local session identity.

## What It Does

- Polls eBay UK and eBay US every 10 minutes by default.
- Uses editable keywords from `config.yml`.
- Covers Buy It Now and auction search pages.
- Parses sealed Pokemon / One Piece product types, Pokemon Center ETBs, booster boxes, booster bundles, tins, collections, loose packs, and raw/graded cards.
- Detects mixed-lot listings and values each parsed component separately.
- Builds active-listing median baselines per strict product key.
- Requires 5+ samples for a reliable baseline; fewer samples are flagged as low confidence.
- Alerts via Telegram and dedupes by listing ID unless the price drops.
- Stores seen listings, snapshots, and baseline history in SQLite.

## Setup

```powershell
cd C:\Users\USER\telegram_bot\ebay_monitoring_bot
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env`:

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
EBAY_PROXY_URL=
DISCORD_NEW_USER_WEBHOOK_URL=...
```

Edit `config.yml` to change keywords, polling interval, baseline threshold, sample size, and parser assumptions.

If you want Camoufox refresh enabled on the VPS, install/fetch its browser once:

```powershell
python -m camoufox fetch
```

Keep `EBAY_PROXY_URL` pinned to the same identity if you use a proxy. Do not acquire cookies on one IP and reuse them on another.

## Run Once

```powershell
python run.py --once --dry-run
```

This prints qualifying alerts, rather than every fetched listing. To test one search page and print
the parsed listings directly in the terminal:

```powershell
python test_fetcher.py "one piece booster box" --no-proxy
```

Omit `--no-proxy` when the proxy configured in `.env` is available.

## Run Continuously

```powershell
python run.py
```

Continuous mode also runs the Telegram preference interface. Users can send:

- `/start` or `/keywords` to choose marketplaces and alert keywords.
- `/status` to view saved alerts.
- `/stop` to pause delivery without deleting selections.
- `/help` to view commands.

Marketplace and keyword selections are committed when the user presses **Save alerts**, stored in SQLite, and checked every
`poll_interval_seconds` (10 minutes by default). The monitor searches the union of active user
keywords and routes each matching listing only to subscribed chats. Alert history and the
per-cycle alert cap are maintained separately for every user.

## Alert Logic

For simple listings, the bot uses them as baseline samples only when one confident product component is parsed.

For mixed lots, it:

1. Parses each component from title text.
2. Looks up each component's current active median.
3. Sums component values.
4. Compares that total against item price plus postage.
5. Alerts when the listing is at least `alert_threshold_below_median` below the summed baseline.

Listings with uncertain contents can still alert, but they are flagged as `contents uncertain`.

## Deployment Notes

Run this bot as its own service on the VPS, separate from the YC bot. Keep its `.env` and SQLite database out of Git.

Example service name:

```text
ebay-monitoring-bot.service
```
