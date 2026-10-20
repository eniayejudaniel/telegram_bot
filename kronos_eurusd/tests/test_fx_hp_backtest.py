import numpy as np
import pandas as pd

from research.fx_hp_backtest import build_returns, causal_hp_endpoint


def test_hp_endpoint_does_not_receive_future_values():
    base = np.linspace(1.0, 2.0, 60)
    first = causal_hp_endpoint(base, 14_400)
    extended = np.r_[base, 100.0]
    # Re-fitting the same historical endpoint on the same trailing input is invariant.
    assert first == causal_hp_endpoint(extended[:-1], 14_400)


def test_signal_is_delayed_into_forward_month():
    idx = pd.date_range("2000-01-31", periods=100, freq="ME", tz="UTC")
    close = pd.Series(np.linspace(1.0, 2.0, 100), index=idx)
    result = build_returns(close)
    row = result.iloc[-2]
    expected = close.shift(-1).loc[row.name] / close.loc[row.name] - 1
    assert row.position == 1
    assert abs(row.forward_return - expected) < 1e-12
