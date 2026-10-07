# Virtual football prediction system

The predictor targets Bet9ja/Sportradar Bundesliga, Euro League, and English
League matches in three markets: Over 1.5, Under 3.5, and 12 (no draw).

## Train and backtest

```powershell
python analyze_virtual_league_hit_rates.py
python virtual_prediction_system.py
python forward_validation.py
python -m unittest test_virtual_prediction_system.py
```

Training is chronological: feature values for a match are calculated before
that match updates team, H2H, table, and league state. The oldest 75% trains
the model and the newest 25% is held out.

Features include league prior, rolling points per game, rolling goals for and
against, draw tendency, market-specific team form, last-ten H2H behavior,
current-season table PPG difference, and Elo strength.

`build_combination()` accepts candidate dictionaries containing `match_id`,
`market`, `probability`, and decimal `odds`. It selects no more than one leg
per match, multiplies decimal odds, and reports an approximate joint
probability. Use predictions as probability estimates, not guarantees.

`forward_validation.py` performs expanding-window validation. It begins with
12,000 training matches, predicts the next 6,000 without seeing their results,
then expands training and repeats. Every prediction is saved for auditing.

## Live shadow test

1. Copy `.env.example` to `.env` and set the bot token.
2. Send `/start` to the Telegram bot. If `TELEGRAM_CHAT_ID` is blank, the
   service discovers the most recent chat automatically.
3. Run `python live_runner.py`.

Telegram access is restricted by `TELEGRAM_ADMIN_USER_IDS`. An administrator
is subscribed automatically. A new user subscribes with `/start <invite-code>`;
each configured invite code can be redeemed only once. `/stop` unsubscribes,
while `/subscribers` and `/remove <user-id>` are administrator-only commands.
Rotate `TELEGRAM_INVITE_CODE` after issuing another invitation.

Predictions and settlements are stored in `data/shadow_test.sqlite3`. The
runner never places a wager. It ignores any result field attached to a match
whose official status is not `Ended`.

The deployed conservative shadow strategy creates a ticket only when two or
three distinct-match legs reach total decimal odds of at least 2.00. It applies
market-specific probability thresholds, a stricter Euro League threshold, and
a minimum joint-probability rule. The optimizer selects the eligible ticket
with the highest model joint probability. Results are tagged by strategy so
the previous 10-odds experiment cannot contaminate the new forward test.

`retrain_live_models.py` trains from settled live observations using a strict
chronological 75/25 holdout. Markets must pass minimum sample, hit-rate,
calibration, and Brier-score checks. New strategy tickets are recorded silently
until at least `FORWARD_GATE_MIN_TICKETS` settle with positive flat-stake ROI.

## OVH Ubuntu deployment

```bash
sudo useradd --system --home /opt/virtual-prediction --shell /usr/sbin/nologin virtualpredict
sudo mkdir -p /opt/virtual-prediction/data
sudo chown -R virtualpredict:virtualpredict /opt/virtual-prediction
sudo -u virtualpredict python3 -m venv /opt/virtual-prediction/.venv
sudo -u virtualpredict /opt/virtual-prediction/.venv/bin/pip install -r /opt/virtual-prediction/requirements.txt
sudo cp deploy/virtual-prediction.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now virtual-prediction
sudo journalctl -u virtual-prediction -f
```

## Real football pipeline

The `real_football` package uses the official FootyStats API for fixtures,
statistics, H2H information and available odds. It polls every two hours by
default, estimates Over 1.5, Under 3.5 and 12 probabilities, requires a model
edge over implied probability, and sends only combinations within 1.50–1.55.
It reuses the existing authorized Telegram subscriber database.

WhoScored enrichment can be imported from licensed JSON exports or collected
through an explicitly authorized browser session. The collector stores monthly
fixtures/results and the page's embedded `matchCentreData` in SQLite. It uses
conservative request spacing and does not bypass access denials.

HTTP calls use a persistent `curl-cffi` session for connection reuse. An
optional Camoufox cookie manager can capture a user-authorized session locally:

```bash
pip install -r requirements-real-football.txt
python -m camoufox fetch
python -m real_football.cookie_manager https://www.whoscored.com/
python -m real_football.collect_whoscored --stage-id 12345 --month 202610
python -m real_football.cookie_manager https://footystats.org/
python -m real_football.collect_footystats_live --backend camoufox --headed
python -m real_football.collect_footystats_live --backend camoufox
python -m real_football.collect_footystats_live --backend camoufox --poll-seconds 60 --cycles 0
```

The first command runs visibly so the user can complete any consent or sign-in.
Subsequent collection reuses the persistent Camoufox profile and performs the
live-score request inside the same browser context. Cloudflare clearance
cookies are not copied into a different HTTP client.

The cookie manager is domain-restricted, stores files with owner-only
permissions, and is not used to evade access controls. Never commit its profile
or cookie files.
