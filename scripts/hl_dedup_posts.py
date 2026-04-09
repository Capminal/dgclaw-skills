#!/usr/bin/env python3
"""hl_dedup_posts.py -- Find and remove duplicate forum posts.

Fetches the 20 latest posts, groups by (type, pair, side, ~entry_price, ~exit_price),
keeps the newest post in each group, and deletes the rest.

Usage:
    python3 hl_dedup_posts.py              # dry-run (default safe)
    python3 hl_dedup_posts.py --delete     # actually delete duplicates
"""
import sys
import os
import json
import re
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))

from api import DgclawAPI, api_summary_str
from env import load_env, require


# ── Post key extraction (same logic as dgclaw_cli.py) ────────────────────────

def _extract_entry_price(text):
    m = re.search(r'[Ee]ntry[:\s]*\$?([\d.,]+)', text)
    if m:
        try:
            return float(m.group(1).replace(',', '').rstrip('.'))
        except ValueError:
            return None
    return None


def _extract_exit_price(text):
    m = re.search(r'[Ee]xit[:\s]*\$?([\d.,]+)', text)
    if m:
        try:
            return float(m.group(1).replace(',', '').rstrip('.'))
        except ValueError:
            return None
    return None


def _extract_pnl(text):
    """Extract PnL dollar amount from post content."""
    m = re.search(r'PnL:\s*[+-]?\$?([\d.,]+)', text)
    if m:
        try:
            return float(m.group(1).replace(',', ''))
        except ValueError:
            return None
    return None


def _extract_time(text):
    """Extract Time: YYYY/MM/DD HH:MM from post content."""
    m = re.search(r'Time:\s*(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2})', text)
    if m:
        try:
            return datetime.strptime(m.group(1), '%Y/%m/%d %H:%M').replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def extract_key(title, content, created_at=None):
    """Extract (type, pair, side, entry_price, exit_price, time) from a post.

    For closed posts, exit_price is included to distinguish different trades
    on the same pair/side that happen to have similar entry prices.
    For open/BE posts, exit_price is None.
    Time is extracted from content 'Time:' line, with createdAt as fallback.
    """
    tl = title.lower()

    post_time = _extract_time(content)
    # Fallback to post's createdAt metadata for older posts without Time: line
    if post_time is None and created_at:
        try:
            post_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    m = re.search(r'closed\s+(\w+)\s+(short|long)', tl)
    if m:
        return ('closed', m.group(1).upper(), m.group(2).lower(),
                _extract_entry_price(content), _extract_exit_price(content), post_time,
                _extract_pnl(content))

    m = re.search(r'closed[:\s]+(short|long)\s+(\w+)', tl)
    if m:
        return ('closed', m.group(2).upper(), m.group(1).lower(),
                _extract_entry_price(content), _extract_exit_price(content), post_time,
                _extract_pnl(content))

    if 'be stop set' in tl:
        m = re.search(r'(\w+)\s+(short|long)', tl)
        if m:
            return ('be', m.group(1).upper(), m.group(2).lower(),
                    _extract_entry_price(content), None, post_time)

    m = re.search(r'(short|long)\s+(\w+)\s+@\s+\$?([\d.,]+)', tl)
    if m:
        try:
            entry = float(m.group(3).replace(',', ''))
        except ValueError:
            entry = None
        return ('open', m.group(2).upper(), m.group(1).lower(), entry, None, post_time)

    return None


def _prices_match(p1, p2, tolerance=0.0015):
    """Check if two prices match within tolerance. None matches only None."""
    if p1 is None and p2 is None:
        return True
    if p1 is None or p2 is None:
        return False
    if p1 == 0 and p2 == 0:
        return True
    if p1 == 0 or p2 == 0:
        return False
    return abs(p1 - p2) / max(abs(p1), abs(p2)) < tolerance


