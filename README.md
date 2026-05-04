# KNG7 — `first_cheap_03` (Docker)

One **BTC 15m UP/DOWN** market at a time (Gamma slug `btc-updown-15m-<epoch>`).

## Default behavior (`BOT_CHEAP03_ENTRY=btc50_1c`)

Pool-tuned preset: **1¢** first-touch, **Binance** spot move from window anchor **\< \$50**, **\$1** FAK buy, resting TP at **`BOT_TP_LIMIT_PX`** (default **70¢**).

1. After each slug change, the first successful **BTCUSDT** ticker read sets the **anchor** (window-open proxy).
2. On the **first** second a leg midpoint is **≤ `BOT_CHEAP03_PRICE_MAX`** (default **0.01**), if **`abs(BTC_now − anchor) < BOT_BTC_MAX_MOVE_USD`** (default **50**), submit **one** **\$1** USDC **FAK** buy on that leg. If the move gate fails on that **first** cheap touch, **no buy** for the rest of the window (matches backtest semantics).
3. **Take-profit:** **`BOT_TP_POLL_SECONDS`** gate plus a **forced** TP sync every poll while the window is “armed” after a fill — **GTC sells** at **`BOT_TP_LIMIT_PX`** per side for whole-share inventory.

## `dual_limits` (`BOT_CHEAP03_ENTRY=dual_limits`)

1. On each new window, place **GTC limit buys** on **UP** and **DOWN** at **`BOT_CHEAP03_LIMIT_PX`** × **`BOT_CHEAP03_LIMIT_SHARES`**.
2. Default TP price for dual is **99¢** unless **`BOT_TP_LIMIT_PX`** is set.

## `market` (`BOT_CHEAP03_ENTRY=market`)

Legacy: first **≤ `BOT_CHEAP03_PRICE_MAX`** (default **3¢**) touch → **\$1** FAK, no BTC gate. TP default **99¢**.

## Logs

- **`INIT …`** (stdout): entry mode, thresholds, TP price, dry-run, funder hint.
- **`WIN …`** (stdout) when a market-mode buy resolves in your favor on Gamma.
- **`[BTC50] …`**, **`[CHEAP03_ENTRY] …`**, **`[TP] …`** on **stderr** (`BOT_LOG_LEVEL`, default **INFO**).

## Go-live checklist

1. `cp .env.example .env` — set keys and **`POLY_DRY_RUN=false`** when going live.
2. **`BOT_STRATEGY_MODE=first_cheap_03`**.
3. For default strategy: **`BOT_CHEAP03_ENTRY=btc50_1c`**, **`BOT_BTC_MAX_MOVE_USD=50`**, **`BOT_TP_LIMIT_PX=0.70`**, **`BOT_CHEAP03_PRICE_MAX=0.01`**.
4. Ensure outbound HTTPS to **api.binance.com** (or set **`BOT_BTC_FEED_ENABLED=false`** only if you accept disabling the gate — not recommended for `btc50_1c`).
5. `docker compose build && docker compose up -d`
6. `docker compose logs -f bot`

## Ops / risk

- **BTC anchor** is the first Binance read after the slug is active, not necessarily the exchange print at the exact chain second zero.
- **CLOB midpoints** vs tape backtests can differ.
- Venue **minimum notional** still applies to limit sells.

## Repo layout

`config`, `trader`, `market_locator`, `http_session`, `clob_fak`, `requirements.txt`, `cheap03_first_engine.py`, `main.py`.
