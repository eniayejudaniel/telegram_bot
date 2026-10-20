# Kronos EURUSD AI Paper-Trading System

## 1. Purpose and status

This project is a continuously running EURUSD research and paper-trading system. It downloads hourly market data, asks Kronos to generate possible 24-hour price paths, applies deterministic strategy and risk rules, records advisory reviews from DeepSeek and Jev, and reports the result through Telegram and a browser dashboard.

The system does **not** connect to a broker and cannot move real money. Profitability has not been established. The previous strategy version produced a negative one-year result; the current 100-path distribution gate requires a fresh backtest and forward-paper sample.

Production host:

- Server: OVH
- Application directory: `/opt/kronos-eurusd`
- Dashboard: `http://15.204.113.115:8000`
- Instrument: Yahoo Finance `EURUSD=X`
- Time frame: one hour
- Forecast horizon: 24 market hours
- Context: 512 completed candles
- Kronos samples: 100 paths

## 2. Design principles

The system separates responsibilities deliberately:

- **Kronos** generates probabilistic price paths.
- **Plain Python code** calculates indicators, entries, stops, targets, position size, P&L and hard limits.
- **DeepSeek** provides an advisory coherence review.
- **Jev** produces constrained, typed advisory decisions.
- **The owner** controls whether paper trading is enabled and whether AI may ever progress beyond shadow mode.

No model can modify the configured account limits, create a real order, increase position size after sizing, or remove a stop. A model failure is never interpreted as approval.

## 3. High-level architecture

```text
Yahoo Finance hourly EURUSD candles
                  |
                  v
       Data cleaning and freshness gate
                  |
                  v
       Latest 512 completed candles
                  |
                  v
     Kronos-mini: 100 x 24-hour paths
                  |
                  +---------------------> Mean path for dashboard
                  |
                  v
   Regime + ATR + deterministic direction gate
                  |
                  v
  Path agreement / target / stop / EV / dispersion gate
                  |
                  v
       LONG, SHORT, or FLAT candidate
                  |
                  v
       DeepSeek + Jev shadow reviews
                  |
                  v
     Persistent account-risk entry check
                  |
                  v
 Paper position + Telegram + audit log + dashboard
                  |
                  v
 Five-minute stop / target / time-exit monitoring
```

## 4. Repository map

| Path | Responsibility |
|---|---|
| `app.py` | Command-line interface and Flask routes |
| `monitor.py` | Lightweight five-minute monitor entry point |
| `eurusd_trader/config.py` | Environment configuration and validation |
| `eurusd_trader/data.py` | Yahoo Finance download, cleaning and FX timestamps |
| `eurusd_trader/forecast.py` | Kronos model loading and distribution forecast |
| `model/kronos.py` | Kronos implementation, extended to return raw samples |
| `eurusd_trader/strategy.py` | Regime, ATR, entry and 100-path rules |
| `eurusd_trader/ai.py` | OpenRouter/DeepSeek and Jev clients |
| `eurusd_trader/risk.py` | Account ledger, sizing and loss locks |
| `eurusd_trader/paper.py` | Paper-position lifecycle and exit accounting |
| `eurusd_trader/service.py` | End-to-end forecast and alert orchestration |
| `eurusd_trader/telegram.py` | Telegram message formatting and delivery |
| `eurusd_trader/backtest.py` | Causal one-year walk-forward test |
| `templates/index.html` | TradingView Lightweight Charts dashboard |
| `deploy/` | systemd service and timer definitions |
| `artifacts/` | Generated forecast, audit and backtest output; not committed |
| `state/` | Paper position and account ledger; not committed |

## 5. Market data

### Source

The current source is Yahoo Finance through `yfinance`, using symbol `EURUSD=X`. It is suitable for research and paper trading but is not broker-grade. Its candle construction, latency and occasional revisions may differ from a broker's executable feed.

### Cleaning

The downloader:

1. Converts columns to a consistent lowercase format.
2. Keeps timestamp, open, high, low and close.
3. Removes missing rows.
4. Removes duplicate timestamps.
5. Sorts chronologically.
6. Removes the currently forming candle.

