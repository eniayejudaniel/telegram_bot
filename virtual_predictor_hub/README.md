# Virtual Predictor Hub

`virtual_predictor_hub` is the shared home for independent virtual-sports
bookmaker bots. Each bookmaker owns its prediction code, configuration,
runtime data, browser automation, tests, and deployment files.

## Structure

- `bet9ja_virtual_bot/` — complete Bet9ja predictor and betting integration.
- `bet9ja_virtual_bot/prediction_engine.py` — models, features, and combination optimizer.
- `bet9ja_virtual_bot/bet9ja_virtual_bot.py` — polling, alerts, and orchestration.
- `bet9ja_virtual_bot/bet9ja_virtual_betslip.py` — browser and betslip automation.

Future integrations should be siblings, for example
`virtual_predictor_hub/sportybet_virtual_bot/`, without importing private
Bet9ja modules or sharing bookmaker credentials.

The Bet9ja bot covers Bundesliga, Euro League, and English League with
`over_1_5`, `under_3_5`, and `12` selections. Every accumulator is restricted
to one league and different matches, with combined odds between 1.50 and 1.55.

## Local setup

```powershell
Set-Location virtual_predictor_hub\bet9ja_virtual_bot
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
playwright install chromium
Copy-Item .env.example .env
python -m unittest discover -s . -p "test_*.py"
python bet9ja_virtual_bot.py
```

Keep `.env`, browser profiles, diagnostics, screenshots, and SQLite databases
out of source control. Real submission requires both `DRY_RUN=false` and
`BET9JA_ALLOW_REAL_BET=true`.

## Model utilities

```powershell
python analyze_virtual_league_hit_rates.py
python prediction_engine.py
python forward_validation.py
python retrain_live_models.py
```

Training and forward validation are chronological. Match results never update
features before the corresponding prediction is produced.


