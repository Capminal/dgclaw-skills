#!/usr/bin/env python3
"""hl_positions.py -- Check positions, TP/SL, and account summary from Hyperliquid.

Usage:
    python3 hl_positions.py                    # use HL_ADDRESS from .env
    python3 hl_positions.py 0x1234...          # explicit address
    python3 hl_positions.py --orders           # raw open orders
    python3 hl_positions.py --json             # raw JSON
"""
import sys
import os
import json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.join(SCRIPT_DIR, "..")
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
sys.path.insert(0, ROOT_DIR)

from api import HyperliquidAPI, api_summary_str
from env import load_env, require, get
from fmt import (
    table, header_box, separator, price as fmt_price, price_raw,
    pnl_dollar, pct, side_label, bold, dim, cyan, green, red, yellow,
)


def usage():
    print("Usage: hl_positions.py [<address>] [--orders] [--json]")
    print()
    print("Options:")
    print("  <address>   Wallet address (default: HL_ADDRESS from .env)")
    print("  --orders    Show raw open orders")
    print("  --json      Raw JSON output")


def build_tp_sl(orders, positions_data):
    """Build TP/SL map by matching reduceOnly orders to position side.

    For long positions: TP = highest order price, SL = lowest order price
    For short positions: TP = lowest order price, SL = highest order price
    """
    # Collect all reduceOnly order prices per coin
    order_prices = {}
    for o in orders:
        # Only consider reduceOnly orders for TP/SL inference
        if not o.get("reduceOnly", False):
            continue
        coin = o["coin"]
        px = float(o["limitPx"])
        order_prices.setdefault(coin, []).append(px)

    # Build a map of position side per coin
    pos_side = {}
    for ap in positions_data:
        pos = ap["position"]
        coin = pos["coin"]
        szi = float(pos["szi"])
        pos_side[coin] = "short" if szi < 0 else "long"

    # Assign TP/SL
    tp_sl = {}
    for coin, prices in order_prices.items():
        prices_sorted = sorted(prices)
        side = pos_side.get(coin)
        if side == "short":
            # Short: TP = lowest (buy back cheaper), SL = highest (stop loss higher)
            tp = prices_sorted[0] if prices_sorted else None
            sl = prices_sorted[-1] if len(prices_sorted) > 1 else None
        elif side == "long":
            # Long: TP = highest (sell higher), SL = lowest (stop loss lower)
            tp = prices_sorted[-1] if prices_sorted else None
            sl = prices_sorted[0] if len(prices_sorted) > 1 else None
        else:
            # No matching position, skip
            tp = None
            sl = None
        tp_sl[coin] = {"tp": tp, "sl": sl}

    return tp_sl


def output_be_check(hl, address, be_trigger_pct=1.5):
    """Output compact position data for BE stop check."""
    state = hl.get_state(address)
    orders = hl.get_orders(address)
    frontend_orders = hl.get_frontend_orders(address)
    positions_data = state.get("assetPositions", [])

    if not positions_data:
        print("[]")
        return

    # Sort orders by timestamp descending for "latest" logic
    sorted_orders = sorted(orders, key=lambda o: o.get("timestamp", 0), reverse=True)

    result = []
    for ap in positions_data:
        pos = ap["position"]
        coin = pos["coin"]
        szi = float(pos["szi"])
        side = "short" if szi < 0 else "long"
        entry = float(pos["entryPx"])
        pos_value = float(pos.get("positionValue", 0))
        mark = pos_value / abs(szi) if szi != 0 else 0

        # Compute PnL %
        if side == "long":
            pnl_pct = (mark - entry) / entry * 100 if entry else 0
        else:
            pnl_pct = (entry - mark) / entry * 100 if entry else 0

        # Find latest reduceOnly order on SL side
        latest_sl = None
        for o in sorted_orders:
            if o["coin"] == coin and o.get("reduceOnly", False):
                px = float(o["limitPx"])
                if side == "long" and px < mark:
                    latest_sl = px
                    break
                elif side == "short" and px > mark:
                    latest_sl = px
                    break

        # Fallback: check trigger/stop orders from frontend API
        if latest_sl is None:
            for o in frontend_orders:
                if o.get("coin") != coin:
                    continue
                otype = (o.get("orderType") or "").lower()
                if "stop" not in otype:
                    continue
                trigger_px = float(o.get("triggerPx", 0))
                if trigger_px <= 0:
                    continue
                if side == "long" and trigger_px < mark:
                    latest_sl = trigger_px
                    break
                elif side == "short" and trigger_px > mark:
                    latest_sl = trigger_px
                    break

        # Determine if BE move is needed (threshold from strategy config)
        needs_be = False
        if be_trigger_pct > 0 and pnl_pct >= be_trigger_pct and latest_sl is not None:
            sl_dist = abs(latest_sl - entry) / entry if entry else 0
            needs_be = sl_dist > 0.002  # SL still far from entry

        result.append({
            "coin": coin,
            "side": side,
            "entry": round(entry, 6),
            "mark": round(mark, 6),
            "pnl_pct": round(pnl_pct, 2),
            "latest_sl": round(latest_sl, 6) if latest_sl else None,
            "needs_be": needs_be,
        })

    print(json.dumps(result, separators=(',', ':')))


