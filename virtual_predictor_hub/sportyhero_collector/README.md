# SportyHero Analytics Pipeline

An end-to-end, read-only data collection and model-validation pipeline for
SportyBet's SportyHero multiplier game. The project captures completed rounds
from the browser, stores validated observations in SQLite, trains models with
chronological evaluation, and measures predictions through paper-only forward
testing.

## Engineering highlights

- Playwright collector reads completed rounds from the application's Vuex state.
- Validated, conflict-safe SQLite writes preserve round integrity.
- Time-ordered feature generation avoids training on future observations.
- Candidate models are evaluated using Brier score and calibration metrics.
- Fail-closed promotion gates keep underperforming models out of forward tests.
- Paper ledgers track predictions, settlements, and simulated bankroll changes.
- Hardened systemd services support unattended collection and evaluation.

## Architecture

```text
SportyHero browser
       |
       v
  collector.py ---> SQLite round store
                         |
                         v
              continuous_learning.py
                         |
                         v
                  model registry
                    /         \
                   v           v
      forward_predictor.py  sequence_signal.py
```

The modeling and evaluation path is intentionally separated from collection,
so each stage can fail safely without corrupting stored rounds or producing an
unverified signal.

## Components

- `collector.py` — Playwright-based, read-only round collector.
- `storage.py` — schema management and conflict-safe SQLite persistence.
- `modeling.py` — chronological features and regularized logistic modeling.
- `continuous_learning.py` — candidate training, evaluation, and promotion.
- `forward_predictor.py` — pre-result paper predictions and settlement ledger.
- `sequence_signal.py` — optional paper-bankroll sequence tracking.
- `deploy/` — hardened systemd service definitions.
- `tests/` — automated coverage for storage, features, promotion, and settlement.

## Dataset contract

Each completed round is identified by its round number and stored with its
multiplier and collection timestamp. Inserts are idempotent, while conflicting
values for an existing round are rejected for investigation.

A development snapshot contained 19,591 contiguous rounds, from round
`7260284` through `7279874`. Database files are excluded from Git and should be
transferred separately to `data/sportyhero.sqlite3` when required.

## Quick start

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python collector.py --db data/sportyhero.sqlite3 --interval 1
```

The collector uses a persistent browser profile. A visible browser session may
be needed for the initial sign-in or to open the game. The website's internal
state is undocumented and may change, so collector health should be monitored.

## Model validation

Training and evaluation use chronological splits rather than random shuffling.
A candidate is eligible for promotion only when it improves on the base-rate
forecast and remains within the configured calibration limits. Promotion fails
closed when the dataset is insufficient or a validation gate is not satisfied.

## Tests

From this directory, run:

```powershell
python -m unittest discover -s tests -v
python -m py_compile *.py
```

## Responsible scope

This repository does not place bets, access a wallet, or initiate cash-outs.
All predictions and bankroll calculations are simulations. Multiplier games are
designed around random outcomes; historical performance does not guarantee a
profitable edge or future results.
