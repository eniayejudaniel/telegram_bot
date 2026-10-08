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
