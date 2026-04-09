#!/usr/bin/env python3
"""hl_price.py -- Query token prices from Hyperliquid.

Usage:
    python3 hl_price.py ETH BTC SOL    # multiple tokens
    python3 hl_price.py --all           # top 30 sorted by name
    python3 hl_price.py --search doge   # case-insensitive search
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from api import HyperliquidAPI
from fmt import (
    table, header_box, price as fmt_price, bold, dim, cyan, green, yellow,
)


def usage():
    print("Usage: hl_price.py <TOKEN...> | --all | --search <query>")
    print()
    print("Examples:")
    print("  python3 hl_price.py ETH")
    print("  python3 hl_price.py ETH BTC SOL")
    print("  python3 hl_price.py --all")
    print("  python3 hl_price.py --search doge")


def main():
    args = sys.argv[1:]
    if not args:
        usage()
        sys.exit(0)

    hl = HyperliquidAPI()
    prices = hl.get_prices()

    if args[0] == "--all":
        header_box("Hyperliquid Prices (Top 30)")
        pairs = sorted(prices.items(), key=lambda x: x[0])[:30]
        rows = []
        for token, val in pairs:
            p = float(val)
            rows.append([bold(token), green(fmt_price(p))])
        table(
            ["Token", "Price"],
            rows,
            alignments=["<", ">"],
        )
        print(dim(f"\n  ({len(prices)} tokens total, showing top 30)"))

    elif args[0] == "--search":
        if len(args) < 2:
            print("Usage: hl_price.py --search <query>", file=sys.stderr)
            sys.exit(1)
        query = args[1].lower()
        matches = [(k, v) for k, v in sorted(prices.items()) if query in k.lower()]
        if not matches:
            print(f'No tokens matching "{query}"')
            sys.exit(0)
        header_box(f'Search: "{args[1]}"')
        rows = []
        for token, val in matches:
            p = float(val)
            rows.append([bold(token), green(fmt_price(p))])
        table(
            ["Token", "Price"],
            rows,
            alignments=["<", ">"],
        )

    else:
        # Specific tokens
        rows = []
        for token in args:
            upper = token.upper()
            val = prices.get(upper)
            if val is None:
                rows.append([bold(upper), yellow("not found")])
            else:
                p = float(val)
                rows.append([bold(upper), green(fmt_price(p))])
        if len(rows) == 1:
            # Single token: simple one-line output
            print(f"  {rows[0][0]}  {rows[0][1]}")
        else:
            table(
                ["Token", "Price"],
                rows,
                alignments=["<", ">"],
            )


if __name__ == "__main__":
    main()
