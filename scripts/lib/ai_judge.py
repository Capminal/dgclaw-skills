"""AI Judge (Group D) — LLM-based trade setup evaluation via OpenRouter.

Self-contained module using only stdlib (urllib). No pip installs needed.
Controlled by env vars: AI_JUDGE_ENABLED=1, OPENROUTER_API_KEY=sk-or-...
"""
import json
import os
import re
import time
import urllib.request
import urllib.error

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "x-ai/grok-4.1-fast"
TIMEOUT_S = 15

_SYSTEM_PROMPT = (
    "You are a 10 years experienced crypto scalping trade evaluator for a trend-following pullback strategy. "
    "You receive full market data including macro indicators, technical levels, and which "
    "deterministic checks passed. Your job is to decide if this trade setup is worth taking. "
    "Consider trend strength (ADX), macro alignment (BTC direction), momentum (RSI), "
    "price action quality, and any macro warnings. "
    "Respond with ONLY a JSON object: {\"pass\": true, \"reason\": \"brief reason\"} "
    "or {\"pass\": false, \"reason\": \"brief reason\"}. No other text."
)


def is_enabled():
    """Check if AI judge is enabled via env."""
    return os.environ.get("AI_JUDGE_ENABLED", "0") == "1"


def judge(params):
    """Call OpenRouter for a true/false judgment on a trade setup.

    Args:
        params: dict with keys:
            coin, side, price, trend_direction, adx,
            btc_macro, rsi, ema20_distance_pct, volume_ratio,
            candle_color, groups_passed, macro_blocked

    Returns:
        {"passed": bool, "reason": str, "latency_ms": int}
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return {"passed": False, "reason": "error: OPENROUTER_API_KEY not set", "latency_ms": 0}

    prompt = _build_prompt(params)

    payload = {
        "model": MODEL,
        "max_tokens": 150,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    t0 = time.monotonic()
    try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(OPENROUTER_URL, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            result = json.loads(resp.read())
        latency = int((time.monotonic() - t0) * 1000)
        return _parse_response(result, latency)
    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        return {"passed": False, "reason": f"error: {e}", "latency_ms": latency}


def _build_prompt(p):
    """Format token parameters into a structured prompt."""
    macro_warnings = ", ".join(p.get("macro_blocked", [])) or "none"

    recent_fills_lines = ""
    fills = p.get("recent_fills", [])
    if fills:
        rows = []
        for f in fills[:5]:
            pnl = f.get("closedPnl", "?")
            px = f.get("px", "?")
            direction = f.get("dir", "")
            rows.append(f"  {direction}, Entry: {px}, PnL: {pnl}")
        recent_fills_lines = "\n- Recent fills (last 5):\n" + "\n".join(rows)

    return (
        f"Evaluate this scalp trade setup:\n"
        f"- Coin: {p['coin']}, Side: {p['side'].upper()}, Price: ${p['price']}\n"
        f"- 1h Trend: {p['trend_direction'].upper()}, ADX: {p['adx']}\n"
        f"- {p.get('macro_pair', 'BTC')} {p.get('macro_tf', '4h')} Macro: {p.get('macro_direction', p.get('btc_macro', 'N/A'))}\n"
        f"- 15m RSI: {p['rsi']}, EMA20 distance: {p['ema20_distance_pct']}%\n"
        f"- Volume ratio: {p['volume_ratio']}x, Candle: {p['candle_color'].upper()}\n"
        f"- Macro warnings (would have blocked without AI): {macro_warnings}\n"
        f"{recent_fills_lines}\n"
        f"\nShould this trade be taken?"
    )


def _parse_response(result, latency_ms):
    """Extract pass/fail from OpenRouter response."""
    try:
        content = result["choices"][0]["message"]["content"].strip()
        # Strip markdown code blocks if present
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        data = json.loads(content)
        passed = bool(data.get("pass", False))
        reason = str(data.get("reason", ""))[:200]
        return {"passed": passed, "reason": reason, "latency_ms": latency_ms}
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        return {"passed": False, "reason": f"parse_error: {e}", "latency_ms": latency_ms}
