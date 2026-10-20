from __future__ import annotations

import argparse
import json
from pathlib import Path

from flask import Flask, jsonify, render_template

from eurusd_trader.backtest import run_backtest
from eurusd_trader.config import Settings
from eurusd_trader.data import download_eurusd
from eurusd_trader.forecast import KronosEngine
from eurusd_trader.service import generate_snapshot, start_alert_thread
from eurusd_trader.paper import kill_switch, monitor_position
from eurusd_trader.risk import pause, resume, risk_status

web = Flask(__name__, template_folder="templates")
settings = Settings()
_engine = None


def engine():
    global _engine
    if _engine is None:
        _engine = KronosEngine(settings)
    return _engine


@web.get("/")
def index():
    return render_template("index.html")


@web.get("/api/latest")
def latest():
    path = Path("artifacts/latest.json")
    if not path.exists():
        return jsonify(generate_snapshot(engine(), settings))
    return jsonify(json.loads(path.read_text(encoding="utf-8")))


@web.post("/api/refresh")
def refresh():
    return jsonify(generate_snapshot(engine(), settings))


@web.get("/api/risk")
def risk():
    return jsonify(risk_status(settings))


def main():
    parser = argparse.ArgumentParser(description="Kronos EURUSD paper trader")
    sub = parser.add_subparsers(dest="command", required=True)
    forecast = sub.add_parser("forecast")
    forecast.add_argument("--alert", action="store_true", help="Send the generated paper signal to Telegram")
    bt = sub.add_parser("backtest"); bt.add_argument("--stride", type=int, default=120)
    sub.add_parser("monitor", help="Check an open paper position for stop, target, or time exit")
    sub.add_parser("risk-status", help="Show paper balance, limits, and trading lock")
    sub.add_parser("pause", help="Block new paper positions")
    sub.add_parser("resume", help="Resume entries unless a loss limit is active")
    sub.add_parser("kill", help="Block entries and close the open paper position")
    serve = sub.add_parser("serve"); serve.add_argument("--host", default="127.0.0.1"); serve.add_argument("--port", type=int, default=8000); serve.add_argument("--alerts", action="store_true")
    args = parser.parse_args(); settings.validate()
    if args.command == "forecast":
        print(json.dumps(generate_snapshot(engine(), settings, alert=args.alert)["signal"], indent=2))
    elif args.command == "backtest":
        data = download_eurusd(period="2y", interval=settings.interval, symbol=settings.symbol)
        print(json.dumps(run_backtest(data, engine(), settings, stride=args.stride)["summary"], indent=2))
    elif args.command == "monitor":
        print(json.dumps(monitor_position(settings), indent=2))
    elif args.command == "risk-status":
        print(json.dumps(risk_status(settings), indent=2))
    elif args.command == "pause":
        print(json.dumps(pause(settings), indent=2))
    elif args.command == "resume":
        print(json.dumps(resume(settings), indent=2))
    elif args.command == "kill":
        print(json.dumps(kill_switch(settings), indent=2))
    else:
        if args.alerts:
            start_alert_thread(engine(), settings)
        web.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
