"""Santiago Academy premium BTC/USD educational signals. No exchange orders."""
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

PACIFIC = ZoneInfo("America/Los_Angeles")
API = "https://api.binance.us/api/v3/klines"
SYMBOL = "BTCUSD"
SESSION = requests.Session()


def now_pt():
    return datetime.now(PACIFIC)


def money(value):
    return f"${value:,.2f}"


def klines(interval, limit=260):
    response = SESSION.get(API, params={"symbol": SYMBOL, "interval": interval, "limit": limit}, timeout=25)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or len(rows) < 205:
        raise RuntimeError(f"Not enough {interval} candles from Binance.US")
    frame = pd.DataFrame(rows)
    for col, name in ((1, "open"), (2, "high"), (3, "low"), (4, "close"), (5, "volume")):
        frame[name] = pd.to_numeric(frame[col], errors="raise")
    frame["open_time"] = pd.to_datetime(frame[0], unit="ms", utc=True)
    frame["close_time"] = pd.to_datetime(frame[6], unit="ms", utc=True)
    # Exclude incomplete candles, including when the API clock is ahead of the runner.
    frame = frame.loc[frame["close_time"] < pd.Timestamp.now(tz="UTC")].copy()
    if len(frame) < 200:
        raise RuntimeError(f"Not enough closed {interval} candles")
    return frame


def rsi(series, length=14):
    change = series.diff()
    up = change.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    down = (-change.clip(upper=0)).ewm(alpha=1 / length, adjust=False).mean()
    if down.iloc[-1] == 0:
        return 100.0 if up.iloc[-1] > 0 else 50.0
    return float(100 - 100 / (1 + up.iloc[-1] / down.iloc[-1]))


def indicators(frame):
    close = frame["close"]
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    histogram = macd - macd.ewm(span=9, adjust=False).mean()
    prev_close = close.shift()
    tr = pd.concat([(frame["high"] - frame["low"]), (frame["high"] - prev_close).abs(), (frame["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    return {"rsi": rsi(close), "ema20": float(ema20.iloc[-1]), "ema50": float(ema50.iloc[-1]),
            "ema200": float(ema200.iloc[-1]), "macd_hist": float(histogram.iloc[-1]), "atr": float(atr)}


def session_window(at):
    start = at.replace(hour=1, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=30)
    return start, end


def session_stats(frame, start, end):
    # These are completed 5m candles whose close falls within the requested window.
    segment = frame[(frame["open_time"] >= pd.Timestamp(start.astimezone(timezone.utc))) &
                    (frame["close_time"] < pd.Timestamp(end.astimezone(timezone.utc)))].copy()
    if segment.empty:
        return None
    total_volume = float(segment["volume"].sum())
    typical = (segment["high"] + segment["low"] + segment["close"]) / 3
    vwap = float((typical * segment["volume"]).sum() / total_volume) if total_volume > 0 else None
    return {"count": len(segment), "open": float(segment.iloc[0]["open"]),
            "close": float(segment.iloc[-1]["close"]), "high": float(segment["high"].max()),
            "low": float(segment["low"].min()), "vwap": vwap, "volume": total_volume}


def analyze(samples, at):
    m5 = klines("5m")
    h1 = klines("1h")
    h4 = klines("4h")
    start, end = session_window(at)
    window = session_stats(m5, start, end)
    i1, i4 = indicators(h1), indicators(h4)
    last = float(m5.iloc[-1]["close"])
    latest_close = m5.iloc[-1]["close_time"].to_pydatetime().astimezone(PACIFIC)
    # Refuse stale market information; never invent prices or levels.
