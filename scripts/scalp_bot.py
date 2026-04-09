#!/usr/bin/env python3
"""scalp_bot.py -- Automated scalping bot replacing OpenClaw AI cron.

Runs a tick loop (default 3 min) executing the 10-step Scalping Trading
workflow from SKILL.md. All steps are deterministic — no AI needed.

Usage:
    python3 scripts/scalp_bot.py                          # run loop (3 min ticks)
    python3 scripts/scalp_bot.py --once                   # single tick, then exit
    python3 scripts/scalp_bot.py --once --dry-run         # single tick, no trades/posts
    python3 scripts/scalp_bot.py --interval 300           # 5 min ticks
    python3 scripts/scalp_bot.py --strategy strategies/v1_trend_pullback.json
    python3 scripts/scalp_bot.py --env ./agent2.env       # multi-agent
"""
import sys
import os
import json
import time
import signal
import logging
import argparse
import re
import subprocess
from datetime import datetime, timezone

# ── Path setup ───────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.join(SCRIPT_DIR, "..")
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
sys.path.insert(0, ROOT_DIR)

from api import AcpAPI, DgclawAPI, HyperliquidAPI, DEGENCLAW_WALLET, api_summary, _api_log
from env import load_env, require, get
from strategies.lib.loader import load_strategy


# ── Constants ────────────────────────────────────────────────────────────────

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_INTERVAL = 180  # 3 minutes
DEFAULT_LOG_FILE = os.path.join(ROOT_DIR, "logs", "scalp_bot.log")

shutdown_requested = False


# ── Signal handling ──────────────────────────────────────────────────────────

def _handle_signal(signum, frame):
    global shutdown_requested
    sig_name = signal.Signals(signum).name
    logging.info(f"Received {sig_name} — shutting down after current tick")
    shutdown_requested = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Logging setup ────────────────────────────────────────────────────────────

