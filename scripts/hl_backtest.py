#!/usr/bin/env python3
"""hl_backtest.py -- Backtest the trend-following pullback strategy on historical data.

Uses the same strategy logic as the scalp loop:
  1h trend filter (EMA9 vs EMA21 + ADX >= 15) + 15m entry (2/3 groups)

Usage:
    python3 hl_backtest.py ETH BTC SOL              # 7-day default
    python3 hl_backtest.py ETH --days 14
    python3 hl_backtest.py ETH BTC SOL --json
    python3 hl_backtest.py ETH --strategy path/to.json
"""
import sys
import os
import json
import math
import time
from datetime import datetime, timezone
from bisect import bisect_right

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(SCRIPT_DIR, "..")

sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
sys.path.insert(0, REPO_DIR)

from api import HyperliquidAPI
from fmt import (
    table, header_box, separator,
    price as fmt_price, price_raw,
    pnl_dollar, pct, side_label,
    bold, dim, cyan, green, red, yellow,
)
from strategies.lib.indicators import ema, rsi, adx
from strategies.lib.loader import load_strategy


# ── Helpers ──────────────────────────────────────────────────────────────────

def fmt_dur(entry_ms, exit_ms):
    """Format a duration between two millisecond timestamps."""
    secs = (exit_ms - entry_ms) / 1000
    if secs < 3600:
        return f"{int(secs / 60)}m"
    hours = secs / 3600
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def result_colored(result):
    """Colored result label: TP=green, SL=red, SL_BE=yellow."""
    r = result.upper()
    if r == "TP":
        return green("TP")
    elif r == "SL":
        return red("SL")
    elif r == "SL_BE":
        return yellow("SL_BE")
    elif r == "TIME_EXIT":
        return yellow("TIME")
    elif r == "OPEN_AT_END":
        return dim("OPEN")
    return dim(r)


# ── Arg parsing ──────────────────────────────────────────────────────────────

def usage():
    print("Usage: hl_backtest.py [COIN...] [--days N] [--json] [--strategy <path>]")
    print()
    print("Backtest the Trend-Following Pullback strategy on historical data.")
    print("If no COINs specified, uses monitoring_pairs from strategy config.")
    print("  - Same logic as scalp loop (ADX >= 15, 2/3 groups, TP/SL/BE)")
    print("  - Default: 7 days of history")
    print()
    print("Examples:")
    print("  python3 hl_backtest.py                        # pairs from strategy config")
    print("  python3 hl_backtest.py ETH MON VIRTUAL")
    print("  python3 hl_backtest.py --days 14              # config pairs, 14 days")
    print("  python3 hl_backtest.py ETH MON VIRTUAL --json")
    print("  python3 hl_backtest.py ETH --strategy strategies/v1_trend_pullback.json")


def parse_args(argv):
    """Parse CLI arguments. Returns (coins, days, json_mode, strategy_file)."""
    coins = []
    days = 7
    json_mode = False
    strategy_file = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--json":
            json_mode = True
        elif arg == "--days":
            i += 1
            days = int(argv[i])
        elif arg == "--strategy":
            i += 1
            strategy_file = argv[i]
        elif arg.startswith("--"):
            print(f"Error: Unknown flag '{arg}'", file=sys.stderr)
            sys.exit(1)
        else:
            coins.append(arg.upper())
        i += 1

    return coins, days, json_mode, strategy_file


# ── Backtest engine ──────────────────────────────────────────────────────────

