# SportyHero collector — reconstructed

This is an evidence-driven reconstruction of the deleted SportyHero project.
It is not a byte-for-byte recovery of the original untracked source.

The collector observes completed multiplier rounds from SportyBet's in-page
Vuex state and stores them in SQLite. The modeling components are strictly
paper-only: they cannot place a bet, access a wallet, or cash out.

## Recovered evidence

- Three surviving SQLite snapshots contained one `multipliers` table.
- The newest snapshot contains 19,591 contiguous rounds from `7260284` through
  `7279874`.
- Recovered systemd units established the original command-line contracts for
  `forward_predictor.py`, `continuous_learning.py`, and `sequence_signal.py`.
- The stored schema is reproduced exactly in `storage.py`.

The database snapshots are intentionally excluded from Git. They may contain
valuable research data and should be transferred separately into
`data/sportyhero.sqlite3`.

## Components

- `collector.py` — Playwright-based, read-only round collector.
- `storage.py` — validated, conflict-safe SQLite persistence.
- `modeling.py` — chronological features and regularized logistic model.
- `continuous_learning.py` — candidate training with Brier and calibration
  gates; promotion fails closed.
- `forward_predictor.py` — pre-result paper prediction and settlement ledger.
- `sequence_signal.py` — optional paper-bankroll sequence ledger.
- `deploy/` — reconstructed hardened systemd units.

## Local validation

```powershell
python -m unittest discover -s tests -v
python -m py_compile *.py
```

## Collection

Install Playwright and Chromium, then run:

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python collector.py --db data/sportyhero.sqlite3 --interval 1
```

The browser profile is persistent. A visible browser may be required to sign in
or open the game once. The game route and frontend internals are undocumented
and may change.

## Research limitation

Crash outcomes are designed to be random. Historical patterns do not establish
a profitable edge. A model is promoted only when chronological holdout Brier
score beats the base-rate forecast and calibration remains acceptable. Even a
promoted model remains paper-only.