def setup_logging(log_file):
    """Dual logging: console (INFO) + file (DEBUG)."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root.addHandler(console)

    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root.addHandler(file_handler)


# ── Helpers ──────────────────────────────────────────────────────────────────

log = logging.getLogger("scalp_bot")


def now_ms():
    return int(time.time() * 1000)


def midnight_utc_ms():
    """Midnight UTC today in milliseconds."""
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def parse_ts(iso_str):
    """Parse ISO timestamp to epoch milliseconds."""
    if isinstance(iso_str, (int, float)):
        return int(iso_str)
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


def fmt_price(p):
    if p >= 1000:
        return f"{p:,.2f}"
    elif p >= 1:
        return f"{p:.2f}"
    elif p >= 0.01:
        return f"{p:.4f}"
    else:
        return f"{p:.6f}"


def extract_job_id(resp):
    """Extract job ID from ACP create_job response.

    API returns: {"data": {"jobId": 123456}} or {"jobId": 123456}
    """
    data = resp.get("data", resp)
    return str(data.get("jobId", data.get("id", data.get("job_id", ""))))


def run_subprocess(cmd, stdin_data=None, label="subprocess"):
    """Run a subprocess and return (stdout, stderr, returncode)."""
    try:
        result = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=ROOT_DIR,
        )
        if result.returncode != 0:
            log.warning(f"[{label}] exit code {result.returncode}: {result.stderr.strip()}")
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        log.error(f"[{label}] timed out after 120s")
        return "", "timeout", -1
    except Exception as e:
        log.error(f"[{label}] failed: {e}")
        return "", str(e), -1


# ── Risk Management ─────────────────────────────────────────────────────────

class RiskResult:
    def __init__(self, blocked=False, reason="", pair_cooldowns=None):
        self.blocked = blocked
        self.reason = reason
        self.pair_cooldowns = pair_cooldowns or {}


def step2_risk_checks(dgclaw, address, risk_cfg, trade_params):
    """Check consecutive SL cooldown, daily loss pause, same-pair cooldowns."""
    closed_data = dgclaw.closed_trades(address, limit=20)
    closed = closed_data if isinstance(closed_data, list) else closed_data.get("data", [])

    # SKILL.md requires sorting by closedAt descending
    closed.sort(key=lambda t: t.get("closedAt", ""), reverse=True)

    current = now_ms()

    n = risk_cfg.get("consecutive_sl_limit", 3)
    m = risk_cfg.get("cooldown_minutes", 60)
    d = risk_cfg.get("daily_loss_limit", 5)
    p = risk_cfg.get("daily_loss_pause_minutes", 120)
    s = risk_cfg.get("same_pair_cooldown_minutes", 15)

    # ① Consecutive SL cooldown — N losses within a tight window trigger cooldown
    sl_window_minutes = risk_cfg.get("consecutive_sl_window_minutes", 20)
    last_n = closed[:n]
    if len(last_n) == n and all(float(t.get("realizedPnl", 0)) < 0 for t in last_n):
        newest_close = parse_ts(last_n[0].get("closedAt", 0))
        oldest_close = parse_ts(last_n[-1].get("closedAt", 0))
        spread_min = (newest_close - oldest_close) / 60000
        if spread_min <= sl_window_minutes and (current - newest_close) < m * 60 * 1000:
            remaining = m - (current - newest_close) / 60000
            return RiskResult(
                blocked=True,
                reason=f"consecutive_sl_cooldown ({n} SLs in {spread_min:.0f}min, {remaining:.0f}min remaining)"
            )

    # ② Daily loss pause
    today_start = midnight_utc_ms()
    daily_losses = [
        t for t in closed
        if parse_ts(t.get("closedAt", 0)) >= today_start
        and float(t.get("realizedPnl", 0)) < 0
    ]
    if len(daily_losses) >= d:
        most_recent = parse_ts(daily_losses[0].get("closedAt", 0))
        if (current - most_recent) < p * 60 * 1000:
            remaining = p - (current - most_recent) / 60000
            return RiskResult(
                blocked=True,
                reason=f"daily_loss_pause ({len(daily_losses)} losses today, {remaining:.0f}min remaining)"
            )

    # ③ Same-pair cooldown map
    pair_cooldowns = {}
    monitoring_pairs = trade_params.get("monitoring_pairs", [])
    for pair in monitoring_pairs:
        recent = next(
            (t for t in closed if t.get("pair", "").upper() == pair.upper()),
            None
        )
        if recent and float(recent.get("realizedPnl", 0)) < 0:
            closed_at = parse_ts(recent.get("closedAt", 0))
            if (current - closed_at) < s * 60 * 1000:
                remaining = s - (current - closed_at) / 60000
                pair_cooldowns[pair.upper()] = remaining

    return RiskResult(blocked=False, pair_cooldowns=pair_cooldowns)


# ── Tick Steps ───────────────────────────────────────────────────────────────

def step1_check_account(dgclaw, address, hl=None, hl_address=None):
    """Step 1: Check account balance and open positions."""
    account = dgclaw.account(address)
    positions_data = dgclaw.positions(address)
    positions = positions_data if isinstance(positions_data, list) else positions_data.get("data", [])

    # Extract balance — dgclaw endpoint returns {data: {hlBalance, withdrawableBalance}}
    balance = "?"
    if isinstance(account, dict):
        acct = account.get("data", account)
        balance = acct.get("hlBalance", acct.get("withdrawableBalance",
                  acct.get("accountValue", acct.get("withdrawable", "?"))))

    log.info(f"[ACCOUNT]  Balance: ${balance} | Positions: {len(positions)}")

    # Detailed position logging via Hyperliquid API (entry, PnL, TP/SL from orders)
    if hl and hl_address and positions:
        try:
            state = hl.get_state(hl_address)
            orders = hl.get_orders(hl_address)
            hl_positions = state.get("assetPositions", [])

            # Build TP/SL map from reduceOnly orders
            order_prices = {}
            for o in orders:
                if not o.get("reduceOnly", False):
                    continue
                order_prices.setdefault(o["coin"], []).append(float(o["limitPx"]))

            pos_side_map = {}
            for ap in hl_positions:
                pos = ap["position"]
                szi = float(pos["szi"])
                pos_side_map[pos["coin"]] = "short" if szi < 0 else "long"

            tp_sl_map = {}
            for coin, prices in order_prices.items():
                prices_sorted = sorted(prices)
                side = pos_side_map.get(coin)
                if side == "long":
                    tp_sl_map[coin] = {"tp": prices_sorted[-1], "sl": prices_sorted[0] if len(prices_sorted) > 1 else None}
                elif side == "short":
                    tp_sl_map[coin] = {"tp": prices_sorted[0], "sl": prices_sorted[-1] if len(prices_sorted) > 1 else None}

            for ap in hl_positions:
                pos = ap["position"]
                coin = pos["coin"]
                szi = float(pos["szi"])
                side = "short" if szi < 0 else "long"
                entry = float(pos["entryPx"])
                pos_value = float(pos.get("positionValue", 0))
                mark = pos_value / abs(szi) if szi != 0 else entry
                upnl = float(pos["unrealizedPnl"])
                roe = float(pos["returnOnEquity"]) * 100

                tpsl = tp_sl_map.get(coin, {})
                tp = tpsl.get("tp")
                sl = tpsl.get("sl")
                tp_str = f"TP ${fmt_price(tp)}" if tp is not None else "TP --"
                sl_str = f"SL ${fmt_price(sl)}" if sl is not None else "SL --"

                side_icon = "▲" if side == "long" else "▼"
                pnl_sign = "+" if upnl >= 0 else ""
                log.info(
                    f"[ACCOUNT]  {side_icon} {coin:8s} {side:5s} | "
                    f"sz {abs(szi):g} | entry ${fmt_price(entry)} | mark ${fmt_price(mark)} | "
                    f"PnL ${pnl_sign}{upnl:.2f} ({pnl_sign}{roe:.1f}%) | {tp_str} | {sl_str}"
                )
        except Exception as e:
            log.debug(f"[ACCOUNT]  HL detail fetch failed: {e}")

    return account, positions


def step1c_fix_missing_sl_tp(acp, hl, hl_address, trade_params, dry_run=False):
    """Step 1c: Ensure all open HL positions have both TP and SL orders set.

    Fetches current positions and reduce-only orders from Hyperliquid. For any
    position missing a TP or SL, calculates both from strategy trade_params and
    calls perp_modify to set them atomically.
    """
    if not hl or not hl_address:
        return

    try:
        state = hl.get_state(hl_address)
        orders = hl.get_orders(hl_address)
    except Exception as e:
        log.debug(f"[SL/TP-FIX] Could not fetch HL state/orders: {e}")
        return

    hl_positions = state.get("assetPositions", [])
    if not hl_positions:
        return

    # Build reduce-only order price sets per coin
    reduce_prices: dict = {}
    for o in orders:
        if not o.get("reduceOnly", False):
            continue
        reduce_prices.setdefault(o["coin"], []).append(float(o["limitPx"]))

    tp_pct = trade_params.get("tp_pct", 3.0) / 100
    sl_pct = trade_params.get("sl_pct", 1.5) / 100

    for ap in hl_positions:
        pos = ap["position"]
        coin = pos["coin"]
        szi = float(pos["szi"])
        if szi == 0:
            continue

        side = "short" if szi < 0 else "long"
        entry = float(pos["entryPx"])

        prices = sorted(reduce_prices.get(coin, []))
        # Long: expect SL below entry (lowest price) and TP above (highest)
        # Short: expect TP below entry (lowest price) and SL above (highest)
        if side == "long":
            has_tp = any(p > entry for p in prices)
            has_sl = any(p < entry for p in prices)
        else:
            has_tp = any(p < entry for p in prices)
            has_sl = any(p > entry for p in prices)

        if has_tp and has_sl:
            continue

        # Calculate TP/SL from strategy config
        if side == "long":
            tp = entry * (1 + tp_pct)
            sl = entry * (1 - sl_pct)
        else:
            tp = entry * (1 - tp_pct)
            sl = entry * (1 + sl_pct)

        missing = []
        if not has_tp:
            missing.append("TP")
        if not has_sl:
            missing.append("SL")
        log.info(
            f"[SL/TP-FIX] {coin} {side}: missing {'+'.join(missing)} — "
            f"setting TP ${fmt_price(tp)} / SL ${fmt_price(sl)} "
            f"(entry ${fmt_price(entry)}, tp={trade_params.get('tp_pct')}%, sl={trade_params.get('sl_pct')}%)"
        )

        if dry_run:
            log.info(f"[SL/TP-FIX] DRY-RUN: Would set TP/SL for {coin}")
            continue

        try:
            job = acp.create_job(DEGENCLAW_WALLET, "perp_modify", {
                "pair": coin,
                "takeProfit": str(round(tp, 6)),
                "stopLoss": str(round(sl, 6)),
            }, automated=True)
            job_id = extract_job_id(job)
            if not job_id:
                log.error(f"[SL/TP-FIX] {coin}: no job ID in response")
                continue
            acp.poll_job(job_id, timeout=120, interval=10, label=f"SL/TP-FIX {coin}")
            log.info(f"[SL/TP-FIX] {coin}: COMPLETED ✓")
        except RuntimeError as e:
            log.warning(f"[SL/TP-FIX] {coin}: FAILED ✗ — {e}")
        except Exception as e:
            log.error(f"[SL/TP-FIX] {coin}: ERROR — {e}")


def step1b_auto_close_stale(dgclaw, acp, address, trade_params, dry_run=False):
    """Step 1.5: Auto-close positions held longer than max_hold_minutes."""
    max_hold = trade_params.get("max_hold_minutes", 0)
    if max_hold <= 0:
        return []

    max_hold_ms = max_hold * 60_000
    current = now_ms()

    # Fetch recent trades (includes both OPEN and CLOSED with openedAt)
    trades_data = dgclaw.trades(address, limit=20)
    trades = trades_data.get("data", trades_data) if isinstance(trades_data, dict) else trades_data

    open_trades = [t for t in trades if t.get("status") == "OPEN" and t.get("openedAt")]

    if not open_trades:
        log.info(f"[AUTO-CLOSE] No open trades to check")
        return []

    closed_stale = []
    for t in open_trades:
        pair = t.get("pair", "").upper()
        side = t.get("direction", "?").upper()
        entry = t.get("entryPrice", "?")
        opened_at = parse_ts(t["openedAt"])
        hold_ms = current - opened_at
        hold_min = hold_ms / 60_000

        if hold_ms < max_hold_ms:
            log.info(f"[AUTO-CLOSE] {pair} {side}: {hold_min:.0f}min / {max_hold}min — OK")
            continue

        pnl = t.get("unrealizedPnl", t.get("realizedPnl", "?"))
        log.info(f"[AUTO-CLOSE] {pair} {side} @ ${entry}: held {hold_min:.0f}min > {max_hold}min — STALE (uPnL: ${pnl})")

        if dry_run:
            log.info(f"[AUTO-CLOSE] DRY-RUN: Would close {pair} {side}")
            closed_stale.append(pair)
            continue

        try:
            job = acp.create_job(DEGENCLAW_WALLET, "perp_trade", {
                "action": "close",
                "pair": pair,
            }, automated=True)

            job_id = extract_job_id(job)
            if not job_id:
                log.error(f"[AUTO-CLOSE] {pair}: no job ID in response")
                continue

            log.info(f"[AUTO-CLOSE] {pair} {side}: Closing → Job #{job_id}")
            acp.poll_job(job_id, timeout=120, interval=10, label=f"Auto-close {pair}")
            log.info(f"[AUTO-CLOSE] {pair} {side}: CLOSED (time exit)")
            closed_stale.append(pair)
        except Exception as e:
            log.error(f"[AUTO-CLOSE] {pair} {side}: FAILED — {e}")

    if closed_stale:
        log.info(f"[AUTO-CLOSE] Closed {len(closed_stale)} stale positions: {', '.join(closed_stale)}")
    else:
        log.info(f"[AUTO-CLOSE] All {len(open_trades)} positions within {max_hold}min limit")

    return closed_stale


def step3_scan_signals(strategy_path):
    """Step 3: Run hl_scalp.py --json for signal analysis with per-pair detail."""
    cmd = ["python3", os.path.join(SCRIPT_DIR, "hl_scalp.py"), "--json"]
    if strategy_path:
        cmd.extend(["--strategy", strategy_path])

    stdout, stderr, rc = run_subprocess(cmd, label="hl_scalp")
    if rc != 0 or not stdout:
        log.warning(f"[SIGNALS]  hl_scalp.py failed (rc={rc})")
        return {"signals": [], "skipped": 0, "trade_params": {}}

    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError as e:
        log.error(f"[SIGNALS]  Failed to parse hl_scalp output: {e}")
        return {"signals": [], "skipped": 0, "trade_params": {}}

    # --json returns {analyzed_at, strategy, pairs: [...]}
    results = raw.get("pairs", raw) if isinstance(raw, dict) else raw

    # Log macro filter direction
    mf = raw.get("macro_filter", {})
    mf_pair = mf.get("pair", "?")
    mf_tf = mf.get("timeframe", "?")
    mf_dir = mf.get("direction", "N/A")
    mf_icon = "▲ BULLISH" if mf_dir == "up" else "▼ BEARISH" if mf_dir == "down" else "— FLAT"
    mf_bias = "→ longs only" if mf_dir == "up" else "→ shorts only" if mf_dir == "down" else ""
    log.info(f"[MACRO]    {mf_pair} {mf_tf}: {mf_icon} {mf_bias}")

    # results is a list of per-pair dicts from --json mode
    # Build compact-compatible output + log details
    signals = []
    skipped = 0

    for r in results:
        coin = r.get("coin", "?")
        signal = r.get("signal", "none")
        e15 = r.get("entry_15m", {})
        t1h = r.get("trend_1h", {})
        groups_met = e15.get("groups_met", 0)
        groups_detail = e15.get("groups_detail", [])
        skip_reason = r.get("skip_reason") or ""
        confidence = r.get("confidence", "low")
        adx = t1h.get("adx", 0)

        # Build group status string: show ✓/✗ for A, B, C, and D if present
        group_a = "✓A" if e15.get("group_A", e15.get("group_A_pullback")) else "✗A"
        group_b = "✓B" if e15.get("group_B_rsi") else "✗B"
        group_c = "✓C" if e15.get("group_C_volume_candle") else "✗C"
        group_d_val = e15.get("group_D_ai")
        if group_d_val is not None:
            group_d = "✓D" if group_d_val else "✗D"
            group_str = f"{group_a} {group_b} {group_c} {group_d} ({groups_met}/4)"
        else:
            group_str = f"{group_a} {group_b} {group_c} ({groups_met}/3)"

        # Log AI judge reason if Group D was called
        ai_reason = e15.get("ai_reason") or ""
        if group_d_val is not None and ai_reason:
            ai_tag = "✓ PASS" if group_d_val else "✗ FAIL"
            log.info(f"[AI-JUDGE] {coin:8s} {ai_tag} | {ai_reason}")

        if signal != "none":
            stars = "★★★" if confidence == "high" else "★★"
            log.info(f"[SIGNALS]  {coin:8s} {stars} {signal:5s} | {group_str} | ADX {adx:.1f} | {', '.join(groups_detail)}")
            sig_entry = {
                "coin": coin,
                "signal": signal,
                "confidence": confidence,
                "price": r.get("current_price"),
                "entry": r.get("entry_suggested"),
                "tp": r.get("tp"),
                "sl": r.get("sl"),
                "adx": adx,
                "rsi": e15.get("rsi14"),
                "groups": groups_met,
                "detail": groups_detail,
            }
            ai_val = e15.get("group_D_ai")
            if ai_val is not None:
                sig_entry["ai_judge"] = ai_val
            signals.append(sig_entry)
        else:
            skipped += 1
            log.debug(f"[SIGNALS]  {coin:8s} skip | {group_str} | ADX {adx:.1f} | {skip_reason}")

    if signals:
        log.info(f"[SIGNALS]  → {len(signals)} actionable, {skipped} skipped")
    else:
        log.info(f"[SIGNALS]  No signals | {skipped} pairs skipped")

    return {"signals": signals, "skipped": skipped}


def step4_open_trades(acp, signals, positions, pair_cooldowns, trade_params, dry_run=False):
    """Step 4: Open trades for qualifying signals via ACP."""
    existing_pairs = set()
    direction_counts = {"long": 0, "short": 0}

    for p in positions:
        # DGCLAW positions API returns {pair, side, ...}
        coin = p.get("pair", p.get("coin", "")).upper()
        existing_pairs.add(coin)
        side = p.get("side", "").lower()
        if side in ("long", "short"):
            direction_counts[side] = direction_counts.get(side, 0) + 1
        else:
            # Fallback: infer from szi (Hyperliquid format)
            szi = float(p.get("szi", 0))
            direction = "short" if szi < 0 else "long"
            direction_counts[direction] = direction_counts.get(direction, 0) + 1

    log.info(f"[FILTER]   Existing positions: {existing_pairs or 'none'} | "
             f"Longs: {direction_counts['long']}, Shorts: {direction_counts['short']}")

    max_positions = trade_params.get("max_positions", 8)
    max_same_dir = trade_params.get("max_same_direction", max_positions)
    notional = trade_params.get("notional", 1500)
    leverage = trade_params.get("leverage", 5)
    opened = []

    for sig in signals:
        coin = sig["coin"].upper()
        side = sig["signal"]
        confidence = sig.get("confidence", "medium")

        # Only ★★/★★★ signals
        if confidence not in ("medium", "high"):
            log.debug(f"[FILTER]   {coin}: skip (low confidence)")
            continue

        # Already have position
        if coin in existing_pairs:
            log.debug(f"[FILTER]   {coin}: skip (existing position)")
            continue

        # Same-pair cooldown
        if coin in pair_cooldowns:
            log.info(f"[FILTER]   {coin}: ✗ same-pair cooldown ({pair_cooldowns[coin]:.0f}min left)")
            continue

        # Max positions
        if len(positions) + len(opened) >= max_positions:
            log.info(f"[FILTER]   {coin}: ✗ max positions reached ({max_positions})")
            break

        # Max same direction
        current_dir_count = direction_counts.get(side, 0) + sum(1 for o in opened if o["signal"] == side)
        if current_dir_count >= max_same_dir:
            log.info(f"[FILTER]   {coin}: ✗ max {side} positions ({max_same_dir})")
            continue

        log.info(f"[FILTER]   {coin}: ✓ all checks passed")

        if dry_run:
            log.info(f"[TRADE]    DRY-RUN: Would open {side.upper()} {coin} @ ${fmt_price(sig['entry'])}")
            opened.append(sig)
            continue

        # Create ACP job
        try:
            effective_leverage = leverage
            job = acp.create_job(DEGENCLAW_WALLET, "perp_trade", {
                "action": "open",
                "pair": coin,
                "side": side,
                "size": str(notional),
                "leverage": effective_leverage,
            }, automated=True)

            job_id = extract_job_id(job)
            if not job_id:
                log.error(f"[TRADE]    {coin}: no job ID in response: {json.dumps(job)[:200]}")
                continue
            log.info(f"[TRADE]    Opening {side.upper()} {coin} @ ${fmt_price(sig['entry'])} → Job #{job_id}")

            try:
                acp.poll_job(job_id, timeout=150, interval=10, label=f"Trade {coin}")
                log.info(f"[TRADE]    {coin}: COMPLETED ✓")
                opened.append(sig)
            except RuntimeError as e:
                err_str = str(e)
                m = re.search(r"exceeds max (\d+)x", err_str)
                if m:
                    max_lev = int(m.group(1))
                    log.warning(f"[TRADE]    {coin}: leverage {effective_leverage}x rejected — max allowed {max_lev}x, retrying…")
                    retry_job = acp.create_job(DEGENCLAW_WALLET, "perp_trade", {
                        "action": "open",
                        "pair": coin,
                        "side": side,
                        "size": str(notional),
                        "leverage": max_lev,
                    }, automated=True)
                    retry_id = extract_job_id(retry_job)
                    if not retry_id:
                        log.error(f"[TRADE]    {coin}: retry — no job ID in response")
                    else:
                        log.info(f"[TRADE]    {coin}: retry Job #{retry_id} with leverage {max_lev}x")
                        acp.poll_job(retry_id, timeout=150, interval=10, label=f"Trade {coin} retry")
                        log.info(f"[TRADE]    {coin}: COMPLETED ✓ (leverage adjusted to {max_lev}x)")
                        sig["effective_leverage"] = max_lev
                        opened.append(sig)
                else:
                    raise

        except RuntimeError as e:
            log.warning(f"[TRADE]    {coin}: FAILED ✗ — {e}")
        except Exception as e:
            log.error(f"[TRADE]    {coin}: ERROR — {e}")

    return opened


def step5_set_tp_sl(acp, opened_trades, trade_params, dry_run=False):
    """Step 5: Set TP/SL for newly opened trades."""
    tp_pct = trade_params.get("tp_pct", 3.0) / 100
    sl_pct = trade_params.get("sl_pct", 1.5) / 100

    for sig in opened_trades:
        coin = sig["coin"].upper()
        entry = sig["entry"]
        side = sig["signal"]

        # Use pre-computed TP/SL from hl_scalp if available, else recalculate
        tp = sig.get("tp")
        sl = sig.get("sl")
        if tp is None or sl is None:
            if side == "long":
                tp = entry * (1 + tp_pct)
                sl = entry * (1 - sl_pct)
            else:
                tp = entry * (1 - tp_pct)
                sl = entry * (1 + sl_pct)

        log.info(f"[TP/SL]    {coin}: TP ${fmt_price(tp)} ({trade_params.get('tp_pct', 3.0):+.1f}%) "
                 f"SL ${fmt_price(sl)} ({-trade_params.get('sl_pct', 1.5):.1f}%)")

        if dry_run:
            log.info(f"[TP/SL]    DRY-RUN: Would set TP/SL for {coin}")
            continue

        try:
            job = acp.create_job(DEGENCLAW_WALLET, "perp_modify", {
                "pair": coin,
                "takeProfit": str(round(tp, 6)),
                "stopLoss": str(round(sl, 6)),
            }, automated=True)

            job_id = extract_job_id(job)
            if not job_id:
                log.error(f"[TP/SL]    {coin}: no job ID in response")
                continue
            acp.poll_job(job_id, timeout=120, interval=10, label=f"TP/SL {coin}")
            log.info(f"[TP/SL]    {coin}: COMPLETED ✓")

        except RuntimeError as e:
            log.warning(f"[TP/SL]    {coin}: FAILED ✗ — {e}")
        except Exception as e:
            log.error(f"[TP/SL]    {coin}: ERROR — {e}")


def step6_post_signals(opened_trades, dry_run=False):
    """Step 6: Post opening signals to forum."""
    if not opened_trades:
        log.debug("[POST]     No trades to post")
        return

    for sig in opened_trades:
        cmd = ["python3", os.path.join(SCRIPT_DIR, "hl_post_signal.py")]
        if dry_run:
            cmd.append("--dry-run")

        stdin_data = json.dumps(sig)
        stdout, stderr, rc = run_subprocess(cmd, stdin_data=stdin_data, label="hl_post_signal")

        if stderr:
            for line in stderr.split("\n"):
                if line.strip():
                    log.info(f"[POST]     {line.strip()}")
        if rc == 0 and stdout:
            log.debug(f"[POST]     Output: {stdout[:200]}")


def step7_be_check(strategy_path=None, dry_run=False):
    """Step 7: Check positions for BE stop trigger."""
    cmd = ["python3", os.path.join(SCRIPT_DIR, "hl_positions.py"), "--be-check"]
    if strategy_path:
        cmd.extend(["--strategy", strategy_path])
    stdout, stderr, rc = run_subprocess(cmd, label="hl_positions --be-check")

    if stderr:
        for line in stderr.split("\n"):
            if line.strip():
                log.info(f"[BE-CHECK] {line.strip()}")

    if rc != 0 or not stdout:
        log.debug("[BE-CHECK] No positions or check failed")
        return []

    try:
        positions = json.loads(stdout)
    except json.JSONDecodeError:
        log.warning("[BE-CHECK] Failed to parse output")
        return []

    needs_be = [p for p in positions if p.get("needs_be")]
    if needs_be:
        for p in needs_be:
            log.info(f"[BE-CHECK] {p['coin']}: needs BE (pnl +{p['pnl_pct']:.1f}%, "
                     f"SL still at ${fmt_price(p['latest_sl']) if p.get('latest_sl') else '?'})")
    else:
        checked = len(positions)
        log.info(f"[BE-CHECK] {checked} positions checked → 0 need BE")

    return positions


def step8_move_be_stops(acp, be_positions, trade_params, dry_run=False):
    """Step 8: Move SL to break-even and post updates."""
    be_offset = trade_params.get("be_offset_pct", 0.1) / 100
    tp_pct = trade_params.get("tp_pct", 3.0) / 100

    for pos in be_positions:
        if not pos.get("needs_be"):
            continue

        coin = pos["coin"].upper()
        side = pos["side"]
        entry = pos["entry"]

        # Calculate BE price and TP
        if side == "long":
            be_price = entry * (1 + be_offset)
            tp = entry * (1 + tp_pct)
        else:
            be_price = entry * (1 - be_offset)
            tp = entry * (1 - tp_pct)

        log.info(f"[BE-MOVE]  {coin}: SL → ${fmt_price(be_price)} (BE+{trade_params.get('be_offset_pct', 0.1)}%) | TP → ${fmt_price(tp)}")

        if not dry_run:
            try:
                job = acp.create_job(DEGENCLAW_WALLET, "perp_modify", {
                    "pair": coin,
                    "takeProfit": str(round(tp, 6)),
                    "stopLoss": str(round(be_price, 6)),
                }, automated=True)

                job_id = extract_job_id(job)
                if not job_id:
                    log.error(f"[BE-MOVE]  {coin}: no job ID in response")
                    continue
                acp.poll_job(job_id, timeout=120, interval=10, label=f"BE {coin}")
                log.info(f"[BE-MOVE]  {coin}: COMPLETED ✓")

            except RuntimeError as e:
                log.warning(f"[BE-MOVE]  {coin}: FAILED ✗ — {e}")
                continue
            except Exception as e:
                log.error(f"[BE-MOVE]  {coin}: ERROR — {e}")
                continue

        # Post BE update
        post_data = {
            "coin": coin,
            "side": side,
            "entry": entry,
            "mark": pos.get("mark", entry),
            "be_price": round(be_price, 6),
            "tp": round(tp, 6),
        }
        cmd = ["python3", os.path.join(SCRIPT_DIR, "hl_post_be.py")]
        if dry_run:
            cmd.append("--dry-run")

        stdout, stderr, rc = run_subprocess(cmd, stdin_data=json.dumps(post_data), label="hl_post_be")
        if stderr:
            for line in stderr.split("\n"):
                if line.strip():
                    log.info(f"[BE-MOVE]  {line.strip()}")


def step9_post_closed(dry_run=False):
    """Step 9: Post closing signals for recently closed trades."""
    cmd = ["python3", os.path.join(SCRIPT_DIR, "hl_post_closed.py")]
    if dry_run:
        cmd.append("--dry-run")

    stdout, stderr, rc = run_subprocess(cmd, label="hl_post_closed")

    if stdout:
        for line in stdout.split("\n"):
            if line.strip():
                log.info(f"[CLOSED]   {line.strip()}")
    elif rc == 0:
        log.info("[CLOSED]   No new trades to post")


def step10_dedup_posts(dry_run=False):
    """Step 10: Deduplicate forum posts."""
    cmd = ["python3", os.path.join(SCRIPT_DIR, "hl_dedup_posts.py")]
    if not dry_run:
        cmd.append("--delete")

    stdout, stderr, rc = run_subprocess(cmd, label="hl_dedup_posts")

    if stdout:
        lines = stdout.strip().split("\n")
        # Log summary lines only
        for line in lines:
            stripped = line.strip()
            if stripped and ("Duplicates:" in stripped or "Deleted" in stripped
                            or "No duplicates" in stripped or "DELETE" in stripped):
                log.info(f"[DEDUP]    {stripped}")
    if rc == 0 and not stdout:
        log.info("[DEDUP]    Clean — no duplicates")


# ── Tick orchestrator ────────────────────────────────────────────────────────

def run_tick(dgclaw, acp, address, cfg, strategy_path, dry_run=False, hl=None, hl_address=None):
    """Execute one complete tick (10 steps)."""
    trade_params = cfg["trade_params"]
    risk_cfg = cfg.get("risk_management", {})
    t0 = time.monotonic()

    # Reset API call tracking for this tick
    _api_log.clear()

    # Step 1: Account & positions
    account, positions = step1_check_account(dgclaw, address, hl=hl, hl_address=hl_address)

    # Step 1.5: Auto-close stale positions (frees slots for new trades)
    stale_closed = step1b_auto_close_stale(dgclaw, acp, address, trade_params, dry_run)
    if stale_closed:
        account, positions = step1_check_account(dgclaw, address, hl=hl, hl_address=hl_address)

    # Step 1c: Fix positions missing SL/TP orders
    step1c_fix_missing_sl_tp(acp, hl, hl_address, trade_params, dry_run)

    # Step 2: Risk checks
    risk = step2_risk_checks(dgclaw, address, risk_cfg, trade_params)
    if risk.blocked:
        log.info(f"[RISK]     ⛔ BLOCKED — {risk.reason}")
        log.info("[RISK]     Skipping steps 3-6 (signal scan + trading)")
    else:
        cooldown_strs = [f"{k}({v:.0f}min)" for k, v in risk.pair_cooldowns.items()]
        if cooldown_strs:
            log.info(f"[RISK]     ✓ Pass | Pair cooldowns: {', '.join(cooldown_strs)}")
        else:
            log.info("[RISK]     ✓ Pass — no cooldowns active")

    # Check if at max positions
    max_pos = trade_params.get("max_positions", 8)
    at_max = len(positions) >= max_pos

    opened_trades = []

    if not risk.blocked and not at_max:
        # Step 3: Signal scan
        scan_data = step3_scan_signals(strategy_path)
        signals = scan_data.get("signals", [])

        if signals:
            # Step 4: Open trades
            opened_trades = step4_open_trades(
                acp, signals, positions, risk.pair_cooldowns, trade_params, dry_run
            )

            if opened_trades:
                # Step 5: Set TP/SL
                step5_set_tp_sl(acp, opened_trades, trade_params, dry_run)

                # Step 6: Post opening signals
                step6_post_signals(opened_trades, dry_run)
    elif at_max:
        log.info(f"[RISK]     At max positions ({len(positions)}/{max_pos}) — skip signal scan")

    # Step 7-8: BE stop check + move (skip if BE disabled)
    if trade_params.get("be_trigger_pct", 0) > 0:
        be_positions = step7_be_check(strategy_path, dry_run)
        needs_be = [p for p in be_positions if p.get("needs_be")]
        if needs_be:
            step8_move_be_stops(acp, be_positions, trade_params, dry_run)
    else:
        be_positions = []
        needs_be = []

    # Step 9: Post closed trades (always runs)
    step9_post_closed(dry_run)

    # Step 10: Dedup posts (always runs)
    step10_dedup_posts(dry_run)

    # Summary
    elapsed = time.monotonic() - t0
    api_total, api_ms, _ = api_summary()
    log.info(f"[SUMMARY]  Tick done in {elapsed:.1f}s | "
             f"{len(opened_trades)} traded, {len(needs_be)} BE moved | "
             f"API: {api_total} calls, {api_ms}ms")


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Scalping bot — automated tick loop replacing OpenClaw AI cron",
    )
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"Tick interval in seconds (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--strategy", type=str, default=None,
                        help="Strategy config file (default: strategies/current)")
    parser.add_argument("--env", type=str, default=None,
                        help="Env file path (default: .env)")
    parser.add_argument("--log-file", type=str, default=DEFAULT_LOG_FILE,
                        help=f"Log file path (default: {DEFAULT_LOG_FILE})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip trade execution & posting, log only")
    parser.add_argument("--once", action="store_true",
                        help="Run single tick then exit")
    return parser.parse_args()


def main():
    global shutdown_requested

    args = parse_args()
    setup_logging(args.log_file)

    # Load environment
    env_path = args.env or os.path.join(ROOT_DIR, ".env")
    load_env(env_path)

    # Verify required env vars
    api_key = require("DGCLAW_API_KEY")
    address = require("DGCLAW_ADDRESS")
    acp_key = require("LITE_AGENT_API_KEY")
    require("DGCLAW_AGENT_ID")
    require("DGCLAW_SIGNALS_THREAD_ID")

    # Load strategy
    strategy_path = args.strategy
    cfg = load_strategy(strategy_path)

    # Resolve strategy_path so subprocesses always get an explicit path
    if strategy_path is None:
        strategies_dir = os.path.join(ROOT_DIR, "strategies")
        current_file = os.path.join(strategies_dir, "current")
        with open(current_file) as f:
            strategy_path = os.path.join(strategies_dir, f.read().strip())

    strategy_name = cfg.get("_name", "unknown")
    strategy_version = cfg.get("_version", "?")
    pairs = cfg.get("trade_params", {}).get("monitoring_pairs", [])

    log.info("=" * 60)
    log.info("SCALP BOT STARTED")
    log.info(f"  Strategy : {strategy_name} v{strategy_version}")
    log.info(f"  Pairs    : {', '.join(pairs)}")
    log.info(f"  Interval : {args.interval}s")
    log.info(f"  Dry-run  : {args.dry_run}")
    log.info(f"  AI Judge : {'ON' if get('AI_JUDGE_ENABLED', '0') == '1' else 'OFF'}")
    log.info(f"  Once     : {args.once}")
    log.info(f"  Address  : {address[:8]}...{address[-4:]}")
    log.info(f"  Log file : {args.log_file}")
    log.info("=" * 60)

    # API clients
    dgclaw = DgclawAPI(api_key)
    acp = AcpAPI(acp_key)
    hl = HyperliquidAPI()
    hl_address = get("HL_ADDRESS") or address

    tick_count = 0

    while not shutdown_requested:
        tick_count += 1
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        log.info("")
        log.info(f"═══ Tick #{tick_count} @ {ts} ═══")

        try:
            run_tick(dgclaw, acp, address, cfg, strategy_path, dry_run=args.dry_run, hl=hl, hl_address=hl_address)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"[ERROR]    Tick #{tick_count} failed: {e}", exc_info=True)

        if args.once:
            log.info("Single tick mode — exiting")
            break

        log.info(f"Next tick in {args.interval}s")

        # Interruptible sleep
        for _ in range(args.interval):
            if shutdown_requested:
                break
            time.sleep(1)

    log.info("")
    log.info("=" * 60)
    log.info(f"SCALP BOT STOPPED (after {tick_count} ticks)")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
