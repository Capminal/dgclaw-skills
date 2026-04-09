#!/usr/bin/env python3
"""hl_scalp.py — Multi-pair scalping signal analysis (Trend-Following Pullback).

Strategy: 1h trend filter (EMA9 vs EMA21 + ADX) + 15m entry (2/3 groups)
Optimized for leaderboard scoring: Sortino (40%), Return% (35%), PF (25%)

Usage:
  python3 scripts/hl_scalp.py ETH BTC SOL                  # analyze (human output)
  python3 scripts/hl_scalp.py ETH BTC SOL --json            # JSON output
  python3 scripts/hl_scalp.py ETH --strategy path/to/cfg.json
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

# ── Path setup ────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, REPO_DIR)
sys.path.insert(0, SCRIPT_DIR)

from lib.api import HyperliquidAPI, api_summary_str
from lib.ai_judge import is_enabled as ai_judge_enabled, judge as ai_judge
from lib.env import load_env
from lib.fmt import (
    header_box, separator, signal_badge, side_label, trend_arrow,
    check, price as fmt_price, dim, bold, green, red, yellow, cyan,
)
from strategies.lib.indicators import ema, ema_series, rsi, adx
from strategies.lib.loader import load_strategy


# ── Arg parsing ───────────────────────────────────────────────────────────────

def parse_args(argv):
    """Parse CLI arguments. Returns (coins, json_mode, compact, strategy_file)."""
    coins = []
    json_mode = False
    compact = False
    strategy_file = None

    skip_next = False

    for arg in argv[1:]:
        if skip_next:
            strategy_file = arg
            skip_next = False
            continue

        if arg == "--json":
            json_mode = True
        elif arg == "--compact":
            compact = True
        elif arg == "--strategy":
            skip_next = True
        elif arg.startswith("--"):
            print(f"Error: Unknown flag '{arg}'", file=sys.stderr)
            sys.exit(1)
        else:
            coins.append(arg.upper())

    return coins, json_mode, compact, strategy_file


def usage():
    print("Usage: python3 hl_scalp.py [COIN...] [--json] [--compact] [--strategy FILE]")
    print()
    print("If no COINs specified, uses monitoring_pairs from strategy config.")
    print()
    print("Strategy: Trend-Following Pullback (2/3 entry system)")
    print("  - 1h: EMA9 vs EMA21 trend + ADX >= 15 filter")
    print("  - 15m: 2 of 3 groups: (A) pullback to EMA20, (B) RSI, (C) volume+candle")
    print()
    print("Examples:")
    print("  python3 hl_scalp.py                          # pairs from strategy config")
    print("  python3 hl_scalp.py ETH BTC SOL              # override with specific pairs")
    print("  python3 hl_scalp.py --json --compact          # cron mode, pairs from config")
    sys.exit(0)


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze_pair(coin, candles_1h, candles_15m, cfg, candles_btc_mf=None, recent_fills=None):
    """Analyze a single pair. Returns result dict."""
    TF = cfg["trend_filter"]
    ET = cfg["entry_trigger"]
    TP_cfg = cfg["trade_params"]
    CONF = cfg["confidence"]
    MF = cfg.get("macro_filter", {})

    result = {
        "coin": coin,
        "signal": "none",
        "confidence": "skip",
        "current_price": 0,
        "skip_reason": None,
        "trend_1h": {},
        "trend_btc_mf": None,
        "entry_15m": {},
    }

    # Validate data
    if not isinstance(candles_1h, list) or len(candles_1h) < 20:
        result["skip_reason"] = "insufficient_1h_data"
        return result

    if not isinstance(candles_15m, list) or len(candles_15m) < 20:
        result["skip_reason"] = "insufficient_15m_data"
        return result

    # Parse candles
    candles_1h = sorted(candles_1h, key=lambda c: int(c['t']))
    candles_15m = sorted(candles_15m, key=lambda c: int(c['t']))

    closes_1h = [float(c['c']) for c in candles_1h]
    highs_1h = [float(c['h']) for c in candles_1h]
    lows_1h = [float(c['l']) for c in candles_1h]

    closes_15m = [float(c['c']) for c in candles_15m]
    opens_15m = [float(c['o']) for c in candles_15m]
    volumes_15m = [float(c['v']) for c in candles_15m]

    current_price = closes_15m[-1]
    result["current_price"] = round(current_price, 6)

    # ── Step 1: 1h Trend Filter ──

    ema9_val = ema(closes_1h, TF["ema_fast"])
    ema21_val = ema(closes_1h, TF["ema_slow"])
    adx_val = adx(highs_1h, lows_1h, closes_1h, TF["adx_period"])

    if ema9_val > ema21_val:
        trend_dir = "up"
    elif ema9_val < ema21_val:
        trend_dir = "down"
    else:
        trend_dir = "flat"

    result["trend_1h"] = {
        "direction": trend_dir,
        "ema9": round(ema9_val, 6),
        "ema21": round(ema21_val, 6),
        "adx": round(adx_val, 1),
    }

    ai_mode = ai_judge_enabled()
    macro_blocked = []
    btc_mf_dir = None

    # ADX filter
    if adx_val < TF["adx_min"]:
        if not ai_mode:
            result["skip_reason"] = f"adx_too_low ({adx_val:.1f} < {TF['adx_min']})"
            result["entry_15m"] = {
                "ema20": round(ema(closes_15m, ET["ema_period"]), 6),
                "rsi14": round(rsi(closes_15m, ET["rsi_period"]), 1),
                "volume_ratio": round(
                    volumes_15m[-1] / (sum(volumes_15m) / len(volumes_15m)), 2
                ) if volumes_15m else 0,
                "groups_met": 0,
                "groups_detail": [],
            }
            return result
        macro_blocked.append(f"adx_too_low ({adx_val:.1f} < {TF['adx_min']})")

    # Trend must be directional (even AI can't rescue flat — no direction to trade)
    if trend_dir == "flat":
        result["skip_reason"] = "flat_trend"
        return result

    # ── Step 1b: BTC 4h Macro Filter ──

    if MF.get("enabled") and candles_btc_mf and len(candles_btc_mf) >= 10:
        candles_btc_mf_sorted = sorted(candles_btc_mf, key=lambda c: int(c['t']))
        closes_btc_mf = [float(c['c']) for c in candles_btc_mf_sorted]
        btc_e9 = ema(closes_btc_mf, MF.get("ema_fast", 9))
        btc_e21 = ema(closes_btc_mf, MF.get("ema_slow", 21))
        if btc_e9 > btc_e21:
            btc_mf_dir = "up"
        elif btc_e9 < btc_e21:
            btc_mf_dir = "down"
        else:
            btc_mf_dir = "flat"
        result["trend_btc_mf"] = btc_mf_dir
        if trend_dir == "up" and btc_mf_dir == "down":
            if not ai_mode:
                result["skip_reason"] = "btc_macro_bearish"
                return result
            macro_blocked.append("btc_macro_bearish")
        if trend_dir == "down" and btc_mf_dir == "up":
            if not ai_mode:
                result["skip_reason"] = "btc_macro_bullish"
                return result
            macro_blocked.append("btc_macro_bullish")

    # ── Step 2: 15m Entry Trigger (2/3 groups) ──

    ema20_15m = ema(closes_15m, ET["ema_period"])
    rsi_val = rsi(closes_15m, ET["rsi_period"])
    avg_vol = sum(volumes_15m) / len(volumes_15m) if volumes_15m else 1
    current_vol = volumes_15m[-1]
    vol_ratio = current_vol / avg_vol if avg_vol > 0 else 0
    last_candle_green = closes_15m[-1] >= opens_15m[-1]
    last_candle_red = closes_15m[-1] < opens_15m[-1]

    price_vs_ema20_pct = (
        (current_price - ema20_15m) / ema20_15m * 100
    ) if ema20_15m > 0 else 0

    groups_met = []
    entry_mode = ET.get("entry_mode", "pullback")

    if trend_dir == "up":
        # RSI ceiling: block long entry when market is overbought (reversal risk)
        rsi_ceiling = ET.get("long_entry_rsi_ceiling", 0)
        if rsi_ceiling > 0 and rsi_val > rsi_ceiling:
            if not ai_mode:
                result["skip_reason"] = f"rsi_overbought_ceiling ({rsi_val:.0f} > {rsi_ceiling})"
                result["entry_15m"] = {
                    "ema20": round(ema20_15m, 6),
                    "price_vs_ema20_pct": round(price_vs_ema20_pct, 2),
                    "rsi14": round(rsi_val, 1),
                    "volume_ratio": round(vol_ratio, 2),
                    "candle_color": "green" if last_candle_green else "red",
                    "group_A": False,
                    "group_A_label": entry_mode,
                    "group_B_rsi": False,
                    "group_C_volume_candle": False,
                    "groups_met": 0,
                    "groups_detail": [],
                }
                return result
            macro_blocked.append(f"rsi_overbought_ceiling ({rsi_val:.0f} > {rsi_ceiling})")

        if entry_mode == "breakout":
            # Group A: price must break ABOVE EMA20 (breakout)
            breakout_min = ET.get("group_a_breakout_min_pct", 0.3)
            breakout_max = ET.get("group_a_breakout_max_pct", 2.0)
            group_a = breakout_min <= price_vs_ema20_pct <= breakout_max
            if group_a:
                groups_met.append("A:breakout_above_ema")
            # Group B: RSI confirms upward momentum
            rsi_long_min = ET.get("group_b_rsi_long_min", 55)
            group_b = rsi_val > rsi_long_min
            if group_b:
                groups_met.append(f"B:rsi>{rsi_long_min}")
        else:
            # Group A: price must be at or below EMA20 (true pullback, not hugging from above)
            group_a = -ET["group_a_pullback_pct"] <= price_vs_ema20_pct <= 0
            if group_a:
                groups_met.append("A:pullback_to_ema")
            # Group B: RSI below threshold (pullback recovery zone)
            group_b = rsi_val < ET["group_b_rsi_long_max"]
            if group_b:
                groups_met.append(f"B:rsi<{ET['group_b_rsi_long_max']}")

        # Group C: Volume above avg + green candle
        group_c = vol_ratio > ET["group_c_vol_ratio_min"] and last_candle_green
        if group_c:
            groups_met.append("C:volume+green")

        if not ai_mode:
            if len(groups_met) >= ET["min_groups"]:
                result["signal"] = "long"
            else:
                result["skip_reason"] = f"insufficient_entry_groups ({len(groups_met)}/3)"

    elif trend_dir == "down":
        # RSI floor: block short entry when market is deeply oversold (bounce risk)
        rsi_floor = ET.get("short_entry_rsi_floor", 0)
        if rsi_floor > 0 and rsi_val < rsi_floor:
            if not ai_mode:
                result["skip_reason"] = f"rsi_oversold_floor ({rsi_val:.0f} < {rsi_floor})"
                result["entry_15m"] = {
                    "ema20": round(ema20_15m, 6),
                    "price_vs_ema20_pct": round(price_vs_ema20_pct, 2),
                    "rsi14": round(rsi_val, 1),
                    "volume_ratio": round(vol_ratio, 2),
                    "candle_color": "green" if last_candle_green else "red",
                    "group_A": False,
                    "group_A_label": entry_mode,
                    "group_B_rsi": False,
                    "group_C_volume_candle": False,
                    "groups_met": 0,
                    "groups_detail": [],
                }
                return result
            macro_blocked.append(f"rsi_oversold_floor ({rsi_val:.0f} < {rsi_floor})")

        if entry_mode == "breakout":
            # Group A: price must break BELOW EMA20 (breakout)
            breakout_min = ET.get("group_a_breakout_min_pct", 0.3)
            breakout_max = ET.get("group_a_breakout_max_pct", 2.0)
            group_a = -breakout_max <= price_vs_ema20_pct <= -breakout_min
            if group_a:
                groups_met.append("A:breakout_below_ema")
            # Group B: RSI confirms downward momentum
            rsi_short_max = ET.get("group_b_rsi_short_max", 45)
            group_b = rsi_val < rsi_short_max
            if group_b:
                groups_met.append(f"B:rsi<{rsi_short_max}")
        else:
            # Group A: price must be at or above EMA20 (true bounce, not hugging from below)
            group_a = 0 <= price_vs_ema20_pct <= ET["group_a_pullback_pct"]
            if group_a:
                groups_met.append("A:pullback_to_ema")
            # Group B: RSI above threshold (overbought rejection zone)
            group_b = rsi_val > ET["group_b_rsi_short_min"]
            if group_b:
                groups_met.append(f"B:rsi>{ET['group_b_rsi_short_min']}")

        # Group C: Volume above avg + red candle
        group_c = vol_ratio > ET["group_c_vol_ratio_min"] and last_candle_red
        if group_c:
            groups_met.append("C:volume+red")

        if not ai_mode:
            if len(groups_met) >= ET["min_groups"]:
                result["signal"] = "short"
            else:
                result["skip_reason"] = f"insufficient_entry_groups ({len(groups_met)}/3)"

    # ── Group D: AI Judge (optional) ──

    group_d = None  # None=not called, True/False=result
    ai_reason = None

    if ai_mode and len(groups_met) >= 2:
        candidate_side = "long" if trend_dir == "up" else "short"
        ai_params = {
            "coin": coin,
            "side": candidate_side,
            "price": round(current_price, 6),
            "trend_direction": trend_dir,
            "adx": round(adx_val, 1),
            "macro_pair": MF.get("reference_pair", "BTC"),
            "macro_tf": MF.get("timeframe", "4h"),
            "macro_direction": btc_mf_dir or "N/A",
            "rsi": round(rsi_val, 1),
            "ema20_distance_pct": round(price_vs_ema20_pct, 2),
            "volume_ratio": round(vol_ratio, 2),
            "candle_color": "green" if last_candle_green else "red",
            "macro_blocked": macro_blocked,
            "recent_fills": recent_fills or [],
        }
        ai_result = ai_judge(ai_params)
        group_d = ai_result["passed"]
        ai_reason = ai_result.get("reason", "")
        if group_d:
            groups_met.append("D:ai_judge")

    # ── Signal decision (AI mode) ──

    if ai_mode:
        candidate_side = "long" if trend_dir == "up" else "short"
        # AI Judge has veto power: if called and returned FALSE, always skip
        if group_d is False:
            ai_reason_short = ai_reason[:80] if ai_reason else "rejected"
            result["skip_reason"] = f"ai_judge_rejected ({ai_reason_short})"
        elif len(groups_met) >= 3:
            result["signal"] = candidate_side
        else:
            n = len(groups_met)
            if group_d is None and n < 2:
                result["skip_reason"] = f"insufficient_entry_groups ({n}/4)"
            elif group_d is None:
                result["skip_reason"] = f"insufficient_entry_groups ({n}/4, AI not called)"
            else:
                result["skip_reason"] = f"insufficient_entry_groups_ai ({n}/4)"

    # ── Confidence scoring ──

    if result["signal"] != "none":
        n_groups = len(groups_met)

        if ai_mode:
            high_min = 4
            medium_min = 3
        else:
            high_min = CONF["high_min_groups"]
            medium_min = CONF["medium_min_groups"]

        if n_groups >= high_min:
            result["confidence"] = "high"
        elif n_groups >= medium_min:
            result["confidence"] = "medium"

        # Compute TP/SL from config
        tp_mult = TP_cfg["tp_pct"] / 100
        sl_mult = TP_cfg["sl_pct"] / 100
        if result["signal"] == "long":
            result["entry_suggested"] = round(current_price, 6)
            result["tp"] = round(current_price * (1 + tp_mult), 6)
            result["sl"] = round(current_price * (1 - sl_mult), 6)
        else:
            result["entry_suggested"] = round(current_price, 6)
            result["tp"] = round(current_price * (1 - tp_mult), 6)
            result["sl"] = round(current_price * (1 + sl_mult), 6)
        result["tp_pct"] = TP_cfg["tp_pct"]
        result["sl_pct"] = TP_cfg["sl_pct"]
        result["rr"] = round(TP_cfg["tp_pct"] / TP_cfg["sl_pct"], 1)
        result["be_trigger_pct"] = TP_cfg.get("be_trigger_pct", 0)
        result["be_offset_pct"] = TP_cfg.get("be_offset_pct", 0)

    result["entry_15m"] = {
        "ema20": round(ema20_15m, 6),
        "price_vs_ema20_pct": round(price_vs_ema20_pct, 2),
        "rsi14": round(rsi_val, 1),
        "volume_ratio": round(vol_ratio, 2),
        "candle_color": "green" if last_candle_green else "red",
        "group_A": any("A:" in g for g in groups_met),
        "group_A_label": entry_mode,
        "group_B_rsi": any("B:" in g for g in groups_met),
        "group_C_volume_candle": any("C:" in g for g in groups_met),
        "group_D_ai": group_d,
        "ai_reason": ai_reason,
        "groups_met": len(groups_met),
        "groups_detail": groups_met,
    }

    return result


# ── Output: JSON ──────────────────────────────────────────────────────────────

def output_json(results, cfg):
    """Print JSON output and exit."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    TF = cfg["trend_filter"]
    ET = cfg["entry_trigger"]
    MF = cfg.get("macro_filter", {})
    macro_dir = None
    for r in results:
        if r.get("trend_btc_mf"):
            macro_dir = r["trend_btc_mf"]
            break
    output = {
        "analyzed_at": now_str,
        "strategy": cfg["_name"],
        "trend_tf": TF["timeframe"],
        "entry_tf": ET["timeframe"],
        "macro_filter": {
            "pair": MF.get("reference_pair", "N/A"),
            "timeframe": MF.get("timeframe", "N/A"),
            "direction": macro_dir or "N/A",
        },
        "pairs": results,
    }
    print(json.dumps(output, indent=2))


