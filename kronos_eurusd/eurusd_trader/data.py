from __future__ import annotations

import pandas as pd
import yfinance as yf

OHLC = ["open", "high", "low", "close"]


def download_eurusd(period: str = "2y", interval: str = "1h", symbol: str = "EURUSD=X") -> pd.DataFrame:
    """Download completed EURUSD candles. Two years covers context + one-year test."""
    raw = yf.download(symbol, period=period, interval=interval, auto_adjust=False, progress=False)
    if raw.empty:
        raise RuntimeError(f"No market data returned for {symbol}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw.columns = [str(c).lower().replace(" ", "_") for c in raw.columns]
    frame = raw.rename_axis("timestamp").reset_index()
    frame.columns = [str(c).lower() for c in frame.columns]
    time_col = "datetime" if "datetime" in frame else "date"
    frame = frame.rename(columns={time_col: "timestamp"})
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    keep = ["timestamp", *OHLC]
    frame = frame[keep].dropna().drop_duplicates("timestamp").sort_values("timestamp")
    floor_rule = interval.lower()
    if floor_rule.endswith("m"):
        floor_rule = f"{int(floor_rule[:-1])}min"
    elif floor_rule.endswith("h"):
        floor_rule = f"{int(floor_rule[:-1])}h"
    else:
        floor_rule = "h"
    now = pd.Timestamp.now(tz="UTC").floor(floor_rule)
    return frame.loc[frame["timestamp"] < now].reset_index(drop=True)


def future_market_hours(last: pd.Timestamp, count: int) -> pd.DatetimeIndex:
    """Generate hourly FX timestamps, excluding the Friday 22:00-Sunday 21:00 UTC closure."""
    ts = pd.Timestamp(last)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    out = []
    while len(out) < count:
        ts += pd.Timedelta(hours=1)
        wd, hour = ts.weekday(), ts.hour
        open_hour = not (wd == 5 or (wd == 4 and hour >= 22) or (wd == 6 and hour < 21))
        if open_hour:
            out.append(ts)
    return pd.DatetimeIndex(out)
