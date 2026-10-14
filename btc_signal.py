"""Santiago Academy daily BTC/USD educational market signal."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

KLINES = "https://api.binance.us/api/v3/klines"


def candles(interval: str, limit: int = 220) -> pd.DataFrame:
    response = requests.get(
        KLINES,
        params={"symbol": "BTCUSD", "interval": interval, "limit": limit},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list) or len(data) < 205:
        raise ValueError("Insufficient BTC/USD candle data from Binance.US")
    df = pd.DataFrame(data)
    df["close"] = pd.to_numeric(df[4])
    df["high"] = pd.to_numeric(df[2])
    df["low"] = pd.to_numeric(df[3])
    return df.iloc[:-1].copy()  # Ignore the unfinished candle.


def analyze() -> str:
    df = candles("1h")
    closes = df["close"]
    ema50 = closes.ewm(span=50, adjust=False).mean().iloc[-1]
    ema200 = closes.ewm(span=200, adjust=False).mean().iloc[-1]
    last = float(closes.iloc[-1])
    delta = closes.diff()
    gains = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    losses = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    rsi = 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)

    if last > ema50 > ema200 and 50 <= rsi <= 68:
        direction = "BULLISH"
    elif last < ema50 < ema200 and 32 <= rsi <= 50:
        direction = "BEARISH"
    else:
        direction = "NO TRADE"

    date = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%B %d, %Y")
    header = (
        f"📊 **Santiago Academy | BTC/USD Daily Signal**\n"
        f"Date (Pacific): {date}\n"
        f"Price: ${last:,.2f} | RSI(14): {rsi:.1f}\n"
        f"Market bias: **{direction}**"
    )
    if direction == "NO TRADE":
        return header + "\nNo qualifying setup under the current rules. Educational analysis only; not financial advice."

    recent = df.tail(15)
    stop = float(recent["low"].min()) if direction == "BULLISH" else float(recent["high"].max())
    risk = abs(last - stop)
    if risk <= 0 or risk / last > 0.03 or (direction == "BULLISH" and stop >= last) or (direction == "BEARISH" and stop <= last):
        return header + "\n**NO TRADE:** Stop distance does not meet risk criteria."

    sign = 1 if direction == "BULLISH" else -1
    targets = [last + sign * risk * multiple for multiple in (1, 2, 3)]
    return (
        header
        + f"\nEntry reference: ${last:,.2f}\nStop loss: ${stop:,.2f}"
        + "".join(f"\nTP{i}: ${target:,.2f}" for i, target in enumerate(targets, 1))
        + "\nTarget R:R: 1:1 / 1:2 / 1:3"
        + "\n⚠️ Educational market analysis, not financial advice. Prices may move before posting."
    )


def main() -> None:
    print("BTC_SIGNAL_SCRIPT_STARTED", flush=True)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        raise RuntimeError("DISCORD_WEBHOOK_URL GitHub secret is missing or empty")
    print("Fetching BTC/USD candles and generating signal...", flush=True)
    content = analyze()
    print("Signal generated. Posting to Discord...", flush=True)
    response = requests.post(webhook, json={"content": content}, timeout=20)
    response.raise_for_status()
    print(f"Discord signal sent successfully (HTTP {response.status_code})", flush=True)


if __name__ == "__main__":
    main()
