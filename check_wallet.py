#!/usr/bin/env python3
"""Verify Polymarket CLOB credentials before running the bot."""

from __future__ import annotations

import sys

from config import BotConfig, BotConfigError
from trader import PolymarketTrader, diagnose_clob_wallet, wallet_config_hint_for_error


def main() -> int:
    try:
        config = BotConfig.from_env()
    except BotConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    print("=" * 60)
    print("KNG7 wallet / CLOB check")
    print("=" * 60)

    try:
        trader = PolymarketTrader(config)
    except Exception as exc:
        print(f"FAIL: could not init CLOB client: {exc}")
        print(wallet_config_hint_for_error(exc))
        return 1

    summary = trader.wallet_setup_summary()
    print(f"Signer EOA (private key): {summary.get('eoa')}")
    print(f"POLY_FUNDER (maker):      {summary.get('funder')}")
    print(f"POLY_SIGNATURE_TYPE:      {summary.get('signature_type')}")
    fc = summary.get("funder_is_contract")
    if fc is True:
        print("POLY_FUNDER on-chain:     smart contract (V2 deposit-wallet proxy)")
    elif fc is False:
        print("POLY_FUNDER on-chain:     EOA (no contract bytecode)")
    else:
        print("POLY_FUNDER on-chain:     (could not check)")
    print(f"CLOB v2 client:           {summary.get('clob_v2')}")
    if summary.get("clob_v1_forced"):
        print("CLOB v1 forced:           yes (BOT_CLOB_USE_V1=true)")
    print(f"RELAYER_API_KEY set:      {summary.get('relayer_api_key')}")
    print(f"Balance (CLOB):           ${summary.get('balance_usdc', 0):.2f}")
    print()

    notes = diagnose_clob_wallet(config)
    for note in notes:
        print(f"NOTE: {note}")
    if notes:
        print()

    ok, detail = trader.verify_clob_ready()
    if ok:
        print(f"OK: {detail}")
        return 0

    print(f"FAIL: {detail}")
    print(wallet_config_hint_for_error(Exception(detail)))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
