from __future__ import annotations

import unittest
from types import SimpleNamespace

from binance_ws import BinancePriceFeed
from late_high_engine import LateHighEngine


class _Trader:
    def __init__(self, results: list[tuple[int, int]]) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def cancel_token_orders_confirmed(self, token_id: str) -> tuple[int, int]:
        self.calls.append(token_id)
        return self.results.pop(0)


def _engine(
    *,
    results: list[tuple[int, int]] | None = None,
) -> LateHighEngine:
    engine = LateHighEngine.__new__(LateHighEngine)
    engine.config = SimpleNamespace(
        dry_run=False,
        late_high_cancel_unfilled_at_sec=299,
    )
    engine.trader = _Trader(results or [(1, 1)])
    engine._pending_orders = {}
    engine._save_state = lambda: None
    return engine


class PendingOrderLifecycleTest(unittest.TestCase):
    slug = "btc-updown-5m-1000"
    pending = {"symbol": "BTC", "side": "UP", "token_id": "token"}

    def test_expiry_cancels_open_remainder(self) -> None:
        engine = _engine()
        engine._pending_orders[self.slug] = dict(self.pending)

        engine._manage_pending_orders(1299)

        self.assertNotIn(self.slug, engine._pending_orders)
        self.assertEqual(engine.trader.calls, ["token"])

    def test_failed_cancel_stays_pending_for_retry(self) -> None:
        engine = _engine(results=[(0, 1)])
        engine._pending_orders[self.slug] = dict(self.pending)

        engine._manage_pending_orders(1299)

        self.assertIn(self.slug, engine._pending_orders)

    def test_no_open_remainder_clears_pending_metadata(self) -> None:
        engine = _engine(results=[(0, 0)])
        engine._pending_orders[self.slug] = dict(self.pending)

        engine._manage_pending_orders(1299)

        self.assertNotIn(self.slug, engine._pending_orders)

    def test_order_stays_pending_before_expiry(self) -> None:
        engine = _engine()
        engine._pending_orders[self.slug] = dict(self.pending)

        engine._manage_pending_orders(1280)

        self.assertIn(self.slug, engine._pending_orders)
        self.assertEqual(engine.trader.calls, [])


class BalanceSizingTest(unittest.TestCase):
    def _engine(self) -> LateHighEngine:
        engine = LateHighEngine.__new__(LateHighEngine)
        engine.config = SimpleNamespace(
            late_high_limit_px=0.99,
            late_high_min_shares=5.0,
            late_high_balance_fraction=0.11,
            late_high_strict_balance_fraction=False,
        )
        return engine

    def test_minimum_five_shares_is_allowed_on_small_balance(self) -> None:
        engine = self._engine()

        shares, cost = engine._shares_for_balance(15.0)

        self.assertEqual(shares, 5.0)
        self.assertAlmostEqual(cost, 4.95)
        self.assertEqual(int(15.0 // cost), 3)

    def test_fractional_size_is_used_above_minimum(self) -> None:
        shares, cost = self._engine()._shares_for_balance(100.0)

        self.assertEqual(shares, 11.1111)
        self.assertLessEqual(cost, 11.0)


class AlignmentRuleTest(unittest.TestCase):
    def _engine(self) -> LateHighEngine:
        engine = LateHighEngine.__new__(LateHighEngine)
        engine.config = SimpleNamespace(
            late_high_btc_bps=8.0,
            late_high_eth_bps=8.0,
            late_high_sol_bps=8.0,
            late_high_xrp_bps=10.0,
            late_high_bnb_bps=10.0,
        )
        return engine

    def test_all_five_up_at_threshold_aligns_up(self) -> None:
        self.assertEqual(
            self._engine()._alignment_side(
                {"BTC": 8, "ETH": 8, "SOL": 8, "XRP": 10, "BNB": 10}
            ),
            "UP",
        )

    def test_all_five_down_at_threshold_aligns_down(self) -> None:
        self.assertEqual(
            self._engine()._alignment_side(
                {"BTC": -8, "ETH": -8, "SOL": -8, "XRP": -10, "BNB": -10}
            ),
            "DOWN",
        )

    def test_one_opposite_pair_blocks_signal(self) -> None:
        self.assertIsNone(
            self._engine()._alignment_side(
                {"BTC": 8, "ETH": 8, "SOL": 8, "XRP": 10, "BNB": -10}
            )
        )


class BinanceWindowMoveTest(unittest.TestCase):
    def _feed(self) -> BinancePriceFeed:
        feed = BinancePriceFeed.__new__(BinancePriceFeed)
        feed._symbols = ("BTC", "ETH")
        feed._lock = __import__("threading").Lock()
        feed._window_opens = {}
        feed._prices = {"BTC": __import__("collections").deque(), "ETH": __import__("collections").deque()}
        return feed

    def test_moves_use_each_pairs_own_window_open(self) -> None:
        feed = self._feed()
        now = __import__("time").time()
        feed._window_opens[("BTC", 1000)] = 100.0
        feed._window_opens[("ETH", 1000)] = 200.0
        feed._prices["BTC"].append((now, 100.1))
        feed._prices["ETH"].append((now, 199.8))

        moves, reason = feed.window_moves_bps(1000, max_age_seconds=3.0)

        self.assertEqual(reason, "ok")
        self.assertAlmostEqual(moves["BTC"], 10.0)
        self.assertAlmostEqual(moves["ETH"], -10.0)

    def test_stale_pair_fails_closed(self) -> None:
        feed = self._feed()
        now = __import__("time").time()
        for symbol in feed._symbols:
            feed._window_opens[(symbol, 1000)] = 100.0
            feed._prices[symbol].append((now, 100.1))
        feed._prices["ETH"].clear()
        feed._prices["ETH"].append((now - 5.0, 100.1))

        moves, reason = feed.window_moves_bps(1000, max_age_seconds=3.0)

        self.assertIsNone(moves)
        self.assertEqual(reason, "ETH_price_stale")


class OrderFailureIsolationTest(unittest.TestCase):
    def test_order_exception_is_caught_without_marking_pair_submitted(self) -> None:
        engine = LateHighEngine.__new__(LateHighEngine)
        engine.config = SimpleNamespace(dry_run=False, late_high_limit_px=0.99)
        engine.trader = SimpleNamespace(
            place_limit_buy=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("insufficient balance")
            )
        )
        engine._submitted_slugs = set()
        engine._pending_orders = {}
        engine._save_state = lambda: None
        contract = SimpleNamespace(
            slug="btc-updown-5m-1000",
            up=SimpleNamespace(token_id="up"),
            down=SimpleNamespace(token_id="down"),
        )

        submitted = engine._submit(
            symbol="BTC",
            contract=contract,
            elapsed=280,
            side="UP",
            signal_bps=8.0,
            sized=(5.0, 4.95),
        )

        self.assertFalse(submitted)
        self.assertNotIn(contract.slug, engine._submitted_slugs)


if __name__ == "__main__":
    unittest.main()
