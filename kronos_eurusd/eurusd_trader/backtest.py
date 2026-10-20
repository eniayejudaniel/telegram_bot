from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pandas as pd

from .config import Settings
from .strategy import make_signal


def _trade_return(signal, future: pd.DataFrame, cost_price: float) -> tuple[float, str]:
    if signal.side == "FLAT":
        return 0.0, "flat"
    sign = 1 if signal.side == "LONG" else -1
    exit_price, reason = float(future.close.iloc[-1]), "time"
    for row in future.itertuples():
        # Conservative convention if stop and target occur within the same candle.
        stopped = row.low <= signal.stop if sign == 1 else row.high >= signal.stop
        targeted = row.high >= signal.target if sign == 1 else row.low <= signal.target
        if stopped:
            exit_price, reason = signal.stop, "stop"
            break
        if targeted:
            exit_price, reason = signal.target, "target"
            break
    return (sign * (exit_price - signal.entry) - cost_price) / signal.entry, reason


def run_backtest(candles: pd.DataFrame, engine, settings: Settings, stride: int = 24,
                 output: str = "artifacts/backtest.json") -> dict:
    """Walk forward over the last year. Forecasts never see their evaluation candles."""
    end = candles.timestamp.max()
    start = end - pd.Timedelta(days=365)
    origins = [i for i in range(settings.lookback, len(candles)-settings.pred_len, stride)
               if candles.timestamp.iloc[i] >= start]
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = path.with_suffix(".partial.json")
    run_key = {
        "strategy_version": "distribution-gate-v1",
        "from": str(start), "to": str(end), "stride": stride,
        "lookback": settings.lookback, "pred_len": settings.pred_len,
        "sample_count": settings.sample_count, "origins": len(origins),
        "min_path_agreement": settings.min_path_agreement,
        "min_target_probability": settings.min_target_probability,
        "min_expected_pips": settings.min_expected_pips,
        "max_dispersion_atr": settings.max_dispersion_atr,
    }
    trades = []
    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("run") == run_key:
            trades = checkpoint.get("trades", [])
            print(f"Resuming backtest at {len(trades)}/{len(origins)}", flush=True)
        else:
            print("Existing checkpoint is for different data/settings; starting over", flush=True)
    round_trip_cost_price = (settings.spread_pips + 2*settings.slippage_pips) * 0.0001
    for n, i in enumerate(origins[len(trades):], len(trades) + 1):
        context = candles.iloc[i-settings.lookback:i]
        future = candles.iloc[i:i+settings.pred_len]
        pred, samples = engine.predict_distribution(context, pd.DatetimeIndex(future.timestamp))
        signal = make_signal(context, pred, settings, samples=samples)
        ret, exit_reason = _trade_return(signal, future, round_trip_cost_price if signal.side != "FLAT" else 0)
        trades.append({"timestamp": str(context.timestamp.iloc[-1]), "side": signal.side,
                       "regime": signal.regime, "return": ret, "exit": exit_reason,
                       "metrics": signal.metrics})
        checkpoint_tmp = checkpoint_path.with_suffix(".partial.tmp")
        checkpoint_tmp.write_text(json.dumps({"run": run_key, "trades": trades}, indent=2),
                                  encoding="utf-8")
        checkpoint_tmp.replace(checkpoint_path)
        if n % 10 == 0:
            print(f"Backtest {n}/{len(origins)}", flush=True)
    returns = pd.Series([x["return"] for x in trades], dtype=float)
    active = [x for x in trades if x["side"] != "FLAT"]
    equity = (1 + returns).cumprod()
    dd = equity / equity.cummax() - 1 if len(equity) else pd.Series(dtype=float)
    summary = {
        "from": str(start), "to": str(end), "forecast_origins": len(origins),
        "trades": len(active), "sample_count": settings.sample_count,
        "total_return_pct": float((equity.iloc[-1]-1)*100) if len(equity) else 0,
        "max_drawdown_pct": float(dd.min()*100) if len(dd) else 0,
        "win_rate_pct": float(np.mean([x["return"] > 0 for x in active])*100) if active else 0,
        "avg_trade_pct": float(np.mean([x["return"] for x in active])*100) if active else 0,
    }
    result = {"summary": summary, "trades": trades}
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    checkpoint_path.unlink(missing_ok=True)
    return result
