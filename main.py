#!/usr/bin/env python3
"""KNG7 Docker: **first_cheap_03** — first 3c midpoint touch, $1 USDC FAK per 15m window."""

from __future__ import annotations

import logging
import sys


def _silence_third_party_loggers() -> None:
    logging.getLogger().setLevel(logging.CRITICAL)
    for name in (
        "polymarket_btc_ladder",
        "urllib3",
        "requests",
        "websocket",
        "websockets",
        "py_clob_client",
    ):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def main() -> int:
    _silence_third_party_loggers()

    from config import BotConfig, BotConfigError  # noqa: PLC0415
    from market_locator import GammaMarketLocator  # noqa: PLC0415
    from trader import PolymarketTrader  # noqa: PLC0415

    from cheap03_first_engine import Cheap03FirstEngine  # noqa: PLC0415

    try:
        config = BotConfig.from_env()
    except BotConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if config.strategy_mode != "first_cheap_03":
        print(
            "KNG7 image expects BOT_STRATEGY_MODE=first_cheap_03 "
            f"(got {config.strategy_mode!r}).",
            file=sys.stderr,
        )
        return 2

    locator = GammaMarketLocator(config)
    trader = PolymarketTrader(config)
    Cheap03FirstEngine(config, locator, trader).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
