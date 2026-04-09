#!/usr/bin/env python3
"""close_trade.py -- Close open positions by coin.

Usage:
    python3 scripts/close_trade.py ZEC ZORA
    python3 scripts/close_trade.py ZEC --env ./agent2.env
    python3 scripts/close_trade.py ZEC ZORA --dry-run
"""
import sys
import os
import argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.join(SCRIPT_DIR, "..")
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
sys.path.insert(0, ROOT_DIR)

from api import AcpAPI, DgclawAPI, DEGENCLAW_WALLET
from env import load_env, require


def get_open_pairs(dgclaw, address):
    """Return set of uppercase coin symbols with open positions."""
    positions_data = dgclaw.positions(address)
    positions = positions_data if isinstance(positions_data, list) else positions_data.get("data", [])
    pairs = set()
    for p in positions:
        pair = p.get("pair", p.get("coin", "")).upper()
        if pair:
            pairs.add(pair)
    return pairs


def main():
    parser = argparse.ArgumentParser(description="Close open positions by coin")
    parser.add_argument("coins", nargs="+", help="Coins to close (e.g. ZEC ZORA)")
    parser.add_argument("--env", default=".env", help="Env file (default: .env)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without closing")
    args = parser.parse_args()

    load_env(args.env)
    acp_key = require("LITE_AGENT_API_KEY")
    api_key = require("DGCLAW_API_KEY")
    address = require("DGCLAW_ADDRESS")

    dgclaw = DgclawAPI(api_key)
    acp = AcpAPI(acp_key)

    # Fetch open positions once
    open_pairs = get_open_pairs(dgclaw, address)
    print(f"Open positions: {', '.join(sorted(open_pairs)) if open_pairs else 'none'}")
    print()

    closed = []
    skipped = []

    for coin in args.coins:
        coin = coin.upper()
        if coin not in open_pairs:
            print(f"  {coin}: SKIP — no open position")
            skipped.append(coin)
            continue

        if args.dry_run:
            print(f"  {coin}: [DRY-RUN] Would close position")
            closed.append(coin)
            continue

        try:
            job = acp.create_job(DEGENCLAW_WALLET, "perp_trade", {
                "action": "close",
                "pair": coin,
            }, automated=True)

            job_id = job.get("data", {}).get("jobId")
            if not job_id:
                print(f"  {coin}: ERROR — no job ID in response: {job}")
                continue

            print(f"  {coin}: Closing → Job #{job_id}")
            acp.poll_job(job_id, timeout=120, interval=10, label=f"Close {coin}")
            print(f"  {coin}: CLOSED ✓")
            closed.append(coin)

        except RuntimeError as e:
            print(f"  {coin}: FAILED ✗ — {e}")
        except Exception as e:
            print(f"  {coin}: ERROR — {e}")

    # Summary
    print()
    if closed:
        print(f"Closed: {', '.join(closed)}")
    if skipped:
        print(f"Skipped: {', '.join(skipped)}")


if __name__ == "__main__":
    main()
