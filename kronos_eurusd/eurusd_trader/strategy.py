from __future__ import annotations

from dataclasses import asdict, dataclass
import numpy as np
import pandas as pd

from .config import Settings


@dataclass
class Signal:
    side: str
    regime: str
    entry: float
    stop: float | None
    target: float | None
    time_exit_bars: int
    confidence: float
    reason: str
    metrics: dict | None = None

    def to_dict(self):
        return asdict(self)


def _atr(c: pd.DataFrame, n: int = 14) -> float:
    prev = c.close.shift()
    tr = pd.concat([(c.high-c.low), (c.high-prev).abs(), (c.low-prev).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


def read_regime(c: pd.DataFrame) -> str:
    close = c.close.astype(float)
    fast, slow = close.ewm(span=24).mean(), close.ewm(span=96).mean()
    atr = _atr(c)
    separation = abs(float(fast.iloc[-1] - slow.iloc[-1])) / max(atr, 1e-12)
    vol = close.pct_change().rolling(24).std().iloc[-1]
    baseline = close.pct_change().rolling(24).std().tail(240).median()
    vol_tag = "high-vol" if vol > baseline * 1.5 else "normal-vol"
    if separation < 0.35:
        return f"range/{vol_tag}"
    direction = "bull-trend" if fast.iloc[-1] > slow.iloc[-1] else "bear-trend"
    return f"{direction}/{vol_tag}"


def _path_metrics(samples, side: str, entry: float, stop: float, target: float,
                  atr: float, cost_pips: float) -> dict:
    paths = np.asarray(samples, dtype=float)
    if paths.ndim != 3 or paths.shape[2] < 4:
        raise ValueError("Kronos samples must have shape [sample, horizon, OHLC]")
    sign = 1 if side == "LONG" else -1
    outcomes = []
    for path in paths:
        outcome = "neither"
        for bar in path:
            high, low = float(bar[1]), float(bar[2])
            stopped = low <= stop if sign == 1 else high >= stop
            targeted = high >= target if sign == 1 else low <= target
            if stopped:  # conservative when both occur in one forecast candle
                outcome = "stop"
                break
            if targeted:
                outcome = "target"
                break
        outcomes.append(outcome)
    terminal_pips = sign * (paths[:, -1, 3] - entry) / 0.0001 - cost_pips
    return {
        "sample_count": int(len(paths)),
        "path_agreement": float(np.mean(sign * (paths[:, -1, 3] - entry) > 0)),
        "target_probability": float(np.mean(np.asarray(outcomes) == "target")),
        "stop_probability": float(np.mean(np.asarray(outcomes) == "stop")),
        "neither_probability": float(np.mean(np.asarray(outcomes) == "neither")),
        "expected_terminal_pips": float(np.mean(terminal_pips)),
        "median_terminal_pips": float(np.median(terminal_pips)),
        "terminal_q10_pips": float(np.quantile(terminal_pips, 0.10)),
        "terminal_q90_pips": float(np.quantile(terminal_pips, 0.90)),
        "dispersion_atr": float(np.std(paths[:, -1, 3]) / max(atr, 1e-12)),
    }


def make_signal(candles: pd.DataFrame, forecast: pd.DataFrame, s: Settings, samples=None) -> Signal:
    entry = float(candles.close.iloc[-1])
    atr = _atr(candles)
    regime = read_regime(candles)
    terminal = float(forecast.close.iloc[-1] - entry)
    up = float(forecast.high.max() - entry)
    down = float(entry - forecast.low.min())
    edge = s.min_edge_atr * atr
    side = "FLAT"
    if terminal > 0 and up >= edge and "bear-trend" not in regime:
        side = "LONG"
    elif terminal < 0 and down >= edge and "bull-trend" not in regime:
        side = "SHORT"
    if side == "FLAT":
        return Signal(side, regime, entry, None, None, s.pred_len, 0.0,
                      f"Forecast edge below {s.min_edge_atr:.2f} ATR or conflicts with regime")
    sign = 1 if side == "LONG" else -1
    stop_distance = s.stop_atr * atr * (1.2 if "high-vol" in regime else 1.0)
    predicted_move = up if side == "LONG" else down
    target_distance = max(s.min_risk_reward * stop_distance, predicted_move * 0.8)
    confidence = min(1.0, abs(terminal) / max(atr, 1e-12))
    stop, target = entry-sign*stop_distance, entry+sign*target_distance
    metrics = None
    if samples is not None:
        metrics = _path_metrics(samples, side, entry, stop, target, atr,
                                s.spread_pips + 2 * s.slippage_pips)
        failures = []
        if metrics["path_agreement"] < s.min_path_agreement:
            failures.append("path agreement")
        if metrics["target_probability"] < s.min_target_probability:
            failures.append("target probability")
        if metrics["expected_terminal_pips"] < s.min_expected_pips:
            failures.append("expected value")
        if metrics["dispersion_atr"] > s.max_dispersion_atr:
            failures.append("forecast dispersion")
        if failures:
            return Signal("FLAT", regime, entry, None, None, s.pred_len, confidence,
                          "Rejected by 100-path gate: " + ", ".join(failures), metrics)
        confidence = metrics["path_agreement"]
    return Signal(side, regime, entry, stop, target, s.pred_len, confidence,
                  "Kronos direction, regime, and forecast distribution agree", metrics)
