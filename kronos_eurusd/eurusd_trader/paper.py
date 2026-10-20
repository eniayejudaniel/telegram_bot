from __future__ import annotations

import json
from pathlib import Path
import pandas as pd

from .data import download_eurusd
from .risk import entry_check, pause, position_size, record_close
from .telegram import send_alert

STATE_PATH = Path("state/paper_position.json")


def _read_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def _write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp.replace(STATE_PATH)


def register_signal(signal, signal_time: str, settings) -> dict:
    """Open one risk-sized paper position only after all hard limits pass."""
    current = _read_state()
    if signal.side == "FLAT":
        return {"opened": False, "reason": "FLAT"}
    if current and current.get("status") == "OPEN":
        return {"opened": False, "reason": "POSITION_ALREADY_OPEN"}
    allowed, reason, risk = entry_check(settings)
    if not allowed:
        return {"opened": False, "reason": reason, "risk": risk}
    sizing = position_size(signal.entry, signal.stop, settings)
    state = {"status": "OPEN", "side": signal.side, "regime": signal.regime,
             "entry": signal.entry, "stop": signal.stop, "target": signal.target,
             "opened_at": signal_time,
             "expires_at": str(pd.Timestamp(signal_time) + pd.Timedelta(hours=signal.time_exit_bars)),
             **sizing}
    _write_state(state)
    return {"opened": True, "reason": "OPENED", "position": state, "risk": risk}


def _exit_message(state: dict, exit_price: float, reason: str, closed_at: str) -> str:
    sign = 1 if state["side"] == "LONG" else -1
    pnl_pips = sign * (exit_price - state["entry"]) / 0.0001
    icon = "✅" if pnl_pips > 0 else "🛑" if pnl_pips < 0 else "⏱"
    money = (f"\nNet P&L: ${state['net_pnl_usd']:+,.2f}\nBalance: ${state['balance']:,.2f}"
             if "net_pnl_usd" in state else "")
    return (f"{icon} EURUSD PAPER TRADE CLOSED\n{state['side']} | {reason}\n"
            f"Entry: {state['entry']:.5f}\nExit: {exit_price:.5f}\n"
            f"Result: {pnl_pips:+.1f} pips{money}\nClosed: {closed_at}")


def monitor_position(settings) -> dict:
    """Check the open paper position against completed 5-minute candles."""
    state = _read_state()
    if not state or state.get("status") != "OPEN":
        return {"status": "NO_OPEN_POSITION"}
    bars = download_eurusd(period="5d", interval="5m", symbol=settings.symbol)
    opened = pd.Timestamp(state["opened_at"])
    relevant = bars.loc[bars.timestamp > opened]
    if relevant.empty:
        return {"status": "OPEN", "side": state["side"]}
    side = state["side"]
    exit_price = reason = closed_at = None
    for row in relevant.itertuples():
        stopped = row.low <= state["stop"] if side == "LONG" else row.high >= state["stop"]
        targeted = row.high >= state["target"] if side == "LONG" else row.low <= state["target"]
        expired = row.timestamp >= pd.Timestamp(state["expires_at"])
        if stopped:
            exit_price, reason, closed_at = state["stop"], "STOP LOSS", str(row.timestamp)
            break
        if targeted:
            exit_price, reason, closed_at = state["target"], "TARGET HIT", str(row.timestamp)
            break
        if expired:
            exit_price, reason, closed_at = float(row.close), "24H TIME EXIT", str(row.timestamp)
            break
    if reason is None:
        return {"status": "OPEN", "side": side}
    accounting = record_close(state, exit_price, settings)
    state.update({"status": "CLOSED", "exit": exit_price, "exit_reason": reason,
                  "closed_at": closed_at, **accounting})
    _write_state(state)
    send_alert(settings.telegram_token, settings.telegram_chat_id,
               _exit_message(state, exit_price, reason, closed_at))
    return {"status": "CLOSED", "reason": reason, "exit": exit_price, **accounting}


def kill_switch(settings) -> dict:
    """Disable new entries and close an open paper position at the latest 5m close."""
    pause(settings, "KILL_SWITCH")
    state = _read_state()
    if not state or state.get("status") != "OPEN":
        return {"status": "KILLED", "position": "NONE"}
    bars = download_eurusd(period="1d", interval="5m", symbol=settings.symbol)
    exit_price, closed_at = float(bars.close.iloc[-1]), str(bars.timestamp.iloc[-1])
    accounting = record_close(state, exit_price, settings)
    state.update({"status": "CLOSED", "exit": exit_price, "exit_reason": "KILL SWITCH",
                  "closed_at": closed_at, **accounting})
    _write_state(state)
    send_alert(settings.telegram_token, settings.telegram_chat_id,
               _exit_message(state, exit_price, "KILL SWITCH", closed_at))
    return {"status": "KILLED", "position": "CLOSED", "exit": exit_price, **accounting}
