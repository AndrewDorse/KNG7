# KNG7 — `limit_pair_5m` (Docker)

Places **two GTC limit buys** on each upcoming **5m** Polymarket UP/DOWN window for multiple assets:

- **Symbols:** `BTC, ETH, SOL, BNB, XRP` (`BOT_LIMIT_PAIR_SYMBOLS`)
- **UP** @ **50¢** × **5** shares  
- **DOWN** @ **49¢** × **5** shares  

Gamma slugs: `{sym}-updown-5m-<epoch>` (UTC window start).

## Schedule

Every **`BOT_LIMIT_PAIR_SEARCH_INTERVAL_SEC`** (default **300** = 5 minutes):

1. Compute **`BOT_LIMIT_PAIR_HOURS`** (default **2**) or **`BOT_LIMIT_PAIR_WINDOW_COUNT`** epochs (24 for 2h) starting at the next 5m boundary **after** `now + BOT_LIMIT_PAIR_LEAD_MINUTES` (default **15**).
2. Example: now **07:53** → first window **08:10** UTC, last window ends **10:10** (24 × 5m × 5 symbols = **120** order pairs max per full scan).
3. Resolve each slug on Gamma; cache contracts.
4. Queue windows not yet in `exports/limit_pair_state.json`.
5. Place **one window pair** (UP + DOWN), then wait **`BOT_LIMIT_PAIR_ORDER_SPACING_SEC`** (default **10**) before the next — no burst posting.

When both limits are visible on the CLOB, the slug is marked **done**, removed from the work queue, and persisted so restarts do not duplicate.

**Work queue:** closest window start is always first. Every 5 minutes new epochs are **appended** and the list is re-sorted. Each **10s** cycle tries only the **top** slot. If placement fails (especially **insufficient balance**), that slot stays at the front and is retried after 10s — funds may free up as other orders cancel or windows settle.

## Logs (stdout)

- **`INIT`** — prices, lead, window count, next UTC block, sent/confirmed counts  
- **`SEARCH`** — how many targets resolved / queued  
- **`ORDER`** — slug and prices placed (or `dry_run`)  
- **`CONFIRMED`** — both resting limits seen  

Stderr: **`BOT_LOG_LEVEL`** (default **ERROR** in `main.py`).

## Go-live

1. `cp .env.example .env` — **`POLY_PRIVATE_KEY`**, **`POLY_FUNDER`**, **`POLY_DRY_RUN=false`** for live.  
2. **`BOT_STRATEGY_MODE=limit_pair_5m`**.  
3. Network: **HTTPS** `gamma-api.polymarket.com`, `clob.polymarket.com` (optional **WSS** for WS).  
4. `docker compose build && docker compose up -d`  
5. `docker compose logs -f bot` — confirm **`INIT`** and periodic **`SEARCH`**.

## Repo layout

`config`, `trader`, `market_locator`, `http_session`, `limit_pair_engine.py`, `main.py`. Legacy **`cheap03_first_engine.py`** is kept but not used by default.