def main():
    args = sys.argv[1:]

    show_orders = False
    json_mode = False
    be_check = False
    address = ""
    strategy_path = None

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--orders":
            show_orders = True
        elif arg == "--json":
            json_mode = True
        elif arg == "--be-check":
            be_check = True
        elif arg == "--strategy":
            i += 1
            if i < len(args):
                strategy_path = args[i]
        elif arg.startswith("0x"):
            address = arg
        elif arg in ("--help", "-h"):
            usage()
            sys.exit(0)
        else:
            print(f"Unknown argument: {arg}", file=sys.stderr)
            sys.exit(1)
        i += 1

    # Resolve address
    if not address:
        load_env()
        address = get("HL_ADDRESS")
        if not address:
            print("Error: No HL address provided.", file=sys.stderr)
            print("Set HL_ADDRESS in .env or pass address as argument.", file=sys.stderr)
            sys.exit(1)

    hl = HyperliquidAPI()

    # BE check mode
    if be_check:
        be_trigger_pct = 1.5  # default
        if strategy_path:
            try:
                from strategies.lib.loader import load_strategy
                cfg = load_strategy(strategy_path)
                be_trigger_pct = cfg.get("trade_params", {}).get("be_trigger_pct", 1.5)
            except Exception as e:
                print(f"[BE-CHECK] WARNING: failed to load strategy '{strategy_path}': {e} — using default {be_trigger_pct}%", file=sys.stderr)
        print(f"[BE-CHECK] be_trigger_pct={be_trigger_pct}%", file=sys.stderr)
        output_be_check(hl, address, be_trigger_pct)
        sys.exit(0)

    state = hl.get_state(address)
    orders = hl.get_orders(address)

    # JSON mode
    if json_mode:
        print(json.dumps({"state": state, "orders": orders}, indent=2))
        sys.exit(0)

    # Orders mode
    if show_orders:
        print(json.dumps(orders, indent=2))
        sys.exit(0)

    # Human-readable output
    ms = state.get("marginSummary", {})
    positions_data = state.get("assetPositions", [])

    account_value = float(ms.get("accountValue", 0))
    total_notional = float(ms.get("totalNtlPos", 0))
    margin_used = float(ms.get("totalMarginUsed", 0))
    withdrawable = float(ms.get("withdrawable", state.get("withdrawable", 0)))

    header_box(f"Account: {address[:6]}...{address[-4:]}")
    print(f"  Account Value   : {bold(f'${account_value:,.4f}')} USDC")
    print(f"  Total Notional  : ${total_notional:,.4f}")
    print(f"  Margin Used     : ${margin_used:,.4f}")
    print(f"  Withdrawable    : ${withdrawable:,.4f} USDC")
    separator()

    positions = [ap["position"] for ap in positions_data]

    if not positions:
        print(dim("  No open positions."))
        separator()
        return

    # Build TP/SL from reduceOnly orders
    tp_sl = build_tp_sl(orders, positions_data)

    # Position table
    headers = ["Pair", "Side", "Entry", "Mark", "Size", "Margin", "uPnL", "ROE%", "TP", "SL"]
    rows = []
    for pos in positions:
        coin = pos["coin"]
        szi = float(pos["szi"])
        side = "SHORT" if szi < 0 else "LONG"
        entry = float(pos["entryPx"])
        pos_value = float(pos.get("positionValue", 0))
        mark = pos_value / abs(szi) if szi != 0 else 0
        margin = float(pos["marginUsed"])
        upnl = float(pos["unrealizedPnl"])
        roe = float(pos["returnOnEquity"]) * 100

        tpsl = tp_sl.get(coin, {})
        tp_val = tpsl.get("tp")
        sl_val = tpsl.get("sl")
        tp_str = fmt_price(tp_val) if tp_val is not None else dim("--")
        sl_str = fmt_price(sl_val) if sl_val is not None else dim("--")

        # Colored side
        side_cell = side_label(side)

        # Colored uPnL and ROE
        upnl_cell = pnl_dollar(upnl)
        roe_cell = pct(roe)

        rows.append([
            bold(coin),
            side_cell,
            fmt_price(entry),
            fmt_price(mark),
            f"{abs(szi):g}",
            f"${margin:,.2f}",
            upnl_cell,
            roe_cell,
            tp_str,
            sl_str,
        ])

    table(
        headers,
        rows,
        alignments=["<", "<", ">", ">", ">", ">", ">", ">", ">", ">"],
    )
    separator()


if __name__ == "__main__":
    main()
    print(api_summary_str(), file=sys.stderr)