def output_json_compact(results, cfg):
    """Print compact JSON: only actionable signals, minimal fields, no indent."""
    signals = []
    skipped = 0
    for r in results:
        if r["signal"] == "none":
            skipped += 1
            continue
        sig_entry = {
            "coin": r["coin"],
            "signal": r["signal"],
            "confidence": r["confidence"],
            "price": r["current_price"],
            "entry": r["entry_suggested"],
            "tp": r["tp"],
            "sl": r["sl"],
            "adx": r["trend_1h"]["adx"],
            "rsi": r["entry_15m"]["rsi14"],
            "groups": r["entry_15m"]["groups_met"],
            "detail": r["entry_15m"]["groups_detail"],
        }
        ai_val = r["entry_15m"].get("group_D_ai")
        if ai_val is not None:
            sig_entry["ai_judge"] = ai_val
            sig_entry["ai_reason"] = r["entry_15m"].get("ai_reason", "")
        signals.append(sig_entry)
    tp = cfg["trade_params"]
    print(json.dumps({
        "signals": signals,
        "skipped": skipped,
        "trade_params": {
            "notional": tp["notional"],
            "leverage": tp["leverage"],
            "max_positions": tp.get("max_positions", 10),
            "max_same_direction": tp.get("max_same_direction", tp.get("max_positions", 10)),
        },
    }, separators=(',', ':')))


