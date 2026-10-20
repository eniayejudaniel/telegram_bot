"""Causal backtest of agents.surf #52: FX Moving Averages with HP Filter."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf


def causal_hp_endpoint(values: np.ndarray, smoothing: float) -> float:
    """Last point of a one-sided HP/Whittaker fit ending at the decision date."""
    n = len(values)
    d = np.zeros((n - 2, n))
    rows = np.arange(n - 2)
    d[rows, rows] = 1.0
    d[rows, rows + 1] = -2.0
    d[rows, rows + 2] = 1.0
    trend = np.linalg.solve(np.eye(n) + smoothing * d.T @ d, values)
    return float(trend[-1])


def load_monthly(symbol: str, start: str) -> pd.Series:
    raw = yf.download(symbol, start=start, auto_adjust=False, progress=False)
    if raw.empty:
        raise RuntimeError(f"No Yahoo Finance data returned for {symbol}")
    close = raw["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index, utc=True)
    return close.resample("ME").last().dropna().rename("close")


def build_returns(close: pd.Series, hp_months: int = 60, fast: int = 3,
                  slow: int = 12, smoothing: float = 14_400.0,
                  round_trip_pips: float = 1.2) -> pd.DataFrame:
    causal = pd.Series(np.nan, index=close.index, name="causal_hp")
    for end in range(hp_months - 1, len(close)):
        window = close.iloc[end - hp_months + 1:end + 1].to_numpy(float)
        causal.iloc[end] = causal_hp_endpoint(window, smoothing)

    fast_ma = causal.rolling(fast).mean()
    slow_ma = causal.rolling(slow).mean()
    signal = pd.Series(np.where(fast_ma > slow_ma, 1,
                       np.where(fast_ma < slow_ma, -1, 0)), index=close.index)
    # Signal at month t earns the return from t to t+1. No same-close return.
    forward_return = close.shift(-1) / close - 1
    turnover = signal.diff().abs()
    turnover.iloc[signal.first_valid_index() == signal.index] = np.nan
    if len(turnover):
        first_active = np.flatnonzero(signal.to_numpy() != 0)
        if len(first_active):
            turnover.iloc[first_active[0]] = 1.0
    cost_return = turnover.fillna(0) * (round_trip_pips * 0.0001 / close)
    strategy_return = signal * forward_return - cost_return
    return pd.DataFrame({
        "close": close, "causal_hp": causal, "fast_ma": fast_ma, "slow_ma": slow_ma,
        "position": signal, "forward_return": forward_return,
        "turnover": turnover.fillna(0), "strategy_return": strategy_return,
    }).dropna(subset=["strategy_return", "slow_ma"])


def metrics(frame: pd.DataFrame) -> dict:
    r = frame.strategy_return.astype(float)
    equity = (1 + r).cumprod()
    drawdown = equity / equity.cummax() - 1
    years = len(r) / 12
    annual_return = equity.iloc[-1] ** (1 / years) - 1 if years else 0
    annual_vol = r.std(ddof=1) * np.sqrt(12)
    changes = frame.position.ne(frame.position.shift()).sum()
    return {
        "months": int(len(r)),
        "position_changes": int(changes),
        "long_months": int((frame.position == 1).sum()),
        "short_months": int((frame.position == -1).sum()),
        "total_return_pct": float((equity.iloc[-1] - 1) * 100),
        "annualized_return_pct": float(annual_return * 100),
        "annualized_volatility_pct": float(annual_vol * 100),
        "sharpe_zero_rf": float(annual_return / annual_vol) if annual_vol else 0,
        "max_drawdown_pct": float(drawdown.min() * 100),
        "positive_months_pct": float((r > 0).mean() * 100),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="EURUSD=X")
    parser.add_argument("--start", default="2005-01-01")
    parser.add_argument("--output", default="artifacts/fx_hp_backtest.json")
    args = parser.parse_args()

    close = load_monthly(args.symbol, args.start)
    result = build_returns(close)
    splits = {
        "development_2010_2019": result.loc["2010-01-01":"2019-12-31"],
        "validation_2020_2022": result.loc["2020-01-01":"2022-12-31"],
        "holdout_2023_present": result.loc["2023-01-01":],
        "full_test_2010_present": result.loc["2010-01-01":],
    }
    report = {
        "strategy": "agents.surf #52 FX Moving Averages with HP Filter",
        "symbol": args.symbol,
        "rules": {"frequency": "monthly", "hp_window_months": 60,
                  "hp_lambda": 14400, "fast_ma_months": 3, "slow_ma_months": 12,
                  "round_trip_cost_pips": 1.2, "execution_delay": "next month return"},
        "data_from": str(close.index.min()), "data_to": str(close.index.max()),
        "splits": {name: metrics(frame) for name, frame in splits.items()},
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    result.to_csv(output.with_suffix(".csv"))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
