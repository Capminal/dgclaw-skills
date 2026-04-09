#!/usr/bin/env python3
"""set_tpsl.py -- Manually set TP/SL for an open position.

Usage:
    python3 scripts/set_tpsl.py <PAIR> <tp> <sl>
    python3 scripts/set_tpsl.py ZEC 231 238.5
    python3 scripts/set_tpsl.py BTC 95000 88000 --env ./agent2.env
    python3 scripts/set_tpsl.py ZEC 231 238.5 --dry-run
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


def main():
    parser = argparse.ArgumentParser(description="Manually set TP/SL for an open position")
    parser.add_argument("pair", help="Trading pair (e.g. ZEC, BTC, ETH)")
    parser.add_argument("tp", type=float, help="Take-profit price")
    parser.add_argument("sl", type=float, help="Stop-loss price")
    parser.add_argument("--env", default=".env", help="Env file (default: .env)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without placing order")
    args = parser.parse_args()

    load_env(args.env)
    acp_key = require("LITE_AGENT_API_KEY")

    pair = args.pair.upper()
    tp = args.tp
    sl = args.sl

    print(f"Set TP/SL — {pair}")
    print(f"  TP: {tp}")
    print(f"  SL: {sl}")

    if args.dry_run:
        print("[DRY-RUN] Would call perp_modify — no order placed.")
        return

    acp = AcpAPI(acp_key)
    job = acp.create_job(DEGENCLAW_WALLET, "perp_modify", {
        "pair": pair,
        "takeProfit": str(tp),
        "stopLoss": str(sl),
    }, automated=True)

    job_id = job.get("data", {}).get("jobId")
    if not job_id:
        print(f"ERROR: no job ID in response: {job}")
        sys.exit(1)

    print(f"Job created: {job_id}")
    acp.poll_job(job_id, timeout=120, interval=10, label=f"TP/SL {pair}")
    print(f"COMPLETED — {pair} TP={tp} SL={sl} set successfully.")


if __name__ == "__main__":
    main()
