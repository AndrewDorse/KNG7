# KNG7 — `first_cheap_03` (Docker)

One **BTC 15m UP/DOWN** market at a time (Gamma slug `btc-updown-15m-<epoch>`). Each window:

1. Poll CLOB **midpoints** for UP and DOWN.
2. On the **first** second the sim rule fires — either leg midpoint `≤ BOT_CHEAP03_PRICE_MAX` (default **0.03**), side = cheaper leg if both qualify — submit **one** **$1** USDC **FAK** buy (`py_clob_client` market order).
3. When the **next** window’s slug appears, resolve the **previous** slug on Gamma; if the market is **closed** and the winning outcome matches the bought side, log a **`WIN`** line.

## Logs (stdout)

Only:

- **`INIT …`** once at process start (strategy, slug hint, threshold, notional, dry-run, funder).
- **`WIN slug=… side=up|down`** when a **real** (non–dry-run) buy later resolves in your favor.

Losses, skips, book misses, and errors are **silent** (see ops note below).

## Go-live checklist

1. `cp .env.example .env` and set **`POLY_PRIVATE_KEY`**, **`POLY_FUNDER`**, relayer creds if you use them.
2. Set **`POLY_DRY_RUN=false`** only when you intend real orders.
3. Confirm **`BOT_STRATEGY_MODE=first_cheap_03`**, **`BOT_CHEAP03_NOTIONAL_USDC=1`**, **`BOT_CHEAP03_PRICE_MAX=0.03`**.
4. `docker compose build && docker compose up -d` from this directory.
5. `docker compose logs -f bot` — you should see **`INIT`**; **`WIN`** lines appear only after settled winning windows.

## Ops / risk

- **No loss / error lines** by design; use container health, metrics, or temporarily add logging if you need visibility.
- **Resolution** uses Gamma `closed` + `outcomePrices`; until the market closes, `WIN` is not emitted.
- **Simulation** used recorded **1 Hz mids**; live uses **CLOB midpoints** when available — behavior can differ from backtests.
- **One buy per window**; failed POST does not consume the window (retries next poll).

## Repo layout

Minimal copy from KNG3: `config`, `trader`, `market_locator`, `http_session`, `clob_fak`, `requirements.txt`, plus `cheap03_first_engine.py` and `main.py`.
