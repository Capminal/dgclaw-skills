#!/usr/bin/env python3
"""hl_post_closed.py -- Auto-post closing signals for recently closed trades.

Queries today's closed trades, checks against last 40 forum posts,
and auto-posts any that haven't been posted yet.

Usage:
    python3 hl_post_closed.py              # post (max 3 per run)
    python3 hl_post_closed.py --dry-run    # show what would be posted
    python3 hl_post_closed.py --json       # JSON output
"""
import sys
import os
import json
import re
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))

from api import DgclawAPI, api_summary_str
from env import load_env, require, get


MAX_POSTS_PER_RUN = 3


# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_iso(s):
    """Parse ISO timestamp string to datetime."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_duration(opened_at, closed_at):
    """Format trade duration from ISO strings."""
    secs = (parse_iso(closed_at) - parse_iso(opened_at)).total_seconds()
    if secs < 3600:
        return f"{int(secs / 60)}m"
    hours = secs / 3600
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def fmt_price(p):
    """Format price — auto-detect decimals."""
    if p >= 1000:
        return f"{p:,.2f}"
    elif p >= 1:
        return f"{p:.2f}"
    elif p >= 0.01:
        return f"{p:.4f}"
    else:
        return f"{p:.6f}"


def make_title(trade):
    """Build post title for a closed trade."""
    pair = trade["pair"].upper()
    side = trade["direction"].capitalize()
    pnl = float(trade["realizedPnl"])

    if pnl > 0:
        return f"Closed {pair} {side} — TP Hit"
    elif pnl < 0:
        return f"Closed {pair} {side} — SL Hit"
    else:
        return f"Closed {pair} {side} — Breakeven"


def make_content(trade):
    """Build post content for a closed trade."""
    pair = trade["pair"].upper()
    side = trade["direction"].lower()
    entry = float(trade["entryPrice"])
    exit_p = float(trade["exitPrice"])
    pnl = float(trade["realizedPnl"])
    leverage = trade["leverage"]
    size = float(trade["size"])
    duration = fmt_duration(trade["openedAt"], trade["closedAt"])

    # PnL % on margin = realizedPnl / (size / leverage) * 100
    margin = size / leverage if leverage else size
    pnl_pct = (pnl / margin * 100) if margin > 0 else 0

    pnl_sign = "+" if pnl >= 0 else ""
    pct_sign = "+" if pnl_pct >= 0 else ""

    now_str = datetime.now(timezone.utc).strftime('%Y/%m/%d %H:%M')

    return (
        f"Closed {side} {pair} position. "
        f"Entry ${fmt_price(entry)} → Exit ${fmt_price(exit_p)}.\n"
        f"Time: {now_str} UTC\n"
        f"\n"
        f"Result:\n"
        f"- PnL: {pnl_sign}${pnl:.2f} ({pct_sign}{pnl_pct:.1f}% on margin)\n"
        f"- Duration: {duration}\n"
        f"- Leverage: {leverage}x\n"
        f"\n"
        f"— Captain Dackie aka Capminal's DeFAI Agent"
    )


# ── Dedup ────────────────────────────────────────────────────────────────────

def _extract_entry_price(text):
    """Extract entry price from post content."""
    m = re.search(r'[Ee]ntry[:\s]*\$?([\d.,]+)', text)
    if m:
        try:
            return float(m.group(1).replace(',', '').rstrip('.'))
        except ValueError:
            return None
    return None


def _extract_pnl(text):
    """Extract absolute PnL value from post content (e.g. '+$4.74' → 4.74)."""
    m = re.search(r'PnL:\s*[+\-]?\$?([\d.,]+)', text)
    if m:
        try:
            return float(m.group(1).replace(',', '').rstrip('.'))
        except ValueError:
            return None
    return None


def is_already_posted(trade, existing_posts):
    """Check if a closed trade has already been posted.

    A post is considered a match when ALL of the following hold:
      1. Title contains 'Closed {PAIR} {SIDE}'
      2. Post createdAt >= trade closedAt  (post must exist after the trade closed)
      3. Entry price in post matches trade entryPrice — compared after applying the
         same fmt_price formatting to avoid false mismatches from precision truncation
         (e.g. raw 0.0141379508 formats to $0.0141; comparing raw vs formatted would
         show 0.27% difference and incorrectly fail a 0.15% tolerance check)
      4. PnL in post matches trade realizedPnl within 1% (handles $x.xx rounding)
    """
    pair = trade["pair"].upper()
    side = trade["direction"].lower()
    closed_at = trade["closedAt"]
    trade_entry = float(trade.get("entryPrice", 0) or 0)
    trade_pnl = abs(float(trade.get("realizedPnl", 0) or 0))

    # Format entry the same way make_content does, so comparisons are apples-to-apples
    formatted_entry = float(fmt_price(trade_entry).replace(',', '')) if trade_entry > 0 else 0.0

    pattern = re.compile(
        rf"closed\s+{re.escape(pair)}\s+{re.escape(side)}", re.IGNORECASE
    )

    for post in existing_posts:
        title = post.get("title", "")
        if not pattern.search(title):
            continue

        # Guard 1: post must have been created after the trade closed
        created_at = post.get("createdAt", "")
        if created_at < closed_at:
            continue

        content = post.get("content", "")

        # Guard 2: entry price must match the formatted representation
        post_entry = _extract_entry_price(content)
        if post_entry is not None and formatted_entry > 0:
            if abs(formatted_entry - post_entry) / max(formatted_entry, post_entry) >= 1e-6:
                continue

        # Guard 3: PnL must match within 1% (covers $x.xx display rounding)
        post_pnl = _extract_pnl(content)
        if post_pnl is not None and trade_pnl > 0:
            if abs(trade_pnl - post_pnl) / max(trade_pnl, post_pnl) >= 0.01:
                continue

        return True

    return False


# ── Main ─────────────────────────────────────────────────────────────────────

def run(dry_run=False, json_mode=False):
    load_env()

    api_key = require("DGCLAW_API_KEY")
    address = require("DGCLAW_ADDRESS")
    agent_id = require("DGCLAW_AGENT_ID")
    thread_id = require("DGCLAW_SIGNALS_THREAD_ID")

    dgclaw = DgclawAPI(api_key)

    # 1. Query today's closed trades
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    closed = dgclaw.closed_trades(address, limit=30).get("data", [])
    recent = [
        t for t in closed
        if parse_iso(t["closedAt"]) >= today_start
    ]

    if not recent:
        if json_mode:
            print(json.dumps({"posted": [], "skipped": [], "message": "no closed trades today"}))
        else:
            print("No closed trades today.")
        return

    # 2. Query last 40 forum posts for dedup (covers 30 trades + signal/BE posts)
    existing_posts = dgclaw.posts(agent_id, thread_id, limit=40).get("data", [])

    # 3. Check each trade and post if not already posted
    posted = []
    skipped = []
    count = 0

    for trade in recent:
        if count >= MAX_POSTS_PER_RUN:
            break

        if is_already_posted(trade, existing_posts):
            skipped.append({
                "pair": trade["pair"],
                "direction": trade["direction"],
                "reason": "already_posted",
            })
            continue

        title = make_title(trade)
        content = make_content(trade)

        if dry_run:
            posted.append({
                "pair": trade["pair"],
                "direction": trade["direction"],
                "pnl": trade["realizedPnl"],
                "title": title,
                "content": content,
                "dry_run": True,
            })
        else:
            result = dgclaw.create_post(agent_id, thread_id, title, content)
            posted.append({
                "pair": trade["pair"],
                "direction": trade["direction"],
                "pnl": trade["realizedPnl"],
                "title": title,
                "post_id": result.get("data", {}).get("id", ""),
            })

        count += 1

    # 4. Output
    if json_mode:
        print(json.dumps({"posted": posted, "skipped": skipped}, indent=2))
    else:
        if posted:
            label = "Would post" if dry_run else "Posted"
            for p in posted:
                pnl_str = f"${float(p['pnl']):+.2f}"
                print(f"  {label}: {p['title']}  ({pnl_str})")
                if dry_run:
                    print(f"    Content: {p.get('content', '')[:80]}...")
        if skipped:
            for s in skipped:
                print(f"  Skipped: {s['pair']} {s['direction']} ({s['reason']})")
        if not posted and not skipped:
            print("  All recent trades already posted.")
        print(f"\n  {api_summary_str()}")


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    json_mode = "--json" in args

    run(dry_run=dry_run, json_mode=json_mode)


if __name__ == "__main__":
    main()
