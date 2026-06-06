#!/usr/bin/env python3
"""Polymarket CLOB API wrapper — order placement, cancellation, balances."""

from __future__ import annotations

import math
import os
import threading
import time
from decimal import ROUND_DOWN, Decimal
from functools import wraps
from typing import Any

import requests

_FORCE_CLOB_V1 = os.getenv("BOT_CLOB_USE_V1", "").strip().lower() in ("1", "true", "yes")

_CLOB_V2 = False
if not _FORCE_CLOB_V1:
    try:
        from py_clob_client_v2 import (
            ApiCreds,
            AssetType,
            BalanceAllowanceParams,
            ClobClient,
            MarketOrderArgs,
            OpenOrderParams,
            OrderArgs,
            OrderPayload,
            OrderType,
            PartialCreateOrderOptions,
            Side,
        )
        _CLOB_V2 = True
    except Exception:
        pass

if not _CLOB_V2:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds,
        AssetType,
        BalanceAllowanceParams,
        MarketOrderArgs,
        OpenOrderParams,
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
    )

    Side = None
    OrderPayload = None

from config import (
    HOST,
    CHAIN_ID,
    BUY,
    SELL,
    LOGGER,
    ActiveContract,
    BotConfig,
    TokenMarket,
    parse_balance_response,
)

_ORDER_VERSION_MISMATCH_SNIPPET = "order_version_mismatch"
_BUY_RETRY_DELAY_SECONDS = 2.0
_BUY_RETRY_ATTEMPTS = 3
# FAK cap for USDC-sized buys: best ask + this (default 3¢), clamped to Polymarket max tick 0.99.
_DEFAULT_MARKET_BUY_SLIPPAGE_USD = 0.03
_CLOB_BUY_MAX_PX = 0.99


def _is_order_version_mismatch_error(exc: Exception) -> bool:
    return _ORDER_VERSION_MISMATCH_SNIPPET in str(exc).lower()


def is_deposit_wallet_flow_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "maker address not allowed" in msg or "deposit wallet flow" in msg


def is_api_key_derive_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "could not derive api key" in msg or "derive-api-key" in msg


def wallet_config_hint_for_error(exc: BaseException) -> str:
    """Human-readable fix hints for common Polymarket wallet / CLOB misconfig."""
    lines = [
        "",
        "Wallet / CLOB setup checklist:",
        "  A) Polymarket migrated many accounts to CLOB v2 'deposit wallet' (May 2026).",
        "     Symptom: maker address not allowed + derive-api-key 400, even with correct funder.",
        "     Magic/email accounts: signature_type=1 often FAILS on py_clob_client_v2 (GitHub #56).",
        "  B) Try in order:",
        "     1. BOT_CLOB_USE_V1=true  (legacy py-clob-client — same as old kng_bot3 bot)",
        "     2. POLY_SIGNATURE_TYPE=3  + funder = proxyAddress from polymarket.com/profile HTML",
        "        (View Source → search proxyAddress; NOT always the UI 'deposit' label)",
        "     3. Clear RELAYER_* env vars — use create_or_derive_api_key from POLY_PRIVATE_KEY",
        "        (dashboard 'imported API' keys often mismatch the signer)",
        "     4. Place one small order on polymarket.com UI first (deploys proxy bytecode)",
        "  C) POLY_PRIVATE_KEY = exported Magic/browser key; POLY_FUNDER = profile proxy wallet.",
        "  D. Run: python check_wallet.py",
    ]
    if is_deposit_wallet_flow_error(exc):
        lines.insert(
            1,
            ">>> CLOB rejected the maker address — funder/signature_type mismatch for this account.",
        )
    if is_api_key_derive_error(exc):
        lines.insert(1, ">>> API key derive failed — check private key + funder + RELAYER_* creds.")
    return "\n".join(lines)


def funder_has_contract_code(funder: str) -> bool | None:
    """True if POLY_FUNDER is a deployed contract (V2 deposit-wallet proxy)."""
    try:
        resp = requests.post(
            "https://polygon-rpc.com",
            json={
                "jsonrpc": "2.0",
                "method": "eth_getCode",
                "params": [funder, "latest"],
                "id": 1,
            },
            timeout=12,
        )
        resp.raise_for_status()
        code = str(resp.json().get("result") or "0x")
        return len(code) > 2
    except Exception:
        return None


def diagnose_clob_wallet(config: BotConfig) -> list[str]:
    """Non-fatal hints based on on-chain funder type and client version."""
    notes: list[str] = []
    if _FORCE_CLOB_V1:
        notes.append("CLOB client: py-clob-client v1 (BOT_CLOB_USE_V1=true)")
    elif _CLOB_V2:
        notes.append("CLOB client: py_clob_client_v2 (Polymarket CLOB v2 API)")
    else:
        notes.append("CLOB client: py-clob-client v1 (v2 not installed)")

    if config.relayer_api_key:
        notes.append(
            "RELAYER_API_KEY is set — if orders fail, try clearing RELAYER_* and "
            "let the bot derive API creds from POLY_PRIVATE_KEY (dashboard keys often mismatch)."
        )

    is_contract = funder_has_contract_code(config.funder)
    if is_contract is True and config.signature_type in (0, 1, 2):
        notes.append(
            "POLY_FUNDER is a deployed smart-contract proxy on Polygon — Polymarket likely "
            "migrated this account to the V2 deposit-wallet flow. "
            "Set POLY_SIGNATURE_TYPE=3 (or BOT_CLOB_USE_V1=true to try legacy client)."
        )
    if is_contract is False and config.signature_type == 3:
        notes.append(
            "POLY_FUNDER looks like an EOA but POLY_SIGNATURE_TYPE=3 — "
            "use proxyAddress from polymarket.com/profile as funder."
        )
    if _CLOB_V2 and config.signature_type == 1:
        notes.append(
            "Magic/proxy (signature_type=1) + py_clob_client_v2 is a known broken combo "
            "(Polymarket/py-clob-client-v2#56). Try BOT_CLOB_USE_V1=true first."
        )
    return notes


