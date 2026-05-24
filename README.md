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

After editing `.env`: `docker compose up -d --force-recreate` (no `build` needed unless code changed).

## Schedule

Every **`BOT_LIMIT_PAIR_SEARCH_INTERVAL_SEC`** (default **300** = 5 minutes):

1. Compute **`BOT_LIMIT_PAIR_HOURS`** (default **2**) or **`BOT_LIMIT_PAIR_WINDOW_COUNT`** epochs (24 for 2h) starting at the next 5m boundary **after** `now + BOT_LIMIT_PAIR_LEAD_MINUTES` (default **15**).
2. Example: now **07:53** → first window **08:10** UTC, last window ends **10:10** (24 × 5m × 2 symbols = **48** order pairs max per full scan).
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
