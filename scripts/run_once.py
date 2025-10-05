"""Run one hourly GC bot cycle."""

from __future__ import annotations

import argparse
import json
import os

from gc_bot import RunnerConfig, SlackConfig, load_env_settings, run_hourly_cycle
from gc_bot.notifications import notify_runner_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run single GC bot cycle")
    parser.add_argument("--mode", default="paper", choices=["paper", "real"], help="Execution mode")
    parser.add_argument("--symbol", default="XRP/JPY", help="Trading symbol")
    parser.add_argument("--state-path", default="./data/state/state.json", help="State file path")
    parser.add_argument("--notional", type=float, default=5000.0, help="Notional JPY per entry")
    parser.add_argument("--slippage-bps", type=float, default=5.0, help="Slippage in bps")
    parser.add_argument("--taker-fee-bps", type=float, default=15.0, help="Taker fee in bps")
    parser.add_argument("--initial-capital", type=float, default=100000.0, help="Initial capital in JPY")
    parser.add_argument("--notional-fraction", type=float, default=None, help="Fraction of capital per trade (e.g. 0.05 for 5%)")
    return parser.parse_args()


def main() -> None:
    load_env_settings()
    args = parse_args()
    state_path = args.state_path
    if os.path.isdir(state_path):
        state_path = os.path.join(state_path, "state.json")
    cfg = RunnerConfig(
        mode=args.mode,
        symbol=args.symbol,
        state_path=state_path,
        notional_jpy=args.notional,
        slippage_bps=args.slippage_bps,
        taker_fee_bps=args.taker_fee_bps,
        initial_capital=args.initial_capital,
        notional_fraction=args.notional_fraction,
    )
    slack_cfg = SlackConfig()
    notify_runner_status(
        slack_cfg,
        "GC Bot Run (single) started",
        f"mode={cfg.mode}, symbol={cfg.symbol}, state={cfg.state_path}",
    )
    try:
        result = run_hourly_cycle(cfg)
    except Exception as exc:
        notify_runner_status(
            slack_cfg,
            "GC Bot Run (single) failed",
            f"{type(exc).__name__}: {exc}",
            emoji=":x:",
        )
        raise
    summary_text = f"stage={result.get('stage')}"
    if "reason" in result:
        summary_text += f", reason={result['reason']}"
    notify_runner_status(
        slack_cfg,
        "GC Bot Run (single) completed",
        summary_text,
        emoji=":white_check_mark:",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
