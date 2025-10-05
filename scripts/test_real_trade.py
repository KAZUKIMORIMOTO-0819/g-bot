"""Utility script to place a tiny real trade for connectivity testing."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from gc_bot import load_env_settings
from gc_bot.orders import OrderParams, place_market_buy, place_market_sell


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute a tiny buy/sell cycle on bitFlyer to validate API credentials.",
    )
    parser.add_argument("--symbol", default="XRP/JPY", help="Trading symbol to use")
    parser.add_argument(
        "--notional",
        type=float,
        default=1000.0,
        help="Notional JPY amount for the test trade",
    )
    parser.add_argument(
        "--ref-price",
        type=float,
        default=None,
        help="Reference price used to derive order size (defaults to latest mark price if omitted)",
    )
    parser.add_argument(
        "--slippage-bps",
        type=float,
        default=5.0,
        help="Slippage assumption in basis points",
    )
    parser.add_argument(
        "--taker-fee-bps",
        type=float,
        default=15.0,
        help="Taker fee assumption in basis points",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the would-be request without sending orders",
    )
    return parser.parse_args()


def determine_ref_price(symbol: str) -> float:
    import ccxt

    client = ccxt.bitflyer()
    ticker = client.fetch_ticker(symbol)
    price = ticker.get("last") or ticker.get("close") or ticker.get("ask") or ticker.get("bid")
    if price is None:
        raise RuntimeError(f"Failed to fetch reference price for {symbol}")
    return float(price)


def main() -> None:
    load_env_settings()
    args = parse_args()

    api_key = os.getenv("BFX_API_KEY") or os.getenv("BITFLYER_API_KEY")
    api_secret = os.getenv("BFX_API_SECRET") or os.getenv("BITFLYER_API_SECRET")
    if not api_key or not api_secret:
        print("[ERROR] BFX_API_KEY / BFX_API_SECRET (or BITFLYER_API_KEY / BITFLYER_API_SECRET) must be set.")
        sys.exit(1)

    ref_price = args.ref_price or determine_ref_price(args.symbol)
    order_params = OrderParams(
        mode="real",
        notional_jpy=args.notional,
        slippage_bps=args.slippage_bps,
        taker_fee_bps=args.taker_fee_bps,
        api_key=api_key,
        secret=api_secret,
    )

    size_estimate = args.notional / ref_price if ref_price else 0.0

    base_symbol = args.symbol.split("/")[0]

    print(f"[INFO] Preparing real trade test on {args.symbol}")
    print(f"       notional={args.notional} JPY  ref_price≈{ref_price}  size≈{size_estimate:.6f} {base_symbol}")
    print(f"       slippage_bps={args.slippage_bps}  taker_fee_bps={args.taker_fee_bps}")

    if args.dry_run:
        print("[DRY-RUN] No orders were sent.")
        return

    buy_res = place_market_buy(args.symbol, ref_price, order_params)
    print("[BUY]", buy_res)

    sell_res = place_market_sell(args.symbol, buy_res["size"], buy_res["price"], order_params)
    print("[SELL]", sell_res)


if __name__ == "__main__":
    main()
