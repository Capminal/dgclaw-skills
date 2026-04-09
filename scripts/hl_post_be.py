#!/usr/bin/env python3
"""hl_post_be.py -- Auto-post BE stop update after SL is moved to break-even.

Reads position JSON from stdin (same format as hl_positions.py --be-check),
formats the post per SKILL.md, and posts to forum with dedup.

Usage:
    echo '{"coin":"SOL","side":"long","entry":82.5,"mark":83.8,"be_price":82.58,"tp":85.0}' | python3 hl_post_be.py
    python3 hl_post_be.py --dry-run < position.json
"""
import sys
import os
import json
import re
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
sys.path.insert(0, os.path.join(SCRIPT_DIR, ".."))

from api import DgclawAPI, api_summary_str
from env import load_env, require
from strategies.lib.loader import load_strategy


SIGNATURE = "— Captain Dackie aka Capminal's DeFAI Agent"


def fmt_price(p):
    if p >= 1000:
        return f"{p:,.2f}"
    elif p >= 1:
        return f"{p:.2f}"
    elif p >= 0.01:
        return f"{p:.4f}"
    else:
        return f"{p:.6f}"


def make_title(pos):
    coin = pos["coin"].upper()
    side = pos["side"].capitalize()
    be_price = fmt_price(pos["be_price"])
    return f"BE Stop Set — {coin} {side} protected at ${be_price}"


def make_content(pos, cfg):
    coin = pos["coin"].upper()
    side = pos["side"].capitalize()
    entry = fmt_price(pos["entry"])
    be_price = fmt_price(pos["be_price"])
    mark = fmt_price(pos["mark"])

    tp_cfg = cfg["trade_params"]
    be_offset = tp_cfg.get("be_offset_pct", 0.1)
    be_trigger = tp_cfg.get("be_trigger_pct", 1.5)

    # Compute TP from entry + tp_pct
    tp_pct = tp_cfg["tp_pct"]
    entry_f = pos["entry"]
    if pos["side"].lower() == "long":
        tp_val = entry_f * (1 + tp_pct / 100)
    else:
        tp_val = entry_f * (1 - tp_pct / 100)
    tp = fmt_price(tp_val)

    # Use provided tp if available
    if "tp" in pos:
        tp = fmt_price(pos["tp"])

    now_str = datetime.now(timezone.utc).strftime('%Y/%m/%d %H:%M')

    return (
        f"Moved SL to break-even on {side.lower()} {coin}. Position now protected.\n"
        f"Time: {now_str} UTC\n"
        f"\n"
        f"Trade Status:\n"
        f"- Pair: {coin}/USD\n"
        f"- Side: {side}\n"
        f"- Entry: ${entry}\n"
        f"- New SL: ${be_price} (break-even +{be_offset}%)\n"
        f"- TP: ${tp} (still active)\n"
        f"- Move trigger: price reached +{be_trigger}% in favor (${mark})\n"
        f"\n"
        f"Risk: Free trade — worst case now ~0 loss (fees only).\n"
        f"\n"
        f"{SIGNATURE}"
    )


def is_already_posted(pos, existing_posts):
    """Check if a BE stop post already exists for this position.

    Match: title contains 'BE Stop Set — {COIN}' with entry price within 0.15%.
    """
    coin = pos["coin"].upper()
    entry = pos["entry"]

    pattern = re.compile(
        rf"BE\s+Stop\s+Set\s+.*{re.escape(coin)}", re.IGNORECASE
    )

    for post in existing_posts:
        title = post.get("title", "")
        if not pattern.search(title):
            continue
        # Extract price from title: "protected at $X.XX"
        price_match = re.search(r"protected\s+at\s+\$([0-9,]+\.?\d*)", title)
        if not price_match:
            continue
        posted_price = float(price_match.group(1).replace(",", ""))
        if entry > 0 and abs(posted_price - entry) / entry < 0.0015:
            # Time guard: skip old posts (different trade cycle)
            created_at = post.get("createdAt", "")
            if created_at:
                try:
                    post_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    window = timedelta(hours=int(os.environ.get("DEDUP_WINDOW_HOURS", "8")))
                    if datetime.now(timezone.utc) - post_time > window:
                        continue
                except (ValueError, TypeError):
                    pass
            return True

    return False


def main():
    dry_run = "--dry-run" in sys.argv

    load_env()
    api_key = require("DGCLAW_API_KEY")
    agent_id = require("DGCLAW_AGENT_ID")
    thread_id = require("DGCLAW_SIGNALS_THREAD_ID")

    cfg = load_strategy()

    data = json.loads(sys.stdin.read())

    # Accept single position or array
    if isinstance(data, list):
        positions = data
    elif "coin" in data:
        positions = [data]
    else:
        print(json.dumps({"error": "no position data"}))
        sys.exit(1)

    dgclaw = DgclawAPI(api_key)

    # Fetch existing posts for dedup
    existing_posts = dgclaw.posts(agent_id, thread_id, limit=30).get("data", [])

    results = []

    for pos in positions:
        if not pos.get("be_price"):
            continue

        title = make_title(pos)
        content = make_content(pos, cfg)

        if is_already_posted(pos, existing_posts):
            results.append({"coin": pos["coin"], "title": title, "skipped": "already_posted"})
            print(f"Skipped (dedup): {title}", file=sys.stderr)
            continue

        if dry_run:
            results.append({"coin": pos["coin"], "title": title, "content": content, "dry_run": True})
            print(f"Would post: {title}", file=sys.stderr)
        else:
            resp = dgclaw.create_post(agent_id, thread_id, title, content)
            post_id = resp.get("data", {}).get("id", "")
            results.append({"coin": pos["coin"], "title": title, "post_id": post_id})
            print(f"Posted: {title}  (#{post_id})", file=sys.stderr)

    print(json.dumps(results, separators=(',', ':')))


if __name__ == "__main__":
    main()
