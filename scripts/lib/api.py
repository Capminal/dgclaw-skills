"""API clients for Hyperliquid, ACP (direct HTTP), and Degenerate Claw."""
import json
import os
import time
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── API call tracking ────────────────────────────────────────────────────────

_api_log = []  # list of (method, url, duration_ms)
_API_TIMING = os.environ.get("API_TIMING", "0") == "1"


def _log_call(method, url, duration_ms):
    """Record an API call for summary reporting."""
    _api_log.append((method, url, duration_ms))
    if _API_TIMING:
        # Short URL: strip query params, keep last 2 path segments
        short = url.split("?")[0].split("/")[-2:]
        print(f"  [{method}] {'/'.join(short)} → {duration_ms}ms", file=sys.stderr)


def api_summary():
    """Return (total_calls, total_ms, per_call_list) for the current session."""
    total = len(_api_log)
    total_ms = sum(d for _, _, d in _api_log)
    return total, total_ms, list(_api_log)


def api_summary_str():
    """Return a one-line summary string of all API calls."""
    total, total_ms, calls = api_summary()
    if not calls:
        return "API: 0 calls"
    return f"API: {total} calls, {total_ms}ms total, avg {total_ms // total}ms/call"


# ── HTTP helpers ─────────────────────────────────────────────────────────────

_DEFAULT_UA = "dgclaw-bot/1.0"


def _post(url, data, headers=None):
    """POST JSON, return parsed response."""
    body = json.dumps(data).encode()
    hdrs = {"Content-Type": "application/json", "User-Agent": _DEFAULT_UA}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    _log_call("POST", url, int((time.monotonic() - t0) * 1000))
    return result


def _get(url, headers=None):
    """GET request, return parsed JSON."""
    hdrs = {"User-Agent": _DEFAULT_UA}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    _log_call("GET", url, int((time.monotonic() - t0) * 1000))
    return result


def _patch(url, data, headers=None):
    """PATCH JSON, return parsed response."""
    body = json.dumps(data).encode()
    hdrs = {"Content-Type": "application/json", "User-Agent": _DEFAULT_UA}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="PATCH")
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    _log_call("PATCH", url, int((time.monotonic() - t0) * 1000))
    return result


def _delete(url, headers=None):
    """DELETE request, return parsed JSON."""
    hdrs = {"User-Agent": _DEFAULT_UA}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs, method="DELETE")
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    _log_call("DELETE", url, int((time.monotonic() - t0) * 1000))
    return result


# ── Hyperliquid API ──────────────────────────────────────────────────────────

HL_URL = "https://api.hyperliquid.xyz/info"


class HyperliquidAPI:

    def get_prices(self):
        """Fetch all mid-prices. Returns {token: price_str, ...}."""
        return _post(HL_URL, {"type": "allMids"})

    def get_candles(self, coin, interval, start_ms, end_ms):
        """Fetch OHLCV candles."""
        return _post(HL_URL, {
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval,
                    "startTime": start_ms, "endTime": end_ms},
        })

    def get_candles_parallel(self, requests):
        """Fetch multiple candle sets in parallel.

        Args:
            requests: list of (coin, interval, start_ms, end_ms)
        Returns:
            dict: {(coin, interval): candles_list}
        """
        results = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {}
            for coin, interval, start_ms, end_ms in requests:
                f = pool.submit(self.get_candles, coin, interval, start_ms, end_ms)
                futures[f] = (coin, interval)
            for f in as_completed(futures):
                key = futures[f]
                try:
                    results[key] = f.result()
                except Exception as e:
                    print(f"Warning: Failed to fetch {key}: {e}", file=sys.stderr)
                    results[key] = []
        return results

    def get_state(self, address):
        """Fetch clearinghouse state for an address."""
        return _post(HL_URL, {"type": "clearinghouseState", "user": address})

    def get_orders(self, address):
        """Fetch open orders for an address."""
        return _post(HL_URL, {"type": "openOrders", "user": address})

    def get_frontend_orders(self, address):
        """Fetch frontend open orders including trigger/stop orders."""
        return _post(HL_URL, {"type": "frontendOpenOrders", "user": address})

    def get_fills(self, address, coin=None, limit=10):
        """Fetch recent fills for an address, optionally filtered by coin.

        Returns list of dicts with: coin, side, px, sz, closedPnl, dir, time
        """
        import time as _time
        start_ms = int((_time.time() - 7 * 86400) * 1000)  # last 7 days
        fills = _post(HL_URL, {
            "type": "userFillsByTime",
            "user": address,
            "startTime": start_ms,
            "aggregateByTime": False,
        })
        if coin:
            fills = [f for f in fills if f.get("coin") == coin]
        # Sort newest first
        fills.sort(key=lambda f: f.get("time", 0), reverse=True)
        return fills[:limit]


# ── ACP Direct API (replaces `acp` CLI) ─────────────────────────────────────

ACP_BASE = "https://claw-api.virtuals.io"
DEGENCLAW_WALLET = "0xd478a8B40372db16cA8045F28C6FE07228F3781A"
SUBSCRIBE_WALLET = "0xC751AF68b3041eDc01d4A0b5eC4BFF2Bf07Bae73"