Forecast timestamps skip the normal FX weekend closure from Friday 22:00 UTC through Sunday 21:00 UTC.

### Freshness gate

If the most recent completed candle is more than `MAX_DATA_AGE_MINUTES` old, the result is forced to `FLAT` with a `STALE_DATA` reason. The current default is 120 minutes.

During the weekend this may intentionally classify data as stale. No new position should be opened from an old Friday candle.

## 6. Kronos forecasting

The production model defaults to:

- Model: `NeoQuasar/Kronos-mini`
- Tokenizer: `NeoQuasar/Kronos-Tokenizer-2k`
- Maximum context: 512 candles
- Prediction length: 24 candles
- Samples: 100
- Temperature: 1.0
- Top-p: 0.9

Kronos generates an array shaped approximately as:

```text
[100 samples, 24 future candles, OHLC features]
```

The mean across samples becomes the yellow forecast line shown on the dashboard. Strategy qualification uses every individual path rather than only that mean.

The 100 paths are scenarios sampled from the model, not guarantees or calibrated market probabilities. Their statistics must be validated against observed outcomes.

## 7. Regime classification

The regime combines trend separation and recent volatility.

### Trend

- Fast exponential moving average: 24 candles
- Slow exponential moving average: 96 candles
- Separation is divided by 14-period ATR.
- Separation below `0.35 ATR` is classified as a range.
- Otherwise, fast above slow is a bull trend and fast below slow is a bear trend.

### Volatility

The system compares the latest 24-hour return volatility with the median 24-hour volatility from the last 240 observations.

- Above 1.5 times the baseline: `high-vol`
- Otherwise: `normal-vol`

Example labels:

- `bull-trend/normal-vol`
- `bear-trend/high-vol`
- `range/normal-vol`

## 8. Signal construction

### Initial direction gate

A long candidate requires:

1. The mean forecast's final close is above entry.
2. Its predicted upward excursion reaches at least `MIN_EDGE_ATR × ATR`.
3. The historical regime is not a bear trend.

A short uses the opposite rules. If neither qualifies, the signal is `FLAT`.

The current minimum edge is `0.35 ATR`.

### Stop

Normal-volatility stop distance:

```text
1.25 × ATR
```

High-volatility stop distance:

```text
1.25 × ATR × 1.20 = 1.50 × ATR
```

### Target

The target distance is the larger of:

```text
1.25 × stop distance
```

or:

```text
80% of the maximum favorable excursion in the mean forecast
```

### Time exit

Every position expires after 24 hourly candles, even if neither target nor stop is reached.

## 9. Distribution gate

For an initial long or short, each of the 100 paths is evaluated against the proposed stop and target.

If one forecast candle touches both stop and target, the stop is counted first. This is deliberately conservative because hourly OHLC data cannot show the intrabar order.

The metrics are:

- `path_agreement`: fraction of paths ending in the proposed direction.
- `target_probability`: fraction hitting target before stop.
- `stop_probability`: fraction hitting stop before target.
- `neither_probability`: fraction reaching neither threshold.
- `expected_terminal_pips`: mean directional terminal move after estimated costs.
- `median_terminal_pips`: median terminal move after costs.
- `terminal_q10_pips` and `terminal_q90_pips`: forecast range percentiles.
- `dispersion_atr`: standard deviation of terminal prices divided by ATR.

Current acceptance thresholds:

| Rule | Threshold |
|---|---:|
| Directional path agreement | At least 55% |
| Target-before-stop probability | At least 45% |
| Expected terminal value | At least 0 pips after costs |
| Terminal dispersion | No more than 2.5 ATR |

Failure of any rule converts the candidate to `FLAT`. These thresholds are hypotheses and should be changed only after out-of-sample sensitivity testing.

## 10. AI review layer

### Operating mode

The default is:

```text
AI_ENFORCEMENT=shadow
```

In shadow mode, the AI results are logged and may appear in Telegram, but cannot change the final quantitative candidate or paper-position state.

