from types import SimpleNamespace
import pytest

from eurusd_trader.config import Settings
from eurusd_trader import paper, risk


def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(risk, "RISK_PATH", tmp_path / "risk.json")
    monkeypatch.setattr(paper, "STATE_PATH", tmp_path / "position.json")
    return Settings(paper_balance=25000, risk_per_trade=.01, daily_loss_limit=.03, max_drawdown=.06)


def test_first_trade_risks_250_dollars(monkeypatch, tmp_path):
    settings = isolated(monkeypatch, tmp_path)
    sizing = risk.position_size(1.1000, 1.0900, settings)
    assert sizing["risk_usd"] == 250
    stopped_loss = sizing["units"] * .01 + sizing["estimated_cost_usd"]
    assert stopped_loss == pytest.approx(250)
    assert sizing["standard_lots"] == pytest.approx(.24703557)


def test_paused_ledger_rejects_entry(monkeypatch, tmp_path):
    settings = isolated(monkeypatch, tmp_path)
    risk.pause(settings)
    allowed, reason, _ = risk.entry_check(settings)
    assert allowed is False
    assert reason == "MANUAL_PAUSE"


def test_registration_persists_risk_sized_position(monkeypatch, tmp_path):
    settings = isolated(monkeypatch, tmp_path)
    signal = SimpleNamespace(side="LONG", regime="bull-trend/normal-vol", entry=1.1,
                             stop=1.09, target=1.12, time_exit_bars=24)
    result = paper.register_signal(signal, "2026-10-20T12:00:00Z", settings)
    assert result["opened"] is True
    assert result["position"]["risk_usd"] == 250


def test_daily_loss_limit_locks_entries(monkeypatch, tmp_path):
    settings = isolated(monkeypatch, tmp_path)
    ledger = risk.load_ledger(settings)
    ledger["daily_realized_pnl"] = -750
    ledger["balance"] = 24_250
    risk.save_ledger(ledger)
    allowed, reason, status = risk.entry_check(settings)
    assert allowed is False
    assert reason == "DAILY_LOSS_LIMIT"
    assert status["trading_enabled"] is False


def test_drawdown_limit_locks_entries(monkeypatch, tmp_path):
    settings = isolated(monkeypatch, tmp_path)
    ledger = risk.load_ledger(settings)
    ledger["balance"] = 23_500
    risk.save_ledger(ledger)
    allowed, reason, _ = risk.entry_check(settings)
    assert allowed is False
    assert reason == "MAX_DRAWDOWN_LIMIT"
