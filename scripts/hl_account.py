#!/usr/bin/env python3
"""hl_account.py -- Check dgclaw account: balance, positions, and recent trades.

Balance/trades via DgClaw backend (DGCLAW_ADDRESS).
Positions via direct Hyperliquid API (HL_ADDRESS) — same source as hl_positions.py.

Usage:
    python3 scripts/hl_account.py                     # addresses from .env
    python3 scripts/hl_account.py --trades            # include recent closed trades
    python3 scripts/hl_account.py --trades 30         # last 30 closed trades
    python3 scripts/hl_account.py --env ./agent2.env  # multi-agent
    python3 scripts/hl_account.py --json              # raw JSON
"""
import sys
import os
import json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.join(SCRIPT_DIR, "..")
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
sys.path.insert(0, ROOT_DIR)

from api import HyperliquidAPI, DgclawAPI, api_summary_str
from env import load_env, require, get
from fmt import (
    table, header_box, separator,
    pnl_dollar, pct, side_label, bold, dim, green,
)


def usage():
    print("Usage: hl_account.py [--trades [N]] [--env <file>] [--json]")
    print()
    print("Options:")
    print("  --trades [N] Show last N closed trades (default: 20)")
    print("  --env <file> Load env from file (default: .env)")
    print("  --json       Raw JSON output")
    print()
    print("Env vars required:")
    print("  DGCLAW_ADDRESS  — agent wallet (balance + trades)")
    print("  HL_ADDRESS      — Hyperliquid address (positions)")


def fmt_price(p):
    if p is None:
        return dim("--")
    p = float(p)
    if p >= 1000:
        return f"${p:,.2f}"
    elif p >= 1:
        return f"${p:,.4f}"
    elif p >= 0.001:
        return f"${p:,.6f}"
    else:
        return f"${p:.8f}"


def fmt_ts(ts):
    """Format ISO timestamp to short human-readable."""
    if not ts:
        return "--"
    # e.g. "2025-04-10T14:32:00.000Z" → "04-10 14:32"
    s = str(ts)
    try:
        date_part = s[5:10]   # MM-DD
        time_part = s[11:16]  # HH:MM
        return f"{date_part} {time_part}"
    except Exception:
        return s[:16]


def print_account(account, dgclaw_address, hl_address):
    # Fields from DgClaw backend (report_bot.py reference)
    balance = float(account.get("hlBalance", 0))
    withdrawable = float(account.get("withdrawableBalance", 0))

    header_box(f"DgClaw Account: {dgclaw_address[:6]}...{dgclaw_address[-4:]}")
    print(f"  HL Balance    : {bold(f'${balance:,.4f}')} USDC")
    print(f"  Withdrawable  : {green(f'${withdrawable:,.4f}')} USDC")
    print(f"  HL Address    : {hl_address[:6]}...{hl_address[-4:]}")
    separator()


def build_tp_sl(orders, positions_data):
    """Build TP/SL map from reduceOnly orders (same logic as hl_positions.py)."""
    order_prices = {}
    for o in orders:
        if not o.get("reduceOnly", False):
            continue
        coin = o["coin"]
        px = float(o["limitPx"])
        order_prices.setdefault(coin, []).append(px)

    pos_side = {}
    for ap in positions_data:
        pos = ap["position"]
        szi = float(pos["szi"])
        pos_side[pos["coin"]] = "short" if szi < 0 else "long"

    tp_sl = {}
    for coin, prices in order_prices.items():
        prices_sorted = sorted(prices)
        side = pos_side.get(coin)
        if side == "long":
            tp = prices_sorted[-1] if prices_sorted else None
            sl = prices_sorted[0] if len(prices_sorted) > 1 else None
        elif side == "short":
            tp = prices_sorted[0] if prices_sorted else None
            sl = prices_sorted[-1] if len(prices_sorted) > 1 else None
        else:
            tp = sl = None
        tp_sl[coin] = {"tp": tp, "sl": sl}
    return tp_sl


def print_positions(positions_data, orders):
    if not positions_data:
        print(dim("  No open positions."))
        separator()
        return

    tp_sl = build_tp_sl(orders, positions_data)

    headers = ["Pair", "Side", "Entry", "Mark", "Size", "Notional", "uPnL", "ROE%", "TP", "SL"]
    rows = []

    for ap in positions_data:
        pos = ap["position"]
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

        rows.append([
            bold(coin),
            side_label(side),
            fmt_price(entry),
            fmt_price(mark),
            f"{abs(szi):g}",
            f"${pos_value:,.2f}",
            pnl_dollar(upnl),
            pct(roe),
            fmt_price(tp_val) if tp_val else dim("--"),
            fmt_price(sl_val) if sl_val else dim("--"),
        ])

    table(
        headers,
        rows,
        alignments=["<", "<", ">", ">", ">", ">", ">", ">", ">", ">"],
    )
    separator()