def _normalized_tick_size(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    return s if s in {"0.1", "0.01", "0.001", "0.0001"} else None


def _clob_taker_size_shares(size: float) -> float:
    """Polymarket CLOB: taker (outcome share) size — max 4 decimal places, no float noise."""
    if size <= 0:
        return 0.0
    q = Decimal(str(float(size))).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    return float(f"{float(q):.4f}")


def _float_field(x: Any) -> float:
    try:
        if x is None or x == "":
            return 0.0
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _open_order_token_id(o: dict[str, Any]) -> str:
    return str(
        o.get("asset_id")
        or o.get("assetId")
        or o.get("token_id")
        or o.get("tokenId")
        or ""
    )


def _open_order_remaining_shares(o: dict[str, Any]) -> float:
    """Resting size on an open order (handles micro-unit fixed-point from the CLOB)."""
    from clob_fak import _decode_fixed_size

    raw_size = o.get("size")
    if raw_size is not None and raw_size != "":
        v = _float_field(raw_size)
        if v > 1_000_000:
            return max(0.0, _decode_fixed_size(raw_size))
        return max(0.0, v)
    orig = o.get("original_size") or o.get("originalSize")
    matched = o.get("size_matched") or o.get("sizeMatched") or 0
    if orig is not None and orig != "":
        o_raw = _float_field(orig)
        o_sz = _decode_fixed_size(orig) if o_raw > 1_000_000 else o_raw
        m_raw = _float_field(matched)
        m_sz = (
            _decode_fixed_size(matched)
            if matched not in (None, "") and m_raw > 1_000_000
            else m_raw
        )
        return max(0.0, o_sz - m_sz)
    return 0.0


def _open_order_side_upper(o: dict[str, Any]) -> str:
    return str(o.get("side") or "").upper()


def _open_order_price(o: dict[str, Any]) -> float:
    return _float_field(o.get("price"))


def _retry(max_attempts=2, backoff_base=0.5, retryable=(requests.RequestException,)):
    """Decorator: retry on transient network errors with exponential backoff."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except retryable as exc:
                    last_exc = exc
                    if attempt < max_attempts:
                        wait = backoff_base * (2 ** (attempt - 1))
                        LOGGER.debug(
                            "Retry %d/%d for %s after %.1fs: %s",
                            attempt, max_attempts, fn.__name__, wait, exc,
                        )
                        time.sleep(wait)
            raise last_exc
        return wrapper
    return decorator


class PolymarketTrader:
    """Handles all CLOB API interactions: credentials, orders, balances."""

    def __init__(self, config: BotConfig):
        self.config = config
        self._buy_side = Side.BUY if _CLOB_V2 and Side is not None else BUY
        self._sell_side = Side.SELL if _CLOB_V2 and Side is not None else SELL

        self.client = self._new_client()
        self._set_client_api_creds(prefer_env=True)
        LOGGER.info(
            "API credentials set for funder: %s (CLOB client: %s)",
            config.funder,
            "v2" if _CLOB_V2 else "v1",
        )

        # One taker pipeline at a time: FAK POST + confirm + retries must finish (or raise)
        # before another marketable order is sent — avoids overlap when the CLOB is slow.
        self._taker_order_lock = threading.Lock()

        # --- Token spending allowances (required before first trade) ---
        self._setup_allowances()

        self._ws_feed: Any = None
        if self.config.polymarket_ws_enabled:
            try:
                from polymarket_ws import MarketWsFeed

                self._ws_feed = MarketWsFeed(self.config.polymarket_ws_url)
                self._ws_feed.start()
            except Exception as exc:
                LOGGER.error(
                    "Polymarket market WS disabled (start failed); using REST order book: %s",
                    exc,
                )
                self._ws_feed = None

    @property
    def ws_quotes_active(self) -> bool:
        return self._ws_feed is not None

    def sync_ws_subscriptions(self, contracts: list[ActiveContract | None]) -> None:
        """Point the market WebSocket at all UP/DOWN token IDs for active lanes (deduped)."""
        if self._ws_feed is None:
            return
        ids: list[str] = []
        seen: set[str] = set()
        for c in contracts:
            if c is None:
                continue
            for tok in (c.up, c.down):
                tid = str(tok.token_id or "")
                if tid and tid not in seen:
                    seen.add(tid)
                    ids.append(tid)
        if not ids:
            return
        self._ws_feed.set_assets(sorted(ids))

    def _ws_bid_ask_mid(self, token_id: str) -> tuple[float, float, float] | None:
        if self._ws_feed is None:
            return None
        row = self._ws_feed.best_bid_ask_for(
            token_id, max_age_sec=float(self.config.polymarket_ws_max_age_seconds)
        )
        if row is None:
            return None
        bid, ask = row
        if bid <= 0 or ask <= 0:
            return None
        return bid, ask, (bid + ask) / 2.0

    def get_ws_midpoint(self, token_id: str) -> float | None:
        """Midpoint from the market WebSocket only; returns None until a fresh quote exists."""
        ws = self._ws_bid_ask_mid(token_id)
        return ws[2] if ws is not None else None

    def _rest_midpoint_clob(self, token_id: str) -> float | None:
        """CLOB ``/midpoint`` (works when one side of the book is empty)."""
        try:
            raw = self.client.get_midpoint(token_id)
        except Exception:
            return None
        if raw is None:
            return None
        if isinstance(raw, dict):
            v = raw.get("mid")
            if v is None:
                v = raw.get("price") or raw.get("value")
            if v is None or v == "":
                return None
            try:
                out = float(v)
            except (TypeError, ValueError):
                return None
            return out if out > 0 else None
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                return None
            try:
                out = float(s)
            except ValueError:
                return None
            return out if out > 0 else None
        try:
            out = float(raw)
        except (TypeError, ValueError):
            return None
        return out if out > 0 else None

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------
    def _new_client(self) -> ClobClient:
        return ClobClient(
            HOST,
            chain_id=CHAIN_ID,
            key=self.config.private_key,
            signature_type=self.config.signature_type,
            funder=self.config.funder,
        )

    def _set_client_api_creds(self, *, prefer_env: bool) -> None:
        if prefer_env and self.config.relayer_api_key:
            creds = ApiCreds(
                api_key=self.config.relayer_api_key,
                api_secret=self.config.relayer_secret or "",
                api_passphrase=self.config.relayer_passphrase or "",
            )
            self.client.set_api_creds(creds)
            return
        create_or_derive = getattr(self.client, "create_or_derive_api_key", None)
        if callable(create_or_derive):
            try:
                creds = create_or_derive()
                if creds is None:
                    raise RuntimeError("create_or_derive_api_key returned None")
                self.client.set_api_creds(creds)
                return
            except Exception as exc:
                LOGGER.error("create_or_derive_api_key failed: %s", exc)
                raise RuntimeError(f"CLOB API credentials failed: {exc}") from exc
        try:
            creds = self.client.derive_api_key()
            if creds is None:
                raise RuntimeError("derive_api_key returned None")
        except Exception as exc:
            LOGGER.error("derive_api_key failed: %s", exc)
            try:
                creds = self.client.create_api_key(int(time.time() * 1000))
            except Exception as create_exc:
                LOGGER.error("create_api_key failed: %s", create_exc)
                raise RuntimeError(
                    f"CLOB API credentials failed (derive and create). {create_exc}"
                ) from create_exc
            if creds is None:
                raise RuntimeError("create_api_key returned None")
        self.client.set_api_creds(creds)

    def signer_eoa_address(self) -> str | None:
        """Address derived from POLY_PRIVATE_KEY (order signer for proxy/Safe flows)."""
        try:
            addr = self.client.get_address()
            return str(addr) if addr else None
        except Exception:
            if _CLOB_V2:
                try:
                    from py_clob_client_v2.signer import Signer

                    return Signer(self.config.private_key, CHAIN_ID).address()
                except Exception:
                    return None
            return None

    def wallet_setup_summary(self) -> dict[str, Any]:
        bal = 0.0
        try:
            bal = self.wallet_balance_usdc()
        except Exception:
            pass
        return {
            "eoa": self.signer_eoa_address(),
            "funder": self.config.funder,
            "signature_type": self.config.signature_type,
            "balance_usdc": bal,
            "relayer_api_key": bool(self.config.relayer_api_key),
            "clob_v2": _CLOB_V2,
            "clob_v1_forced": _FORCE_CLOB_V1,
            "funder_is_contract": funder_has_contract_code(self.config.funder),
        }

    def validate_wallet_config(self) -> tuple[bool, str]:
        """Pre-flight checks before placing maker orders."""
        summary = self.wallet_setup_summary()
        eoa = (summary.get("eoa") or "").strip().lower()
        funder = (summary.get("funder") or "").strip().lower()
        sig = int(summary.get("signature_type") or 0)

        if not eoa or not funder:
            return False, "Missing EOA (from private key) or POLY_FUNDER"

        if sig in (1, 2) and eoa == funder:
            return (
                False,
                "POLY_FUNDER equals signer EOA but POLY_SIGNATURE_TYPE is "
                f"{sig} (proxy/Safe). Set POLY_FUNDER to your Polymarket profile "
                "deposit address (polymarket.com → Profile / Deposit), not MetaMask EOA.",
            )
        if sig == 0 and _CLOB_V2 and not _FORCE_CLOB_V1:
            return (
                False,
                "POLY_SIGNATURE_TYPE=0 (raw EOA) is rejected on CLOB v2 for most accounts. "
                "Try BOT_CLOB_USE_V1=true, or POLY_SIGNATURE_TYPE=3 with profile proxyAddress.",
            )
        if (
            summary.get("funder_is_contract") is True
            and sig in (0, 1, 2)
            and _CLOB_V2
            and not _FORCE_CLOB_V1
        ):
            return (
                False,
                "Account uses a V2 deposit-wallet proxy (contract at POLY_FUNDER) but "
                f"POLY_SIGNATURE_TYPE={sig}. Set POLY_SIGNATURE_TYPE=3 or BOT_CLOB_USE_V1=true.",
            )
        if (
            _CLOB_V2
            and not _FORCE_CLOB_V1
            and sig == 1
            and summary.get("balance_usdc", 0) <= 0
        ):
            return (
                False,
                "Magic/proxy wallet (type 1) + CLOB v2 client: balance reads $0 and orders "
                "are often rejected. Try BOT_CLOB_USE_V1=true (legacy client like old bot).",
            )
        if summary.get("balance_usdc", 0) <= 0:
            return (
                False,
                f"CLOB balance is $0 for funder={summary.get('funder')} "
                f"(signature_type={sig}). Check POLY_FUNDER and deposit USDC on Polymarket.",
            )
        return True, (
            f"eoa={summary.get('eoa')} funder={summary.get('funder')} "
            f"balance=${summary.get('balance_usdc', 0):.2f} signature_type={sig}"
        )

    def verify_clob_ready(self) -> tuple[bool, str]:
        """Lightweight live check: API creds + collateral balance readable."""
        ok, msg = self.validate_wallet_config()
        if ok:
            return True, msg
        try:
            bal = self.wallet_balance_usdc()
            return (
                False,
                f"{msg} (balance_read=${bal:.2f})",
            )
        except Exception as exc:
            return False, f"{msg}; balance read error: {exc}"


    def _setup_allowances(self) -> None:
        """Sync USDC collateral allowance with the CLOB (newer SDK) or legacy on-chain setup."""
        if hasattr(self.client, "set_allowances") and not _CLOB_V2:
            try:
                self.client.set_allowances(signature_type=self.config.signature_type)
                LOGGER.info("Allowances set successfully (set_allowances)")
                return
            except Exception as exc:
                LOGGER.warning("set_allowances failed, trying API sync: %s", exc)
        try:
            self.client.update_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=self.config.signature_type,
                )
            )
            LOGGER.info("Collateral balance/allowance synced (update_balance_allowance)")
        except Exception as exc:
            LOGGER.warning("Allowance sync issue (may already be set on-chain): %s", exc)

    def _refresh_api_creds(self) -> None:
        """Refresh L2 API credentials from wallet signer (used on version/auth drift errors)."""
        self.client = self._new_client()
        self._set_client_api_creds(prefer_env=False)
        LOGGER.info("Rebuilt client and refreshed API credentials for funder: %s", self.config.funder)
        try:
            self._setup_allowances()
        except Exception as exc:
            LOGGER.warning("Allowance re-sync after API refresh failed: %s", exc)

    def _market_order_options_for_token(self, token: TokenMarket) -> PartialCreateOrderOptions | None:
        """
        Build market-order options required by newer Polymarket CLOB versions.
        Prefer authoritative CLOB metadata for this token (tick_size/neg_risk),
        fallback to Gamma snapshot fields only if CLOB metadata calls fail.
        """
        tick: str | None = None
        neg: bool | None = None
        try:
            tick = _normalized_tick_size(self.client.get_tick_size(token.token_id))
        except Exception:
            tick = _normalized_tick_size(getattr(token, "minimum_tick_size", None))
        try:
            neg = bool(self.client.get_neg_risk(token.token_id))
        except Exception:
            neg = getattr(token, "neg_risk", None)
        # Both fields are optional in SDK options. If both unavailable, omit options entirely.
        if tick is None and neg is None:
            LOGGER.warning(
                "Missing tick_size/neg_risk for token %s; market order options omitted",
                token.token_id[:20] + "…",
            )
            return None
        return PartialCreateOrderOptions(
            tick_size=tick,
            neg_risk=bool(neg) if neg is not None else None,
        )

    def _create_and_post_market_order(
        self, margs: MarketOrderArgs, options: PartialCreateOrderOptions | None
    ) -> dict[str, Any]:
        """
        Prefer SDK's atomic create+post method when present; fallback to legacy two-step.
        """
        create_and_post = getattr(self.client, "create_and_post_market_order", None)
        if callable(create_and_post):
            try:
                return create_and_post(margs, options=options, order_type=(margs.order_type or OrderType.FOK))
            except TypeError:
                return create_and_post(margs, options=options)
        signed = self.client.create_market_order(margs, options=options)
        return self.client.post_order(signed, margs.order_type or OrderType.FOK)

    def _sleep_before_buy_retry(
        self,
        *,
        attempt: int,
        token: TokenMarket,
        amount_hint: float | None,
        reason: Exception,
        context: str,
    ) -> None:
        if attempt >= _BUY_RETRY_ATTEMPTS:
            return
        LOGGER.warning(
            "Buy order error (%s) token=%s amount=%.4f; retrying %d/%d in %.1fs: %s",
            context,
            token.token_id[:20] + "…",
            float(amount_hint or 0.0),
            attempt + 1,
            _BUY_RETRY_ATTEMPTS,
            _BUY_RETRY_DELAY_SECONDS,
            reason,
        )
        time.sleep(_BUY_RETRY_DELAY_SECONDS)

    # ------------------------------------------------------------------
    # Balance checks
    # ------------------------------------------------------------------

    def wallet_balance_usdc(self) -> float:
        """Return available USDC balance as a float."""
        try:
            resp = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=self.config.signature_type,
                )
            )
            return parse_balance_response(resp)
        except Exception as e:
            LOGGER.debug("Balance fetch error: %s", e)
            return 0.0

    def token_balance(self, token_id: str) -> float:
        """Return how many shares of a specific conditional token we hold."""
        try:
            resp = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                    signature_type=self.config.signature_type,
                )
            )
            return parse_balance_response(resp)
        except Exception:
            return 0.0

    def token_balance_allowance_refreshed(self, token_id: str) -> float:
        """Sync conditional allowance with CLOB then read balance (API can lag; use for reconciliation)."""
        try:
            self.client.update_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                    signature_type=self.config.signature_type,
                )
            )
        except Exception as exc:
            LOGGER.debug("update_balance_allowance conditional %s: %s", token_id[:16], exc)
        return self.token_balance(token_id)

    def has_sufficient_balance(self, required_usdc: float) -> bool:
        """Check if wallet has enough USDC for planned orders.
        Returns True if balance >= required, else logs warning and returns False."""
        balance = self.wallet_balance_usdc()
        if balance < required_usdc:
            LOGGER.warning(
                "Insufficient balance: have $%.2f, need $%.2f",
                balance, required_usdc,
            )
            return False
        LOGGER.debug("Balance OK: have $%.2f, need $%.2f", balance, required_usdc)
        return True

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_limit_buy(
        self,
        token: TokenMarket,
        price: float,
        size: int,
        *,
        fee_rate_bps: int | None = None,
        post_only: bool = False,
    ) -> dict[str, Any]:
        """Place a GTC limit buy order. Returns the CLOB response dict.

        Args:
            token: TokenMarket with .token_id
            price: limit price (0.01–0.99)
            size:  number of shares (integer)

        Order submissions are intentionally not retried automatically. If the network drops
        after POST, the exchange may still have accepted the order; retrying can double-fill.
        """
        with self._taker_order_lock:
            order_kwargs: dict[str, Any] = {
                "token_id": token.token_id,
                "price": round(price, 2),
                "size": float(size),
                "side": self._buy_side,
            }
            if fee_rate_bps is not None and not _CLOB_V2:
                order_kwargs["fee_rate_bps"] = fee_rate_bps
            order = OrderArgs(**order_kwargs)
            create_and_post = getattr(self.client, "create_and_post_order", None)
            if callable(create_and_post):
                try:
                    return create_and_post(order_args=order, options=None, order_type=OrderType.GTC, post_only=post_only)
                except TypeError:
                    return create_and_post(order, None, OrderType.GTC)
            signed = self.client.create_order(order)
            return self.client.post_order(signed, OrderType.GTC, post_only=post_only)

    def place_marketable_buy(
        self,
        token: TokenMarket,
        price: float,
        size: float,
        *,
        fee_rate_bps: int | None = None,
    ) -> dict[str, Any]:
        """Place an aggressive buy intended to fill immediately."""
        with self._taker_order_lock:
            return self._place_marketable_buy_impl(
                token, price, size, fee_rate_bps=fee_rate_bps
            )

    def _place_marketable_buy_impl(
        self,
        token: TokenMarket,
        price: float,
        size: float,
        *,
        fee_rate_bps: int | None = None,
    ) -> dict[str, Any]:
        sz = _clob_taker_size_shares(size)
        last_exc: Exception | None = None
        refreshed_creds = False
        for attempt in range(1, _BUY_RETRY_ATTEMPTS + 1):
            order_kwargs: dict[str, Any] = {
                "token_id": token.token_id,
                "price": round(price, 2),
                "size": sz,
                "side": self._buy_side,
            }
            if fee_rate_bps is not None and not _CLOB_V2:
                order_kwargs["fee_rate_bps"] = fee_rate_bps
            order = OrderArgs(**order_kwargs)
            create_and_post = getattr(self.client, "create_and_post_order", None)
            try:
                if callable(create_and_post):
                    try:
                        return create_and_post(order_args=order, options=None, order_type=OrderType.FAK)
                    except TypeError:
                        return create_and_post(order, None, OrderType.FAK)
                signed = self.client.create_order(order)
                return self.client.post_order(signed, OrderType.FAK)
            except Exception as exc:
                last_exc = exc
                if _is_order_version_mismatch_error(exc) and not refreshed_creds:
                    try:
                        self._refresh_api_creds()
                        refreshed_creds = True
                    except Exception as cred_exc:
                        LOGGER.warning("API cred refresh failed after buy mismatch: %s", cred_exc)
                self._sleep_before_buy_retry(
                    attempt=attempt,
                    token=token,
                    amount_hint=float(sz),
                    reason=exc,
                    context="marketable_buy",
                )
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("marketable buy failed without exception")

    def place_marketable_buy_with_result(
        self,
        token: TokenMarket,
        price: float,
        size: float,
        *,
        confirm_get_order: bool = True,
        fee_rate_bps: int | None = None,
        requested_usdc: float | None = None,
    ) -> Any:
        """Submit one FAK buy; parse POST body and optionally confirm fill via GET /order.

        This path is intentionally single-shot. Taker-order POST retries can create duplicate
        fills when the first POST succeeds but the client loses the response.
        """
        with self._taker_order_lock:
            return self._place_marketable_buy_with_result_impl(
                token,
                price,
                size,
                confirm_get_order=confirm_get_order,
                fee_rate_bps=fee_rate_bps,
                requested_usdc=requested_usdc,
            )

    def _place_marketable_buy_with_result_impl(
        self,
        token: TokenMarket,
        price: float,
        size: float,
        *,
        confirm_get_order: bool = True,
        fee_rate_bps: int | None = None,
        requested_usdc: float | None = None,
    ) -> Any:
        from clob_fak import fak_buy_with_confirm

        sz = _clob_taker_size_shares(size)
        last_exc: Exception | None = None
        refreshed_creds = False
        raw: dict[str, Any] | None = None
        for attempt in range(1, _BUY_RETRY_ATTEMPTS + 1):
            order_kwargs: dict[str, Any] = {
                "token_id": token.token_id,
                "price": round(price, 2),
                "size": sz,
                "side": self._buy_side,
            }
            if fee_rate_bps is not None and not _CLOB_V2:
                order_kwargs["fee_rate_bps"] = fee_rate_bps
            order = OrderArgs(**order_kwargs)
            create_and_post = getattr(self.client, "create_and_post_order", None)
            try:
                if callable(create_and_post):
                    try:
                        raw = create_and_post(order_args=order, options=None, order_type=OrderType.FAK)
                    except TypeError:
                        raw = create_and_post(order, None, OrderType.FAK)
                else:
                    signed = self.client.create_order(order)
                    raw = self.client.post_order(signed, OrderType.FAK)
                break
            except Exception as exc:
                last_exc = exc
                if _is_order_version_mismatch_error(exc) and not refreshed_creds:
                    try:
                        self._refresh_api_creds()
                        refreshed_creds = True
                    except Exception as cred_exc:
                        LOGGER.warning("API cred refresh failed after buy mismatch: %s", cred_exc)
                self._sleep_before_buy_retry(
                    attempt=attempt,
                    token=token,
                    amount_hint=float(sz),
                    reason=exc,
                    context="marketable_buy_with_result",
                )
        if raw is None:
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("marketable buy with result failed without response")
        return fak_buy_with_confirm(
            self.client.get_order,
            raw,
            requested_shares=float(sz),
            limit_price=float(price),
            confirm=confirm_get_order,
            requested_usdc=requested_usdc,
        )

    def place_market_buy_usdc(
        self,
        token: TokenMarket,
        usdc: float,
        *,
        fee_rate_bps: int | None = None,
    ) -> dict[str, Any]:
        """Buy at the current book (``create_market_order``): spend ``usdc`` USDC, not a share count."""
        with self._taker_order_lock:
            return self._place_market_buy_usdc_impl(token, usdc, fee_rate_bps=fee_rate_bps)

    def _place_market_buy_usdc_impl(
        self,
        token: TokenMarket,
        usdc: float,
        *,
        fee_rate_bps: int | None = None,
    ) -> dict[str, Any]:
        u = float(usdc)
        if u <= 0:
            raise ValueError("usdc must be > 0")
        max_attempts = _BUY_RETRY_ATTEMPTS
        last_exc: Exception | None = None
        refreshed_creds = False
        for attempt in range(1, max_attempts + 1):
            opts = self._market_order_options_for_token(token)
            margs = MarketOrderArgs(
                token_id=token.token_id,
                amount=u,
                side=self._buy_side,
                price=0.0,
                order_type=OrderType.FAK,
            )
            if fee_rate_bps is not None and hasattr(margs, "fee_rate_bps"):
                margs.fee_rate_bps = fee_rate_bps
            try:
                return self._create_and_post_market_order(margs, options=opts)
            except Exception as exc:
                last_exc = exc
                if _is_order_version_mismatch_error(exc) and not refreshed_creds:
                    try:
                        self._refresh_api_creds()
                        refreshed_creds = True
                    except Exception as cred_exc:
                        LOGGER.warning("API cred refresh failed after order_version_mismatch: %s", cred_exc)
                if attempt < max_attempts:
                    self._sleep_before_buy_retry(
                        attempt=attempt,
                        token=token,
                        amount_hint=u,
                        reason=exc,
                        context="market_buy_usdc",
                    )
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("market buy failed without exception")

    def place_market_buy_usdc_with_result(
        self,
        token: TokenMarket,
        usdc: float,
        *,
        confirm_get_order: bool = True,
        fee_rate_bps: int | None = None,
    ) -> Any:
        """FAK buy for ``usdc`` budget: limit = **best ask + slippage** (default +3¢), capped at 0.99.

        Uses an explicit FAK limit (not ``create_market_order``) so the crossing price is
        deterministic. Override cents with ``BOT_MARKET_BUY_SLIPPAGE_USD`` (e.g. ``0.03``).
        """
        with self._taker_order_lock:
            return self._place_market_buy_usdc_with_result_impl(
                token,
                usdc,
                confirm_get_order=confirm_get_order,
                fee_rate_bps=fee_rate_bps,
            )

    def _place_market_buy_usdc_with_result_impl(
        self,
        token: TokenMarket,
        usdc: float,
        *,
        confirm_get_order: bool = True,
        fee_rate_bps: int | None = None,
    ) -> Any:
        from clob_fak import FakBuyResult

        u = float(usdc)
        if u <= 0:
            raise ValueError("usdc must be > 0")
        slip_raw = os.getenv("BOT_MARKET_BUY_SLIPPAGE_USD")
        try:
            slip = float(slip_raw) if slip_raw not in (None, "") else _DEFAULT_MARKET_BUY_SLIPPAGE_USD
        except (TypeError, ValueError):
            slip = _DEFAULT_MARKET_BUY_SLIPPAGE_USD
        if not math.isfinite(slip) or slip < 0:
            slip = _DEFAULT_MARKET_BUY_SLIPPAGE_USD
        ask = self.get_best_ask(token.token_id)
        if ask is None or ask <= 0.0:
            raise RuntimeError("market_buy_usdc: no_best_ask_empty_book")
        ask_f = float(ask)
        if not math.isfinite(ask_f):
            raise RuntimeError("market_buy_usdc: invalid_ask")
        ask_r = round(ask_f, 2)
        target = round(ask_f + float(slip), 2)
        # At least the visible ask (2dp), plus slippage when room below 0.99.
        limit_px = min(_CLOB_BUY_MAX_PX, max(ask_r, target))
        if limit_px <= 0.0:
            raise RuntimeError("market_buy_usdc: computed_limit_non_positive")
        # Size chosen so nominal cap at limit_px does not exceed budget (FAK fill economics still capped in clob_fak).
        size_hint = u / limit_px
        sz = _clob_taker_size_shares(size_hint)
        if sz <= 0.0:
            raise RuntimeError("market_buy_usdc: share_size_zero_after_rounding")
        res = self._place_marketable_buy_with_result_impl(
            token,
            limit_px,
            sz,
            confirm_get_order=confirm_get_order,
            fee_rate_bps=fee_rate_bps,
            requested_usdc=u,
        )
        if isinstance(res, FakBuyResult) and not res.matched_any:
            raise RuntimeError(res.error or "market_buy_usdc_no_fill")
        return res

    def place_limit_sell(
        self, token: TokenMarket, price: float, size: int
    ) -> dict[str, Any]:
        """Place a GTC limit sell order (for exiting held positions).

        Args:
            token: TokenMarket with .token_id
            price: limit price (0.01–0.99)
            size:  number of shares (integer)
        """
        order = OrderArgs(
            token_id=token.token_id,
            price=round(price, 2),
            size=float(size),
            side=self._sell_side,
        )
        create_and_post = getattr(self.client, "create_and_post_order", None)
        if callable(create_and_post):
            try:
                return create_and_post(order_args=order, options=None, order_type=OrderType.GTC, post_only=False)
            except TypeError:
                return create_and_post(order, None, OrderType.GTC)
        signed = self.client.create_order(order)
        return self.client.post_order(signed, OrderType.GTC)

    def place_marketable_sell(
        self, token: TokenMarket, price: float, size: float
    ) -> dict[str, Any]:
        """Place an aggressive sell intended to fill immediately.

        Uses FAK so small cleanup leftovers do not remain resting on the book.
        """
        with self._taker_order_lock:
            return self._place_marketable_sell_impl(token, price, size)

    def _place_marketable_sell_impl(
        self, token: TokenMarket, price: float, size: float
    ) -> dict[str, Any]:
        order = OrderArgs(
            token_id=token.token_id,
            price=round(price, 2),
            size=_clob_taker_size_shares(size),
            side=self._sell_side,
        )
        opts = self._market_order_options_for_token(token)
        create_and_post = getattr(self.client, "create_and_post_order", None)
        if callable(create_and_post):
            try:
                return create_and_post(
                    order_args=order,
                    options=opts,
                    order_type=OrderType.FAK,
                    post_only=False,
                )
            except TypeError:
                return create_and_post(order, opts, OrderType.FAK)
        signed = self.client.create_order(order, options=opts)
        return self.client.post_order(signed, OrderType.FAK)

    def flatten_conditional_at_price(
        self,
        token: TokenMarket,
        price: float,
        *,
        max_rounds: int = 8,
        position_eps: float = 0.01,
        pause_sec: float = 0.35,
    ) -> tuple[bool, float]:
        """FAK-sell until ``token`` balance is below ``position_eps`` or rounds exhausted."""
        last_pos = 0.0
        for _ in range(max(1, int(max_rounds))):
            last_pos = self.token_balance_allowance_refreshed(token.token_id)
            if last_pos < position_eps:
                return True, last_pos
            self.place_marketable_sell(token, price, last_pos)
            time.sleep(max(0.1, float(pause_sec)))
        last_pos = self.token_balance_allowance_refreshed(token.token_id)
        return last_pos < position_eps, last_pos

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def get_open_orders(self) -> list[dict[str, Any]]:
        """Fetch all currently open orders for this account."""
        try:
            if hasattr(self.client, "get_open_orders"):
                return self.client.get_open_orders(OpenOrderParams())
            return self.client.get_orders(OpenOrderParams())
        except Exception:
            return []

    def resting_buy_shares_on_token(
        self,
        token_id: str,
        open_orders: list[dict[str, Any]] | None = None,
    ) -> float:
        """Sum all resting BUY size on ``token_id`` (any limit price)."""
        try:
            raw = open_orders if open_orders is not None else self.get_open_orders()
        except Exception:
            return 0.0
        total = 0.0
        for o in raw:
            if _open_order_token_id(o) != token_id:
                continue
            if _open_order_side_upper(o) != BUY:
                continue
            total += _open_order_remaining_shares(o)
        return total

    def resting_buy_shares_near(
        self,
        token_id: str,
        price: float,
        *,
        tol: float = 0.01,
        open_orders: list[dict[str, Any]] | None = None,
    ) -> float:
        """Sum resting BUY size on ``token_id`` at limit prices within ``tol`` of ``price``."""
        try:
            raw = open_orders if open_orders is not None else self.get_open_orders()
        except Exception:
            return 0.0
        total = 0.0
        px_target = float(price)
        for o in raw:
            if _open_order_token_id(o) != token_id:
                continue
            if _open_order_side_upper(o) != BUY:
                continue
            if abs(_open_order_price(o) - px_target) > tol:
                continue
            total += _open_order_remaining_shares(o)
        return total

    def has_sufficient_resting_buy(
        self,
        token_id: str,
        price: float,
        size: float,
        *,
        tol: float = 0.01,
        open_orders: list[dict[str, Any]] | None = None,
    ) -> bool:
        want = float(size)
        if want <= 0:
            return True
        return self.resting_buy_shares_near(
            token_id, price, tol=tol, open_orders=open_orders
        ) >= want - 1e-6

    def has_open_limit_buy_near(
        self, token_id: str, price: float, *, tol: float = 0.01
    ) -> bool:
        """True if a resting BUY exists on ``token_id`` with limit price within ``tol`` of ``price``."""
        return self.resting_buy_shares_near(token_id, price, tol=tol) > 1e-6

    def cancel_excess_limit_buys(
        self,
        token_id: str,
        price: float,
        max_shares: float,
        *,
        tol: float = 0.01,
        open_orders: list[dict[str, Any]] | None = None,
    ) -> int:
        """Cancel BUY orders at ``price`` until total resting size is <= ``max_shares``."""
        try:
            raw = open_orders if open_orders is not None else self.get_open_orders()
        except Exception:
            return 0
        px_target = float(price)
        cap = float(max_shares)
        matches: list[tuple[str, float]] = []
        for o in raw:
            if _open_order_token_id(o) != token_id:
                continue
            if _open_order_side_upper(o) != BUY:
                continue
            if abs(_open_order_price(o) - px_target) > tol:
                continue
            rem = _open_order_remaining_shares(o)
            if rem <= 1e-6:
                continue
            oid = str(o.get("id") or o.get("orderID") or "")
            if oid:
                matches.append((oid, rem))
        total = sum(rem for _, rem in matches)
        if total <= cap + 1e-6:
            return 0
        cancelled = 0
        for oid, rem in sorted(matches, key=lambda x: x[1], reverse=True):
            if total <= cap + 1e-6:
                break
            if self.cancel_order(oid):
                total -= rem
                cancelled += 1
        return cancelled

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order by ID. Returns True on success."""
        try:
            if hasattr(self.client, "cancel_order"):
                if OrderPayload is not None:
                    self.client.cancel_order(OrderPayload(orderID=order_id))
                else:
                    self.client.cancel_order(order_id)
            else:
                self.client.cancel(order_id)
            return True
        except Exception as exc:
            LOGGER.error("Cancel failed %s: %s", order_id, exc)
            return False

    def _order_status_upper(self, order_id: str) -> str:
        try:
            o = self.get_order(order_id)
            return str(
                o.get("status") or o.get("order_status") or o.get("state") or ""
            ).upper()
        except Exception:
            return ""

    def cancel_order_confirmed(
        self,
        order_id: str,
        *,
        open_orders: list[dict[str, Any]] | None = None,
        max_wait_sec: float = 6.0,
    ) -> bool:
        """Cancel and poll until the order is gone or status is CANCELLED."""
        oid = str(order_id or "").strip()
        if not oid:
            return False
        if not self.cancel_order(oid):
            return False
        deadline = time.monotonic() + max(0.5, float(max_wait_sec))
        while time.monotonic() < deadline:
            st = self._order_status_upper(oid)
            if st in ("CANCELLED", "CANCELED"):
                return True
            try:
                raw = open_orders if open_orders is not None else self.get_open_orders()
            except Exception:
                raw = []
            open_ids = {
                str(o.get("id") or o.get("orderID") or "") for o in (raw or [])
            }
            if oid not in open_ids:
                return True
            time.sleep(0.25)
        return False

    def open_orders_for_token(
        self,
        token_id: str,
        open_orders: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Open orders with remaining size on ``token_id`` (any side/price)."""
        try:
            raw = open_orders if open_orders is not None else self.get_open_orders()
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for o in raw:
            if _open_order_token_id(o) != token_id:
                continue
            if _open_order_remaining_shares(o) <= 1e-6:
                continue
            out.append(o)
        return out

    def resting_order_shares_on_token(
        self,
        token_id: str,
        open_orders: list[dict[str, Any]] | None = None,
    ) -> float:
        """Sum remaining size on all open orders for ``token_id``."""
        return sum(
            _open_order_remaining_shares(o)
            for o in self.open_orders_for_token(token_id, open_orders=open_orders)
        )

    def cancel_token_orders_confirmed(
        self,
        token_id: str,
        open_orders: list[dict[str, Any]] | None = None,
    ) -> tuple[int, int]:
        """Cancel all open orders on ``token_id``; returns (confirmed, attempted)."""
        orders = self.open_orders_for_token(token_id, open_orders=open_orders)
        if not orders:
            return 0, 0
        confirmed = 0
        for o in orders:
            oid = str(o.get("id") or o.get("orderID") or "")
            if oid and self.cancel_order_confirmed(oid, open_orders=None):
                confirmed += 1
        return confirmed, len(orders)

    def flatten_window_contract(
        self,
        contract: ActiveContract,
        exit_price: float,
        *,
        max_sell_rounds: int = 8,
        position_eps: float = 0.01,
    ) -> dict[str, Any]:
        """
        Cancel every open order and FAK-sell every position on this window's UP/DOWN tokens.
        """
        token_ids = {contract.up.token_id, contract.down.token_id}
        cancel_attempted = 0
        cancel_confirmed = 0
        sell_attempts: list[str] = []

        open_orders = self.get_open_orders()
        for tid in token_ids:
            c_ok, c_att = self.cancel_token_orders_confirmed(
                tid, open_orders=open_orders
            )
            cancel_confirmed += c_ok
            cancel_attempted += c_att
            open_orders = self.get_open_orders()

        for token in (contract.up, contract.down):
            ok, pos_after = self.flatten_conditional_at_price(
                token,
                exit_price,
                max_rounds=max_sell_rounds,
                position_eps=position_eps,
            )
            sell_attempts.append(f"{token.outcome}:ok={ok},pos={pos_after:g}")

        open_orders = self.get_open_orders()
        up_rest = self.resting_order_shares_on_token(
            contract.up.token_id, open_orders=open_orders
        )
        down_rest = self.resting_order_shares_on_token(
            contract.down.token_id, open_orders=open_orders
        )
        up_pos = self.token_balance_allowance_refreshed(contract.up.token_id)
        down_pos = self.token_balance_allowance_refreshed(contract.down.token_id)
        flat = (
            up_rest <= 1e-6
            and down_rest <= 1e-6
            and up_pos < position_eps
            and down_pos < position_eps
        )
        return {
            "flat": flat,
            "cancel_confirmed": cancel_confirmed,
            "cancel_attempted": cancel_attempted,
            "up_rest": up_rest,
            "down_rest": down_rest,
            "up_pos": up_pos,
            "down_pos": down_pos,
            "sells": sell_attempts,
        }

    @_retry()
    def get_order(self, order_id: str) -> dict[str, Any]:
        """Fetch one order by ID."""
        return self.client.get_order(order_id)

    def cancel_all_orders(self, open_orders: list[dict[str, Any]] | None = None) -> int:
        """Cancel all open orders. If open_orders not provided, fetches them first.
        Returns count of successfully cancelled orders."""
        if open_orders is None:
            open_orders = self.get_open_orders()
        cancelled = 0
        for order in open_orders:
            oid = str(order.get("id") or order.get("orderID") or "")
            if oid and self.cancel_order(oid):
                cancelled += 1
        if cancelled:
            LOGGER.info("Cancelled %d/%d open orders", cancelled, len(open_orders))
        return cancelled

    def sync_tp_limit_sells(self, contract: ActiveContract, *, tp: float, dry_run: bool) -> None:
        """Resting take-profit: GTC limit sells at ``tp`` per outcome token for whole-share inventory.

        If both UP and DOWN have inventory, places (or refreshes) two sells.
        Open sell size plus free conditional balance is used so shares escrowed in an existing TP still count.
        """
        tp = round(float(tp), 2)
        try:
            open_orders = self.get_open_orders()
        except Exception as exc:
            LOGGER.debug("[TP] %s | get_open_orders failed: %s", contract.slug, exc)
            open_orders = []
        for token in (contract.up, contract.down):
            self._sync_tp_limit_for_token(contract.slug, token, open_orders, tp=tp, dry_run=dry_run)

    def sync_tp_limit_sells_99c(self, contract: ActiveContract, *, dry_run: bool) -> None:
        """Backward-compatible alias: TP at $0.99."""
        self.sync_tp_limit_sells(contract, tp=0.99, dry_run=dry_run)

    def _sync_tp_limit_for_token(
        self,
        slug: str,
        token: TokenMarket,
        open_orders: list[dict[str, Any]],
        *,
        tp: float,
        dry_run: bool,
    ) -> None:
        tid = token.token_id
        match_tol = 0.005
        size_tol = 0.05

        tp_orders = [
            o
            for o in open_orders
            if _open_order_token_id(o) == tid
            and _open_order_side_upper(o) == SELL
            and abs(_open_order_price(o) - tp) <= match_tol
        ]
        reserved = sum(_open_order_remaining_shares(o) for o in tp_orders)
        try:
            free = self.token_balance_allowance_refreshed(tid)
        except Exception as exc:
            LOGGER.debug("[TP] %s | %s balance read failed: %s", slug, token.outcome, exc)
            return

        total = free + reserved
        want = int(math.floor(total + 1e-9))

        if want < 1:
            if not tp_orders:
                return
            if dry_run:
                LOGGER.debug("[TP dry_run] %s | %s | would cancel stale TP (no inventory)", slug, token.outcome)
                return
            for o in tp_orders:
                oid = str(o.get("id") or o.get("orderID") or "")
                if oid:
                    self.cancel_order(oid)
            return

        if reserved > 0 and abs(reserved - float(want)) <= size_tol:
            return

        if dry_run:
            LOGGER.debug(
                "[TP dry_run] %s | %s | would refresh $%.2f sell x %d (free=%.4f reserved=%.4f)",
                slug,
                token.outcome,
                tp,
                want,
                free,
                reserved,
            )
            return

        for o in tp_orders:
            oid = str(o.get("id") or o.get("orderID") or "")
            if oid:
                self.cancel_order(oid)

        try:
            free2 = self.token_balance_allowance_refreshed(tid)
        except Exception:
            free2 = free
        want2 = int(math.floor(free2 + 1e-9))
        if want2 < 1:
            return
        try:
            resp = self.place_limit_sell(token, tp, want2)
            oid = str(resp.get("orderID") or resp.get("id") or "")
            LOGGER.debug(
                "[TP] %s | %s | placed $%.2f GTC sell x %d order=%s",
                slug,
                token.outcome,
                tp,
                want2,
                oid[:16] if oid else "n/a",
            )
        except Exception as exc:
            LOGGER.error(
                "[TP] %s | %s | place $%.2f x %d failed: %s",
                slug,
                token.outcome,
                tp,
                want2,
                exc,
            )

    # ------------------------------------------------------------------
    # Market data — live market price
    # ------------------------------------------------------------------

    @_retry()
    def get_market_price(self, token_id: str) -> float | None:
        """Get current market price for a token from the Polymarket CLOB API.

        Uses the /price endpoint which returns the actual trading price,
        not the order book ask (which can be distorted by low liquidity).

        Returns float price or None on failure."""
        try:
            resp = requests.get(
                f"{HOST}/price",
                params={"token_id": token_id, "side": "buy"},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            price = float(data.get("price", 0))
            if price > 0:
                LOGGER.debug("Market price for %s…: %.4f", token_id[:16], price)
                return price
            return None
        except Exception as exc:
            LOGGER.debug("Market price fetch failed for %s: %s", token_id[:16], exc)
            return None

    # ------------------------------------------------------------------
    # Market data — order book normalization
    # ------------------------------------------------------------------

    def _normalize_book_entries(self, entries: Any) -> list[dict[str, str]]:
        """Convert order book entries to list of {"price": str, "size": str} dicts.

        The py_clob_client library may return:
          - A list of dicts (older versions)
          - A list of objects with .price/.size attributes (newer versions)
          - None or empty

        This normalizer handles all cases so downstream code always works."""
        if not entries:
            return []

        normalized = []
        for entry in entries:
            if isinstance(entry, dict):
                normalized.append({
                    "price": str(entry.get("price", "0")),
                    "size": str(entry.get("size", "0")),
                })
            else:
                # Object with attributes (OrderSummary, etc.)
                normalized.append({
                    "price": str(getattr(entry, "price", "0")),
                    "size": str(getattr(entry, "size", "0")),
                })
        return normalized

    @_retry()
    def get_order_book(self, token_id: str) -> dict[str, Any]:
        """Get full order book for a token, normalized to dict format.

        Always returns:
            {"bids": [{"price": "0.46", "size": "100"}, ...],
             "asks": [{"price": "0.54", "size": "50"}, ...]}

        Handles both dict and object responses from py_clob_client.
        Returns empty dict on failure."""
        try:
            book = self.client.get_order_book(token_id)

            # --- Already a dict (older library versions) ---
            if isinstance(book, dict):
                result = {
                    "bids": self._normalize_book_entries(book.get("bids")),
                    "asks": self._normalize_book_entries(book.get("asks")),
                }
                LOGGER.debug(
                    "Book (dict) for %s…: %d bids, %d asks",
                    token_id[:16], len(result["bids"]), len(result["asks"]),
                )
                return result

            # --- Object with attributes (newer library versions) ---
            raw_bids = getattr(book, "bids", None) or []
            raw_asks = getattr(book, "asks", None) or []

            result = {
                "bids": self._normalize_book_entries(raw_bids),
                "asks": self._normalize_book_entries(raw_asks),
            }

            LOGGER.debug(
                "Book (obj) for %s…: %d bids, %d asks",
                token_id[:16], len(result["bids"]), len(result["asks"]),
            )
            return result

        except Exception as exc:
            LOGGER.debug("Order book fetch failed for %s: %s", token_id[:16], exc)
            return {}

    # ------------------------------------------------------------------
    # Derived market data helpers
    # ------------------------------------------------------------------

    def get_best_ask(self, token_id: str) -> float | None:
        """Get lowest ask price — what it costs to buy right now.

        Prefer Polymarket **market** WebSocket when enabled; else REST order book scan.
        """
        ws = self._ws_bid_ask_mid(token_id)
        if ws is not None:
            return ws[1]
        try:
            book = self.get_order_book(token_id)
            asks = book.get("asks") or []
            if not asks:
                return None
            best: float | None = None
            for a in asks:
                p = float(a.get("price", 0))
                if p > 0 and (best is None or p < best):
                    best = p
            return best
        except Exception:
            return None

    def get_best_bid(self, token_id: str) -> float | None:
        """Highest bid — WebSocket first when enabled; else REST book scan."""
        ws = self._ws_bid_ask_mid(token_id)
        if ws is not None:
            return ws[0]
        try:
            book = self.get_order_book(token_id)
            bids = book.get("bids") or []
            if not bids:
                return None
            best: float | None = None
            for b in bids:
                p = float(b.get("price", 0) or 0)
                if p > 0 and (best is None or p > best):
                    best = p
            return best
        except Exception:
            return None

    def get_midpoint(self, token_id: str) -> float | None:
        """Mid from best bid + best ask — WebSocket first when enabled; else REST book scan."""
        ws = self._ws_bid_ask_mid(token_id)
        if ws is not None:
            return ws[2]
        try:
            book = self.get_order_book(token_id)
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if not bids or not asks:
                return self._rest_midpoint_clob(token_id)
            best_bid: float | None = None
            for b in bids:
                p = float(b.get("price", 0) or 0)
                if p > 0 and (best_bid is None or p > best_bid):
                    best_bid = p
            best_ask: float | None = None
            for a in asks:
                p = float(a.get("price", 0) or 0)
                if p > 0 and (best_ask is None or p < best_ask):
                    best_ask = p
            if best_bid is None or best_ask is None:
                return self._rest_midpoint_clob(token_id)
            return (best_bid + best_ask) / 2.0
        except Exception:
            return self._rest_midpoint_clob(token_id)

    def get_spread(self, token_id: str) -> dict[str, float | None]:
        """Get best bid, best ask, and spread for a token.
        Useful for deciding whether cheap orders are likely to fill."""
        result: dict[str, float | None] = {
            "best_bid": None, "best_ask": None, "spread": None,
        }
        try:
            book = self.get_order_book(token_id)
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if bids:
                result["best_bid"] = float(bids[0].get("price", 0))
            if asks:
                result["best_ask"] = float(asks[0].get("price", 0))
            if result["best_bid"] and result["best_ask"]:
                result["spread"] = result["best_ask"] - result["best_bid"]
        except Exception:
            pass
        return result