def keys_match(k1, k2, tolerance=0.0015):
    """Check if two keys represent the same trade (0.15% price tolerance).

    For closed posts: require BOTH entry AND exit price to match,
    preventing different trades on the same pair from being flagged.
    """
    if k1 is None or k2 is None:
        return False
    if k1[0] != k2[0] or k1[1] != k2[1] or k1[2] != k2[2]:
        return False
    if not _prices_match(k1[3], k2[3], tolerance):
        return False
    # For closed posts, also require exit price AND PnL match
    if k1[0] == 'closed':
        if not _prices_match(k1[4], k2[4], tolerance):
            return False
        # PnL must match within 5% to distinguish different trades on same pair
        pnl1 = k1[6] if len(k1) > 6 else None
        pnl2 = k2[6] if len(k2) > 6 else None
        if pnl1 is not None and pnl2 is not None and max(pnl1, pnl2) > 0:
            if abs(pnl1 - pnl2) / max(pnl1, pnl2) >= 0.05:
                return False
    # Time proximity check: if both posts have time, must be within window
    t1 = k1[5] if len(k1) > 5 else None
    t2 = k2[5] if len(k2) > 5 else None
    if t1 is not None and t2 is not None:
        window = timedelta(hours=int(os.environ.get("DEDUP_WINDOW_HOURS", "8")))
        if abs(t1 - t2) > window:
            return False
    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def run(do_delete=False):
    load_env()

    api_key = require("DGCLAW_API_KEY")
    agent_id = require("DGCLAW_AGENT_ID")
    thread_id = require("DGCLAW_SIGNALS_THREAD_ID")

    dgclaw = DgclawAPI(api_key)

    # 1. Fetch 20 latest posts
    posts = dgclaw.posts(agent_id, thread_id, limit=40).get("data", [])
    if not posts:
        print("No posts found.")
        return

    # 2. Sort by createdAt descending (newest first) to keep the most recent occurrence.
    # Keeping newest is critical for closed-trade posts: if an old post from a prior
    # trade predates the current trade's closedAt, is_already_posted Guard 1 would
    # reject it and the bot would re-post indefinitely. Keeping the newest ensures
    # is_already_posted always finds a post with createdAt >= closedAt.
    posts_sorted = sorted(posts, key=lambda p: p.get("createdAt", ""), reverse=True)

    # 3. Find duplicates — keep earliest, mark later as duplicates
    kept = []       # (post, key)
    to_delete = []  # (post, key, original_post)

    for post in posts_sorted:
        key = extract_key(post.get("title", ""), post.get("content", ""), post.get("createdAt", ""))
        if key is None:
            kept.append((post, key))
            continue

        # Check if this key matches any already-kept post
        match = None
        for kept_post, kept_key in kept:
            if keys_match(key, kept_key):
                match = kept_post
                break

        if match:
            to_delete.append((post, key, match))
        else:
            kept.append((post, key))

    # 4. Report
    print(f"Posts scanned: {len(posts)}")
    print(f"Unique: {len(kept)}")
    print(f"Duplicates: {len(to_delete)}")

    if not to_delete:
        print("\nNo duplicates found.")
        return

    print()
    for post, key, original in to_delete:
        key_label = f"{key[0]}:{key[1]}:{key[2]}" if key else "?"
        print(f"  DELETE #{post['id']}  {post.get('title', '')[:50]}")
        print(f"    dup of #{original['id']}  {original.get('title', '')[:50]}")
        print(f"    key: {key_label}")
        print()

    # 5. Delete if requested
    if do_delete:
        forum_key = require("DGCLAW_FORUM_API_KEY")
        deleted = 0
        for post, key, original in to_delete:
            try:
                dgclaw.delete_post(agent_id, thread_id, post["id"], forum_key)
                print(f"  Deleted #{post['id']}")
                deleted += 1
            except Exception as e:
                print(f"  Failed to delete #{post['id']}: {e}")
        print(f"\nDeleted {deleted}/{len(to_delete)} duplicate posts.")
    else:
        print(f"Dry run — add --delete to remove {len(to_delete)} duplicates.")

    print(f"\n{api_summary_str()}")


def main():
    do_delete = "--delete" in sys.argv
    run(do_delete=do_delete)


if __name__ == "__main__":
    main()
