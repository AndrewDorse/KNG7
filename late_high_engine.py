#!/usr/bin/env python3
"""Multi-combination Binance alignment engine using 99c GTC limit buys."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from binance_ws import BinancePriceFeed
from config import ActiveContract, BotConfig, LOGGER
from market_locator import GammaMarketLocator
from trader import PolymarketTrader

_WINDOW_MINUTES = 5
_WINDOW_SEC = _WINDOW_MINUTES * 60


def _out(msg: str) -> None:
    print(msg, flush=True)


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


def _combination_key(symbols: tuple[str, ...]) -> str:
    return "+".join(symbols)


class LateHighEngine:
    def __init__(
        self,
        config: BotConfig,
        locator: GammaMarketLocator,
        trader: PolymarketTrader,
        binance_feed: BinancePriceFeed | None = None,
    ) -> None:
        self.config = config
        self.locator = locator
        self.trader = trader
        self._state_path = _resolve_state_path(config.late_high_state_path)
        self._window_start: int | None = None
        self._window_side: str | None = None
        self._sent_pairs: dict[str, str] = {}
        self._fired_combinations: set[str] = set()
        self._last_idle_log_mono: dict[str, float] = {}
        self._cached_balance_usdc: float | None = None
        self._next_balance_refresh_mono = 0.0
        self._last_gate_log_mono = 0.0
        self._deadline_logged = False
        self._binance_feed = binance_feed or BinancePriceFeed(
            config.late_high_symbols,
            url=config.late_high_binance_ws_url,
            history_seconds=30.0,
            request_timeout_seconds=config.request_timeout_seconds,
        )
        self._binance_feed.start()
        self._load_state()

    def _load_state(self) -> None:
        if not self._state_path.is_file():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.error("late_high state load failed (%s): %s", self._state_path, exc)
            return
        if not isinstance(raw, dict):
            return
        current_start = (int(time.time()) // _WINDOW_SEC) * _WINDOW_SEC
        state_start = raw.get("window_start")
        if state_start is not None and int(state_start) == current_start:
            self._window_start = current_start
            self._window_side = str(raw.get("window_side") or "").upper() or None
            sent = raw.get("sent_pairs") or {}
            if isinstance(sent, dict):
                self._sent_pairs = {
                    str(symbol).upper(): str(side).upper()
                    for symbol, side in sent.items()
                    if str(symbol).upper() in self.config.late_high_symbols
                }
            self._fired_combinations = {
                str(value) for value in raw.get("fired_combinations", [])
            }
            return

        # One-time migration from the previous submitted-slug state format.
        submitted = raw.get("submitted_slugs") or []
        migrated: dict[str, str] = {}
        for slug in submitted:
            text = str(slug)
            if not text.endswith(f"-{current_start}"):
                continue
            symbol = text.split("-updown-", 1)[0].upper()
            if symbol in self.config.late_high_symbols:
                migrated[symbol] = "SENT"
        if migrated:
            self._window_start = current_start
            self._sent_pairs = migrated

    def _save_state(self) -> None:
        payload = {
            "window_start": self._window_start,
            "window_side": self._window_side,
            "sent_pairs": self._sent_pairs,
            "fired_combinations": sorted(self._fired_combinations),
            "updated_at": int(time.time()),
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._state_path)

    def _roll_window(self, start_ts: int) -> None:
        if self._window_start == start_ts:
            return
        previous = self._window_start
        self._window_start = start_ts
        self._window_side = None
        self._sent_pairs = {}
        self._fired_combinations = set()
        self._deadline_logged = False
        self._save_state()
        _out(f"WINDOW_RESET previous={previous} current={start_ts}")

    def _active_contracts(self, now_ts: int) -> dict[str, ActiveContract]:
        start_ts = (now_ts // _WINDOW_SEC) * _WINDOW_SEC
        contracts: dict[str, ActiveContract] = {}
        for symbol in self.config.late_high_symbols:
            contract = self.locator.get_contract_for_window_start(
                _WINDOW_MINUTES,
                start_ts,
                market_symbol=symbol,
            )
            if contract is not None:
                contracts[symbol] = contract
        self.trader.sync_ws_subscriptions(list(contracts.values()))
        return contracts

    def _thresholds(self) -> dict[str, float]:
        return {
            "BTC": self.config.late_high_btc_bps,
            "ETH": self.config.late_high_eth_bps,
            "SOL": self.config.late_high_sol_bps,
            "XRP": self.config.late_high_xrp_bps,
            "BNB": self.config.late_high_bnb_bps,
            "DOGE": self.config.late_high_doge_bps,
        }

    def _active_combinations(self) -> tuple[tuple[str, ...], ...]:
        enabled = set(self.config.late_high_symbols)
        return tuple(
            combination
            for combination in self.config.late_high_combinations
            if set(combination).issubset(enabled)
        )

    def _disabled_combinations(self) -> tuple[tuple[str, ...], ...]:
        enabled = set(self.config.late_high_symbols)
        return tuple(
            combination
            for combination in self.config.late_high_combinations
            if not set(combination).issubset(enabled)
        )

    def _combination_side(
        self,
        combination: tuple[str, ...],
        moves: dict[str, float],
    ) -> str | None:
        thresholds = self._thresholds()
        if all(moves.get(symbol, float("-inf")) >= thresholds[symbol] for symbol in combination):
            return "UP"
        if all(moves.get(symbol, float("inf")) <= -thresholds[symbol] for symbol in combination):
            return "DOWN"
        return None

    def _shares_for_balance(self, balance: float) -> tuple[float, float] | None:
        px = self.config.late_high_limit_px
        risk_budget = balance * self.config.late_high_balance_fraction
        minimum_cost = px * self.config.late_high_min_shares
        if (
            self.config.late_high_strict_balance_fraction
            and risk_budget + 1e-9 < minimum_cost
        ):
            return None
        shares = max(risk_budget / px, self.config.late_high_min_shares)
        shares = math.floor(shares * 10_000) / 10_000
        cost = shares * px
        if balance + 1e-9 < cost:
            return None
        return shares, cost

    def _balance_usdc(self) -> float:
        if self.config.dry_run:
            return 100.0
        mono = time.monotonic()
        if self._cached_balance_usdc is None or mono >= self._next_balance_refresh_mono:
            self._cached_balance_usdc = self.trader.wallet_balance_usdc()
            self._next_balance_refresh_mono = mono + self.config.late_high_balance_refresh_seconds
        return self._cached_balance_usdc

    def _submit(
        self,
        *,
        symbol: str,
        contract: ActiveContract,
        elapsed: int,
        side: str,
        signal_bps: float,
        sized: tuple[float, float],
        combination: str,
    ) -> bool:
        shares, cost = sized
        token = contract.up if side == "UP" else contract.down
        limit_px = self.config.late_high_limit_px
        if self.config.dry_run:
            _out(
                f"ORDER dry_run symbol={symbol} slug={contract.slug} side={side} "
                f"combination={combination} move={signal_bps:.2f}bps "
                f"limit={limit_px:.2f} shares={shares:.4f} cost={cost:.4f} elapsed={elapsed}s"
            )
        else:
            try:
                self.trader.place_limit_buy(token, limit_px, shares)
            except Exception as exc:  # noqa: BLE001
                _out(
                    f"ORDER_FAIL symbol={symbol} slug={contract.slug} side={side} "
                    f"combination={combination} reason={type(exc).__name__}:{exc}"
                )
                return False
            _out(
                f"ORDER symbol={symbol} slug={contract.slug} side={side} "
                f"combination={combination} move={signal_bps:.2f}bps "
                f"limit={limit_px:.2f} shares={shares:.4f} cost={cost:.4f} elapsed={elapsed}s"
            )
        self._sent_pairs[symbol] = side
        self._save_state()
        return True

    def _evaluate_combinations(
        self,
        *,
        start_ts: int,
        elapsed: int,
        moves: dict[str, float],
        contracts: dict[str, ActiveContract],
    ) -> None:
        for combination in self._active_combinations():
            key = _combination_key(combination)
            if key in self._fired_combinations:
                continue
            side = self._combination_side(combination, moves)
            if side is None:
                continue
            if self._window_side is not None and side != self._window_side:
                _out(
                    f"BLOCK window={start_ts} combination={key} side={side} "
                    f"reason=window_side_locked locked={self._window_side}"
                )
                continue
            missing_contracts = [symbol for symbol in combination if symbol not in contracts]
            if missing_contracts:
                _out(
                    f"WAIT window={start_ts} combination={key} "
                    f"reason=contracts_unavailable symbols={','.join(missing_contracts)}"
                )
                continue

            self._window_side = side
            self._fired_combinations.add(key)
            self._save_state()
            unsent = [symbol for symbol in combination if symbol not in self._sent_pairs]
            detail = ",".join(f"{symbol}={moves[symbol]:.2f}" for symbol in combination)
            if not unsent:
                _out(
                    f"SIGNAL_NO_NEW window={start_ts} combination={key} side={side} "
                    f"elapsed={elapsed}s moves={detail}"
                )
                continue

            balance_snapshot = self._balance_usdc()
            sized = self._shares_for_balance(balance_snapshot)
            if sized is None:
                _out(
                    f"SKIP window={start_ts} combination={key} "
                    f"reason=insufficient_for_min_shares balance={balance_snapshot:.4f}"
                )
                continue
            _out(
                f"SIGNAL window={start_ts} combination={key} side={side} "
                f"new_pairs={','.join(unsent)} elapsed={elapsed}s moves={detail} "
                f"balance={balance_snapshot:.4f}"
            )
            for symbol in unsent:
                self._submit(
                    symbol=symbol,
                    contract=contracts[symbol],
                    elapsed=elapsed,
                    side=side,
                    signal_bps=(
                        moves[symbol] if side == "UP" else -moves[symbol]
                    ),
                    sized=sized,
                    combination=key,
                )

    def _tick(self) -> None:
        now_ts = int(time.time())
        start_ts = (now_ts // _WINDOW_SEC) * _WINDOW_SEC
        elapsed = now_ts - start_ts
        self._roll_window(start_ts)
        self._binance_feed.prepare_window(start_ts)
        contracts = self._active_contracts(now_ts)
        if elapsed < self.config.late_high_entry_lo_sec:
            return
        if elapsed > self.config.late_high_entry_hi_sec:
            if not self._deadline_logged:
                _out(
                    f"WINDOW_DONE window={start_ts} fired={sorted(self._fired_combinations)} "
                    f"sent={self._sent_pairs}"
                )
                self._deadline_logged = True
            return

        moves, reason = self._binance_feed.window_moves_bps(
            start_ts,
            max_age_seconds=self.config.late_high_binance_max_age_seconds,
            symbols=tuple(
                dict.fromkeys(
                    symbol
                    for combination in self._active_combinations()
                    for symbol in combination
                )
            ),
        )
        if moves is None:
            mono = time.monotonic()
            if mono - self._last_gate_log_mono >= 1.0:
                _out(f"WAIT window={start_ts} reason={reason} elapsed={elapsed}s")
                self._last_gate_log_mono = mono
            return

        self._evaluate_combinations(
            start_ts=start_ts,
            elapsed=elapsed,
            moves=moves,
            contracts=contracts,
        )

    def run(self) -> None:
        active = ",".join(_combination_key(value) for value in self._active_combinations())
        disabled = ",".join(_combination_key(value) for value in self._disabled_combinations())
        thresholds = self._thresholds()
        threshold_text = ",".join(
            f"{symbol}:{thresholds[symbol]:g}"
            for symbol in self.config.late_high_symbols
        )
        _out(
            "INIT "
            f"strategy=late_high_5m symbols={','.join(self.config.late_high_symbols)} "
            f"entry={self.config.late_high_entry_lo_sec}-{self.config.late_high_entry_hi_sec}s "
            f"combinations={active or 'none'} disabled={disabled or 'none'} "
            f"thresholds={threshold_text}bps "
            f"limit={self.config.late_high_limit_px:.2f} "
            f"balance_fraction={self.config.late_high_balance_fraction:.2f} "
            f"strict_fraction={self.config.late_high_strict_balance_fraction} "
            f"min_shares={self.config.late_high_min_shares:g} "
            "order_lifecycle=platform_managed_hold_to_resolution "
            f"poll={self.config.poll_interval_seconds:g}s dry_run={self.config.dry_run} "
            f"state={self._state_path}"
        )
        while True:
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("late_high_5m tick error: %s", exc)
            time.sleep(self.config.poll_interval_seconds)
