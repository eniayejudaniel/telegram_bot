import numpy as np

from eurusd_trader.ai import deepseek_context, jev_decision
from eurusd_trader.config import Settings
from eurusd_trader.strategy import make_signal
from test_eurusd_trader import candles


class Response:
    def __init__(self, data): self.data = data
    def raise_for_status(self): pass
    def json(self): return self.data


def test_distribution_gate_accepts_consistent_paths():
    history = candles()
    entry = history.close.iloc[-1]
    forecast = candles(24, slope=0.0002)
    forecast[["open", "high", "low", "close"]] += entry - forecast.open.iloc[0]
    samples = np.repeat(forecast[["open", "high", "low", "close"]].to_numpy()[None, :, :], 100, axis=0)
    signal = make_signal(history, forecast, Settings(), samples=samples)
    assert signal.side == "LONG"
    assert signal.metrics["sample_count"] == 100
    assert signal.metrics["path_agreement"] == 1.0


def test_deepseek_response_is_validated():
    content = '{"allow_new_trade":true,"event_risk":0.2,"regime":"trend","size_multiplier":0.5,"reason_code":"OK","summary":"clear"}'
    post = lambda *args, **kwargs: Response({"choices": [{"message": {"content": content}}]})
    result = deepseek_context({}, Settings(), post=post)
    assert result["status"] == "ok"
    assert result["allow_new_trade"] is True


def test_jev_response_is_validated():
    data = {"answers": {"action": {"choice": "ENTER_LONG", "confidence": .8},
                        "allow_trade": {"noul": True}}}
    post = lambda *args, **kwargs: Response(data)
    result = jev_decision({}, Settings(), post=post)
    assert result["choice"] == "ENTER_LONG"
    assert result["allow_trade"] is True