`AI_ENFORCEMENT=deepseek_veto` exists, but should not be enabled until shadow outcomes demonstrate that the veto improves unseen performance. Even in that mode, DeepSeek may only reject; it cannot create or reverse a trade.

AI calls are skipped for `FLAT` candidates.

### DeepSeek through OpenRouter

Default model:

```text
deepseek/deepseek-v4.1-flash-1017
```

DeepSeek receives a compact candidate state and returns:

- `allow_new_trade`
- `event_risk`
- `regime`
- `size_multiplier`
- `reason_code`
- `summary`

Despite the `event_risk` field name, the current implementation does **not** provide a verified live news or economic-calendar feed. DeepSeek is instructed not to claim news access. The value is therefore an evidence/uncertainty review, not a dependable macro-event detector.

On timeout, malformed JSON or API failure, the result is `DEEPSEEK_UNAVAILABLE`, `allow_new_trade=false`, and maximum risk. In shadow mode this is logged but does not block the deterministic strategy.

### Jev

Jev receives the candidate plus the DeepSeek result. It currently answers:

- Choice: `ENTER_LONG`, `ENTER_SHORT`, or `HOLD`
- Noul: probability that sufficient evidence exists to allow the trade

Keys beginning with `jv_live_` use the hosted Jev endpoint. Other keys use the official TypeSafe `systemone` endpoint. The model defaults to `jev-latest`.

Jev's direction is advisory. It cannot change a long candidate into a short order or vice versa. A separate setup-quality `Score` question is a planned enhancement and is not yet implemented.

## 11. Hard paper-risk engine

### Account configuration

| Control | Current value |
|---|---:|
| Starting paper balance | $25,000 |
| Risk per trade | 1% of current balance |
| Initial trade-risk budget | $250 |
| Daily realized-loss limit | 3% of starting balance = $750 |
| Maximum drawdown | 6% of equity high-water mark = initially $1,500 |

### Position sizing

For EURUSD, the quote currency is USD, so approximate dollar P&L equals units multiplied by the price change.

```text
cost_distance = (spread_pips + 2 × slippage_pips) × 0.0001
units = risk_budget_usd / (absolute(entry - stop) + cost_distance)
standard_lots = units / 100,000
```

Including estimated round-trip costs inside the denominator keeps a modeled stop-out within the risk budget rather than adding costs on top of it.

The default cost assumption is:

- Spread: 0.8 pip
- Slippage: 0.2 pip per side
- Total modeled round-trip cost: 1.2 pips

### Daily loss lock

Closed-trade net P&L is accumulated by UTC date. At or below `-$750`, new entries are disabled. A daily-loss lock automatically clears on the next UTC date if environment-level trading remains enabled.

Unrealized losses are not currently included in the daily limit because only one position is allowed and it has a hard stop. This remains a limitation.

### Drawdown lock

The ledger maintains the highest closed-equity balance. Drawdown is:

```text
high_water_mark - current_balance
```

At 6% or more, entries are disabled. Unlike a daily lock, a drawdown lock does not automatically disappear with a new day. `resume` rechecks the boundary and cannot bypass it.

### Entry-order protections

Before a paper position is registered:

1. Signal must be LONG or SHORT.
2. No other paper position may be open.
3. Environment-level trading must be enabled.
4. Persistent ledger must be enabled.
5. Daily loss must be below its limit.
6. Drawdown must be below its limit.
7. Stop distance must be positive.
8. Position size is calculated and stored.

## 12. Paper-position lifecycle

An accepted position records:

- Direction and regime
- Entry, stop and target
- Open and expiration timestamps
- Risk budget
- Units and standard lots
- Stop distance in pips
- Estimated transaction cost

Every five minutes the monitor downloads completed five-minute bars since entry. For every bar it checks, in order:

1. Stop loss
2. Target
3. 24-hour expiration

Stop is checked first when both boundaries appear in one candle.

At exit, the engine calculates:

```text
gross P&L = direction × (exit - entry) × units
cost = 1.2 pips × units
net P&L = gross P&L - cost
```

