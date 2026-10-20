import pandas as pd

from eurusd_trader.config import Settings
from eurusd_trader.data import future_market_hours
from eurusd_trader.strategy import make_signal, read_regime
from eurusd_trader.paper import _exit_message


def candles(n=600, slope=0.00001):
    close = pd.Series([1.08 + i*slope for i in range(n)])
    return pd.DataFrame({"timestamp": pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC"),
                         "open": close, "high": close+0.0003, "low": close-0.0003, "close": close})


def test_future_market_hours_skips_weekend():
    out = future_market_hours(pd.Timestamp("2025-01-03 21:00", tz="UTC"), 2)
    assert list(out) == [pd.Timestamp("2025-01-05 21:00", tz="UTC"), pd.Timestamp("2025-01-05 22:00", tz="UTC")]


def test_signal_has_explicit_risk_rules():
    history = candles()
    future = candles(24, slope=0.0002)
    future[["open", "high", "low", "close"]] += history.close.iloc[-1] - future.open.iloc[0]
    signal = make_signal(history, future, Settings())
    assert signal.side == "LONG"
    assert signal.stop < signal.entry < signal.target
    assert signal.time_exit_bars == 24


def test_regime_is_readable():
    assert read_regime(candles()).split("/")[0] in {"bull-trend", "bear-trend", "range"}


def test_exit_alert_reports_signed_pips():
    state = {"side": "LONG", "entry": 1.1000}
    text = _exit_message(state, 1.1020, "TARGET HIT", "2026-01-01 00:00 UTC")
    assert "TARGET HIT" in text
    assert "+20.0 pips" in text