# ── Output: Human-readable ───────────────────────────────────────────────────

def output_human(results):
    """Print colored human-readable output."""
    time_short = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header_box(f"SCALP SIGNALS | {time_short}")

    actionable = []

    for r in results:
        coin = r["coin"]
        price_str = fmt_price(r["current_price"])

        if r["signal"] == "none":
            reason = r.get("skip_reason", "no signal")
            print(f"  {dim(coin):<16} {dim('|')}  {dim('-- SKIP')}  {dim('|')}  {price_str}  {dim('|')}  {dim(reason)}")
            print()
            continue

        sig = r["signal"].upper()
        conf = r["confidence"]
        e = r["entry_15m"]
        groups_n = e["groups_met"]
        total_groups = 4 if e.get("group_D_ai") is not None else 3

        tp_str = fmt_price(r["tp"])
        sl_str = fmt_price(r["sl"])

        print(
            f"  {bold(coin):<5} {dim('|')}  "
            f"{side_label(sig)} {signal_badge(conf)} ({groups_n}/{total_groups})  {dim('|')}  "
            f"{price_str}  {dim('|')}  "
            f"TP {green(tp_str)} (+{r['tp_pct']}%)  {dim('|')}  "
            f"SL {red(sl_str)} (-{r['sl_pct']}%)"
        )

        # 1h trend
        t = r["trend_1h"]
        ema_rel = "EMA9>EMA21" if t["direction"] == "up" else "EMA9<EMA21"
        print(f"        {dim('|')}  1h: {trend_arrow(t['direction'])} ({ema_rel}, ADX {t['adx']})")

        # 15m entry groups
        parts = [
            f"[A] {e.get('group_A_label', 'pullback')} {check(e['group_A'])}",
            f"[B] RSI {e['rsi14']} {check(e['group_B_rsi'])}",
            f"[C] vol {e['volume_ratio']}x + {e['candle_color'].upper()} {check(e['group_C_volume_candle'])}",
        ]
        if e.get("group_D_ai") is not None:
            ai_reason_short = f" ({e['ai_reason'][:40]})" if e.get("ai_reason") else ""
            parts.append(f"[D] AI {check(e['group_D_ai'])}{ai_reason_short}")
        print(f"        {dim('|')}  15m: {'  '.join(parts)}")
        print()

        if conf in ("high", "medium"):
            stars = signal_badge(conf)
            actionable.append(f"{coin} -> {side_label(sig)} {stars}")

    separator()
    if actionable:
        print(f"  ACTION: {'  |  '.join(actionable)}")
    else:
        print("  ACTION: No signals -- all pairs skipped")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        usage()

    load_env()  # Load .env so AI_JUDGE env vars are available

    coins, json_mode, compact, strategy_file = parse_args(sys.argv)

    # Load strategy config
    cfg = load_strategy(strategy_file)

    # Fall back to strategy config pairs if no CLI pairs given
    if not coins:
        coins = cfg.get("trade_params", {}).get("monitoring_pairs", [])
        if not coins:
            print("Error: No coins specified (pass as args or set monitoring_pairs in strategy config)", file=sys.stderr)
            sys.exit(1)

    # Compute time windows: 50 x 1h candles, 50 x 15m candles, 30 x macro filter candles
    end_ms = int(time.time() * 1000)
    start_1h = end_ms - (50 * 3600000)
    start_15m = end_ms - (50 * 900000)

    MF = cfg.get("macro_filter", {})
    btc_ref = MF.get("reference_pair", "BTC")
    mf_tf = MF.get("timeframe", "4h")
    mf_hours = int(mf_tf.replace("h", ""))
    start_mf = end_ms - (30 * mf_hours * 3600000)

    # Build parallel fetch requests
    requests = []
    for coin in coins:
        requests.append((coin, "1h", start_1h, end_ms))
        requests.append((coin, "15m", start_15m, end_ms))

    # Add BTC macro filter fetch if enabled
    if MF.get("enabled"):
        requests.append((btc_ref, mf_tf, start_mf, end_ms))

    # Fetch all candles in parallel
    hl = HyperliquidAPI()
    candle_data = hl.get_candles_parallel(requests)

    candles_btc_mf = candle_data.get((btc_ref, mf_tf), []) if MF.get("enabled") else None

    # Fetch recent fills per coin if AI judge is enabled
    fills_by_coin = {}
    if ai_judge_enabled():
        hl_address = os.environ.get("HL_ADDRESS", "").strip()
        if hl_address:
            try:
                for coin in coins:
                    fills_by_coin[coin] = hl.get_fills(hl_address, coin=coin, limit=5)
            except Exception:
                pass  # fills are optional context, don't break signal analysis

    # Analyze each pair
    results = []
    for coin in coins:
        candles_1h = candle_data.get((coin, "1h"), [])
        candles_15m = candle_data.get((coin, "15m"), [])
        result = analyze_pair(
            coin, candles_1h, candles_15m, cfg,
            candles_btc_mf=candles_btc_mf,
            recent_fills=fills_by_coin.get(coin),
        )
        results.append(result)

    # Output
    if json_mode:
        if compact:
            output_json_compact(results, cfg)
        else:
            output_json(results, cfg)
        return

    output_human(results)


if __name__ == "__main__":
    main()
    print(api_summary_str(), file=sys.stderr)
