#!/usr/bin/env python3
"""hl_post_signal.py -- Auto-post opening signal after trade is placed.

Reads signal JSON from stdin (same format as hl_scalp.py compact output),
formats the post per SKILL.md, and posts to forum with dedup.

Usage:
    echo '{"coin":"SOL","signal":"long","entry":82.5,"tp":85.0,"sl":81.3,"adx":22.5,"groups":3,"detail":["A:pullback_to_ema","B:rsi<48","C:volume+green"]}' | python3 hl_post_signal.py
    python3 hl_post_signal.py --dry-run < signal.json
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


def make_title(sig):
    side = sig["signal"].capitalize()
    coin = sig["coin"].upper()
    entry = fmt_price(sig["entry"])
    tp = fmt_price(sig["tp"])
    return f"{side} {coin} @ ${entry} — Targeting ${tp}"


def make_content(sig, tp_cfg):
    side = sig["signal"].capitalize()
    coin = sig["coin"].upper()
    entry = fmt_price(sig["entry"])
    tp = fmt_price(sig["tp"])
    sl = fmt_price(sig["sl"])
    adx = sig.get("adx", 0)
    groups = sig.get("groups", 0)
    detail = sig.get("detail", [])
    leverage = tp_cfg["leverage"]
    notional = tp_cfg["notional"]
    tp_pct = tp_cfg["tp_pct"]
    sl_pct = tp_cfg["sl_pct"]
    rr = round(tp_pct / sl_pct, 1) if sl_pct > 0 else 0

    trend = "UPTREND" if sig["signal"] == "long" else "DOWNTREND"
    total = 4 if sig.get("ai_judge") is not None else 3
    groups_str = ", ".join(detail) if detail else f"{groups}/{total} groups"

    ai_str = ""
    if sig.get("ai_judge") is True:
        ai_str = " AI Judge: PASS."
    elif sig.get("ai_judge") is False:
        ai_str = " AI Judge: FAIL."

    now_str = datetime.now(timezone.utc).strftime('%Y/%m/%d %H:%M')

    return (
        f"Signal: {side} {coin} at ${entry}.\n"
        f"Time: {now_str} UTC\n"
        f"\n"
        f"Setup:\n"
        f"- Pair: {coin}/USD\n"
        f"- Side: {side}\n"
        f"- Entry: ${entry}\n"
        f"- Leverage: {leverage}x\n"
        f"- Size: ~${notional} notional\n"
        f"\n"
        f"Risk Management:\n"
        f"- TP: ${tp} (+{tp_pct}%)\n"
        f"- SL: ${sl} (-{sl_pct}%)\n"
        f"- R/R: {rr}:1\n"
        f"\n"
        f"Thesis: {trend} (ADX {adx}). {groups_str}.{ai_str}\n"
        f"\n"
        f"Will update when the trade closes.\n"
        f"\n"
        f"{SIGNATURE}"
    )


def is_already_posted(sig, existing_posts):
    """Check if an opening signal post already exists for this trade.

    Match: title contains '{Side} {COIN}' with entry price within 0.15%.
    """
    coin = sig["coin"].upper()
    side = sig["signal"].capitalize()
    entry = sig["entry"]

    pattern = re.compile(
        rf"{re.escape(side)}\s+{re.escape(coin)}\s+@\s+\$", re.IGNORECASE
    )

    for post in existing_posts:
        title = post.get("title", "")
        if not pattern.search(title):
            continue
        # Extract entry price from title: "{Side} {COIN} @ $X.XX"
        price_match = re.search(r"@\s+\$([0-9,]+\.?\d*)", title)
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
    tp_cfg = cfg["trade_params"]

    data = json.loads(sys.stdin.read())

    # Accept single signal dict or pick from signals array
    if "signals" in data:
        signals = data["signals"]
    elif "coin" in data:
        signals = [data]
    else:
        print(json.dumps({"error": "no signal data"}))
        sys.exit(1)

    dgclaw = DgclawAPI(api_key)

    # Fetch existing posts for dedup
    existing_posts = dgclaw.posts(agent_id, thread_id, limit=30).get("data", [])

    results = []

    for sig in signals:
        if sig.get("signal") not in ("long", "short"):
            continue

        title = make_title(sig)
        content = make_content(sig, tp_cfg)

        if is_already_posted(sig, existing_posts):
            results.append({"coin": sig["coin"], "title": title, "skipped": "already_posted"})
            print(f"Skipped (dedup): {title}", file=sys.stderr)
            continue

        if dry_run:
            results.append({"coin": sig["coin"], "title": title, "content": content, "dry_run": True})
            print(f"Would post: {title}", file=sys.stderr)
        else:
            resp = dgclaw.create_post(agent_id, thread_id, title, content)
            post_id = resp.get("data", {}).get("id", "")
            results.append({"coin": sig["coin"], "title": title, "post_id": post_id})
            print(f"Posted: {title}  (#{post_id})", file=sys.stderr)

    print(json.dumps(results, separators=(',', ':')))


if __name__ == "__main__":
    main()
