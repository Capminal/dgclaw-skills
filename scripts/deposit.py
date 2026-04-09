#!/usr/bin/env python3
"""deposit.py -- Manually deposit USDC into Hyperliquid trading account.

Bridges USDC from agent wallet (Base) → Arbitrum → Hyperliquid.
Minimum 6 USDC. Allow up to 30 min for settlement before trading.

Usage:
    python3 scripts/deposit.py 100
    python3 scripts/deposit.py 50 --env ./agent2.env
    python3 scripts/deposit.py 100 --dry-run
"""
import sys
import os
import argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.join(SCRIPT_DIR, "..")
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
sys.path.insert(0, ROOT_DIR)

from api import AcpAPI, DEGENCLAW_WALLET
from env import load_env, require

MIN_DEPOSIT = 6


def extract_job_id(resp):
    """Extract jobId from ACP response."""
    return resp.get("data", {}).get("jobId")


def main():
    parser = argparse.ArgumentParser(description="Deposit USDC into Hyperliquid account")
    parser.add_argument("amount", type=float, help="Amount of USDC to deposit (min 6)")
    parser.add_argument("--env", default=".env", help="Env file (default: .env)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without placing order")
    args = parser.parse_args()

    if args.amount < MIN_DEPOSIT:
        print(f"ERROR: Minimum deposit is {MIN_DEPOSIT} USDC (got {args.amount})")
        sys.exit(1)

    load_env(args.env)
    acp_key = require("LITE_AGENT_API_KEY")

    amount = args.amount

    print(f"Deposit USDC to Hyperliquid")
    print(f"  Amount : ${amount}")
    print(f"  Route  : Base → Arbitrum → Hyperliquid")
    print(f"  SLA    : up to 30 min for settlement")
    print()

    if args.dry_run:
        print("[DRY-RUN] Would deposit — no transaction placed.")
        return

    acp = AcpAPI(acp_key)

    try:
        job = acp.create_job(DEGENCLAW_WALLET, "perp_deposit", {
            "amount": str(amount),
        }, automated=True)

        job_id = extract_job_id(job)
        if not job_id:
            print(f"ERROR: no job ID in response: {job}")
            sys.exit(1)

        print(f"Depositing → Job #{job_id}")
        acp.poll_job(job_id, timeout=1800, interval=15, label=f"Deposit ${amount}")
        print(f"Deposit COMPLETED ✓")

    except RuntimeError as e:
        print(f"Deposit FAILED ✗ — {e}")
        sys.exit(1)

    print()
    print(f"COMPLETED — ${amount} USDC deposited to Hyperliquid")


if __name__ == "__main__":
    main()
