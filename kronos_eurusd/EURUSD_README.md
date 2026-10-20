# Kronos EURUSD paper trader

Full operator, architecture, risk and deployment documentation is available in [`docs/SYSTEM_DOCUMENTATION.md`](docs/SYSTEM_DOCUMENTATION.md).

This extension downloads completed EURUSD hourly candles and passes the latest 512 OHLC candles to Kronos-mini. It retains all 100 sampled 24-candle paths, charts their mean forecast, and uses the full distribution to gate entry/stop/target/time-exit rules. It also includes a causal one-year walk-forward backtest, a TradingView Lightweight Charts dashboard, and Telegram paper alerts. The mini checkpoint is the practical default for CPU paper trading; set the model/tokenizer IDs in `.env` to use small or base on a GPU.

## Setup

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. The first run downloads the public Kronos model weights from Hugging Face.

Optional AI review uses `OPENROUTER_API_KEY` with the configured DeepSeek model and `JEV_API_KEY` with Jev. Keep `AI_ENFORCEMENT=shadow` while collecting forward-test evidence: AI decisions are logged and shown in Telegram, but cannot create/reverse trades or alter hard stops. `deepseek_veto` may be enabled later only after the shadow audit demonstrates value. Historical backtests do not call either API, avoiding current-information leakage into old decisions.

```powershell
python app.py forecast
python app.py forecast --alert
python app.py backtest --stride 120
python app.py risk-status
python app.py pause
python app.py resume
python app.py kill
python app.py serve --host 0.0.0.0 --port 8000 --alerts
```

Open http://localhost:8000. `--alerts` sends one paper alert when a new completed hourly candle appears. It never places an order.

On deployment, an hourly timer generates entry signals and a separate five-minute timer checks the open paper position. Telegram receives a closing alert for `TARGET HIT`, `STOP LOSS`, or `24H TIME EXIT`, including the result in pips. Only one paper position is tracked at a time.

The default backtest evaluates roughly one forecast per trading week over the entire trailing year. Set `--stride 24` for daily origins or `--stride 1` for every hourly origin; with 100 samples these denser runs are computationally expensive. Results are saved to `artifacts/backtest.json`; the latest dashboard snapshot is `artifacts/latest.json`.

Risk model: a trade is allowed only when the predicted terminal direction and maximum excursion clear 0.35 ATR and do not conflict with the EMA regime. The 100-path gate then requires sufficient directional agreement, target-before-stop probability, expected terminal value after costs, and bounded dispersion. Stops begin at 1.25 ATR (20% wider in high volatility), targets are at least 1.25R, and open trades exit after 24 candles. The backtest charges configured spread and slippage and assumes the stop fills first if stop and target touch in the same candle.

Paper risk starts at $25,000 and risks 1% of current balance per accepted position. New entries lock after a 3% daily realized loss or a 6% drawdown from the paper equity high-water mark. Position size is calculated from the fixed dollar risk and stop distance. Data older than 120 minutes produces `FLAT / STALE_DATA`. `pause` blocks new positions, `resume` rechecks hard limits, and `kill` blocks entries and closes an open paper position at the latest available five-minute close.

This is research software and paper trading only, not financial advice. Yahoo Finance's FX feed is convenient but not broker-grade; validate against the intended broker before any live use.
