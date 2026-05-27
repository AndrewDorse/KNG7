# KNG7 — `limit_pair_5m` (Docker)

Places **two GTC limit buys** on each upcoming **5m** Polymarket UP/DOWN window for multiple assets:

- **Symbols:** `BTC, ETH` (`BOT_LIMIT_PAIR_SYMBOLS`)
- **UP** @ **50¢** × **10** shares  
- **DOWN** @ **49¢** × **10** shares  

Gamma slugs: `{sym}-updown-5m-<epoch>` (UTC window start).

## Config (`.env` only — no rebuild)

| Variable | Example | Meaning |
|----------|---------|---------|
| `BOT_LIMIT_PAIR_SYMBOLS` | `BTC,ETH` | Pairs to trade |
| `BOT_LIMIT_PAIR_UP_PX` | `0.50` | UP limit buy price |
| `BOT_LIMIT_PAIR_DOWN_PX` | `0.49` | DOWN limit buy price |
| `BOT_LIMIT_PAIR_SHARES` | `10` | Shares per leg |
| `NEXT_WINDOWS_SEARCH_MINUTES` | `60` | Rolling horizon for each SEARCH cycle |
| `BOT_LIMIT_PAIR_SKIP_FIRST_WINDOWS` | `3` | Skip first N future windows per symbol |

After editing `.env`: `docker compose up -d --force-recreate` (no `build` needed unless code changed).

## Schedule

Every **`BOT_LIMIT_PAIR_SEARCH_INTERVAL_SEC`** (default **300** = 5 minutes):

1. List all **not-yet-started** 5m UTC windows in the next **`NEXT_WINDOWS_SEARCH_MINUTES`** (default **60**).
2. On **first SEARCH after startup only**, skip the first **3** future windows per symbol (`BOT_LIMIT_PAIR_SKIP_FIRST_WINDOWS`) — ~15 min lead. Later 5‑min cycles use **all** windows in the 60‑min horizon.
3. **Rebuild** the work queue from that list (minus slugs already **done** in state). Re-queued slots clear stale `submitted` flags so orders can be posted again after restart.
4. Resolve each slug on Gamma; cache contracts.
5. Place **one window pair** (UP + DOWN), then wait **`BOT_LIMIT_PAIR_ORDER_SPACING_SEC`** (default **10**) before the next.

Example: now **07:53** → epochs **07:55…08:55** → after skip **08:10…08:55** × BTC,ETH.

When **both** sides have a resting buy and/or filled position (no duplicate posts), the slug is marked **done** and persisted. Logs: `SKIP_POST`, `POST`, `DONE`, `ADVANCE`, `PENDING`.

**Work queue:** closest window first. Each **10s** cycle tries the **top** slot only. If placement fails (balance, etc.), retry after 10s. Logs **`IDLE`** every 60s when the queue is empty or the wallet is blocked.

### T+15s risk cleanup (inside each window)

At **T+15s** (example: UP filled, DOWN 49¢ limit still open):

1. **`CLEANUP_TRIGGER`** — arms flatten for **this window only**.
2. Every **1s** until **T+60s** (and until flat): **cancel ALL** orders on UP+DOWN tokens, **FAK sell ALL** positions @ **1¢**.
3. **`CLEANUP_DONE`** when no orders and no positions remain.

Logs: `CLEANUP_TRIGGER`, `CLEANUP_FLATTEN`, `CLEANUP_POLL`, `CLEANUP_DONE`, `CLEANUP_ERROR`.

## Logs (stdout)

- **`INIT`** — prices, lead, window count, next UTC block, sent/confirmed counts  
- **`SEARCH`** — how many targets resolved / queued  
- **`ORDER`** — slug and prices placed (or `dry_run`)  
- **`CONFIRMED`** — both resting limits seen  

Stderr: **`BOT_LOG_LEVEL`** (default **ERROR** in `main.py`).

## Wallet / “deposit wallet flow” errors

If orders fail with **`maker address not allowed`** or **`Could not derive api key`**:

| How you log in to Polymarket | `POLY_SIGNATURE_TYPE` | `POLY_FUNDER` |
|------------------------------|----------------------|---------------|
| Email / Magic link | `1` | Profile **deposit** address (not EOA) |
| MetaMask / browser wallet | `2` | Profile **deposit** address (not MetaMask EOA) |
| New deposit-wallet accounts | `3` | Deposit wallet address |

- **`POLY_PRIVATE_KEY`** — key that controls the Polymarket account (exports from your wallet or Polymarket setup).
- **`POLY_FUNDER`** — copy from **polymarket.com → Profile / Deposit** (where your USDC balance lives). It is usually **different** from your MetaMask address.
- Optional: **`RELAYER_API_KEY`** (+ secret + passphrase) from Polymarket if auto derive fails.

Verify before live trading:

```bash
docker compose run --rm bot python check_wallet.py
```

Bot exits at startup with **`WALLET_CHECK FAIL`** if config looks wrong (when `POLY_DRY_RUN=false`).

## Go-live

1. `cp .env.example .env` — **`POLY_PRIVATE_KEY`**, **`POLY_FUNDER`**, **`POLY_DRY_RUN=false`** for live.  
2. **`BOT_STRATEGY_MODE=limit_pair_5m`**.  
3. Network: **HTTPS** `gamma-api.polymarket.com`, `clob.polymarket.com` (optional **WSS** for WS).  
4. `docker compose build && docker compose up -d`
5. `docker compose logs -f bot` — confirm **`INIT`** shows `state=/app/data/...` and periodic **`SEARCH`**.

### Docker permissions

The image **entrypoint** runs briefly as root to `chown` bind-mounted `./logs` and `./exports`, then runs the bot as **`appuser`**. Persistent state defaults to the **`bot_data`** volume at **`/app/data/limit_pair_state.json`** (writable even when host `./exports` is root-owned).

If you still see `PermissionError` on the host mount:

```bash
sudo chown -R "$(id -u):$(id -g)" ./exports ./logs
docker compose up -d --force-recreate
```

## Repo layout

`config`, `trader`, `market_locator`, `http_session`, `limit_pair_engine.py`, `main.py`. Legacy **`cheap03_first_engine.py`** is kept but not used by default.
