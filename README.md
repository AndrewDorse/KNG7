# KNG7 — `first_cheap_03` (Docker)

One **BTC 15m UP/DOWN** market at a time (Gamma slug `btc-updown-15m-<epoch>`).

## Default behavior (`BOT_CHEAP03_ENTRY=dual_limits`)

1. On each **new window** (slug change), place **GTC limit buys** on **UP** and **DOWN** at **`BOT_CHEAP03_LIMIT_PX`** (default **0.03**) for **`BOT_CHEAP03_LIMIT_SHARES`** (default **34**) shares each side (~\$1 notional per side at 3¢).
2. While both bids are live, the engine **polls take-profit** every **`BOT_POLL_INTERVAL_SECONDS`**: **GTC limit sells at \$0.99** for each token where you hold whole shares (including shares already escrowed in an existing 99¢ sell).
3. When the **next** window’s slug appears, the previous slug may resolve on Gamma for **`WIN`** (market-entry mode only tracks one-sided pending; dual mode is inventory + TP).

## Legacy market mode (`BOT_CHEAP03_ENTRY=market`)

1. Poll CLOB **midpoints** for UP and DOWN.
2. On the **first** poll where either leg midpoint `≤ BOT_CHEAP03_PRICE_MAX`, pick the cheaper leg if both qualify — submit **one** **\$1** USDC **FAK** buy.
3. After a fill, one **forced** TP sync runs; then TP follows **`BOT_TP_POLL_SECONDS`**.

## Logs

- **`INIT …`** once at start (stdout): entry mode, limits, dry-run, funder hint.
- **`WIN …`** (stdout) only in **market** mode when a real buy later resolves in your favor.
- **`[INFO] [CHEAP03_ENTRY] …`** / **`[TP99] …`** on **stderr** via logger `polymarket_btc_ladder` (level from **`BOT_LOG_LEVEL`**, default **INFO**).

## Go-live checklist

1. `cp .env.example .env` and set **`POLY_PRIVATE_KEY`**, **`POLY_FUNDER`**, relayer creds if used.
2. Set **`POLY_DRY_RUN=false`** for real orders.
3. Confirm **`BOT_STRATEGY_MODE=first_cheap_03`**, **`BOT_CHEAP03_ENTRY=dual_limits`** (or `market` for FAK-only).
4. Optional: **`BOT_TP_POLL_SECONDS`** (used with the first TP gate in the loop; dual mode also **forces** TP sync each poll after both entry limits are placed).
5. `docker compose build && docker compose up -d` from this directory.
6. `docker compose logs -f bot` — expect **`INIT`**, **`[CHEAP03_ENTRY]`** lines when limits post, **`[TP99]`** when sells at 99¢ are placed or refreshed.

## Ops / risk

- **Resolution / WIN** uses Gamma `closed` + `outcomePrices`; dual mode does not set `_pending` per fill — treat PnL in your own accounting if needed.
- **CLOB midpoints** vs backtests can differ.
- **Minimum order** rules are enforced by the venue; if a limit post fails, check logs and share count × price ≥ ~\$1 where applicable.

## Repo layout

`config`, `trader`, `market_locator`, `http_session`, `clob_fak`, `requirements.txt`, `cheap03_first_engine.py`, `main.py`.
