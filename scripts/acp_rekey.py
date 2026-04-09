#!/usr/bin/env python3
"""Regenerate ACP API key for Captain Dackie via browser login.

Usage:
    python3 scripts/acp_rekey.py

Flow:
    1. Fetch auth URL from acpx.virtuals.io
    2. Open browser for login
    3. Poll for session token
    4. Fetch agents, find Captain Dackie
    5. Regenerate API key
    6. Save to .env
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

ACP_AUTH_URL = "https://acpx.virtuals.io"
CAPTAIN_WALLET = os.environ.get("DGCLAW_ADDRESS", "")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(SCRIPT_DIR, "..", ".env")


BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def http_get(url, headers=None):
    hdrs = {"User-Agent": BROWSER_UA}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def http_post(url, data=None, headers=None):
    body = json.dumps(data or {}).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def login():
    print("\n[1/5] Fetching auth URL...")
    res = http_get(f"{ACP_AUTH_URL}/api/auth/lite/auth-url")
    payload = res.get("data", res)
    auth_url = payload.get("authUrl")
    request_id = payload.get("requestId")

    if not auth_url or not request_id:
        print(f"Error: Invalid auth response: {res}")
        sys.exit(1)

    print(f"[2/5] Opening browser for login...")
    print(f"      URL: {auth_url}")
    subprocess.run(["open", auth_url], check=False)

    print("[2/5] Polling for login confirmation (max 5 min)...")
    for attempt in range(60):
        time.sleep(5)
        try:
            status_res = http_get(
                f"{ACP_AUTH_URL}/api/auth/lite/auth-status?requestId={request_id}"
            )
            sp = status_res.get("data", status_res)
            token = sp.get("token") or sp.get("sessionToken")
            if token:
                print(f"\n[2/5] Login successful!")
                return token
        except Exception:
            pass
        print(f"      Waiting... (attempt {attempt + 1}/60)", end="\r")

    print("\nError: Login timeout")
    sys.exit(1)


def fetch_agents(session_token):
    print("[3/5] Fetching agents...")
    res = http_get(
        f"{ACP_AUTH_URL}/api/agents/lite",
        {"Authorization": f"Bearer {session_token}"},
    )
    agents = res.get("data", res)
    if not isinstance(agents, list):
        print(f"Error: Unexpected agents response: {res}")
        sys.exit(1)
    for a in agents:
        print(f"      - {a.get('name')} ({a.get('walletAddress', '')[:10]}...)")
    return agents


def regenerate_key(wallet, session_token):
    print(f"[4/5] Regenerating API key for {wallet[:10]}...")
    for attempt in range(3):
        try:
            res = http_post(
                f"{ACP_AUTH_URL}/api/agents/lite/{wallet}/regenerate-api",
                {},
                {"Authorization": f"Bearer {session_token}"},
            )
            p = res.get("data", res)
            key = p.get("apiKey") or p.get("key")
            if key:
                print(f"[4/5] New key: {key[:8]}...{key[-4:]}")
                return key
            print(f"      Unexpected response: {res}")
        except Exception as e:
            print(f"      Attempt {attempt + 1} failed: {e}")
            time.sleep(2)

    print("Error: Failed to regenerate key after 3 attempts")
    sys.exit(1)


def save_to_env(key):
    print(f"[5/5] Saving to {ENV_FILE}")

    lines = []
    if os.path.isfile(ENV_FILE):
        with open(ENV_FILE) as f:
            lines = f.readlines()

    # Replace or append LITE_AGENT_API_KEY
    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith("LITE_AGENT_API_KEY="):
            new_lines.append(f"LITE_AGENT_API_KEY={key}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"LITE_AGENT_API_KEY={key}\n")

    with open(ENV_FILE, "w") as f:
        f.writelines(new_lines)

    print(f"[5/5] Done! LITE_AGENT_API_KEY updated.")


def main():
    print("=== ACP API Key Regenerator ===")

    session_token = login()
    agents = fetch_agents(session_token)

    # Find Captain Dackie
    target = None
    for a in agents:
        if a.get("walletAddress", "").lower() == CAPTAIN_WALLET.lower():
            target = a
            break

    if not target:
        print(f"\nCaptain Dackie ({CAPTAIN_WALLET}) not found in agents.")
        if agents:
            print("Using first agent instead.")
            target = agents[0]
        else:
            print("Error: No agents found")
            sys.exit(1)

    print(f"\n      Target: {target.get('name')} ({target.get('walletAddress')})")

    new_key = regenerate_key(target["walletAddress"], session_token)
    save_to_env(new_key)

    # Verify
    print("\n[Verify] Testing new key...")
    try:
        req = urllib.request.Request(
            "https://claw-api.virtuals.io/acp/jobs/active",
            headers={"x-api-key": new_key},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        print(f"[Verify] Status {resp.status} - Key works!")
    except urllib.error.HTTPError as e:
        print(f"[Verify] HTTP {e.code} - Key may need a moment to propagate")

    print("\n=== Complete ===")


if __name__ == "__main__":
    main()
