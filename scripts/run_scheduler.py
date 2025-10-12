"""Run GC bot in a simple hourly scheduler loop."""

from __future__ import annotations

import argparse
import os
import time

import schedule

from gc_bot import RunnerConfig, SlackConfig, load_env_settings, run_hourly_cycle
from gc_bot.notifications import notify_runner_status


def parse_args():
    parser = argparse.ArgumentParser(description="Schedule GC bot hourly execution")
    parser.add_argument("--mode", default="paper", choices=["paper", "real"], help="Execution mode")
    parser.add_argument("--symbol", default="XRP/JPY", help="Trading symbol")
    parser.add_argument("--state-path", default="./data/state/state.json", help="State file path")
    parser.add_argument("--notional", type=float, default=5000.0, help="Notional JPY per entry")
    parser.add_argument("--slippage-bps", type=float, default=5.0, help="Slippage in bps")
    parser.add_argument("--taker-fee-bps", type=float, default=15.0, help="Taker fee in bps")
    parser.add_argument("--initial-capital", type=float, default=100000.0, help="Initial capital in JPY")
    parser.add_argument("--notional-fraction", type=float, default=None, help="Fraction of capital per trade (e.g. 0.05 for 5%)")
    parser.add_argument("--use-rsi-filter", action="store_true", help="Enable RSI filter for GC entries")
    parser.add_argument("--rsi-period", type=int, default=14, help="RSI period when filter is enabled")
    parser.add_argument("--rsi-min", type=float, default=None, help="Minimum RSI threshold (inclusive)")
    parser.add_argument("--rsi-max", type=float, default=None, help="Maximum RSI threshold (inclusive)")
    return parser.parse_args()


def run_job(cfg: RunnerConfig, slack_cfg: SlackConfig):
    notify_runner_status(
        slack_cfg,
        "GC Bot Run (scheduled) started",
        f"mode={cfg.mode}, symbol={cfg.symbol}, fraction={cfg.notional_fraction}, rsi={cfg.use_rsi_filter}",
    )
    try:
        result = run_hourly_cycle(cfg)
    except Exception as exc:
        notify_runner_status(
            slack_cfg,
            "GC Bot Run (scheduled) failed",
            f"{type(exc).__name__}: {exc}",
            emoji=":x:",
        )
        print(f"[scheduler] cycle failed: {exc}")
        return
    summary_text = f"stage={result.get('stage')}"
    if "reason" in result:
        summary_text += f", reason={result['reason']}"
    if cfg.use_rsi_filter:
        sig = result.get('signal') or {}
        summary_text += f", passes_rsi={sig.get('passes_rsi_filter')}"
    notify_runner_status(
        slack_cfg,
        "GC Bot Run (scheduled) completed",
        summary_text,
        emoji=":white_check_mark:",
    )


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
        use_rsi_filter=args.use_rsi_filter,
        rsi_period=args.rsi_period,
        rsi_min=args.rsi_min,
        rsi_max=args.rsi_max,
    )
    slack_cfg = SlackConfig()

    schedule.every().hour.at(":05").do(run_job, cfg=cfg, slack_cfg=slack_cfg)
    run_job(cfg, slack_cfg)

    while True:
        schedule.run_pending()
        time.sleep(5)


if __name__ == "__main__":
    main()
