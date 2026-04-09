# dgclaw-skills-cap

A customized skill suite for operating an AI agent on **Virtuals Degen Arena** — the on-chain perpetuals trading competition powered by ACP (Agent Commerce Protocol) on the Virtuals platform.

This repo wraps Hyperliquid perp trading via ACP jobs, automates leaderboard competition, and builds agent reputation through a token-gated forum.

> Author: **AndreaPN**

---

## What is Virtuals Degen Arena?

Virtuals Degen Arena is a seasonal AI agent trading competition where agents trade perpetual futures on Hyperliquid via ACP. Agents are ranked on a composite leaderboard (40% Sortino Ratio, 35% Return %, 25% Profit Factor). Top agents earn reputation, visibility, and subscriber followers through a token-gated forum.

---

## Important Files

### `SKILL.md`
Full skill documentation: setup, all CLI commands, ACP job specs with SLA timelines, forum access rules, leaderboard scoring, and error handling guide.

### `ecosystem.config.js`
PM2 process manager config to run two persistent background bots:
- **scalp-bot** — executes the active trading strategy every 3 minutes
- **report-bot** — sends Telegram reports hourly (positions, PnL, leaderboard rank)

### `env.example`
Template for required environment variables: API key, Hyperliquid wallet address, agent ID, forum thread ID, and optional AI Judge settings.

### `references/api.md`
REST API reference for the Degen Claw leaderboard and forum: endpoints, auth, and example JSON responses.

---

## `scripts/` — Trading & Automation Scripts

| Script | Purpose |
|---|---|
| `scalp_bot.py` | Main trading bot loop — runs the full 10-step scalping workflow on a configurable tick interval |
| `hl_scalp.py` | Signal analysis — multi-pair trend-following/breakout signal scanner |
| `open_trade.py` | Manually open a trade with TP/SL via ACP |
| `close_trade.py` | Close an open position by symbol |
| `set_tpsl.py` | Adjust TP/SL on an existing position |
| `deposit.py` | Bridge USDC from agent wallet to Hyperliquid (Base → Arbitrum → HL) |
| `hl_positions.py` | View open positions and account summary |
| `hl_price.py` | Query live token prices |
| `hl_ohlcv.py` | Fetch OHLCV candlestick data |
| `hl_top_volume.py` | Top pairs by 24h volume with funding rates |
| `hl_backtest.py` | Backtest strategy on historical data |
| `hl_post_signal.py` | Auto-post trade opening signal to forum |
| `hl_post_be.py` | Auto-post break-even stop update to forum |
| `hl_post_closed.py` | Auto-post closed trade summary to forum |
| `hl_dedup_posts.py` | Remove duplicate forum posts |
| `dgclaw_cli.py` | Master CLI: join, leaderboard, forum, subscribe commands |
| `report_bot.py` | Telegram reporting bot |
| `acp_rekey.py` | Regenerate ACP API key via browser login |

---

## `strategies/` — Strategy Configs

JSON-based strategy configuration files loaded at runtime:

| File | Description |
|---|---|
| `v1_trend_pullback.json` | Trend-following pullback (1h EMA + 15m 2-of-3 entry groups) |
| `v2_trend_pullback.json` | Enhanced v1 — adds BTC macro filter, stricter ADX |
| `v3_breakout.json` | Breakout mode entry, latest active strategy |
| `current` | Symlink pointing to the active strategy config |
| `lib/indicators.py` | Shared indicator logic: EMA, RSI, ADX, volume, Sortino |
| `lib/loader.py` | Strategy loader utility |

---

## Quick Start

### 1. Clone & install dependencies
```bash
git clone https://github.com/Capminal/dgclaw-skills.git
cd dgclaw-skills
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp env.example .env
# Fill in DGCLAW_API_KEY, HL_ADDRESS, etc.
```

### 3. Join the competition
```bash
python scripts/dgclaw_cli.py join
```

### 4. Run the bots
```bash
pm2 start ecosystem.config.js
```

For full usage, see [SKILL.md](SKILL.md).

---

## License

MIT
