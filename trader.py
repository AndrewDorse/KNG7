#!/usr/bin/env python3
"""Polymarket CLOB API wrapper — order placement, cancellation, balances."""

from __future__ import annotations

import math
import threading
import time
from decimal import ROUND_DOWN, Decimal
from functools import wraps
from typing import Any

import requests

_CLOB_V2 = False
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
    HOST, CHAIN_ID, BUY, SELL, LOGGER,
    ActiveContract,
    BotConfig,
    TokenMarket,
    parse_balance_response,
)

_ORDER_VERSION_MISMATCH_SNIPPET = "order_version_mismatch"
_BUY_RETRY_DELAY_SECONDS = 2.0
_BUY_RETRY_ATTEMPTS = 3


def _is_order_version_mismatch_error(exc: Exception) -> bool:
    return _ORDER_VERSION_MISMATCH_SNIPPET in str(exc).lower()


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
        # For mismatch recovery, force a fresh derive/create sequence from signer.
        try:
            creds = self.client.derive_api_key()
            if creds is None:
                raise RuntimeError("derive_api_key returned None")
        except Exception:
            creds = self.client.create_api_key(int(time.time() * 1000))
            if creds is None:
                raise RuntimeError("create_api_key returned None")
        self.client.set_api_creds(creds)


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
            )

    def _place_marketable_buy_with_result_impl(
        self,
        token: TokenMarket,
        price: float,
        size: float,
        *,
        confirm_get_order: bool = True,
        fee_rate_bps: int | None = None,
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
        """FAK market buy sized in **USDC**; signed price comes from the order book (py_clob_client)."""
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
        from clob_fak import fak_buy_with_confirm

        u = float(usdc)
        if u <= 0:
            raise ValueError("usdc must be > 0")
        max_attempts = _BUY_RETRY_ATTEMPTS
        last_exc: Exception | None = None
        refreshed_creds = False
        raw: dict[str, Any] | None = None
        limit_px = 0.0
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
            limit_px = float(margs.price)
            try:
                raw = self._create_and_post_market_order(margs, options=opts)
                break
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
                        context="market_buy_usdc_with_result",
                    )
                    continue
                raise
        if raw is None:
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("market buy with result failed without response")
        est_max_sh = u / 0.01 + 10.0
        return fak_buy_with_confirm(
            self.client.get_order,
            raw,
            requested_shares=est_max_sh,
            limit_price=limit_px,
            confirm=confirm_get_order,
            requested_usdc=u,
        )

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
        create_and_post = getattr(self.client, "create_and_post_order", None)
        if callable(create_and_post):
            try:
                return create_and_post(order_args=order, options=None, order_type=OrderType.FAK, post_only=False)
            except TypeError:
                return create_and_post(order, None, OrderType.FAK)
        signed = self.client.create_order(order)
        return self.client.post_order(signed, OrderType.FAK)

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

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order by ID. Returns True on success."""
        try:
            if hasattr(self.client, "cancel_order"):
                self.client.cancel_order(OrderPayload(orderID=order_id))
            else:
                self.client.cancel(order_id)
            return True
        except Exception as exc:
            LOGGER.debug("Cancel failed %s: %s", order_id, exc)
            return False

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

    def sync_tp_limit_sells_99c(self, contract: ActiveContract, *, dry_run: bool) -> None:
        """Resting take-profit: one GTC limit sell at $0.99 per outcome token for whole-share inventory.

        Poll from the engine (~15s). If both UP and DOWN have inventory, places (or refreshes) two sells.
        Open sell size plus free conditional balance is used so shares escrowed in an existing TP still count.
        """
        try:
            open_orders = self.get_open_orders()
        except Exception as exc:
            LOGGER.warning("[TP99] %s | get_open_orders failed: %s", contract.slug, exc)
            open_orders = []
        for token in (contract.up, contract.down):
            self._sync_tp99_for_token(contract.slug, token, open_orders, dry_run=dry_run)

    def _sync_tp99_for_token(
        self,
        slug: str,
        token: TokenMarket,
        open_orders: list[dict[str, Any]],
        *,
        dry_run: bool,
    ) -> None:
        tid = token.token_id
        tp = 0.99
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
            LOGGER.warning("[TP99] %s | %s balance read failed: %s", slug, token.outcome, exc)
            return

        total = free + reserved
        want = int(math.floor(total + 1e-9))

        if want < 1:
            if not tp_orders:
                return
            if dry_run:
                LOGGER.debug("[TP99 dry_run] %s | %s | would cancel stale TP (no inventory)", slug, token.outcome)
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
                "[TP99 dry_run] %s | %s | would refresh $%.2f sell x %d (free=%.4f reserved=%.4f)",
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
            LOGGER.info(
                "[TP99] %s | %s | placed $%.2f GTC sell x %d order=%s",
                slug,
                token.outcome,
                tp,
                want2,
                oid[:16] if oid else "n/a",
            )
        except Exception as exc:
            LOGGER.warning(
                "[TP99] %s | %s | place $%.2f x %d failed: %s",
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

        Uses the **minimum** ask across the book; the CLOB does not always guarantee
        sort order, and ``[0]`` is not always the best ask. Empty book → None (SHAMAN
        then skips with ``PM_skip no_ask`` — common when the outcome has no offers yet).
        """
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
        """Get highest bid price — what we'd receive selling right now.

        Useful for assessing liquidation value of unpaired positions."""
        try:
            book = self.get_order_book(token_id)
            bids = book.get("bids") or []
            if bids:
                price = float(bids[0].get("price", 0))
                return price if price > 0 else None
            return None
        except Exception:
            return None

    def get_midpoint(self, token_id: str) -> float | None:
        """Get current midpoint price for a token from the orderbook.
        Returns None if orderbook is empty or unavailable."""
        try:
            book = self.get_order_book(token_id)
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if not bids or not asks:
                return None
            best_bid = float(bids[0].get("price", 0))
            best_ask = float(asks[0].get("price", 0))
            if best_bid <= 0 or best_ask <= 0:
                return None
            return (best_bid + best_ask) / 2.0
        except Exception:
            return None

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
