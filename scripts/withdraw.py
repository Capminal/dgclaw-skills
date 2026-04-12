#!/usr/bin/env python3
"""withdraw.py -- Manually withdraw USDC from Hyperliquid trading account.

Bridges USDC from Hyperliquid → Arbitrum → Base.
Minimum 2 USDC. Allow up to 30 min for settlement.

Usage:
    python3 scripts/withdraw.py 50
    python3 scripts/withdraw.py 50 --recipient 0xYourBaseAddress
    python3 scripts/withdraw.py 50 --env ./agent2.env
    python3 scripts/withdraw.py 50 --dry-run
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

MIN_WITHDRAW = 2


def extract_job_id(resp):
    """Extract jobId from ACP response."""
    return resp.get("data", {}).get("jobId")


def main():
    parser = argparse.ArgumentParser(description="Withdraw USDC from Hyperliquid account")
    parser.add_argument("amount", type=float, help="Amount of USDC to withdraw (min 2)")
    parser.add_argument("--recipient", default=None, help="Base address to receive USDC (default: agent wallet)")
    parser.add_argument("--env", default=".env", help="Env file (default: .env)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without placing order")
    args = parser.parse_args()

    if args.amount < MIN_WITHDRAW:
        print(f"ERROR: Minimum withdrawal is {MIN_WITHDRAW} USDC (got {args.amount})")
        sys.exit(1)

    load_env(args.env)
    acp_key = require("LITE_AGENT_API_KEY")

    amount = args.amount

    print(f"Withdraw USDC from Hyperliquid")
    print(f"  Amount    : ${amount}")
    print(f"  Recipient : {args.recipient or 'agent wallet (default)'}")
    print(f"  Route     : Hyperliquid → Arbitrum → Base")
    print(f"  SLA       : up to 30 min for settlement")
    print()

    if not args.dry_run:
        # Check withdrawable balance before submitting
        dgclaw_address = os.environ.get("DGCLAW_ADDRESS")
        if dgclaw_address:
            try:
                api = DgclawAPI(api_key="", base_url=None)
                account_raw = api.account(dgclaw_address)
                account = account_raw.get("data", account_raw)
                withdrawable = float(account.get("withdrawableBalance", 0))
                print(f"  Withdrawable balance: ${withdrawable:.2f}")
                if amount > withdrawable:
                    print(f"ERROR: Requested ${amount} exceeds withdrawable balance ${withdrawable:.2f}")
                    sys.exit(1)
                print()
            except Exception as e:
                print(f"Warning: Could not fetch account balance — {e}")
                print()

    if args.dry_run:
        print("[DRY-RUN] Would withdraw — no transaction placed.")
        return

    acp = AcpAPI(acp_key)

    requirements = {"amount": str(amount)}
    if args.recipient:
        requirements["recipient"] = args.recipient

    try:
        job = acp.create_job(DEGENCLAW_WALLET, "perp_withdraw", requirements, automated=True)

        job_id = extract_job_id(job)
        if not job_id:
            print(f"ERROR: no job ID in response: {job}")
            sys.exit(1)

        print(f"Withdrawing → Job #{job_id}")
        acp.poll_job(job_id, timeout=1800, interval=15, label=f"Withdraw ${amount}")
        print(f"Withdrawal COMPLETED ✓")

    except RuntimeError as e:
        print(f"Withdrawal FAILED ✗ — {e}")
        sys.exit(1)

    print()
    dest = args.recipient or "agent wallet"
    print(f"COMPLETED — ${amount} USDC withdrawn to {dest} (Base)")


if __name__ == "__main__":
    main()
