#!/usr/bin/env python3
"""open_trade.py -- Manually open a trade with TP/SL.

Uses notional & leverage from strategy config. Sets TP/SL after opening.

Usage:
    python3 scripts/open_trade.py ZORA long 150.5 148.0
    python3 scripts/open_trade.py BTC short 95000 98000
    python3 scripts/open_trade.py SOL long 180 170 --env ./agent2.env
    python3 scripts/open_trade.py SOL long 180 170 --dry-run
"""
import re
import sys
import os
import argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.join(SCRIPT_DIR, "..")
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
sys.path.insert(0, ROOT_DIR)

from api import AcpAPI, DEGENCLAW_WALLET
from env import load_env, require
from strategies.lib.loader import load_strategy


def extract_job_id(resp):
    """Extract jobId from ACP response."""
    return resp.get("data", {}).get("jobId")


def main():
    parser = argparse.ArgumentParser(description="Manually open a trade with TP/SL")
    parser.add_argument("pair", help="Trading pair (e.g. ZORA, BTC, ETH)")
    parser.add_argument("side", choices=["long", "short"], help="Trade direction")
    parser.add_argument("tp", type=float, help="Take-profit price")
    parser.add_argument("sl", type=float, help="Stop-loss price")
    parser.add_argument("--env", default=".env", help="Env file (default: .env)")
    parser.add_argument("--strategy", default=None, help="Strategy config file")
    parser.add_argument("--dry-run", action="store_true", help="Preview without placing order")
    args = parser.parse_args()

    load_env(args.env)
    acp_key = require("LITE_AGENT_API_KEY")

    cfg = load_strategy(args.strategy)
    tp_cfg = cfg["trade_params"]
    notional = tp_cfg["notional"]
    leverage = tp_cfg["leverage"]

    pair = args.pair.upper()
    side = args.side
    tp = args.tp
    sl = args.sl

    print(f"Open {side.upper()} {pair}")
    print(f"  Notional : ${notional}")
    print(f"  Leverage : {leverage}x")
    print(f"  TP       : {tp}")
    print(f"  SL       : {sl}")
    print()

    if args.dry_run:
        print("[DRY-RUN] Would open trade and set TP/SL — no order placed.")
        return

    acp = AcpAPI(acp_key)

    # Step 1: Open trade
    try:
        effective_leverage = leverage
        job = acp.create_job(DEGENCLAW_WALLET, "perp_trade", {
            "action": "open",
            "pair": pair,
            "side": side,
            "size": str(notional),
            "leverage": effective_leverage,
        }, automated=True)

        job_id = extract_job_id(job)
        if not job_id:
            print(f"ERROR: no job ID in response: {job}")
            sys.exit(1)

        print(f"Opening → Job #{job_id}")
        try:
            acp.poll_job(job_id, timeout=300, interval=10, label=f"Trade {pair}")
            print(f"Trade OPENED ✓")
        except RuntimeError as e:
            # Handle leverage rejection — retry with max allowed
            m = re.search(r"exceeds max (\d+)x", str(e))
            if m:
                max_lev = int(m.group(1))
                print(f"Leverage {effective_leverage}x rejected — retrying with {max_lev}x")
                retry_job = acp.create_job(DEGENCLAW_WALLET, "perp_trade", {
                    "action": "open",
                    "pair": pair,
                    "side": side,
                    "size": str(notional),
                    "leverage": max_lev,
                }, automated=True)
                retry_id = extract_job_id(retry_job)
                if not retry_id:
                    print(f"ERROR: retry — no job ID in response")
                    sys.exit(1)
                print(f"Retry → Job #{retry_id}")
                acp.poll_job(retry_id, timeout=300, interval=10, label=f"Trade {pair} retry")
                print(f"Trade OPENED ✓ (leverage {max_lev}x)")
            else:
                raise

    except RuntimeError as e:
        print(f"Trade FAILED ✗ — {e}")
        sys.exit(1)

    # Step 2: Set TP/SL
    print()
    print(f"Setting TP/SL — TP {tp} | SL {sl}")
    try:
        job = acp.create_job(DEGENCLAW_WALLET, "perp_modify", {
            "pair": pair,
            "takeProfit": str(round(tp, 6)),
            "stopLoss": str(round(sl, 6)),
        }, automated=True)

        job_id = extract_job_id(job)
        if not job_id:
            print(f"ERROR: no job ID for TP/SL: {job}")
            sys.exit(1)

        acp.poll_job(job_id, timeout=120, interval=10, label=f"TP/SL {pair}")
        print(f"TP/SL SET ✓")

    except RuntimeError as e:
        print(f"TP/SL FAILED ✗ — {e}")
        sys.exit(1)

    print()
    print(f"COMPLETED — {side.upper()} {pair} opened, TP={tp} SL={sl}")


if __name__ == "__main__":
    main()
