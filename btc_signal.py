"""Santiago Academy Premium BTC/USD educational analysis; never places orders."""
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

PT = ZoneInfo("America/Los_Angeles")
API = "https://api.binance.us/api/v3/klines"
SESSION = requests.Session()

def candles(interval, limit=260):
    response = SESSION.get(API, params={"symbol": "BTCUSD", "interval": interval, "limit": limit}, timeout=25)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list) or len(data) < 205:
        raise RuntimeError(f"Insufficient {interval} BTCUSD candles from Binance.US")
    df = pd.DataFrame(data)
    for idx, name in [(1,"open"),(2,"high"),(3,"low"),(4,"close"),(5,"volume")]:
        df[name] = pd.to_numeric(df[idx], errors="raise")
    df["open_time"] = pd.to_datetime(df[0], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df[6], unit="ms", utc=True)
    df = df.loc[df["close_time"] < pd.Timestamp.now(tz="UTC")].copy()
    if len(df) < 200:
        raise RuntimeError(f"Insufficient completed {interval} candles")
    return df

def metrics(df):
    close = df["close"]
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean().iloc[-1]
    rsi = 100 - 100/(1 + gain/loss) if loss > 0 else (100 if gain > 0 else 50)
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    hist = (macd - macd.ewm(span=9, adjust=False).mean()).iloc[-1]
    prev = close.shift()
    true_range = pd.concat([df["high"]-df["low"], (df["high"]-prev).abs(), (df["low"]-prev).abs()], axis=1).max(axis=1)
    atr = true_range.ewm(alpha=1/14, adjust=False).mean().iloc[-1]
    return dict(rsi=float(rsi), ema20=float(ema20), ema50=float(ema50),
                ema200=float(ema200), macd=float(hist), atr=float(atr))

def usd(x):
    return f"${x:,.2f}"

def build_signal():
    now = datetime.now(PT)
    print("BTC_PREMIUM_START Pacific=" + now.isoformat(), flush=True)
    m5, h1, h4 = candles("5m"), candles("1h"), candles("4h")
    last_close = m5.iloc[-1]["close_time"].to_pydatetime().astimezone(PT)
    if datetime.now(timezone.utc) - m5.iloc[-1]["close_time"].to_pydatetime() > timedelta(minutes=16):
        raise RuntimeError("BTC data stale: most recent completed 5m candle is over 16 minutes old")
    a, b = metrics(h1), metrics(h4)
    price = float(m5.iloc[-1]["close"])
    start = now.replace(hour=1, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=30)
    window = m5.loc[(m5["open_time"] >= pd.Timestamp(start.astimezone(timezone.utc))) &
                    (m5["close_time"] < pd.Timestamp(end.astimezone(timezone.utc)))].copy()
    # A manual test outside the 1:00–1:30 AM period must not pretend a full review occurred.
    completed_window = now >= end and len(window) >= 6
    if completed_window:
        high, low = float(window["high"].max()), float(window["low"].min())
        vol = float(window["volume"].sum())
        typical = (window["high"]+window["low"]+window["close"])/3
        vwap = float((typical*window["volume"]).sum()/vol) if vol > 0 else None
        window_note = f"1:00–1:30 AM PT review: {len(window)} completed 5m candles | H {usd(high)} / L {usd(low)} | VWAP {usd(vwap) if vwap is not None else 'N/A'}"
    else:
        high, low, vwap = None, None, None
        window_note = "1:00–1:30 AM PT review: incomplete/unavailable (manual test or early run)"
    bull = price > a["ema50"] > a["ema200"] and price > b["ema50"] and a["macd"] > 0 and 50 <= a["rsi"] <= 68
    bear = price < a["ema50"] < a["ema200"] and price < b["ema50"] and a["macd"] < 0 and 32 <= a["rsi"] <= 50
    direction = "BULLISH" if bull else "BEARISH" if bear else "NO TRADE"
    reasons = []
    if direction == "NO TRADE":
        reasons.append("Trend, momentum and RSI filters do not all align.")
    if not completed_window:
        reasons.append("Full 1:00–1:30 AM review is not confirmed.")
        direction = "NO TRADE"
    header = (f"📊 **SANTIAGO ACADEMY | PREMIUM BTC/USD SIGNAL**\n"
              f"**Time:** {now:%b %d, %Y %I:%M %p} Pacific\n"
              f"**Source:** Binance.US BTCUSD completed candles (5m / 1h / 4h)\n"
              f"**Reference price:** {usd(price)} (5m close {last_close:%I:%M %p} PT)\n"
              f"**Decision:** **{direction}**\n"
              f"**Strategy:** 1H EMA trend + 4H confirmation + MACD/RSI + session structure\n"
              f"**1H:** RSI {a['rsi']:.1f} | EMA50 {usd(a['ema50'])} | EMA200 {usd(a['ema200'])} | MACD hist {a['macd']:+.2f}\n"
              f"**4H:** RSI {b['rsi']:.1f} | EMA50 {usd(b['ema50'])} | ATR {usd(b['atr'])}\n"
              f"{window_note}\n")
    if direction != "NO TRADE":
        entry = price
        stop = (low if bull else high)
        risk = entry-stop if bull else stop-entry
        if risk <= 0 or risk/entry > 0.03 or risk/entry < 0.0005:
            direction = "NO TRADE"
            reasons.append("Session-based stop distance failed risk controls.")
        else:
            sign = 1 if bull else -1
            targets = [entry+sign*risk*k for k in (1,2,3)]
            entry_range = (min(entry, entry-sign*0.15*risk), max(entry, entry-sign*0.15*risk))
            header = header.replace(f"**Decision:** **{'BULLISH' if bull else 'BEARISH'}**", f"**Decision:** **{direction}**")
            return (header + f"**Entry watch zone:** {usd(entry_range[0])}–{usd(entry_range[1])} (conditional, not a market order)\n"
                    f"**Stop:** {usd(stop)} | **TP1:** {usd(targets[0])} (1:1)\n"
                    f"**TP2:** {usd(targets[1])} (1:2) | **TP3:** {usd(targets[2])} (1:3)\n"
                    f"**Reasoning:** 1H/4H trend and momentum aligned; stop based on session extreme.\n"
                    f"**Invalidation:** Break of {usd(stop)}; cancel if setup changes before entry.\n"
                    f"⚠️ Educational analysis only, not financial advice. Crypto trading can result in total loss.")
    header = header.replace(f"**Decision:** **{'BULLISH' if bull else 'BEARISH'}**", "**Decision:** **NO TRADE**")
    return (header + "**Entry / stop / TP1–TP3 / R:R:** N/A — no defensible setup\n"
            f"**Reasoning:** {' '.join(reasons) or 'No qualifying setup.'}\n"
            "**Invalidation / reassessment:** Reassess after confirmed 1H trend and momentum alignment.\n"
            "⚠️ Educational analysis only, not financial advice. Crypto trading can result in total loss.")

def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL GitHub Actions secret")
    message = build_signal()
    if len(message) > 2000:
        raise RuntimeError(f"Discord message too long: {len(message)} characters")
    print("BTC_PREMIUM_POST_ATTEMPT characters=" + str(len(message)), flush=True)
    response = SESSION.post(webhook, json={"content": message, "allowed_mentions": {"parse": []}},
                            params={"wait": "true"}, timeout=30)
    response.raise_for_status()
    result = response.json()
    if not result.get("id"):
        raise RuntimeError("Discord did not return a message ID")
    print("BTC_PREMIUM_DISCORD_SENT message_id=" + str(result["id"]), flush=True)

if __name__ == "__main__":
    main()
