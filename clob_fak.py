#!/usr/bin/env python3
"""Parse Polymarket CLOB FAK (and related) order POST responses; optional GET /order confirmation."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import Any

LOGGER = logging.getLogger("polymarket_btc_ladder")


def _cap_fak_fill_to_requested(
    requested_shares: float, filled_sh: float, filled_usdc: float
) -> tuple[float, float, float]:
    """Outcome shares must never exceed what we asked the CLOB to buy (mis-parsed takingAmount, etc.)."""
    cap = float(max(0.0, float(requested_shares)))
    if filled_sh <= cap + 1e-6:
        apx = (filled_usdc / filled_sh) if filled_sh > 1e-12 else 0.0
        return filled_sh, filled_usdc, apx
    LOGGER.warning(
        "CLOB FAK: clamping filled_shares %.6f down to requested %.6f (usdc scaled)",
        filled_sh,
        cap,
    )
    scale = cap / filled_sh if filled_sh > 1e-12 else 0.0
    new_usdc = filled_usdc * scale
    apx = (new_usdc / cap) if cap > 1e-12 else 0.0
    return cap, new_usdc, apx


def _cap_fak_fill_to_requested_usdc(
    requested_usdc: float, filled_sh: float, filled_usdc: float
) -> tuple[float, float, float]:
    """When the order was sized in **USDC** (market buy), do not report more than that spent."""
    cap = float(max(0.0, float(requested_usdc)))
    if filled_usdc <= cap + 1e-6:
        apx = (filled_usdc / filled_sh) if filled_sh > 1e-12 else 0.0
        return filled_sh, filled_usdc, apx
    LOGGER.warning(
        "CLOB FAK: clamping filled_usdc $%.4f down to requested $%.4f (sh scaled)",
        filled_usdc,
        cap,
    )
    scale = cap / filled_usdc if filled_usdc > 1e-12 else 0.0
    new_sh = filled_sh * scale
    new_usdc = cap
    apx = (new_usdc / new_sh) if new_sh > 1e-12 else 0.0
    return new_sh, new_usdc, apx


@dataclass(slots=True)
class FakBuyResult:
    ok: bool
    order_id: str
    status: str
    requested_shares: float
    filled_shares: float
    filled_usdc: float
    avg_price: float
    error: str
    raw: dict[str, Any]

    @property
    def matched_any(self) -> bool:
        return self.ok and self.filled_shares > 1e-9


def _f(x: Any) -> float:
    try:
        if x is None or x == "":
            return 0.0
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _decode_fixed_size(raw: Any) -> float:
    """OpenOrder sizes are fixed-point with 6 decimals (see CLOB OpenAPI)."""
    v = _f(raw)
    if v <= 0:
        return 0.0
    return v / 1_000_000.0


def _open_order_buy_economics(od: dict) -> tuple[float, float] | None:
    """
    BUY fill economics from an OpenOrder-shaped dict when present.

    Polymarket documents ``OpenOrder.price`` as the **limit** price, not execution VWAP.
    Prefer ``takingAmount`` / ``makingAmount`` (outcome shares received / USDC spent) when
    the API includes them on GET /order so we do not treat the limit as the fill average.
    """
    taking = _f(od.get("takingAmount")) or _f(od.get("taking_amount"))
    making = _f(od.get("makingAmount")) or _f(od.get("making_amount"))
    if taking > 1e-12 and making >= 0:
        return taking, making
    return None


def parse_fak_buy_post_response(
    resp: Any,
    *,
    requested_shares: float,
    limit_price: float,
    requested_usdc: float | None = None,
) -> FakBuyResult:
    """Interpret POST /order JSON after submitting a marketable (FAK) buy.

    If ``requested_usdc`` is set (USDC-denominated market buy), cap spend to that; else cap by
    ``requested_shares`` (limit-order / share path).
    """
    if not isinstance(resp, dict):
        return FakBuyResult(
            ok=False,
            order_id="",
            status="",
            requested_shares=requested_shares,
            filled_shares=0.0,
            filled_usdc=0.0,
            avg_price=0.0,
            error="non_dict_response",
            raw={},
        )

    if resp.get("success") is False:
        err = str(resp.get("errorMsg") or resp.get("error") or "success_false")
        return FakBuyResult(
            ok=False,
            order_id=str(resp.get("orderID") or resp.get("order_id") or ""),
            status=str(resp.get("status") or ""),
            requested_shares=requested_shares,
            filled_shares=0.0,
            filled_usdc=0.0,
            avg_price=0.0,
            error=err,
            raw=resp,
        )

    order_id = str(resp.get("orderID") or resp.get("order_id") or "")
    status = str(resp.get("status") or "").lower()
    taking = _f(resp.get("takingAmount"))
    making = _f(resp.get("makingAmount"))

    filled_sh = 0.0
    filled_usdc = 0.0

    # BUY: receive outcome shares (taking), spend USDC (making) — when API populates both.
    if taking > 1e-12 and making >= 0:
        filled_sh = taking
        filled_usdc = making
    elif status == "matched" and (
        (requested_shares and requested_shares > 0)
        or (requested_usdc and requested_usdc > 1e-12)
    ):
        if requested_usdc and requested_usdc > 1e-12:
            # Rough split when the POST omits taking/making (VWAP unknown).
            lp = max(float(limit_price), 0.01)
            filled_sh = float(requested_usdc) / lp
            filled_usdc = float(requested_usdc)
        else:
            filled_sh = float(requested_shares)
            filled_usdc = filled_sh * limit_price
        LOGGER.debug("FAK post: matched but no amounts; estimated @ limit / usdc budget")

    if requested_usdc is not None and float(requested_usdc) > 1e-12:
        filled_sh, filled_usdc, avg_px = _cap_fak_fill_to_requested_usdc(
            float(requested_usdc), filled_sh, filled_usdc
        )
    else:
        filled_sh, filled_usdc, avg_px = _cap_fak_fill_to_requested(
            requested_shares, filled_sh, filled_usdc
        )
    if avg_px <= 1e-12:
        avg_px = float(limit_price)

    ok = filled_sh > 1e-12
    if status in {"unmatched"} and filled_sh <= 1e-12:
        ok = False

    return FakBuyResult(
        ok=ok,
        order_id=order_id,
        status=status,
        requested_shares=requested_shares,
        filled_shares=filled_sh,
        filled_usdc=filled_usdc,
        avg_price=avg_px,
        error="",
        raw=resp,
    )


def refine_fak_buy_with_get_order(
    get_order_fn: Any,
    order_id: str,
    *,
    limit_price: float,
    attempts: int = 10,
    delay_sec: float = 0.12,
) -> tuple[float, float, float]:
    """
    Poll GET /order until size_matched > 0 or attempts exhausted.
    Returns (filled_shares, filled_usdc, avg_price).
    """
    if not order_id:
        return 0.0, 0.0, 0.0
    last_sh = 0.0
    last_px = float(limit_price)
    for i in range(max(1, attempts)):
        try:
            od = get_order_fn(order_id)
        except Exception as exc:
            LOGGER.debug("get_order %s attempt %s: %s", order_id[:18], i, exc)
            time.sleep(delay_sec)
            continue
        if not isinstance(od, dict):
            time.sleep(delay_sec)
            continue
        matched = _decode_fixed_size(od.get("size_matched"))
        px_lim = _f(od.get("price")) or float(limit_price)
        last_px = px_lim
        if matched > 1e-9:
            econ = _open_order_buy_economics(od)
            if econ is not None:
                sh, usdc = econ
                apx = usdc / sh if sh > 1e-12 else float(limit_price)
                LOGGER.info(
                    "FAK confirm GET /order: matched=%.4f sh vwap=%.4f (~$%.2f) [taking/making]",
                    sh,
                    apx,
                    usdc,
                )
                return sh, usdc, apx
            usdc = matched * px_lim
            LOGGER.warning(
                "FAK confirm GET /order: OpenOrder has no taking/making; using size_matched * "
                "OpenOrder.price — **price is LIMIT (FAK cap), not execution VWAP** "
                "(see Polymarket OpenOrder docs). matched=%.4f limit_px=%.4f (~$%.2f). "
                "Heartbeats / PnL use this as avg until economics appear.",
                matched,
                px_lim,
                usdc,
            )
            return matched, usdc, px_lim
        last_sh = matched
        time.sleep(delay_sec)
    if last_sh > 1e-9:
        return last_sh, last_sh * last_px, last_px
    return 0.0, 0.0, 0.0


def fak_buy_with_confirm(
    get_order_fn: Any,
    post_resp: Any,
    *,
    requested_shares: float,
    limit_price: float,
    confirm: bool = True,
    requested_usdc: float | None = None,
) -> FakBuyResult:
    """Parse POST response; optionally refine fill via GET /order."""
    base = parse_fak_buy_post_response(
        post_resp,
        requested_shares=requested_shares,
        limit_price=limit_price,
        requested_usdc=requested_usdc,
    )
    if base.filled_shares > 1e-9:
        if requested_usdc is not None and float(requested_usdc) > 1e-12:
            sh, usdc, apx = _cap_fak_fill_to_requested_usdc(
                float(requested_usdc), base.filled_shares, base.filled_usdc
            )
        else:
            sh, usdc, apx = _cap_fak_fill_to_requested(
                requested_shares, base.filled_shares, base.filled_usdc
            )
        if apx <= 1e-12:
            apx = base.avg_price
        return replace(
            base,
            filled_shares=sh,
            filled_usdc=usdc,
            avg_price=apx,
            ok=sh > 1e-9,
        )
    if not base.order_id:
        return base
    if not confirm:
        return base
    if base.status not in {"matched", "delayed", "unmatched", "live", ""}:
        return base

    sh, usdc, apx = refine_fak_buy_with_get_order(
        get_order_fn,
        base.order_id,
        limit_price=limit_price,
    )
    if sh <= 1e-9:
        return FakBuyResult(
            ok=False,
            order_id=base.order_id,
            status=base.status or "unconfirmed",
            requested_shares=requested_shares,
            filled_shares=0.0,
            filled_usdc=0.0,
            avg_price=0.0,
            error="no_fill_confirmed",
            raw=base.raw,
        )
    if requested_usdc is not None and float(requested_usdc) > 1e-12:
        sh, usdc, apx = _cap_fak_fill_to_requested_usdc(float(requested_usdc), sh, usdc)
    else:
        sh, usdc, apx = _cap_fak_fill_to_requested(requested_shares, sh, usdc)
    if apx <= 1e-12 and sh > 1e-12:
        apx = usdc / sh
    return FakBuyResult(
        ok=True,
        order_id=base.order_id,
        status="matched_confirmed",
        requested_shares=requested_shares,
        filled_shares=sh,
        filled_usdc=usdc,
        avg_price=apx,
        error="",
        raw=base.raw,
    )
