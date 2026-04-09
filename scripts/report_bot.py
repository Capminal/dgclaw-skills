#!/usr/bin/env python3
"""report_bot.py -- Telegram trading report bot replacing OpenClaw AI cron.

Collects account data, positions, closed trades, BE alerts, and leaderboard
rank, then sends a concise report to Telegram. Runs as a PM2-managed loop.

Usage:
    python3 scripts/report_bot.py --once                # single report
    python3 scripts/report_bot.py --once --dry-run      # preview, don't send
    python3 scripts/report_bot.py --interval 1800       # loop every 30 min
    python3 scripts/report_bot.py --env ./agent2.env    # multi-agent
"""
import sys
import os
import json
import time
import signal
import logging
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ── Path setup ───────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.join(SCRIPT_DIR, "..")
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
sys.path.insert(0, ROOT_DIR)

from api import HyperliquidAPI, DgclawAPI, api_summary, _api_log
from env import load_env, require, get

# ── Constants ────────────────────────────────────────────────────────────────

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_INTERVAL = 1800  # 30 minutes
DEFAULT_LOG_FILE = os.path.join(ROOT_DIR, "logs", "report_bot.log")
TELEGRAM_API = "https://api.telegram.org"
AGENT_NAME = "Capminal"

shutdown_requested = False

# ── Signal handling ──────────────────────────────────────────────────────────

def _handle_signal(signum, frame):
    global shutdown_requested
    sig_name = signal.Signals(signum).name
    logging.info(f"Received {sig_name} — shutting down after current tick")
    shutdown_requested = True

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ── Logging ──────────────────────────────────────────────────────────────────

def setup_logging(log_file):
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root.addHandler(console)

    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root.addHandler(fh)

log = logging.getLogger("report_bot")

# ── Helpers ──────────────────────────────────────────────────────────────────

def fmt_price(p):
    if p >= 1000:
        return f"{p:,.2f}"
    elif p >= 1:
        return f"{p:.2f}"
    elif p >= 0.01:
        return f"{p:.4f}"
    else:
        return f"{p:.6f}"


def fmt_pnl(v):
    return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def send_telegram(bot_key, chat_id, text):
    """Send message via Telegram Bot API. Returns True on success."""
    url = f"{TELEGRAM_API}/bot{bot_key}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        log.error(f"Telegram API error {e.code}: {body[:200]}")
        return False
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


# ── Data Collection ──────────────────────────────────────────────────────────

def collect_account(dgclaw, address):
    """Step 1: Account balance."""
    try:
        data = dgclaw.account(address)
        acct = data.get("data", data)
        return {
            "balance": float(acct.get("hlBalance", 0)),
            "withdrawable": float(acct.get("withdrawableBalance", 0)),
        }
    except Exception as e:
        log.error(f"Failed to get account: {e}")
        return {"balance": 0, "withdrawable": 0}


def collect_positions(hl, address):
    """Step 2: Open positions with TP/SL."""
    try:
        state = hl.get_state(address)
        orders = hl.get_orders(address)
        positions_data = state.get("assetPositions", [])

        if not positions_data:
            return []

        # Build TP/SL map
        order_prices = {}
        for o in orders:
            if not o.get("reduceOnly", False):
                continue
            coin = o["coin"]
            px = float(o["limitPx"])
            order_prices.setdefault(coin, []).append(px)

        pos_side_map = {}
        for ap in positions_data:
            pos = ap["position"]
            szi = float(pos["szi"])
            pos_side_map[pos["coin"]] = "short" if szi < 0 else "long"

        positions = []
        for ap in positions_data:
            pos = ap["position"]
            coin = pos["coin"]
            szi = float(pos["szi"])
            side = "short" if szi < 0 else "long"
            entry = float(pos["entryPx"])
            pos_value = float(pos.get("positionValue", 0))
            mark = pos_value / abs(szi) if szi != 0 else 0
            upnl = float(pos["unrealizedPnl"])
            roe = float(pos["returnOnEquity"]) * 100

            # TP/SL from orders
            prices_sorted = sorted(order_prices.get(coin, []))
            if side == "long":
                tp = prices_sorted[-1] if prices_sorted else None
                sl = prices_sorted[0] if len(prices_sorted) > 1 else None
            else:
                tp = prices_sorted[0] if prices_sorted else None
                sl = prices_sorted[-1] if len(prices_sorted) > 1 else None

            positions.append({
                "coin": coin, "side": side, "entry": entry,
                "mark": mark, "upnl": upnl, "roe": roe,
                "tp": tp, "sl": sl,
            })

        return positions
    except Exception as e:
        log.error(f"Failed to get positions: {e}")
        return []


def collect_closed_trades(dgclaw, address, limit=5):
    """Step 3: Recently closed trades."""
    try:
        data = dgclaw.closed_trades(address, limit=limit)
        trades = data if isinstance(data, list) else data.get("data", [])
        return trades[:limit]
    except Exception as e:
        log.error(f"Failed to get closed trades: {e}")
        return []