The balance, daily realized P&L, high-water mark and closed-trade count are then updated atomically using a temporary file replacement.

## 13. Telegram behavior

The hourly forecast alert contains:

- LONG, SHORT or FLAT
- Regime
- Entry, stop and target
- Time exit
- Path agreement
- Target and stop probabilities
- Expected terminal move
- DeepSeek and Jev opinion when available
- Whether the paper position was opened or rejected
- Position units, lots and dollar risk for an opened position

The exit alert contains:

- Exit reason: target, stop, time exit or kill switch
- Entry and exit prices
- Signed result in pips
- Net dollar P&L
- Updated paper balance

The bot currently sends outbound alerts only. Telegram `/pause`, `/resume`, `/status` and `/kill` commands are **not yet implemented**; use the server commands below.

## 14. Dashboard and API

The dashboard uses TradingView Lightweight Charts and displays:

- Last 512 historical candles
- Mean of the 100 Kronos paths for the next 24 hours
- Current signal, regime, entry, stop, target and time exit

Routes:

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Browser dashboard |
| GET | `/api/latest` | Latest cached forecast, or generate if missing |
| POST | `/api/refresh` | Generate a fresh forecast |
| GET | `/api/risk` | Paper balance and risk status |

The current HTTP service has no authentication and is exposed on port 8000. Do not place secrets in API output. Before real-money use, add TLS, authentication, firewall restrictions and rate limiting. The refresh route can trigger expensive model inference and should not remain publicly writable in a hardened deployment.

## 15. Persistent files

### `state/risk.json`

Stores:

- Starting and current balance
- High-water mark
- UTC daily P&L date and value
- Trading-enabled state
- Lock reason
- Closed-trade count

### `state/paper_position.json`

Stores the current or most recently closed paper position and its accounting.

### `artifacts/latest.json`

Stores chart history, mean forecast, signal and AI review for the latest snapshot.

### `artifacts/decision_audit.jsonl`

Append-only JSON Lines record of timestamp, signal and AI result.

These directories are excluded from Git because they contain runtime state. Back them up before moving servers. Never delete an open-position state file while the monitor is active.

## 16. Backtesting

Run:

```bash
.venv/bin/python app.py backtest --stride 120
```

The backtest:

- Downloads approximately two years so a 512-candle context exists.
- Evaluates origins in the trailing year.
- Gives Kronos only candles available before each origin.
- Forecasts the next 24 candles.
- Applies the regime and 100-path strategy.
- Charges spread and slippage.
- Uses stop-first handling for ambiguous candles.
- Saves checkpoints so long runs can resume.

Stride controls forecast frequency:

- `120`: approximately weekly; quicker baseline
- `24`: approximately daily; much slower
- `1`: every eligible hour; computationally expensive

The current backtest reports fractional returns per trade. It does not yet simulate the new $25,000 account ledger, dynamic 1% sizing, daily-loss lock or 6% account lock. Therefore the next risk-adjusted report should extend the backtest before it is treated as production-equivalent.

DeepSeek and Jev are intentionally excluded from historical backtests. Calling a present-day language model with current knowledge for old decisions could create information leakage. AI value must first be measured with contemporaneously logged shadow decisions.

## 17. Installation

### Windows development

```powershell
cd C:\Users\USER\telegram_bot\kronos_eurusd
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Add secrets to `.env`; never commit that file.

### Required secrets

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `OPENROUTER_API_KEY` for DeepSeek review
- `JEV_API_KEY` for Jev review

The first Kronos run downloads public model files from Hugging Face.

## 18. Environment reference

```dotenv
SYMBOL=EURUSD=X
INTERVAL=1h
LOOKBACK=512
PRED_LEN=24
SAMPLE_COUNT=100

MODEL_ID=NeoQuasar/Kronos-mini
TOKENIZER_ID=NeoQuasar/Kronos-Tokenizer-2k
DEVICE=auto
TEMPERATURE=1.0
TOP_P=0.9

