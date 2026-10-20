from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import pandas as pd

from .ai import review_candidate
from .data import download_eurusd
from .strategy import Signal, make_signal, read_regime
from .telegram import format_alert, send_alert
from .paper import register_signal


def _decision(engine, settings, candles):
    latest = pd.Timestamp(candles["timestamp"].iloc[-1])
    now = pd.Timestamp.now(tz="UTC")
    if latest.tzinfo is None:
        latest = latest.tz_localize("UTC")
    age_minutes = (now - latest).total_seconds() / 60
    if age_minutes > settings.max_data_age_minutes:
        signal = Signal("FLAT", read_regime(candles), float(candles.close.iloc[-1]), None, None,
                        settings.pred_len, 0.0,
                        f"STALE_DATA: latest candle is {age_minutes:.0f} minutes old")
        empty = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close"])
        return empty, signal, {"mode": settings.ai_enforcement,
                               "deepseek": {"status": "not_run"}, "jev": {"status": "not_run"}}
    forecast, samples = engine.predict_distribution(candles)
    signal = make_signal(candles, forecast, settings, samples=samples)
    ai = review_candidate(signal, str(candles["timestamp"].iloc[-1]), settings)
    if (settings.ai_enforcement == "deepseek_veto" and signal.side != "FLAT"
            and not ai["deepseek"].get("allow_new_trade", False)):
        signal = Signal("FLAT", signal.regime, signal.entry, None, None,
                        signal.time_exit_bars, signal.confidence,
                        "Rejected by DeepSeek event-risk veto", signal.metrics)
    return forecast, signal, ai


def _write_audit(candle_timestamp: str, signal, ai) -> None:
    path = Path("artifacts/decision_audit.jsonl")
    path.parent.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": candle_timestamp, "signal": signal.to_dict(), "ai": ai}) + "\n")


def generate_snapshot(engine, settings, alert: bool = False) -> dict:
    candles = download_eurusd(interval=settings.interval, symbol=settings.symbol)
    forecast, signal, ai = _decision(engine, settings, candles)
    result = {
        "history": candles.tail(settings.lookback).assign(timestamp=lambda x: x.timestamp.astype(str)).to_dict("records"),
        "forecast": forecast.assign(timestamp=lambda x: x.timestamp.astype(str)).to_dict("records"),
        "signal": signal.to_dict(),
        "ai": ai,
    }
    path = Path("artifacts/latest.json"); path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    _write_audit(str(candles["timestamp"].iloc[-1]), signal, ai)
    if alert:
        registration = register_signal(signal, str(candles["timestamp"].iloc[-1]), settings)
        send_alert(settings.telegram_token, settings.telegram_chat_id, format_alert(signal, ai, registration))
    return result


def alert_loop(engine, settings):
    last_candle = None
    while True:
        try:
            candles = download_eurusd(interval=settings.interval, symbol=settings.symbol)
            current = str(candles.timestamp.iloc[-1])
            if current != last_candle:
                _, signal, ai = _decision(engine, settings, candles)
                _write_audit(current, signal, ai)
                registration = register_signal(signal, current, settings)
                send_alert(settings.telegram_token, settings.telegram_chat_id,
                           format_alert(signal, ai, registration))
                last_candle = current
        except Exception as exc:
            print(f"Alert loop error: {exc}", flush=True)
        time.sleep(settings.alert_interval_seconds)


def start_alert_thread(engine, settings):
    thread = threading.Thread(target=alert_loop, args=(engine, settings), daemon=True)
    thread.start()
    return thread