def run_backtest(coins, days, strategy_file):
    """Execute the backtest. Returns (closed_trades, coin_data, cfg)."""
    cfg = load_strategy(strategy_file)

    TF = cfg["trend_filter"]
    ET = cfg["entry_trigger"]
    TP_cfg = cfg["trade_params"]
    BT = cfg["backtest_params"]
    CONF = cfg["confidence"]
    MF = cfg.get("macro_filter", {})
    mf_tf = MF.get("timeframe", "4h")
    mf_hours = int(mf_tf.replace("h", ""))

    RM = cfg.get("risk_management", {})

    NOTIONAL = TP_cfg["notional"]
    LEVERAGE = TP_cfg["leverage"]
    TP_PCT = TP_cfg["tp_pct"]
    SL_PCT = TP_cfg["sl_pct"]
    BE_TRIGGER_PCT = TP_cfg.get("be_trigger_pct", 0)
    BE_OFFSET_PCT = TP_cfg.get("be_offset_pct", 0)
    MAX_HOLD_MS = TP_cfg.get("max_hold_minutes", 0) * 60_000
    MAX_CONCURRENT = BT["max_concurrent"]
    WARMUP_15M = BT["warmup_15m"]
    WARMUP_1H = BT["warmup_1h"]
    WARMUP_MF = BT.get(f"warmup_{mf_tf}", BT.get("warmup_4h", 15))
    MAX_SAME_DIR = TP_cfg.get("max_same_direction", MAX_CONCURRENT)
    RSI_SHORT_FLOOR = ET.get("short_entry_rsi_floor", 0)
    RSI_LONG_CEILING = ET.get("long_entry_rsi_ceiling", 0)
    ENTRY_MODE = ET.get("entry_mode", "pullback")

    CONSEC_SL_LIMIT = RM.get("consecutive_sl_limit", 0)
    COOLDOWN_MS = RM.get("cooldown_minutes", 0) * 60_000
    CONSEC_SL_WINDOW_MS = RM.get("consecutive_sl_window_minutes", 20) * 60_000
    SAME_PAIR_CD_MS = RM.get("same_pair_cooldown_minutes", 0) * 60_000
    DAILY_LOSS_LIMIT = RM.get("daily_loss_limit", 0)
    DAILY_LOSS_PAUSE_MS = RM.get("daily_loss_pause_minutes", 0) * 60_000

    # ── Compute time windows with warm-up ────────────────────────
    end_ms = int(time.time() * 1000)
    start_1h = end_ms - ((days * 24 + 50) * 3_600_000)
    start_15m = end_ms - ((days * 96 + 50) * 900_000)
    start_mf = end_ms - ((days * (24 // mf_hours) + 20) * mf_hours * 3_600_000)

    # ── Fetch candles in parallel ────────────────────────────────
    hl = HyperliquidAPI()
    requests = []
    for coin in coins:
        requests.append((coin, "1h", start_1h, end_ms))
        requests.append((coin, "15m", start_15m, end_ms))

    # Add BTC macro filter fetch if enabled
    btc_ref = MF.get("reference_pair", "BTC")
    if MF.get("enabled"):
        requests.append((btc_ref, mf_tf, start_mf, end_ms))

    raw_data = hl.get_candles_parallel(requests)

    # ── Load BTC macro filter data ───────────────────────────────
    btc_candles_mf = []
    btc_ts_mf = []
    if MF.get("enabled"):
        raw_mf_btc = raw_data.get((btc_ref, mf_tf), [])
        if isinstance(raw_mf_btc, list) and len(raw_mf_btc) >= WARMUP_MF:
            btc_candles_mf = sorted(raw_mf_btc, key=lambda c: int(c['t']))
            btc_ts_mf = [int(c['t']) for c in btc_candles_mf]
        else:
            print(f"Warning: Insufficient BTC {mf_tf} data ({len(raw_mf_btc) if isinstance(raw_mf_btc, list) else 0} candles) — macro filter disabled for this run", file=sys.stderr)

    # ── Load and validate data ───────────────────────────────────
    coin_data = {}
    for coin in coins:
        raw_1h = raw_data.get((coin, "1h"), [])
        raw_15m = raw_data.get((coin, "15m"), [])

        if not isinstance(raw_1h, list) or len(raw_1h) < 30:
            count = len(raw_1h) if isinstance(raw_1h, list) else 0
            print(f"Warning: Insufficient 1h data for {coin} ({count} candles)", file=sys.stderr)
            continue
        if not isinstance(raw_15m, list) or len(raw_15m) < 50:
            count = len(raw_15m) if isinstance(raw_15m, list) else 0
            print(f"Warning: Insufficient 15m data for {coin} ({count} candles)", file=sys.stderr)
            continue

        candles_1h = sorted(raw_1h, key=lambda c: int(c['t']))
        candles_15m = sorted(raw_15m, key=lambda c: int(c['t']))

        coin_data[coin] = {
            "candles_1h": candles_1h,
            "candles_15m": candles_15m,
            "ts_1h": [int(c['t']) for c in candles_1h],
        }

    if not coin_data:
        print("Error: No valid coin data loaded", file=sys.stderr)
        sys.exit(1)

    # ── Build merged timeline of all 15m candles ─────────────────
    all_events = []
    for coin in coin_data:
        for i, candle in enumerate(coin_data[coin]["candles_15m"]):
            all_events.append((int(candle['t']), coin, i))
    all_events.sort(key=lambda x: x[0])

    # ── Walk through timeline ────────────────────────────────────
    open_positions = []
    closed_trades = []

    # Risk management state
    recent_sl_times: list = []    # timestamps of recent SL hits (for window check)
    cooldown_until = 0
    skip_cooldown = 0
    cooldown_triggers = 0
    pair_last_sl: dict = {}       # coin → timestamp of last SL close
    skip_same_pair_cd = 0
    daily_loss_timestamps: list = []
    daily_pause_until = 0
    skip_daily_pause = 0
    daily_pause_triggers = 0

    backtest_start_ms = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    for timestamp, coin, candle_idx in all_events:
        cd = coin_data[coin]
        candles_15m = cd["candles_15m"]
        candles_1h = cd["candles_1h"]
        ts_1h = cd["ts_1h"]

        # Need enough history for indicators
        if candle_idx < WARMUP_15M:
            continue

        # Only count trades after backtest start
        if timestamp < backtest_start_ms:
            continue

        candle = candles_15m[candle_idx]
        candle_high = float(candle['h'])
        candle_low = float(candle['l'])
        candle_close = float(candle['c'])
        candle_open = float(candle['o'])

        # ── Step A: Check BE trigger + TP/SL for open positions ──
        for pos in open_positions[:]:
            if pos['coin'] != coin:
                continue

            # Time-based auto-close
            if MAX_HOLD_MS > 0 and (timestamp - pos['entry_time']) >= MAX_HOLD_MS:
                exit_price = candle_close
                if pos['side'] == 'long':
                    pnl_pct = (exit_price - pos['entry_price']) / pos['entry_price'] * 100
                else:
                    pnl_pct = (pos['entry_price'] - exit_price) / pos['entry_price'] * 100
                pnl_val = NOTIONAL * (pnl_pct / 100)
                closed_trades.append({
                    "coin": pos['coin'],
                    "side": pos['side'],
                    "entry_price": pos['entry_price'],
                    "exit_price": exit_price,
                    "tp_price": pos['tp_price'],
                    "sl_price": pos['sl_price'],
                    "entry_time": pos['entry_time'],
                    "exit_time": timestamp,
                    "pnl": round(pnl_val, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "result": "time_exit",
                })
                open_positions.remove(pos)
                continue

            # Break-Even Stop
            if BE_TRIGGER_PCT > 0 and not pos.get('be_triggered', False):
                if pos['side'] == 'long':
                    be_level = pos['entry_price'] * (1 + BE_TRIGGER_PCT / 100)
                    if candle_high >= be_level:
                        pos['sl_price'] = pos['entry_price'] * (1 + BE_OFFSET_PCT / 100)
                        pos['be_triggered'] = True
                else:
                    be_level = pos['entry_price'] * (1 - BE_TRIGGER_PCT / 100)
                    if candle_low <= be_level:
                        pos['sl_price'] = pos['entry_price'] * (1 - BE_OFFSET_PCT / 100)
                        pos['be_triggered'] = True

            if pos['side'] == 'long':
                sl_hit = candle_low <= pos['sl_price']
                tp_hit = candle_high >= pos['tp_price']
            else:
                sl_hit = candle_high >= pos['sl_price']
                tp_hit = candle_low <= pos['tp_price']

            if not sl_hit and not tp_hit:
                continue

            # Conservative: if both hit same candle, assume SL first
            if sl_hit:
                exit_price = pos['sl_price']
                result = 'sl_be' if pos.get('be_triggered', False) else 'sl'
            else:
                exit_price = pos['tp_price']
                result = 'tp'

            if pos['side'] == 'long':
                pnl_pct = (exit_price - pos['entry_price']) / pos['entry_price'] * 100
            else:
                pnl_pct = (pos['entry_price'] - exit_price) / pos['entry_price'] * 100

            pnl_val = NOTIONAL * (pnl_pct / 100)

            closed_trades.append({
                "coin": pos['coin'],
                "side": pos['side'],
                "entry_price": pos['entry_price'],
                "exit_price": exit_price,
                "tp_price": pos['tp_price'],
                "sl_price": pos['sl_price'],
                "entry_time": pos['entry_time'],
                "exit_time": timestamp,
                "pnl": round(pnl_val, 2),
                "pnl_pct": round(pnl_pct, 2),
                "result": result,
            })
            open_positions.remove(pos)

            # ── Consecutive SL cooldown tracking (with time window) ──
            if CONSEC_SL_LIMIT > 0:
                if result == 'sl':
                    recent_sl_times.append(timestamp)
                    # Keep only SLs within the window
                    recent_sl_times = [t for t in recent_sl_times if (timestamp - t) <= CONSEC_SL_WINDOW_MS]
                    if len(recent_sl_times) >= CONSEC_SL_LIMIT:
                        cooldown_until = timestamp + COOLDOWN_MS
                        cooldown_triggers += 1
                        recent_sl_times.clear()
                else:
                    recent_sl_times.clear()

            # ── Same-pair cooldown tracking ──
            if SAME_PAIR_CD_MS > 0 and result == 'sl':
                pair_last_sl[coin] = timestamp

            # ── Daily loss tracking ──
            if DAILY_LOSS_LIMIT > 0 and pnl_val < 0:
                daily_loss_timestamps.append(timestamp)

        # ── Step B: Check for new signal ─────────────────────────
        if len(open_positions) >= MAX_CONCURRENT:
            continue

        if any(p['coin'] == coin for p in open_positions):
            continue

        # Cooldown check (consecutive SL)
        if CONSEC_SL_LIMIT > 0 and timestamp < cooldown_until:
            skip_cooldown += 1
            continue

        # Daily loss pause check
        if DAILY_LOSS_LIMIT > 0:
            DAY_MS = 86_400_000
            day_start = (timestamp // DAY_MS) * DAY_MS
            today_losses = [t for t in daily_loss_timestamps if t >= day_start]
            if len(today_losses) >= DAILY_LOSS_LIMIT:
                last_loss_ts = max(today_losses)
                if timestamp - last_loss_ts < DAILY_LOSS_PAUSE_MS:
                    if daily_pause_until < timestamp:
                        daily_pause_until = last_loss_ts + DAILY_LOSS_PAUSE_MS
                        daily_pause_triggers += 1
                    skip_daily_pause += 1
                    continue

        # Same-pair cooldown check
        if SAME_PAIR_CD_MS > 0:
            last_sl_ts = pair_last_sl.get(coin, 0)
            if last_sl_ts > 0 and timestamp - last_sl_ts < SAME_PAIR_CD_MS:
                skip_same_pair_cd += 1
                continue

        # Get 1h candles up to current timestamp
        idx_1h = bisect_right(ts_1h, timestamp)
        if idx_1h < WARMUP_1H:
            continue

        closes_1h = [float(c['c']) for c in candles_1h[:idx_1h]]
        highs_1h = [float(c['h']) for c in candles_1h[:idx_1h]]
        lows_1h = [float(c['l']) for c in candles_1h[:idx_1h]]

        # 15m window up to current candle (inclusive)
        window_end = candle_idx + 1
        closes_15m = [float(c['c']) for c in candles_15m[:window_end]]
        opens_15m = [float(c['o']) for c in candles_15m[:window_end]]
        volumes_15m = [float(c['v']) for c in candles_15m[:window_end]]

        current_price = closes_15m[-1]

        # ── 1h Trend Filter ──
        ema9_val = ema(closes_1h, TF["ema_fast"])
        ema21_val = ema(closes_1h, TF["ema_slow"])
        adx_val = adx(highs_1h, lows_1h, closes_1h, TF["adx_period"])

        if ema9_val > ema21_val:
            trend_dir = "up"
        elif ema9_val < ema21_val:
            trend_dir = "down"
        else:
            continue  # flat

        if adx_val < TF["adx_min"]:
            continue

        # ── BTC Macro Filter ──
        if MF.get("enabled") and btc_ts_mf:
            idx_mf = bisect_right(btc_ts_mf, timestamp)
            if idx_mf >= WARMUP_MF:
                btc_closes_mf = [float(c['c']) for c in btc_candles_mf[:idx_mf]]
                btc_e9 = ema(btc_closes_mf, MF.get("ema_fast", 9))
                btc_e21 = ema(btc_closes_mf, MF.get("ema_slow", 21))
                if btc_e9 > btc_e21:
                    btc_mf_dir = "up"
                elif btc_e9 < btc_e21:
                    btc_mf_dir = "down"
                else:
                    btc_mf_dir = "flat"
                if trend_dir == "up" and btc_mf_dir == "down":
                    continue  # macro bearish, no longs
                if trend_dir == "down" and btc_mf_dir == "up":
                    continue  # macro bullish, no shorts

        # ── 15m Entry Trigger (2/3 groups) ──
        ema20_15m = ema(closes_15m, ET["ema_period"])
        rsi_val = rsi(closes_15m, ET["rsi_period"])
        avg_vol = sum(volumes_15m) / len(volumes_15m) if volumes_15m else 1
        current_vol = volumes_15m[-1]
        vol_ratio = current_vol / avg_vol if avg_vol > 0 else 0
        last_candle_green = closes_15m[-1] >= opens_15m[-1]
        last_candle_red = closes_15m[-1] < opens_15m[-1]
        price_vs_ema20_pct = ((current_price - ema20_15m) / ema20_15m * 100) if ema20_15m > 0 else 0

        groups_met = 0
        signal = "none"

        if trend_dir == "up":
            # RSI ceiling: block long entry when overbought (reversal risk)
            if RSI_LONG_CEILING > 0 and rsi_val > RSI_LONG_CEILING:
                continue

            if ENTRY_MODE == "breakout":
                breakout_min = ET.get("group_a_breakout_min_pct", 0.3)
                breakout_max = ET.get("group_a_breakout_max_pct", 2.0)
                if breakout_min <= price_vs_ema20_pct <= breakout_max:
                    groups_met += 1
                if rsi_val > ET.get("group_b_rsi_long_min", 55):
                    groups_met += 1
            else:
                # Group A: price must be at or below EMA20 (true pullback, not hugging from above)
                if -ET["group_a_pullback_pct"] <= price_vs_ema20_pct <= 0:
                    groups_met += 1
                if rsi_val < ET["group_b_rsi_long_max"]:
                    groups_met += 1
            if vol_ratio > ET["group_c_vol_ratio_min"] and last_candle_green:
                groups_met += 1
            if groups_met >= ET["min_groups"]:
                signal = "long"

        elif trend_dir == "down":
            # RSI floor: block short entry when deeply oversold (bounce risk)
            if RSI_SHORT_FLOOR > 0 and rsi_val < RSI_SHORT_FLOOR:
                continue

            if ENTRY_MODE == "breakout":
                breakout_min = ET.get("group_a_breakout_min_pct", 0.3)
                breakout_max = ET.get("group_a_breakout_max_pct", 2.0)
                if -breakout_max <= price_vs_ema20_pct <= -breakout_min:
                    groups_met += 1
                if rsi_val < ET.get("group_b_rsi_short_max", 45):
                    groups_met += 1
            else:
                # Group A: price must be at or above EMA20 (true bounce, not hugging from below)
                if 0 <= price_vs_ema20_pct <= ET["group_a_pullback_pct"]:
                    groups_met += 1
                if rsi_val > ET["group_b_rsi_short_min"]:
                    groups_met += 1
            if vol_ratio > ET["group_c_vol_ratio_min"] and last_candle_red:
                groups_met += 1
            if groups_met >= ET["min_groups"]:
                signal = "short"

        if signal == "none":
            continue

        # Confidence
        confidence = "high" if groups_met >= CONF["high_min_groups"] else "medium"

        # max_same_direction cap
        same_dir_count = sum(1 for p in open_positions if p['side'] == signal)
        if same_dir_count >= MAX_SAME_DIR:
            continue

        # Open position
        entry_price = current_price
        if signal == "long":
            tp_price = entry_price * (1 + TP_PCT / 100)
            sl_price = entry_price * (1 - SL_PCT / 100)
        else:
            tp_price = entry_price * (1 - TP_PCT / 100)
            sl_price = entry_price * (1 + SL_PCT / 100)

        open_positions.append({
            "coin": coin,
            "side": signal,
            "entry_price": entry_price,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "entry_time": timestamp,
            "confidence": confidence,
            "groups_met": groups_met,
            "be_triggered": False,
        })

    # ── Close remaining open positions at last price ─────────────
    for pos in open_positions[:]:
        coin = pos['coin']
        last_candle = coin_data[coin]["candles_15m"][-1]
        exit_price = float(last_candle['c'])
        exit_time = int(last_candle['t'])

        if pos['side'] == 'long':
            pnl_pct = (exit_price - pos['entry_price']) / pos['entry_price'] * 100
        else:
            pnl_pct = (pos['entry_price'] - exit_price) / pos['entry_price'] * 100

        pnl_val = NOTIONAL * (pnl_pct / 100)

        closed_trades.append({
            "coin": coin,
            "side": pos['side'],
            "entry_price": pos['entry_price'],
            "exit_price": exit_price,
            "tp_price": pos['tp_price'],
            "sl_price": pos['sl_price'],
            "entry_time": pos['entry_time'],
            "exit_time": exit_time,
            "pnl": round(pnl_val, 2),
            "pnl_pct": round(pnl_pct, 2),
            "result": "open_at_end",
        })
        open_positions.remove(pos)

    # Sort trades chronologically
    closed_trades.sort(key=lambda t: t['entry_time'])

    risk_stats = {
        "cooldown_triggers": cooldown_triggers,
        "skip_cooldown": skip_cooldown,
        "daily_pause_triggers": daily_pause_triggers,
        "skip_daily_pause": skip_daily_pause,
        "skip_same_pair_cd": skip_same_pair_cd,
    }

    return closed_trades, coin_data, cfg, risk_stats


# ── Metrics computation ──────────────────────────────────────────────────────

def compute_metrics(closed_trades, coin_data, cfg, risk_stats=None):
    """Compute all backtest metrics from closed trades."""
    TP_cfg = cfg["trade_params"]
    NOTIONAL = TP_cfg["notional"]
    LEVERAGE = TP_cfg["leverage"]

    total_trades = len(closed_trades)
    wins = [t for t in closed_trades if t['pnl'] > 0]
    losses = [t for t in closed_trades if t['pnl'] <= 0]
    win_count = len(wins)
    loss_count = len(losses)
    be_stops = len([t for t in closed_trades if t['result'] == 'sl_be'])
    time_exits = len([t for t in closed_trades if t['result'] == 'time_exit'])
    win_rate = win_count / total_trades if total_trades > 0 else 0

    gross_profit = sum(t['pnl'] for t in wins) if wins else 0
    gross_loss = abs(sum(t['pnl'] for t in losses)) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    total_pnl = sum(t['pnl'] for t in closed_trades)

    # Max drawdown
    peak = 0
    max_dd = 0
    cumulative = 0
    for t in closed_trades:
        cumulative += t['pnl']
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Sortino ratio
    returns = [t['pnl_pct'] for t in closed_trades]
    avg_return = sum(returns) / len(returns) if returns else 0
    downside_returns = [r for r in returns if r < 0]
    if downside_returns and total_trades > 0:
        downside_sq = sum(r ** 2 for r in downside_returns) / total_trades
        downside_dev = math.sqrt(downside_sq)
        sortino = avg_return / downside_dev if downside_dev > 0 else float('inf')
    else:
        sortino = float('inf') if total_trades > 0 else 0

    # Per-coin breakdown
    by_coin = {}
    for coin in coin_data:
        ct = [t for t in closed_trades if t['coin'] == coin]
        cw = [t for t in ct if t['pnl'] > 0]
        cl = [t for t in ct if t['pnl'] <= 0]
        by_coin[coin] = {
            "trades": len(ct),
            "wins": len(cw),
            "losses": len(cl),
            "win_rate": len(cw) / len(ct) if ct else 0,
            "pnl": round(sum(t['pnl'] for t in ct), 2),
        }

    # Avg win / avg loss
    avg_win = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['pnl'] for t in losses) / len(losses) if losses else 0

    # Time range
    first_key = list(coin_data.keys())[0]
    first_ts = coin_data[first_key]["candles_15m"][0]
    last_ts = coin_data[first_key]["candles_15m"][-1]
    start_dt = datetime.fromtimestamp(int(first_ts['t']) / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(int(last_ts['t']) / 1000, tz=timezone.utc)

    return {
        "total_trades": total_trades,
        "win_count": win_count,
        "loss_count": loss_count,
        "be_stops": be_stops,
        "time_exits": time_exits,
        "win_rate": win_rate,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "total_pnl": total_pnl,
        "max_dd": max_dd,
        "sortino": sortino,
        "by_coin": by_coin,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "risk_stats": risk_stats or {},
    }


# ── JSON output ──────────────────────────────────────────────────────────────

def output_json(closed_trades, coin_data, cfg, metrics, days):
    """Print JSON output matching the original bash script format."""
    TF = cfg["trend_filter"]
    ET = cfg["entry_trigger"]
    TP_cfg = cfg["trade_params"]

    pf = metrics["profit_factor"]
    sr = metrics["sortino"]

    output = {
        "backtest": {
            "coins": list(coin_data.keys()),
            "days": days,
            "start": metrics["start_dt"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": metrics["end_dt"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "strategy": cfg["_name"],
            "strategy_version": cfg.get("_version", ""),
            "params": {
                "adx_min": TF["adx_min"],
                "group_a_pct": ET["group_a_pullback_pct"],
                "group_b_rsi_up": ET["group_b_rsi_long_max"],
                "group_b_rsi_down": ET["group_b_rsi_short_min"],
                "group_c_vol": ET["group_c_vol_ratio_min"],
                "min_groups": ET["min_groups"],
                "notional": TP_cfg["notional"],
                "leverage": TP_cfg["leverage"],
                "tp_pct": TP_cfg["tp_pct"],
                "sl_pct": TP_cfg["sl_pct"],
                "be_trigger_pct": TP_cfg.get("be_trigger_pct", 0),
                "be_offset_pct": TP_cfg.get("be_offset_pct", 0),
                "max_hold_minutes": TP_cfg.get("max_hold_minutes", 0),
            },
        },
        "performance": {
            "total_trades": metrics["total_trades"],
            "wins": metrics["win_count"],
            "losses": metrics["loss_count"],
            "win_rate": round(metrics["win_rate"], 4),
            "total_pnl": round(metrics["total_pnl"], 2),
            "profit_factor": round(pf, 4) if pf != float('inf') else "inf",
            "sortino_ratio": round(sr, 4) if sr != float('inf') else "inf",
            "max_drawdown": round(metrics["max_dd"], 2),
            "avg_win": round(metrics["avg_win"], 2),
            "avg_loss": round(metrics["avg_loss"], 2),
            "be_stops": metrics["be_stops"],
            "time_exits": metrics["time_exits"],
        },
        "by_coin": metrics["by_coin"],
        "risk_management": metrics.get("risk_stats", {}),
        "trades": closed_trades,
    }
    print(json.dumps(output, indent=2))


# ── Human-readable output ────────────────────────────────────────────────────

def output_human(closed_trades, coin_data, cfg, metrics, days):
    """Print colored, formatted human output."""
    TF = cfg["trend_filter"]
    ET = cfg["entry_trigger"]
    TP_cfg = cfg["trade_params"]

    BE_TRIGGER_PCT = TP_cfg.get("be_trigger_pct", 0)
    BE_OFFSET_PCT = TP_cfg.get("be_offset_pct", 0)

    coins_label = " ".join(coin_data.keys())
    start_str = metrics["start_dt"].strftime("%Y-%m-%d")
    end_str = metrics["end_dt"].strftime("%Y-%m-%d")

    # ── Header box ───────────────────────────────────────────────
    header_box(f"BACKTEST | {coins_label} | {days} days | {start_str} -> {end_str}")

    # ── Strategy + params ────────────────────────────────────────
    print(f"  Strategy : {cfg['_name']} (ADX>={TF['adx_min']}, {ET['min_groups']}/3 groups)")
    be_label = f", BE @+{BE_TRIGGER_PCT}%->+{BE_OFFSET_PCT}%" if BE_TRIGGER_PCT > 0 else ""
    hold_label = f", Max Hold {TP_cfg.get('max_hold_minutes', 0)}m" if TP_cfg.get('max_hold_minutes') else ""
    print(f"  Params   : ${TP_cfg['notional']} x {TP_cfg['leverage']}x, TP +{TP_cfg['tp_pct']}%, SL -{TP_cfg['sl_pct']}%{be_label}{hold_label}")
    separator()

    total_trades = metrics["total_trades"]

    if total_trades == 0:
        print("  No trades generated during backtest period.")
        separator("=")
        return

    # ── Performance summary ──────────────────────────────────────
    pf = metrics["profit_factor"]
    sr = metrics["sortino"]
    pf_str = f"{pf:.2f}" if pf != float('inf') else "inf"
    sr_str = f"{sr:.2f}" if sr != float('inf') else "inf"

    print(f"  Trades: {total_trades}  |  W/L: {metrics['win_count']}/{metrics['loss_count']}  |  WR: {metrics['win_rate']*100:.1f}%")
    print(f"  PnL: {pnl_dollar(metrics['total_pnl'])}  |  PF: {pf_str}  |  Sortino: {sr_str}")
    be_str = f"  |  BE Stops: {metrics['be_stops']}" if BE_TRIGGER_PCT > 0 else ""
    time_str = f"  |  Time Exits: {metrics['time_exits']}" if metrics.get('time_exits') else ""
    print(f"  Max DD: {pnl_dollar(-metrics['max_dd'])}  |  Avg Win: {pnl_dollar(metrics['avg_win'])}  |  Avg Loss: {pnl_dollar(metrics['avg_loss'])}{be_str}{time_str}")

    # Risk management stats
    rs = metrics.get("risk_stats", {})
    risk_lines = []
    if rs.get("cooldown_triggers"):
        risk_lines.append(f"Consec-SL cooldowns: {rs['cooldown_triggers']} ({rs['skip_cooldown']} skipped)")
    if rs.get("daily_pause_triggers"):
        risk_lines.append(f"Daily-loss pauses: {rs['daily_pause_triggers']} ({rs['skip_daily_pause']} skipped)")
    if rs.get("skip_same_pair_cd"):
        risk_lines.append(f"Same-pair CD skips: {rs['skip_same_pair_cd']}")
    if risk_lines:
        print(f"  {yellow('  |  '.join(risk_lines))}")
    separator()

    # ── Per-coin table ───────────────────────────────────────────
    headers = ["Coin", "Trades", "W/L", "PnL", "WR%"]
    rows = []
    for coin, stats in sorted(metrics["by_coin"].items()):
        wl = f"{stats['wins']}/{stats['losses']}"
        pnl_val = stats['pnl']
        pnl_cell = pnl_dollar(pnl_val)
        wr_cell = f"{stats['win_rate']*100:.1f}%"
        rows.append([bold(coin), str(stats['trades']), wl, pnl_cell, wr_cell])

    table(headers, rows, alignments=["<", ">", ">", ">", ">"])
    separator()

    # ── Trade log (last 10) ──────────────────────────────────────
    recent_count = min(10, len(closed_trades))
    recent = closed_trades[-recent_count:]

    print(f"  {bold(f'TRADE LOG (last {recent_count})')}")
    log_headers = ["#", "Coin", "Side", "Entry", "Exit", "PnL", "Dur", "Result"]
    log_rows = []
    for idx, t in enumerate(recent):
        n = total_trades - len(recent) + idx + 1
        dur = fmt_dur(t['entry_time'], t['exit_time'])
        log_rows.append([
            str(n),
            t['coin'],
            side_label(t['side']),
            fmt_price(t['entry_price']),
            fmt_price(t['exit_price']),
            pnl_dollar(t['pnl']),
            dur,
            result_colored(t['result']),
        ])

    table(
        log_headers,
        log_rows,
        alignments=[">", "<", "<", ">", ">", ">", ">", "<"],
    )
    separator("=")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args:
        usage()
        sys.exit(0)

    coins, days, json_mode, strategy_file = parse_args(args)

    # Fall back to strategy config pairs if no CLI pairs given
    if not coins:
        _cfg = load_strategy(strategy_file)
        coins = _cfg.get("trade_params", {}).get("monitoring_pairs", [])
        if not coins:
            print("Error: No coins specified (pass as args or set monitoring_pairs in strategy config)", file=sys.stderr)
            sys.exit(1)

    closed_trades, coin_data, cfg, risk_stats = run_backtest(coins, days, strategy_file)
    metrics = compute_metrics(closed_trades, coin_data, cfg, risk_stats)

    if json_mode:
        output_json(closed_trades, coin_data, cfg, metrics, days)
    else:
        output_human(closed_trades, coin_data, cfg, metrics, days)


if __name__ == "__main__":
    main()