MIN_EDGE_ATR=0.35
STOP_ATR=1.25
MIN_RISK_REWARD=1.25
MIN_PATH_AGREEMENT=0.55
MIN_TARGET_PROBABILITY=0.45
MIN_EXPECTED_PIPS=0.0
MAX_DISPERSION_ATR=2.5

SPREAD_PIPS=0.8
SLIPPAGE_PIPS=0.2
MAX_DATA_AGE_MINUTES=120

PAPER_BALANCE=25000
RISK_PER_TRADE=0.01
DAILY_LOSS_LIMIT=0.03
MAX_DRAWDOWN=0.06
TRADING_ENABLED=true

AI_ENABLED=true
AI_ENFORCEMENT=shadow
OPENROUTER_MODEL=deepseek/deepseek-v4.1-flash-1017
JEV_MODEL=jev-latest

ALERT_INTERVAL_SECONDS=3600
```

`LOOKBACK`, `PRED_LEN` and `SAMPLE_COUNT` are validated as 512, 24 and 100 for this strategy.

## 19. Local commands

```powershell
# Generate and cache without Telegram
python app.py forecast

# Generate, apply paper-entry checks, and alert Telegram
python app.py forecast --alert

# Check open position immediately
python app.py monitor

# Account and lock status
python app.py risk-status

# Block entries without closing a position
python app.py pause

# Re-enable only if hard limits permit
python app.py resume

# Block entries and close an open paper position at latest 5m close
python app.py kill

# Run dashboard locally
python app.py serve --host 127.0.0.1 --port 8000
```

## 20. OVH operations

Connect:

```powershell
ssh -i "$env:USERPROFILE\.ssh\ovh_yc_bot" ubuntu@15.204.113.115
```

Then:

```bash
cd /opt/kronos-eurusd

# Inspect risk
.venv/bin/python app.py risk-status

# Inspect timers and web service
systemctl status kronos-eurusd-alert.timer
systemctl status kronos-eurusd-monitor.timer
systemctl status kronos-eurusd-web.service

# Recent logs
journalctl -u kronos-eurusd-alert.service -n 100 --no-pager
journalctl -u kronos-eurusd-monitor.service -n 100 --no-pager
journalctl -u kronos-eurusd-web.service -n 100 --no-pager

# Restart dashboard after deployment
sudo systemctl restart kronos-eurusd-web.service

# Enable timers after maintenance
sudo systemctl enable --now kronos-eurusd-alert.timer
sudo systemctl enable --now kronos-eurusd-monitor.timer

# Temporarily stop scheduled work
sudo systemctl stop kronos-eurusd-alert.timer
sudo systemctl stop kronos-eurusd-monitor.timer
```

### Production schedule

- Forecast: hourly with up to 120 seconds randomized delay
- Position monitor: every five minutes with up to 20 seconds randomized delay
- Dashboard: continuously served by Gunicorn on port 8000

## 21. Pause, resume and kill semantics

### Pause

`pause` writes `MANUAL_PAUSE` to the persistent ledger. Forecasts and alerts may continue, but candidate positions are rejected.

### Resume

`resume` sets the ledger to enabled and immediately checks daily loss and drawdown. It cannot override a breached hard limit. Environment variable `TRADING_ENABLED=false` also prevents resumption.

### Kill

`kill` first blocks new entries. If a paper position exists, it downloads the latest completed five-minute candle, closes at its close price, charges modeled costs, updates the ledger and sends an exit alert.

This is a paper kill switch. It has no broker connection and therefore cannot close a real external position.

## 22. Testing

Focused tests:

```powershell
python -m pytest -q --basetemp tests\.pytest_tmp `
  tests\test_eurusd_trader.py `
  tests\test_ai_strategy.py `
  tests\test_fx_hp_backtest.py `
  tests\test_risk.py
