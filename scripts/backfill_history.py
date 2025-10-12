"""Backfill historical OHLCV data for longer time spans."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from gc_bot import CCXTConfig, SlackConfig, fetch_ohlcv_range_ccxt, load_env_settings
from gc_bot.notifications import notify_runner_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill historical OHLCV candles")
    parser.add_argument("--exchange", default="bitflyer", help="ccxt exchange id")
    parser.add_argument("--symbol", default="XRP/JPY", help="Trading symbol")
    parser.add_argument("--timeframe", default="1h", help="OHLCV timeframe (e.g. 1h)")
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="Number of days to look back from now when start date is not specified",
    )
    parser.add_argument("--start", type=str, default=None, help="Explicit start timestamp (JST, e.g. 2023-10-01)")
    parser.add_argument("--end", type=str, default=None, help="Explicit end timestamp (JST, defaults to now)")
    parser.add_argument(
        "--chunk-limit",
        type=int,
        default=None,
        help="Override per-request limit when paginating fetchOHLCV",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="./data/candles/xrpjpy_1h_history.parquet",
        help="Output parquet path",
    )
    parser.add_argument(
        "--csv-output",
        type=str,
        default=None,
        help="Optional CSV output path (if omitted, CSV is not written)",
    )
    return parser.parse_args()


def determine_window(args: argparse.Namespace) -> tuple[pd.Timestamp, pd.Timestamp]:
    end = pd.Timestamp(args.end, tz="Asia/Tokyo") if args.end else pd.Timestamp.now(tz="Asia/Tokyo")
    start: pd.Timestamp
    if args.start:
        start = pd.Timestamp(args.start, tz="Asia/Tokyo")
    else:
        start = end - pd.Timedelta(days=args.days)
    return start, end


def main() -> None:
    load_env_settings()
    args = parse_args()

    start_jst, end_jst = determine_window(args)
    cfg = CCXTConfig(
        exchange_id=args.exchange,
        symbol=args.symbol,
        timeframe=args.timeframe,
        limit=max(args.chunk_limit or 200, 1),
    )

    slack_cfg = SlackConfig()
    notify_runner_status(
        slack_cfg,
        "GC Bot Historical backfill started",
        f"exchange={cfg.exchange_id}, symbol={cfg.symbol}, timeframe={cfg.timeframe}, start={start_jst}, end={end_jst}",
    )
    try:
        df = fetch_ohlcv_range_ccxt(
            cfg,
            start=start_jst,
            end=end_jst,
            chunk_limit=args.chunk_limit,
            progress=True,
        )
    except Exception as exc:
        notify_runner_status(
            slack_cfg,
            "GC Bot Historical backfill failed",
            f"{type(exc).__name__}: {exc}",
            emoji=":x:",
        )
        raise

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, index=True)

    csv_path = None
    if args.csv_output:
        csv_path = Path(args.csv_output)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(csv_path, index=True, encoding="utf-8")

    meta = df.attrs.get("meta", {})
    notify_runner_status(
        slack_cfg,
        "GC Bot Historical backfill completed",
        f"rows={meta.get('rows')}, start={meta.get('start_jst')}, end={meta.get('end_jst')}",
        emoji=":white_check_mark:",
    )
    print(f"Saved {len(df)} rows to {output_path}")
    if csv_path:
        print(f"CSV copy: {csv_path}")


if __name__ == "__main__":
    main()
