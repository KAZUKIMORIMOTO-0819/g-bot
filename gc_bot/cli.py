"""Command line interface for running GC bot cycles."""

from __future__ import annotations

import argparse
import json

from .config import RunnerConfig, load_env_settings
from .runner import run_hourly_cycle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run single GC bot cycle")
    parser.add_argument("--mode", default="paper", choices=["paper", "real"], help="Execution mode")
    parser.add_argument("--symbol", default="XRP/JPY", help="Trading symbol")
    parser.add_argument("--state-path", default="./data/state/state.json", help="State file path")
    parser.add_argument("--notional", type=float, default=5000.0, help="Notional JPY per entry")
    parser.add_argument("--slippage-bps", type=float, default=5.0, help="Slippage in bps")
    parser.add_argument("--taker-fee-bps", type=float, default=15.0, help="Taker fee in bps")
    parser.add_argument("--use-rsi-filter", action="store_true", help="Enable RSI filter for GC entries")
    parser.add_argument("--rsi-period", type=int, default=14, help="RSI period when filter is enabled")
    parser.add_argument("--rsi-min", type=float, default=None, help="Minimum RSI threshold (inclusive) for entries")
    parser.add_argument("--rsi-max", type=float, default=None, help="Maximum RSI threshold (inclusive) for entries")
    return parser.parse_args()


def main() -> None:
    load_env_settings()
    args = parse_args()
    cfg = RunnerConfig(
        mode=args.mode,
        symbol=args.symbol,
        state_path=args.state_path,
        notional_jpy=args.notional,
        slippage_bps=args.slippage_bps,
        taker_fee_bps=args.taker_fee_bps,
        use_rsi_filter=args.use_rsi_filter,
        rsi_period=args.rsi_period,
        rsi_min=args.rsi_min,
        rsi_max=args.rsi_max,
    )
    result = run_hourly_cycle(cfg)
    print(json.dumps(result, ensure_ascii=False, indent=2))


__all__ = ["main"]