```

The current focused suite covers:

- Weekend timestamp handling
- Entry/stop/target construction
- Readable regimes
- Exit-message pip signs
- Distribution acceptance
- DeepSeek/Jev response parsing
- Strategy research utilities
- $250 initial risk budget
- Cost-inclusive position sizing
- Manual pause
- Position persistence
- Daily loss lock
- Drawdown lock

The upstream Kronos regression suite is separate and may download larger model checkpoints.

## 23. Failure behavior

| Failure | Behavior |
|---|---|
| Yahoo returns no data | Forecast/monitor command fails and systemd records the error |
| Latest candle too old | FLAT with `STALE_DATA` |
| Fewer than 512 candles | Forecast raises an explicit error |
| OpenRouter missing/fails | DeepSeek returns conservative error result |
| Jev missing/fails | Jev returns `HOLD` and disallows trade advisory |
| AI fails in shadow mode | Deterministic strategy remains unchanged; failure is logged |
| Position already open | New paper position rejected |
| Daily loss reached | Persistent entry lock |
| Drawdown reached | Persistent entry lock |
| Telegram delivery fails | Command fails and systemd logs it; state may already be written |
| Server restarts | JSON ledger and position state survive |

An important operational detail: position state is written before the opening Telegram message is delivered. A Telegram outage can therefore leave a valid paper position without an entry message. Always check `app.py risk-status` and `state/paper_position.json` after notification failures.

## 24. Security

- Keep `.env` readable only by the deployment user.
- Never paste API keys into logs, screenshots or documentation.
- Rotate any key that may have been exposed.
- Restrict SSH to key authentication.
- Put the dashboard behind a firewall or authenticated reverse proxy.
- Add HTTPS before exposing account-like information publicly.
- Do not expose the Telegram token in client-side JavaScript.
- Back up runtime state securely.
- Do not add broker credentials until an explicit live-trading design and approval process exists.

## 25. Known limitations

1. Yahoo Finance is not an execution-quality FX feed.
2. The system uses one instrument and one timeframe.
3. Forecast probabilities are model-sample frequencies, not proven calibrated probabilities.
4. AI reviews have no verified point-in-time news/calendar input.
5. AI remains unvalidated shadow advice.
6. The backtest does not yet mirror dynamic account sizing and loss locks.
7. Paper fills assume stop/target prices without gap or liquidity modeling beyond fixed costs.
8. The five-minute monitor can react several minutes after a boundary is crossed.
9. Runtime persistence uses JSON files rather than a transactional database.
10. Telegram control commands are not implemented.
11. Dashboard/API authentication is not implemented.
12. There is no broker adapter and no real emergency liquidation capability.

## 26. Promotion policy

Do not enable AI veto enforcement or real-money execution merely because a short test is profitable.

Recommended evidence sequence:

1. Complete a production-matching one-year walk-forward backtest.
2. Run threshold sensitivity tests instead of selecting one lucky parameter set.
3. Evaluate performance across bull, bear, range, high-volatility and normal-volatility periods.
4. Stress spread and slippage assumptions.
5. Collect at least 100 eligible AI shadow candidates with eventual outcomes.
6. Compare Kronos-only decisions against hypothetical DeepSeek/Jev filtering.
7. Confirm improvement on data not used to select thresholds.
8. Continue paper trading through a meaningful live period.
9. Require explicit owner approval for every promotion.

Any later real-money version should start with smaller risk than the paper configuration, broker-side stops, authenticated controls, idempotent order handling, reconciliation, market-session safeguards and independent monitoring.

## 27. Recommended next version

Priority order:

1. Extend the backtest with dynamic account equity, 1% sizing and loss locks.
2. Move audit, positions and ledger data to SQLite with migrations.
3. Add Jev setup-quality `Score` while keeping it shadow-only.
4. Add decision-to-outcome joins and AI calibration reports.
5. Add authenticated Telegram `/status`, `/pause`, `/resume` and `/kill` commands.
6. Protect the dashboard with HTTPS and authentication.
7. Add provider retries, circuit breakers and health metrics.
8. Replace or validate Yahoo data against the intended broker feed.
9. Add an economic-calendar source with point-in-time storage.
10. Add automated backup and restore tests for runtime state.

---

This system is experimental research software, not financial advice. A backtest or paper result cannot guarantee future performance.
