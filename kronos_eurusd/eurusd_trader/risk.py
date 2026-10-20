from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

RISK_PATH = Path("state/risk.json")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def initial_ledger(settings) -> dict:
    return {"initial_balance": settings.paper_balance, "balance": settings.paper_balance,
            "high_water_mark": settings.paper_balance, "daily_date": _today(),
            "daily_realized_pnl": 0.0, "trading_enabled": settings.trading_enabled,
            "lock_reason": None, "closed_trades": 0}


def load_ledger(settings) -> dict:
    if RISK_PATH.exists():
        ledger = json.loads(RISK_PATH.read_text(encoding="utf-8"))
    else:
        ledger = initial_ledger(settings)
        save_ledger(ledger)
    if ledger.get("daily_date") != _today():
        ledger.update({"daily_date": _today(), "daily_realized_pnl": 0.0})
        if str(ledger.get("lock_reason", "")).startswith("DAILY_LOSS"):
            ledger.update({"lock_reason": None, "trading_enabled": settings.trading_enabled})
        save_ledger(ledger)
    return ledger


def save_ledger(ledger: dict) -> None:
    RISK_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = RISK_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    temp.replace(RISK_PATH)


def risk_status(settings) -> dict:
    ledger = load_ledger(settings)
    drawdown = ledger["high_water_mark"] - ledger["balance"]
    return {**ledger,
            "risk_budget_usd": ledger["balance"] * settings.risk_per_trade,
            "daily_loss_limit_usd": settings.paper_balance * settings.daily_loss_limit,
            "max_drawdown_usd": ledger["high_water_mark"] * settings.max_drawdown,
            "drawdown_usd": drawdown,
            "drawdown_pct": drawdown / ledger["high_water_mark"] if ledger["high_water_mark"] else 0.0}


def entry_check(settings) -> tuple[bool, str, dict]:
    status = risk_status(settings)
    if not settings.trading_enabled or not status["trading_enabled"]:
        return False, status.get("lock_reason") or "TRADING_DISABLED", status
    if status["daily_realized_pnl"] <= -status["daily_loss_limit_usd"]:
        pause(settings, "DAILY_LOSS_LIMIT")
        return False, "DAILY_LOSS_LIMIT", risk_status(settings)
    if status["drawdown_usd"] >= status["max_drawdown_usd"]:
        pause(settings, "MAX_DRAWDOWN_LIMIT")
        return False, "MAX_DRAWDOWN_LIMIT", risk_status(settings)
    return True, "OK", status


def position_size(entry: float, stop: float, settings) -> dict:
    status = risk_status(settings)
    risk_usd = status["risk_budget_usd"]
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        raise ValueError("Stop distance must be positive")
    cost_distance = (settings.spread_pips + 2 * settings.slippage_pips) * 0.0001
    units = risk_usd / (stop_distance + cost_distance)
    return {"risk_usd": risk_usd, "units": units, "standard_lots": units / 100_000,
            "stop_pips": stop_distance / 0.0001, "estimated_cost_usd": cost_distance * units}


def record_close(state: dict, exit_price: float, settings) -> dict:
    ledger = load_ledger(settings)
    sign = 1 if state["side"] == "LONG" else -1
    gross = sign * (exit_price - state["entry"]) * state["units"]
    cost = (settings.spread_pips + 2 * settings.slippage_pips) * 0.0001 * state["units"]
    pnl = gross - cost
    ledger["balance"] += pnl
    ledger["daily_realized_pnl"] += pnl
    ledger["high_water_mark"] = max(ledger["high_water_mark"], ledger["balance"])
    ledger["closed_trades"] = ledger.get("closed_trades", 0) + 1
    save_ledger(ledger)
    allowed, reason, status = entry_check(settings)
    return {"gross_pnl_usd": gross, "cost_usd": cost, "net_pnl_usd": pnl,
            "balance": status["balance"], "trading_allowed": allowed, "lock_reason": None if allowed else reason}


def pause(settings, reason: str = "MANUAL_PAUSE") -> dict:
    ledger = load_ledger(settings)
    ledger.update({"trading_enabled": False, "lock_reason": reason})
    save_ledger(ledger)
    return risk_status(settings)


def resume(settings) -> dict:
    ledger = load_ledger(settings)
    ledger.update({"trading_enabled": True, "lock_reason": None})
    save_ledger(ledger)
    allowed, reason, status = entry_check(settings)
    if not allowed:
        status["resume_rejected"] = reason
    return status