def collect_be_check(hl, address):
    """Step 4: BE stop check."""
    try:
        state = hl.get_state(address)
        orders = hl.get_orders(address)
        positions_data = state.get("assetPositions", [])
        sorted_orders = sorted(orders, key=lambda o: o.get("timestamp", 0), reverse=True)

        alerts = []
        for ap in positions_data:
            pos = ap["position"]
            coin = pos["coin"]
            szi = float(pos["szi"])
            side = "short" if szi < 0 else "long"
            entry = float(pos["entryPx"])
            pos_value = float(pos.get("positionValue", 0))
            mark = pos_value / abs(szi) if szi != 0 else 0

            pnl_pct = ((mark - entry) / entry * 100) if side == "long" else ((entry - mark) / entry * 100)
            if entry == 0:
                pnl_pct = 0

            # Find latest SL order
            latest_sl = None
            for o in sorted_orders:
                if o["coin"] == coin and o.get("reduceOnly", False):
                    px = float(o["limitPx"])
                    if (side == "long" and px < entry) or (side == "short" and px > entry):
                        latest_sl = px
                        break

            needs_be = False
            if pnl_pct >= 1.5 and latest_sl is not None:
                sl_dist = abs(latest_sl - entry) / entry if entry else 0
                needs_be = sl_dist > 0.002

            if needs_be or pnl_pct >= 1.2:
                alerts.append({
                    "coin": coin, "side": side, "pnl_pct": round(pnl_pct, 2),
                    "needs_be": needs_be,
                })

        return alerts
    except Exception as e:
        log.error(f"Failed to check BE: {e}")
        return []


def collect_leaderboard_rank(dgclaw, agent_name):
    """Step 6: Leaderboard rank."""
    try:
        matches = dgclaw.leaderboard_agent(agent_name)
        if matches:
            entry = matches[0]
            return {
                "rank": entry.get("rank", "?"),
                "score": entry.get("compositeScore", entry.get("score", "?")),
                "pnl": entry.get("mtmPnl", entry.get("pnl", "?")),
            }
        return None
    except Exception as e:
        log.error(f"Failed to get leaderboard: {e}")
        return None


# ── Report Formatting ────────────────────────────────────────────────────────

def _plain_table(headers, rows):
    """Build a borderless fixed-width table for Telegram monospace."""
    col_widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    sep = "  ".join("─" * w for w in col_widths)
    def fmt_row(cells):
        return "  ".join(f"{str(c):<{w}}" for c, w in zip(cells, col_widths))
    out = [fmt_row(headers), sep]
    for r in rows:
        out.append(fmt_row(r))
    return "\n".join(out)


def format_report(account, positions, closed_trades, be_alerts, leaderboard):
    """Format the Telegram report as plain aligned tables in code blocks."""
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines = [f"*{AGENT_NAME} Report* — {ts}", ""]

    # Header: Balance | Rank
    balance_str = f"${account['balance']:,.2f} USDC"
    if leaderboard:
        rank = leaderboard['rank']
        score = leaderboard['score']
        if isinstance(score, (int, float)):
            score = f"{score:.1f}"
        lines.append(f"Balance: *{balance_str}*   Rank: *#{rank}* (Score: {score})")
    else:
        lines.append(f"Balance: *{balance_str}*")

    # Open positions table
    lines.append("")
    if positions:
        lines.append(f"*Open Positions ({len(positions)})*")
        total_upnl = sum(p["upnl"] for p in positions)

        headers = ["Coin", "Side", "Entry", "Mark", "uPnL", "ROE", "TP", "SL"]
        rows = []
        for p in positions:
            rows.append([
                p["coin"],
                p["side"].upper(),
                f"${fmt_price(p['entry'])}",
                f"${fmt_price(p['mark'])}",
                fmt_pnl(p["upnl"]),
                f"{p['roe']:+.1f}%",
                f"${fmt_price(p['tp'])}" if p["tp"] else "-",
                f"${fmt_price(p['sl'])}" if p["sl"] else "-",
            ])

        lines.append("```")
        lines.append(_plain_table(headers, rows))
        lines.append(f"─" * 30)
        lines.append(f"Total uPnL  {fmt_pnl(total_upnl)}")
        lines.append("```")
    else:
        lines.append("_No open positions._")

    # BE alerts
    if be_alerts:
        be_needing = [a for a in be_alerts if a["needs_be"]]
        if be_needing:
            lines.append("")
            lines.append("*BE Stop Alerts*")
            headers = ["Coin", "Side", "PnL%", "Action"]
            rows = [[a["coin"], a["side"].upper(), f"+{a['pnl_pct']:.1f}%", "Move to BE"] for a in be_needing]
            lines.append("```")
            lines.append(_plain_table(headers, rows))
            lines.append("```")

    # Closed trades table
    if closed_trades:
        lines.append("")
        lines.append("*Recent Closed Trades*")
        headers = ["Pair", "Dir", "PnL"]
        rows = []
        for t in closed_trades[:5]:
            pnl = float(t.get("realizedPnl", 0))
            rows.append([
                t.get("pair", "?").upper(),
                t.get("direction", "?").upper(),
                fmt_pnl(pnl),
            ])
        lines.append("```")
        lines.append(_plain_table(headers, rows))
        lines.append("```")

    return "\n".join(lines)