def print_trades(trades):
    if not trades:
        print(dim("  No closed trades found."))
        separator()
        return

    headers = ["Time", "Pair", "Side", "Entry", "Exit", "Size", "PnL", "PnL%"]
    rows = []

    for t in trades:
        ts      = fmt_ts(t.get("closedAt", t.get("createdAt", t.get("time"))))
        pair    = t.get("pair", t.get("coin", "?"))
        side    = t.get("direction", t.get("side", "?")).upper()
        entry   = t.get("entryPrice", t.get("entryPx"))
        exit_p  = t.get("exitPrice", t.get("closedPrice", t.get("exitPx")))
        size    = t.get("size", t.get("szi"))
        rpnl    = t.get("realizedPnl", t.get("closedPnl", t.get("pnl")))
        pnl_pct = t.get("pnlPct", t.get("returnPct"))

        try:
            pnl_cell = pnl_dollar(float(rpnl)) if rpnl is not None else dim("--")
        except Exception:
            pnl_cell = dim("--")

        try:
            pct_cell = pct(float(pnl_pct) * 100 if abs(float(pnl_pct)) <= 1 else float(pnl_pct)) if pnl_pct is not None else dim("--")
        except Exception:
            pct_cell = dim("--")

        try:
            size_cell = f"{abs(float(size)):g}" if size is not None else dim("--")
        except Exception:
            size_cell = dim("--")

        rows.append([
            dim(ts),
            bold(pair),
            side_label(side),
            fmt_price(entry),
            fmt_price(exit_p),
            size_cell,
            pnl_cell,
            pct_cell,
        ])

    table(
        headers,
        rows,
        alignments=["<", "<", "<", ">", ">", ">", ">", ">"],
    )
    separator()


def main():
    args = sys.argv[1:]

    env_file = ".env"
    json_mode = False
    show_trades = False
    trade_limit = 20

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--json":
            json_mode = True
        elif arg == "--trades":
            show_trades = True
            if i + 1 < len(args) and args[i + 1].isdigit():
                i += 1
                trade_limit = int(args[i])
        elif arg == "--env":
            i += 1
            if i < len(args):
                env_file = args[i]
        elif arg in ("--help", "-h"):
            usage()
            sys.exit(0)
        else:
            print(f"Unknown argument: {arg}", file=sys.stderr)
            usage()
            sys.exit(1)
        i += 1

    load_env(env_file)

    dgclaw_address = require("DGCLAW_ADDRESS")
    hl_address = require("HL_ADDRESS")

    dgclaw = DgclawAPI(api_key="")
    hl = HyperliquidAPI()

    try:
        account_raw = dgclaw.account(dgclaw_address)
        account = account_raw.get("data", account_raw)
    except Exception as e:
        print(f"Error fetching account: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        state = hl.get_state(hl_address)
        orders = hl.get_orders(hl_address)
        positions_data = state.get("assetPositions", [])
    except Exception as e:
        print(f"Warning: Could not fetch positions — {e}", file=sys.stderr)
        state = {}
        orders = []
        positions_data = []

    trades_raw = []
    if show_trades:
        try:
            trades_raw = dgclaw.closed_trades(dgclaw_address, limit=trade_limit)
            if isinstance(trades_raw, dict):
                trades_raw = trades_raw.get("data", [])
        except Exception as e:
            print(f"Warning: Could not fetch trades — {e}", file=sys.stderr)

    # JSON mode
    if json_mode:
        out = {"account": account, "positions": positions_data, "orders": orders}
        if show_trades:
            out["trades"] = trades_raw
        print(json.dumps(out, indent=2))
        sys.exit(0)

    # Human-readable output
    print_account(account, dgclaw_address, hl_address)

    print(f"  {bold('Open Positions')} ({len(positions_data)})")
    separator()
    print_positions(positions_data, orders)

    if show_trades:
        print(f"  {bold(f'Closed Trades')} (last {trade_limit})")
        separator()
        print_trades(trades_raw)


if __name__ == "__main__":
    main()
    print(api_summary_str(), file=sys.stderr)
