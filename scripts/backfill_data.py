"""Utility to fetch and persist the latest OHLCV cache."""

from __future__ import annotations

import argparse

from gc_bot import CCXTConfig, SlackConfig, load_env_settings
from gc_bot.data import fetch_ohlcv_latest_ccxt
from gc_bot.notifications import notify_runner_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill latest OHLCV via ccxt")
    parser.add_argument("--exchange", default="bitflyer", help="ccxt exchange id")
    parser.add_argument("--symbol", default="XRP/JPY", help="Symbol to download")
    parser.add_argument("--timeframe", default="1h", help="OHLCV timeframe")
    parser.add_argument("--limit", type=int, default=200, help="Number of bars to collect")
    return parser.parse_args()


def main() -> None:
    load_env_settings()
    args = parse_args()
    cfg = CCXTConfig(exchange_id=args.exchange, symbol=args.symbol, timeframe=args.timeframe, limit=args.limit)
    slack_cfg = SlackConfig()
    notify_runner_status(
        slack_cfg,
        "GC Bot Backfill started",
        f"exchange={cfg.exchange_id}, symbol={cfg.symbol}, timeframe={cfg.timeframe}, limit={cfg.limit}",
    )
    try:
        df = fetch_ohlcv_latest_ccxt(cfg)
    except Exception as exc:
        notify_runner_status(
            slack_cfg,
            "GC Bot Backfill failed",
            f"{type(exc).__name__}: {exc}",
            emoji=":x:",
        )
        raise
    meta = df.attrs.get("meta", {})
    print(f"Saved {len(df)} rows to {meta.get('csv_path')} (parquet={meta.get('parquet_path')})")
    notify_runner_status(
        slack_cfg,
        "GC Bot Backfill completed",
        f"rows={len(df)}, csv={meta.get('csv_path')}",
        emoji=":white_check_mark:",
    )


if __name__ == "__main__":
    main()