# ── Tick ─────────────────────────────────────────────────────────────────────

def run_tick(dgclaw, hl, address, hl_address, bot_key, chat_id, dry_run=False):
    """Collect data and send report."""
    _api_log.clear()
    t0 = time.monotonic()

    # Collect all data
    log.info("[COLLECT]  Gathering account data...")
    account = collect_account(dgclaw, address)
    log.info(f"[COLLECT]  Balance: ${account['balance']:,.2f}")

    log.info("[COLLECT]  Gathering positions...")
    positions = collect_positions(hl, hl_address)
    log.info(f"[COLLECT]  Positions: {len(positions)}")

    log.info("[COLLECT]  Gathering closed trades...")
    closed = collect_closed_trades(dgclaw, address, limit=5)
    log.info(f"[COLLECT]  Closed trades: {len(closed)}")

    log.info("[COLLECT]  Checking BE stops...")
    be_alerts = collect_be_check(hl, hl_address)
    be_needing = [a for a in be_alerts if a["needs_be"]]
    log.info(f"[COLLECT]  BE alerts: {len(be_needing)}")

    log.info("[COLLECT]  Checking leaderboard...")
    leaderboard = collect_leaderboard_rank(dgclaw, AGENT_NAME)
    if leaderboard:
        log.info(f"[COLLECT]  Rank: #{leaderboard['rank']}")
    else:
        log.info("[COLLECT]  Leaderboard: not found")

    # Format report
    report = format_report(account, positions, closed, be_alerts, leaderboard)

    elapsed = time.monotonic() - t0
    api_total, api_ms, _ = api_summary()

    if dry_run:
        log.info("[REPORT]   DRY-RUN — report preview:")
        for line in report.split("\n"):
            log.info(f"  | {line}")
        log.info(f"[SUMMARY]  Done in {elapsed:.1f}s | API: {api_total} calls, {api_ms}ms")
        return True

    # Send to Telegram
    log.info("[SEND]     Sending to Telegram...")
    ok = send_telegram(bot_key, chat_id, report)
    if ok:
        log.info(f"[SEND]     ✓ Sent successfully")
    else:
        log.error(f"[SEND]     ✗ Failed to send")

    log.info(f"[SUMMARY]  Done in {elapsed:.1f}s | API: {api_total} calls, {api_ms}ms")
    return ok


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Telegram trading report bot — replaces OpenClaw AI cron",
    )
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"Report interval in seconds (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--env", type=str, default=None,
                        help="Env file path (default: .env)")
    parser.add_argument("--log-file", type=str, default=DEFAULT_LOG_FILE,
                        help=f"Log file path (default: {DEFAULT_LOG_FILE})")
    parser.add_argument("--chat-id", type=str, default=None,
                        help="Telegram chat ID (default: from env TELEGRAM_CHAT_ID)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview report without sending to Telegram")
    parser.add_argument("--once", action="store_true",
                        help="Send single report then exit")
    return parser.parse_args()


def main():
    global shutdown_requested

    args = parse_args()
    setup_logging(args.log_file)

    # Load environment
    env_path = args.env or os.path.join(ROOT_DIR, ".env")
    load_env(env_path)

    # Required env vars
    api_key = require("DGCLAW_API_KEY")
    address = require("DGCLAW_ADDRESS")
    hl_address = require("HL_ADDRESS")
    bot_key = require("TELEBOT_KEY")
    chat_id = args.chat_id or get("TELEGRAM_CHAT_ID", "1245463966")

    log.info("=" * 50)
    log.info("REPORT BOT STARTED")
    log.info(f"  Interval : {args.interval}s ({args.interval // 60}min)")
    log.info(f"  Dry-run  : {args.dry_run}")
    log.info(f"  Once     : {args.once}")
    log.info(f"  Chat ID  : {chat_id}")
    log.info(f"  Address  : {address[:8]}...{address[-4:]}")
    log.info("=" * 50)

    # API clients
    dgclaw = DgclawAPI(api_key)
    hl = HyperliquidAPI()

    tick_count = 0

    while not shutdown_requested:
        tick_count += 1
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        log.info("")
        log.info(f"═══ Report #{tick_count} @ {ts} ═══")

        try:
            run_tick(dgclaw, hl, address, hl_address, bot_key, chat_id, dry_run=args.dry_run)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Report #{tick_count} failed: {e}", exc_info=True)

        if args.once:
            log.info("Single report mode — exiting")
            break

        log.info(f"Next report in {args.interval}s ({args.interval // 60}min)")

        for _ in range(args.interval):
            if shutdown_requested:
                break
            time.sleep(1)

    log.info("")
    log.info(f"REPORT BOT STOPPED (after {tick_count} reports)")


if __name__ == "__main__":
    main()
