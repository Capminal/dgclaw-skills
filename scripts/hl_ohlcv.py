#!/usr/bin/env python3
"""hl_ohlcv.py -- Fetch OHLCV candlestick data from Hyperliquid.

Usage:
    python3 hl_ohlcv.py ETH                              # 20 candles, 1h
    python3 hl_ohlcv.py ETH --interval 4h --candles 10   # custom
    python3 hl_ohlcv.py ETH --json                        # JSON output
"""
import sys
import os
import json
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from api import HyperliquidAPI
from fmt import (
    table, header_box, separator, price as fmt_price, price_raw,
    pct, trend_arrow, bold, dim, cyan, green, red, yellow,
)

VALID_INTERVALS = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "3d", "1w"]

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000,
    "4h": 14_400_000, "8h": 28_800_000, "12h": 43_200_000,
    "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000,
}


def usage():
    print("Usage: hl_ohlcv.py <COIN> [--interval <interval>] [--candles <n>] [--json]")
    print()
    print("Options:")
    print(f"  --interval   Candle interval (default: 1h)")
    print(f"               Valid: {' '.join(VALID_INTERVALS)}")
    print(f"  --candles    Number of candles (default: 20)")
    print(f"  --json       JSON output with candles + stats")
    print()
    print("Examples:")
    print("  python3 hl_ohlcv.py ETH")
    print("  python3 hl_ohlcv.py BTC --interval 4h --candles 10")
    print("  python3 hl_ohlcv.py SOL --interval 1d --candles 7 --json")


def compute_stats(parsed):
    """Compute stats dict from parsed candles (sorted oldest first)."""
    closes = [c["c"] for c in parsed]
    current_price = closes[-1]
    first_price = closes[0]
    range_high = max(c["h"] for c in parsed)
    range_low = min(c["l"] for c in parsed)
    range_pct = ((range_high - range_low) / range_low * 100) if range_low > 0 else 0
    total_volume = sum(c["v"] for c in parsed)
    avg_volume = total_volume / len(parsed) if parsed else 0
    total_trades = sum(c["n"] for c in parsed)
    green_candles = sum(1 for c in parsed if c["c"] >= c["o"])
    red_candles = len(parsed) - green_candles
    change_pct = ((current_price - first_price) / first_price * 100) if first_price > 0 else 0

    # Trend from recent candles
    recent = parsed[-min(5, len(parsed)):]
    recent_green = sum(1 for c in recent if c["c"] >= c["o"])
    if recent_green > len(recent) / 2:
        trend = "up"
    elif recent_green < len(recent) / 2:
        trend = "down"
    else:
        trend = "neutral"

    return {
        "current_price": round(current_price, 6),
        "change_pct": round(change_pct, 2),
        "range_high": round(range_high, 6),
        "range_low": round(range_low, 6),
        "range_pct": round(range_pct, 2),
        "total_volume": round(total_volume, 2),
        "avg_volume_per_bar": round(avg_volume, 2),
        "total_trades": total_trades,
        "trend": trend,
        "green_candles": green_candles,
        "red_candles": red_candles,
        "num_candles": len(parsed),
    }


def main():
    args = sys.argv[1:]
    if not args:
        usage()
        sys.exit(0)

    coin = args[0].upper()
    interval = "1h"
    candles_count = 20
    json_mode = False

    i = 1
    while i < len(args):
        if args[i] == "--interval":
            interval = args[i + 1].lower()
            i += 2
        elif args[i] == "--candles":
            candles_count = int(args[i + 1])
            i += 2
        elif args[i] == "--json":
            json_mode = True
            i += 1
        else:
            print(f"Error: Unknown argument '{args[i]}'", file=sys.stderr)
            sys.exit(1)

    if interval not in VALID_INTERVALS:
        print(f"Error: Invalid interval '{interval}'", file=sys.stderr)
        print(f"Valid: {' '.join(VALID_INTERVALS)}", file=sys.stderr)
        sys.exit(1)

    # Compute time window
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (candles_count * INTERVAL_MS[interval])

    # Fetch candles
    hl = HyperliquidAPI()
    raw_candles = hl.get_candles(coin, interval, start_ms, end_ms)

    if isinstance(raw_candles, dict) and "error" in raw_candles:
        print(f"Error from API: {raw_candles['error']}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(raw_candles, list) or len(raw_candles) == 0:
        print(f"No candle data returned for {coin} ({interval})", file=sys.stderr)
        sys.exit(1)

    # Parse
    parsed = []
    for c in raw_candles:
        parsed.append({
            "t": int(c["t"]),
            "T": int(c["T"]),
            "o": float(c["o"]),
            "h": float(c["h"]),
            "l": float(c["l"]),
            "c": float(c["c"]),
            "v": float(c["v"]),
            "n": int(c["n"]),
        })
    parsed.sort(key=lambda x: x["t"])

    stats = compute_stats(parsed)

    # JSON mode
    if json_mode:
        output = {
            "coin": coin,
            "interval": interval,
            "candles": raw_candles,
            "stats": stats,
        }
        print(json.dumps(output, indent=2))
        sys.exit(0)

    # Human-readable output
    header_box(f"{coin}  |  {interval}  |  {len(parsed)} candles")

    # Stats summary
    print(f"  Current      : {fmt_price(stats['current_price'])}")
    print(f"  Range ({len(parsed):>3})   : {price_raw(stats['range_low'])} - {price_raw(stats['range_high'])}  ({pct(stats['range_pct'])} spread)")
    print(f"  Change       : {pct(stats['change_pct'])}  ({price_raw(parsed[0]['o'])} -> {price_raw(stats['current_price'])})")
    print(f"  Volume ({len(parsed):>3})  : {stats['total_volume']:,.2f}  |  avg {stats['avg_volume_per_bar']:,.2f}/bar")
    print(f"  Trend        : {trend_arrow(stats['trend'])}  ({green(str(stats['green_candles']))} green / {red(str(stats['red_candles']))} red)")
    separator()

    # Candle table (newest first)
    headers = ["Time (UTC)", "Open", "High", "Low", "Close", "Volume", "Trades"]
    rows = []
    for c in reversed(parsed):
        dt = datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc)
        time_str = dt.strftime("%m-%d %H:%M")

        # Color the close price: green if bullish, red if bearish
        close_val = price_raw(c["c"])
        if c["c"] >= c["o"]:
            close_cell = green(close_val)
        else:
            close_cell = red(close_val)

        rows.append([
            time_str,
            price_raw(c["o"]),
            price_raw(c["h"]),
            price_raw(c["l"]),
            close_cell,
            f"{c['v']:,.2f}",
            f"{c['n']:,}",
        ])

    table(
        headers,
        rows,
        alignments=["<", ">", ">", ">", ">", ">", ">"],
    )


if __name__ == "__main__":
    main()
