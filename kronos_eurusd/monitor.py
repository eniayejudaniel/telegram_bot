"""Lightweight entry point for the five-minute paper-position monitor."""
import json

from eurusd_trader.config import Settings
from eurusd_trader.paper import monitor_position

if __name__ == "__main__":
    print(json.dumps(monitor_position(Settings()), indent=2))
