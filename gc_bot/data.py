"""Data acquisition and signal computation utilities."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .config import CCXTConfig, SignalParams
from .timeutils import TZ_JST, TZ_UTC, floor_to_full_hour_utc

LOG_DIR = Path(os.getenv("GC_CANDLES_LOG_DIR", "./data/candles/logs"))
DATA_DIR = Path(os.getenv("GC_CANDLES_DATA_DIR", "./data/candles"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger(__name__)


def _init_exchange(cfg: CCXTConfig):
    """Initialise a ccxt exchange instance using config credentials."""
    import ccxt  # imported lazily to keep module import light

    klass = getattr(ccxt, cfg.exchange_id)
    return klass(
        {
            "apiKey": cfg.api_key or "",
            "secret": cfg.secret or "",
            "enableRateLimit": True,
            "timeout": cfg.timeout_ms,
        }
    )


def _fetch_ohlcv_direct(exchange, cfg: CCXTConfig, since_ms: int) -> List[List[Any]]:
    """Fetch OHLCV data directly via ccxt fetch_ohlcv."""
    return exchange.fetch_ohlcv(
        symbol=cfg.symbol,
        timeframe=cfg.timeframe,
        since=since_ms,
        limit=cfg.limit + 5,
    )


def _fetch_ohlcv_via_trades(exchange, cfg: CCXTConfig, since_ms: int, until_ms: int) -> List[List[Any]]:
    """Backfill OHLCV candles by aggregating trades when fetch_ohlcv is unavailable."""
    all_trades: List[Dict[str, Any]] = []
    cursor = since_ms
    while True:
        trades = exchange.fetch_trades(
            symbol=cfg.symbol,
            since=cursor,
            limit=cfg.trades_page_limit,
        )
        if not trades:
            break
        all_trades.extend(trades)
        last_ts = trades[-1]["timestamp"]
        if last_ts >= until_ms:
            break
        cursor = last_ts + 1
        rate_limit = getattr(exchange, "rateLimit", None)
        time.sleep(rate_limit / 1000.0 if rate_limit else 0.2)

    if not all_trades:
        return []

    tdf = pd.DataFrame(
        {"ts": t["timestamp"], "price": float(t["price"]), "amount": float(t["amount"])}
        for t in all_trades
    )
    period_ms = cfg.period_sec * 1000
    tdf["bucket"] = (tdf["ts"] // period_ms) * period_ms

    def ohlc_agg(group: pd.DataFrame) -> pd.Series:
        ordered = group.sort_values("ts")
        return pd.Series(
            {
                "open": ordered["price"].iloc[0],
                "high": group["price"].max(),
                "low": group["price"].min(),
                "close": ordered["price"].iloc[-1],
                "volume": group["amount"].sum(),
            }
        )

    odf = tdf.groupby("bucket").apply(ohlc_agg).reset_index().rename(columns={"bucket": "timestamp_ms"})
    odf = odf[(odf["timestamp_ms"] >= since_ms) & (odf["timestamp_ms"] <= until_ms)]
    return odf[["timestamp_ms", "open", "high", "low", "close", "volume"]].values.tolist()


def fetch_ohlcv_latest_ccxt(cfg: CCXTConfig) -> pd.DataFrame:
    """Fetch the latest confirmed OHLCV data and persist CSV/Parquet caches."""
    now_utc = pd.Timestamp.utcnow().to_pydatetime().replace(tzinfo=TZ_UTC)
    cutoff_utc = floor_to_full_hour_utc(now_utc)
    cutoff_ms = int(cutoff_utc.timestamp() * 1000)

    need = cfg.limit + 5
    span_ms = need * cfg.period_sec * 1000
    since_ms = cutoff_ms - span_ms

    exchange = _init_exchange(cfg)
    last_err: Optional[Exception] = None
    for attempt in range(1, cfg.max_retries + 1):
        try:
            exchange.load_markets()
            break
        except Exception as exc:
            last_err = exc
            time.sleep(cfg.retry_backoff_sec ** (attempt - 1))
    else:
        raise RuntimeError(f"load_markets failed after {cfg.max_retries} retries: {last_err}")

    if cfg.symbol not in exchange.symbols:
        raise ValueError(f"Symbol {cfg.symbol} not available on {cfg.exchange_id}")

    rows: List[List[Any]] = []
    has_direct = getattr(exchange, "has", {}).get("fetchOHLCV", False)
    fetch_func = _fetch_ohlcv_direct if has_direct else _fetch_ohlcv_via_trades
    args = (exchange, cfg, since_ms) if has_direct else (exchange, cfg, since_ms, cutoff_ms)

    for attempt in range(1, cfg.max_retries + 1):
        try:
            rows = fetch_func(*args)
            break
        except Exception as exc:
            last_err = exc
            time.sleep(cfg.retry_backoff_sec ** (attempt - 1))
    else:
        raise RuntimeError(f"Data fetch failed after {cfg.max_retries} retries: {last_err}")

    if not rows:
        raise ValueError("No OHLCV rows acquired")

    df = pd.DataFrame(rows, columns=["timestamp_ms", "open", "high", "low", "close", "volume"])
    df = df[df["timestamp_ms"] <= cutoff_ms].sort_values("timestamp_ms")
    if len(df) > cfg.limit:
        df = df.iloc[-cfg.limit :].copy()

    df["close_time_utc"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df["open_time_utc"] = df["close_time_utc"] - pd.to_timedelta(cfg.period_sec, unit="s")
    df["close_time_jst"] = df["close_time_utc"].dt.tz_convert(TZ_JST)
    df["open_time_jst"] = df["open_time_utc"].dt.tz_convert(TZ_JST)

    df = df.set_index("close_time_jst")[
        [
            "open_time_jst",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_time_utc",
            "close_time_utc",
            "timestamp_ms",
        ]
    ]

    ts_label = pd.Timestamp.now(tz=TZ_JST).strftime("%Y%m%d_%H%M%S")
    csv_path = LOG_DIR / f"xrpjpy_1h_{ts_label}.csv"
    df.to_csv(csv_path, encoding="utf-8")

    pq_path = DATA_DIR / "xrpjpy_1h_latest.parquet"
    parquet_ok = False
    try:
        import pyarrow  # noqa: F401  # pragma: no cover

        df.to_parquet(pq_path, index=True)
        parquet_ok = True
    except Exception as exc:
        logger.warning("Parquet save skipped: %s", exc)

    df.attrs["meta"] = {
        "exchange": cfg.exchange_id,
        "symbol": cfg.symbol,
        "timeframe": cfg.timeframe,
        "period_sec": cfg.period_sec,
        "limit": cfg.limit,
        "rows": len(df),
        "csv_path": str(csv_path),
        "parquet_path": str(pq_path) if parquet_ok else None,
        "cutoff_utc": cutoff_utc.isoformat(),
        "generated_at_jst": pd.Timestamp.now(tz=TZ_JST).isoformat(timespec="seconds"),
        "used_method": "fetchOHLCV" if has_direct else "trades_aggregate",
    }
    return df


def load_latest_cached_ccxt() -> pd.DataFrame:
    """Load cached ccxt OHLCV data from parquet or CSV logs."""
    pq_path = DATA_DIR / "xrpjpy_1h_latest.parquet"
    if pq_path.exists():
        return pd.read_parquet(pq_path)

    csv_files = sorted(p for p in LOG_DIR.glob("xrpjpy_1h_*.csv"))
    if not csv_files:
        raise FileNotFoundError("No cached parquet or CSV logs found")
    latest = csv_files[-1]
    return pd.read_csv(
        latest,
        parse_dates=["open_time_utc", "close_time_utc", "open_time_jst", "close_time_jst"],
        index_col="close_time_jst",
    )


def add_sma_columns(df: pd.DataFrame, params: SignalParams) -> pd.DataFrame:
    """Attach short/long SMAs to DataFrame derived from close prices."""
    if "close" not in df.columns:
        raise KeyError("DataFrame missing 'close' column")
    if len(df) < params.long_window:
        raise ValueError(f"Require >= {params.long_window} rows to compute SMA, found {len(df)}")
    out = df.copy()
    out["sma_short"] = out["close"].rolling(window=params.short_window, min_periods=params.short_window).mean()
    out["sma_long"] = out["close"].rolling(window=params.long_window, min_periods=params.long_window).mean()
    return out


def detect_golden_cross_latest(
    df_with_sma: pd.DataFrame,
    params: SignalParams,
    last_signaled_bar_ts: Optional[pd.Timestamp] = None,
) -> Dict[str, Any]:
    """Evaluate whether the latest bar produced a golden cross signal."""
    for col in ("sma_short", "sma_long", "close"):
        if col not in df_with_sma.columns:
            raise ValueError("df_with_sma must include sma_short, sma_long, close columns")
    if len(df_with_sma) < 2:
        raise ValueError("Need at least 2 bars for cross detection")

    latest = df_with_sma.iloc[-1]
    prev = df_with_sma.iloc[-2]
    bar_ts = df_with_sma.index[-1]

    if pd.isna(latest["sma_short"]) or pd.isna(latest["sma_long"]) or pd.isna(prev["sma_short"]) or pd.isna(prev["sma_long"]):
        raise ValueError("SMA contains NaN, ensure sufficient history")

    eps = params.epsilon
    crossed_up = (prev["sma_short"] <= prev["sma_long"] + eps) and (latest["sma_short"] > latest["sma_long"] + eps)

    already = False
    if last_signaled_bar_ts is not None and pd.to_datetime(last_signaled_bar_ts) == pd.to_datetime(bar_ts):
        already = True

    return {
        "is_gc": bool(crossed_up),
        "already_signaled": bool(already),
        "bar_ts": bar_ts,
        "price": float(latest["close"]),
        "sma_short": float(latest["sma_short"]),
        "sma_long": float(latest["sma_long"]),
        "prev_sma_short": float(prev["sma_short"]),
        "prev_sma_long": float(prev["sma_long"]),
    }


def update_state_after_signal(state: Dict[str, Any], signal: Dict[str, Any]) -> Dict[str, Any]:
    """Update plain state mapping with last GC timestamp after signal detection."""
    new_state = dict(state or {})
    if signal.get("is_gc", False):
        ts = signal.get("bar_ts")
        if hasattr(ts, "isoformat"):
            ts = ts.isoformat()
        new_state["last_gc_bar_ts"] = ts
    return new_state


__all__ = [
    "fetch_ohlcv_latest_ccxt",
    "load_latest_cached_ccxt",
    "add_sma_columns",
    "detect_golden_cross_latest",
    "update_state_after_signal",
    "LOG_DIR",
    "DATA_DIR",
]