class AcpAPI:

    def __init__(self, api_key):
        self.headers = {"x-api-key": api_key}

    def create_job(self, wallet, offering, requirements, automated=False):
        """Create an ACP job. Returns job data dict."""
        return _post(f"{ACP_BASE}/acp/jobs", {
            "providerWalletAddress": wallet,
            "jobOfferingName": offering,
            "serviceRequirements": requirements,
            "isAutomated": automated,
        }, self.headers)

    def job_status(self, job_id):
        """Get job status."""
        return _get(f"{ACP_BASE}/acp/jobs/{job_id}", self.headers)

    def approve_payment(self, job_id, content="Proceed"):
        """Approve a TRANSACTION phase payment."""
        return _post(
            f"{ACP_BASE}/acp/providers/jobs/{job_id}/negotiation",
            {"accept": True, "content": content},
            self.headers,
        )

    def poll_job(self, job_id, timeout=300, interval=5, label="Job"):
        """Poll job until COMPLETED/FAILED. Auto-approves TRANSACTION phase.

        Returns:
            dict: final job status response
        Raises:
            RuntimeError on timeout or failure
        """
        polls = timeout // interval
        for i in range(polls):
            time.sleep(interval)
            try:
                resp = self.job_status(job_id)
            except Exception:
                continue

            data = resp.get("data", resp)
            if isinstance(data, list):
                data = data[0] if data else {}

            # Check memos (field is "memos", NOT "memoHistory")
            memos = data.get("memos", data.get("memoHistory", []))
            if memos:
                latest = sorted(memos, key=lambda m: m.get("createdAt", ""))[-1]
                phase = latest.get("nextPhase", "PENDING")
                status = latest.get("status", "")
                content = latest.get("content", latest.get("memo", latest.get("message", "")))
            else:
                phase = data.get("phase", data.get("status", "PENDING"))
                status = ""
                content = ""

            phase_upper = phase.upper()

            if phase_upper == "COMPLETED":
                return resp

            if phase_upper in ("FAILED", "REJECTED"):
                raise RuntimeError(f"{label} rejected: {content or json.dumps(data)}")

            if phase_upper == "TRANSACTION":
                if status.upper() == "PENDING":
                    try:
                        self.approve_payment(job_id)
                    except Exception:
                        pass

        raise RuntimeError(f"{label} timed out after {timeout}s")


# ── Degenerate Claw API ─────────────────────────────────────────────────────

DGCLAW_BASE = "https://degen.virtuals.io"
DGCLAW_TRADE_BASE = "https://dgclaw-app-production.up.railway.app"


class DgclawAPI:

    def __init__(self, api_key, base_url=None):
        self.base = base_url or DGCLAW_BASE
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def _g(self, path):
        return _get(f"{self.base}{path}", self.headers)

    def _p(self, path, data):
        return _post(f"{self.base}{path}", data, {
            **self.headers, "Content-Type": "application/json",
        })

    def _pa(self, path, data):
        return _patch(f"{self.base}{path}", data, {
            **self.headers, "Content-Type": "application/json",
        })

    def _d(self, path, headers_override=None):
        hdrs = headers_override or self.headers
        return _delete(f"{self.base}{path}", hdrs)

    # Leaderboard
    def leaderboard(self, limit=20, offset=0):
        return self._g(f"/api/leaderboard?limit={limit}&offset={offset}")

    def leaderboard_agent(self, name):
        data = self._g("/api/leaderboard?limit=1000")
        entries = data.get("data", [])
        return [e for e in entries if name.lower() in e.get("name", "").lower()]

    # Forum
    def forums(self):
        return self._g("/api/forums")

    def forum(self, agent_id):
        return self._g(f"/api/forums/{agent_id}")

    def posts(self, agent_id, thread_id, limit=None):
        url = f"/api/forums/{agent_id}/threads/{thread_id}/posts"
        data = self._g(url).get("data", [])
        if limit:
            data = data[:limit]
        return {"data": data}

    def create_post(self, agent_id, thread_id, title, content):
        return self._p(
            f"/api/forums/{agent_id}/threads/{thread_id}/posts",
            {"title": title, "content": content},
        )

    def delete_post(self, agent_id, thread_id, post_id, forum_api_key):
        """Delete a post. Requires DGCLAW_FORUM_API_KEY (different from DGCLAW_API_KEY)."""
        hdrs = {"Authorization": f"Bearer {forum_api_key}"}
        return self._d(f"/api/forums/{agent_id}/threads/{thread_id}/posts/{post_id}", hdrs)

    def unreplied_posts(self, agent_id):
        return self._g(f"/api/forums/{agent_id}/posts?unreplied=true")

    # Subscription
    def get_price(self, agent_id):
        return self._g(f"/api/agents/{agent_id}/subscription-price")

    def set_price(self, agent_id, price_val):
        return self._pa(f"/api/agents/{agent_id}/settings",
                        {"subscriptionPrice": str(price_val)})

    # Token
    def token_info(self, token_address):
        return _get(f"{self.base}/api/agent-tokens/{token_address}")

    # Trading data (DGCLAW endpoints)
    def account(self, address):
        return _get(f"{DGCLAW_TRADE_BASE}/users/{address}/account")

    def positions(self, address):
        return _get(f"{DGCLAW_TRADE_BASE}/users/{address}/positions")

    def trades(self, address, limit=5):
        return _get(f"{DGCLAW_TRADE_BASE}/users/{address}/perp-trades?limit={limit}")

    def closed_trades(self, address, limit=20):
        return _get(f"{DGCLAW_TRADE_BASE}/users/{address}/perp-trades?status=closed&limit={limit}")
