#!/usr/bin/env python3
"""hl_top_volume.py -- Top volume pairs on Hyperliquid.

Usage:
    python3 hl_top_volume.py              # top 20 by 24h volume
    python3 hl_top_volume.py -n 50        # top 50
    python3 hl_top_volume.py --json       # raw JSON output
"""
import sys
import os
import json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.join(SCRIPT_DIR, "..")
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
sys.path.insert(0, ROOT_DIR)

from api import HyperliquidAPI, api_summary_str, _post, HL_URL
from fmt import table, header_box, separator, bold, dim, green, red, yellow, cyan


def fetch_meta_and_ctxs():
    """Fetch metadata + asset contexts (includes 24h volume, funding, OI)."""
    return _post(HL_URL, {"type": "metaAndAssetCtxs"})


def main():
    args = sys.argv[1:]
    top_n = 20
    json_mode = False

    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-n", "--top"):
            i += 1
            top_n = int(args[i])
        elif arg == "--json":
            json_mode = True
        elif arg in ("--help", "-h"):
            print(__doc__.strip())
            sys.exit(0)
        else:
            print(f"Unknown argument: {arg}", file=sys.stderr)
            sys.exit(1)
        i += 1

    data = fetch_meta_and_ctxs()
    meta = data[0]  # universe metadata
    ctxs = data[1]  # asset contexts

    universe = meta.get("universe", [])

    pairs = []
    for asset_info, ctx in zip(universe, ctxs):
        coin = asset_info["name"]
        volume_24h = float(ctx.get("dayNtlVlm", 0))
        mark_px = float(ctx.get("markPx", 0))
        funding = float(ctx.get("funding", 0))
        open_interest = float(ctx.get("openInterest", 0))
        prev_day_px = float(ctx.get("prevDayPx", 0))
        price_change_pct = ((mark_px - prev_day_px) / prev_day_px * 100) if prev_day_px else 0

        pairs.append({
            "coin": coin,
            "mark_px": mark_px,
            "volume_24h": volume_24h,
            "open_interest": open_interest,
            "funding_rate": funding,
            "price_change_pct": price_change_pct,
        })

    # Sort by 24h volume descending
    pairs.sort(key=lambda x: x["volume_24h"], reverse=True)
    top_pairs = pairs[:top_n]

    if json_mode:
        print(json.dumps(top_pairs, indent=2))
        sys.exit(0)

    # Human-readable output
    header_box(f"Top {top_n} Pairs by 24h Volume on Hyperliquid")

    headers = ["#", "Pair", "Price", "24h Vol ($)", "Open Interest", "Funding", "24h Chg%"]
    rows = []
    for rank, p in enumerate(top_pairs, 1):
        chg = p["price_change_pct"]
        if chg > 0:
            chg_str = green(f"+{chg:.2f}%")
        elif chg < 0:
            chg_str = red(f"{chg:.2f}%")
        else:
            chg_str = dim("0.00%")

        funding = p["funding_rate"]
        if funding > 0:
            fund_str = green(f"{funding:.6f}")
        elif funding < 0:
            fund_str = red(f"{funding:.6f}")
        else:
            fund_str = dim("0.000000")

        rows.append([
            str(rank),
            bold(p["coin"]),
            f"${p['mark_px']:,.4f}" if p["mark_px"] < 10 else f"${p['mark_px']:,.2f}",
            f"${p['volume_24h']:,.0f}",
            f"${p['open_interest']:,.0f}",
            fund_str,
            chg_str,
        ])

    table(
        headers,
        rows,
        alignments=["<", "<", ">", ">", ">", ">", ">"],
    )
    separator()


if __name__ == "__main__":
    main()
    print(api_summary_str(), file=sys.stderr)
