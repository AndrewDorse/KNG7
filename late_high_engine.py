#!/usr/bin/env python3
"""Multi-asset 5m late-price 99c GTC limit-buy engine."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from config import ActiveContract, BotConfig, LOGGER
from market_locator import GammaMarketLocator
from trader import PolymarketTrader

_WINDOW_MINUTES = 5
_WINDOW_SEC = _WINDOW_MINUTES * 60


def _out(msg: str) -> None:
    print(msg, flush=True)


def _slug_start_ts(slug: str) -> int | None:
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return None


def _path_writable(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _resolve_state_path(configured: str) -> Path:
    candidates = [Path(configured.strip())] if configured.strip() else []
    candidates.extend([Path("/app/data/late_high_state.json"), Path("exports/late_high_state.json")])
    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve()) if path.is_absolute() else str(path)
        if key in seen:
            continue
        seen.add(key)
        if _path_writable(path):
            return path
    return candidates[0]


class LateHighEngine:
    def __init__(
        self,
        config: BotConfig,
        locator: GammaMarketLocator,
        trader: PolymarketTrader,
    ) -> None:
        self.config = config
        self.locator = locator
        self.trader = trader
        self._state_path = _resolve_state_path(config.late_high_state_path)
        self._submitted_slugs: set[str] = set()
        self._pending_orders: dict[str, dict[str, object]] = {}
        self._last_idle_log_mono: dict[str, float] = {}
        self._cached_balance_usdc: float | None = None
        self._next_balance_refresh_mono = 0.0
        self._load_state()

    def _load_state(self) -> None:
        if not self._state_path.is_file():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.error("late_high state load failed (%s): %s", self._state_path, exc)
            return
        if isinstance(raw, dict):
            self._submitted_slugs = {str(s) for s in raw.get("submitted_slugs", [])}
            pending = raw.get("pending_orders") or {}
            if isinstance(pending, dict):
                self._pending_orders = {
                    str(slug): dict(value)
                    for slug, value in pending.items()
                    if isinstance(value, dict)
                }

    def _save_state(self) -> None:
        cutoff = int(time.time()) - 48 * 3600
        self._submitted_slugs = {
            slug for slug in self._submitted_slugs if (_slug_start_ts(slug) or 0) >= cutoff
        }
        self._pending_orders = {
            slug: value
            for slug, value in self._pending_orders.items()
            if (_slug_start_ts(slug) or 0) >= cutoff
        }
        payload = {
            "submitted_slugs": sorted(self._submitted_slugs),
            "pending_orders": self._pending_orders,
            "updated_at": int(time.time()),
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._state_path)

    def _active_contracts(self, now_ts: int) -> list[tuple[str, ActiveContract]]:
        start_ts = (now_ts // _WINDOW_SEC) * _WINDOW_SEC
        contracts: list[tuple[str, ActiveContract]] = []
        for symbol in self.config.late_high_symbols:
            contract = self.locator.get_contract_for_window_start(
                _WINDOW_MINUTES,
                start_ts,
                market_symbol=symbol,
            )
            if contract is not None:
                contracts.append((symbol, contract))
        self.trader.sync_ws_subscriptions([contract for _, contract in contracts])
        return contracts

    def _midpoint(self, token_id: str) -> float | None:
        if self.trader.ws_quotes_active:
            return self.trader.get_ws_midpoint(token_id)
        return self.trader.get_midpoint(token_id)

    def _pick_signal(
        self,
        contract: ActiveContract,
        elapsed: int,
    ) -> tuple[str, float] | None:
        if (
            elapsed < self.config.late_high_entry_lo_sec
            or elapsed > self.config.late_high_entry_hi_sec
            or elapsed >= self.config.late_high_cancel_unfilled_at_sec
        ):
            return None
        up_mid = self._midpoint(contract.up.token_id)
        down_mid = self._midpoint(contract.down.token_id)
        if up_mid is None or down_mid is None:
            return None
        candidates: list[tuple[str, float]] = []
        if up_mid > self.config.late_high_min_leg_px:
            candidates.append(("UP", up_mid))
        if down_mid > self.config.late_high_min_leg_px:
            candidates.append(("DOWN", down_mid))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[1], reverse=True)
        return candidates[0]

    def _shares_for_balance(self, balance: float) -> tuple[float, float] | None:
        px = self.config.late_high_limit_px
        risk_budget = balance * self.config.late_high_balance_fraction
        minimum_cost = px * self.config.late_high_min_shares
        if (
            self.config.late_high_strict_balance_fraction
            and risk_budget + 1e-9 < minimum_cost
        ):
            return None
        shares = max(
            risk_budget / px,
            self.config.late_high_min_shares,
        )
        shares = math.floor(shares * 10_000) / 10_000
        cost = shares * px
        if balance + 1e-9 < cost:
            return None
        return shares, cost

    def _balance_usdc(self) -> float:
        if self.config.dry_run:
            return max(
                self.config.late_high_limit_px * self.config.late_high_min_shares,
                10.0,
            )
        mono = time.monotonic()
        if self._cached_balance_usdc is None or mono >= self._next_balance_refresh_mono:
            self._cached_balance_usdc = self.trader.wallet_balance_usdc()
            self._next_balance_refresh_mono = mono + self.config.late_high_balance_refresh_seconds
        return self._cached_balance_usdc

    def _cancel_pending_order(self, slug: str, pending: dict[str, object], reason: str) -> None:
        token_id = str(pending.get("token_id") or "")
        symbol = str(pending.get("symbol") or "")
        side = str(pending.get("side") or "")
        if not token_id:
            self._pending_orders.pop(slug, None)
            self._save_state()
            return
        if self.config.dry_run:
            confirmed = attempted = 1
        else:
            confirmed, attempted = self.trader.cancel_token_orders_confirmed(token_id)
        _out(
            f"CANCEL_PENDING symbol={symbol} slug={slug} side={side} reason={reason} "
            f"confirmed={confirmed} attempted={attempted}"
        )
        if attempted > confirmed:
            return
        self._pending_orders.pop(slug, None)
        self._save_state()

    def _manage_pending_orders(self, now_ts: int) -> None:
        for slug, pending in list(self._pending_orders.items()):
            start_ts = _slug_start_ts(slug)
            if start_ts is None:
                self._pending_orders.pop(slug, None)
                continue
            elapsed = now_ts - start_ts
            if elapsed >= self.config.late_high_cancel_unfilled_at_sec:
                self._cancel_pending_order(slug, pending, "window_expiry")

    def _submit(
        self,
        *,
        symbol: str,
        contract: ActiveContract,
        elapsed: int,
        side: str,
        signal_px: float,
    ) -> None:
        balance = self._balance_usdc()
        if not self.config.dry_run and balance <= 0:
            _out(f"SKIP symbol={symbol} slug={contract.slug} reason=no_usdc_balance")
            return
        sized = self._shares_for_balance(balance)
        if sized is None:
            need = self.config.late_high_limit_px * self.config.late_high_min_shares
            risk_budget = balance * self.config.late_high_balance_fraction
            reason = (
                "risk_budget_below_min"
                if self.config.late_high_strict_balance_fraction
                and risk_budget + 1e-9 < need
                else "insufficient_for_min_shares"
            )
            _out(
                f"SKIP symbol={symbol} slug={contract.slug} reason={reason} "
                f"balance={balance:.4f} risk_budget={risk_budget:.4f} need={need:.4f}"
            )
            return
        shares, cost = sized
        token = contract.up if side == "UP" else contract.down
        limit_px = self.config.late_high_limit_px
        if self.config.dry_run:
            _out(
                f"ORDER dry_run symbol={symbol} slug={contract.slug} side={side} "
                f"signal={signal_px:.3f} limit={limit_px:.2f} shares={shares:.4f} "
                f"cost={cost:.4f} elapsed={elapsed}s"
            )
        else:
            self.trader.place_limit_buy(token, limit_px, shares)
            _out(
                f"ORDER symbol={symbol} slug={contract.slug} side={side} "
                f"signal={signal_px:.3f} limit={limit_px:.2f} shares={shares:.4f} "
                f"cost={cost:.4f} elapsed={elapsed}s"
            )
        self._submitted_slugs.add(contract.slug)
        self._pending_orders[contract.slug] = {
            "symbol": symbol,
            "side": side,
            "token_id": token.token_id,
            "submitted_at": int(time.time()),
            "expires_at": (_slug_start_ts(contract.slug) or int(time.time()))
            + self.config.late_high_cancel_unfilled_at_sec,
        }
        self._save_state()

    def _tick(self) -> None:
        now_ts = int(time.time())
        self._manage_pending_orders(now_ts)
        for symbol, contract in self._active_contracts(now_ts):
            if contract.slug in self._submitted_slugs:
                continue
            start_ts = _slug_start_ts(contract.slug)
            if start_ts is None:
                continue
            elapsed = now_ts - start_ts
            signal = self._pick_signal(contract, elapsed)
            if signal is None:
                mono = time.monotonic()
                if mono - self._last_idle_log_mono.get(symbol, 0.0) >= 30:
                    _out(f"IDLE symbol={symbol} slug={contract.slug} elapsed={elapsed}s")
                    self._last_idle_log_mono[symbol] = mono
                continue
            side, signal_px = signal
            self._submit(
                symbol=symbol,
                contract=contract,
                elapsed=elapsed,
                side=side,
                signal_px=signal_px,
            )

    def run(self) -> None:
        _out(
            "INIT "
            f"strategy=late_high_5m symbols={','.join(self.config.late_high_symbols)} "
            f"entry={self.config.late_high_entry_lo_sec}-{self.config.late_high_entry_hi_sec}s "
            f"signal_gt={self.config.late_high_min_leg_px:.2f} "
            f"limit={self.config.late_high_limit_px:.2f} "
            f"balance_fraction={self.config.late_high_balance_fraction:.2f} "
            f"strict_fraction={self.config.late_high_strict_balance_fraction} "
            f"min_shares={self.config.late_high_min_shares:g} "
            f"cancel_unfilled_at={self.config.late_high_cancel_unfilled_at_sec}s "
            f"poll={self.config.poll_interval_seconds:g}s dry_run={self.config.dry_run} "
            f"state={self._state_path}"
        )
        while True:
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("late_high_5m tick error: %s", exc)
            time.sleep(self.config.poll_interval_seconds)
