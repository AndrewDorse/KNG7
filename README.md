# KNG7 — `first_cheap_03` (Docker)

**Gamma** slugs `btc-updown-5m-<epoch>` and/or `btc-updown-15m-<epoch>`. Set **`BOT_WINDOW_MINUTES`** to **`15`**, **`5`**, or **`5,15`** (comma-separated; order is lane priority in the poll loop). Same **`btc50_1c`** rules per lane: 1¢ first touch, Binance BTC move vs anchor **\< \$50**, **\$1** FAK, TP **70¢** (defaults below).

**Both in one process:** **`BOT_WINDOW_MINUTES=5,15`** (default in `.env.example`) runs **two independent lanes** in a single Python loop—separate anchors, first-cheap flags, TP sync, and pending-resolution per length. **`INIT`** prints both slugs, e.g. `lanes=5m=btc-updown-5m-…+15m=btc-updown-15m-…`. Logs use **`[BTC50] 5m`** / **`[BTC50] 15m`** where relevant. Same wallet for both lanes = **combined exposure**.

## Default behavior (`BOT_CHEAP03_ENTRY=btc50_1c`)

Pool-tuned preset: **1¢** first-touch, **Binance** spot move from window anchor **\< \$50**, **\$1** FAK buy, resting TP at **`BOT_TP_LIMIT_PX`** (default **70¢**).

1. After each slug change, the first successful **BTCUSDT** ticker read sets the **anchor** (window-open proxy).
2. On the **first** second a leg midpoint is **≤ `BOT_CHEAP03_PRICE_MAX`** (default **0.01**), if **`abs(BTC_now − anchor) < BOT_BTC_MAX_MOVE_USD`** (default **50**), submit **one** **\$1** USDC **FAK** buy on that leg. If the move gate fails on that **first** cheap touch, **no buy** for the rest of the window (matches backtest semantics).
3. **Take-profit:** **`BOT_TP_POLL_SECONDS`** between refreshes, plus **one** immediate sync after each entry — **GTC sells** at **`BOT_TP_LIMIT_PX`** per side for whole-share inventory.

## `dual_limits` (`BOT_CHEAP03_ENTRY=dual_limits`)

1. On each new window, place **GTC limit buys** on **UP** and **DOWN** at **`BOT_CHEAP03_LIMIT_PX`** × **`BOT_CHEAP03_LIMIT_SHARES`**.
2. Default TP price for dual is **99¢** unless **`BOT_TP_LIMIT_PX`** is set.

## `market` (`BOT_CHEAP03_ENTRY=market`)

Legacy: first **≤ `BOT_CHEAP03_PRICE_MAX`** (default **3¢**) touch → **\$1** FAK, no BTC gate. TP default **99¢**.

## Logs

- **`INIT …`** (stdout): lanes, entry mode, thresholds, **`poly_ws=on|off`**, dry-run, funder hint.
- **`DEAL_START …`** (stdout) on successful entry; **`WIN …`** (stdout) when a pending market resolves on Gamma.
- **Stderr:** default **`BOT_LOG_LEVEL=ERROR`** — errors only (TP routine lines are DEBUG).

## Polymarket market WebSocket

When **`BOT_POLY_WS_ENABLED=true`** (default), a background thread subscribes to the CLOB **market** channel for all active lane UP/DOWN token IDs. **`get_midpoint` / `get_best_ask` / `get_best_bid`** use WS quotes while fresh (**`BOT_POLY_WS_MAX_AGE_SEC`**), then fall back to REST **`get_order_book`**. Outbound **WSS** to **`BOT_POLY_WS_URL`** must be allowed from the host (same as browser Polymarket).

## Go-live checklist

1. `cp .env.example .env` — set **`POLY_PRIVATE_KEY`**, **`POLY_FUNDER`**, relayer vars if you use them; **`POLY_DRY_RUN=false`** for real orders.
2. **`BOT_STRATEGY_MODE=first_cheap_03`**.
3. For default strategy: **`BOT_CHEAP03_ENTRY=btc50_1c`**, **`BOT_BTC_MAX_MOVE_USD=50`**, **`BOT_TP_LIMIT_PX=0.70`**, **`BOT_CHEAP03_PRICE_MAX=0.01`**, **`BOT_WINDOW_MINUTES`**, **`BOT_MARKET_BUY_SLIPPAGE_USD=0.03`**.
4. Network: **HTTPS** `gamma-api.polymarket.com`, `clob.polymarket.com`, **`api.binance.com`**, and **WSS** `ws-subscriptions-clob.polymarket.com` (or your **`BOT_POLY_WS_URL`**). Set **`BOT_POLY_WS_ENABLED=false`** only if you intentionally want REST-only quotes.
5. `docker compose build && docker compose up -d`
6. `docker compose logs -f bot` — confirm **`INIT`** shows expected lanes and **`poly_ws=on`** (or **`off`** if WS failed; then fix TLS/firewall).

## Ops / risk

- **BTC anchor** is the first Binance read after the slug is active, not necessarily the exchange print at the exact chain second zero.
- **CLOB midpoints** vs tape backtests can differ.
- Venue **minimum notional** still applies to limit sells.

## Repo layout

`config`, `trader`, `market_locator`, `http_session`, `clob_fak`, **`polymarket_ws`**, `requirements.txt`, `cheap03_first_engine.py`, `main.py`.
