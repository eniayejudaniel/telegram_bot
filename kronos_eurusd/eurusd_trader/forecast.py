from __future__ import annotations

import pandas as pd
import torch

from model import Kronos, KronosPredictor, KronosTokenizer
from .config import Settings
from .data import OHLC, future_market_hours


class KronosEngine:
    def __init__(self, settings: Settings):
        settings.validate()
        self.settings = settings
        device = settings.device
        if device == "auto":
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
        tokenizer = KronosTokenizer.from_pretrained(settings.tokenizer_id)
        model = Kronos.from_pretrained(settings.model_id)
        self.predictor = KronosPredictor(model, tokenizer, device=device, max_context=512)
        self.device = device

    def predict(self, candles: pd.DataFrame, future_index=None) -> pd.DataFrame:
        forecast, _ = self.predict_distribution(candles, future_index)
        return forecast

    def predict_distribution(self, candles: pd.DataFrame, future_index=None) -> tuple[pd.DataFrame, object]:
        s = self.settings
        context = candles.tail(s.lookback).copy()
        if len(context) != s.lookback:
            raise ValueError(f"Need exactly {s.lookback} prior candles; got {len(context)}")
        if future_index is None:
            future_index = future_market_hours(context["timestamp"].iloc[-1], s.pred_len)
        samples = self.predictor.predict(
            df=context[OHLC],
            x_timestamp=context["timestamp"],
            y_timestamp=pd.Series(future_index),
            pred_len=s.pred_len,
            T=s.temperature,
            top_p=s.top_p,
            sample_count=s.sample_count,
            verbose=False,
            return_samples=True,
        )
        pred = pd.DataFrame(samples.mean(axis=0), columns=["open", "high", "low", "close", "volume", "amount"])
        pred = pred[OHLC].copy()
        pred.insert(0, "timestamp", pd.DatetimeIndex(future_index))
        return pred.reset_index(drop=True), samples[:, :, :4]
