"""Structured logging helpers for the GC bot."""

from __future__ import annotations

import json
import logging
import os
import traceback
from typing import Any, Dict

import pandas as pd

from .timeutils import now_jst
from .orders import TRADES_DIR, _ensure_tradelog  # type: ignore

LOG_DIR = os.path.join(os.getenv("GC_APP_LOG_DIR", "./data/logs"))
JSONL_DIR = os.path.join(os.getenv("GC_JSONL_DIR", "./data/logs/jsonl"))
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(JSONL_DIR, exist_ok=True)


def setup_structured_logger(name: str = "bot") -> logging.Logger:
    """Return a logger configured with console and file handlers."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(stream_handler)

    file_path = os.path.join(LOG_DIR, "app.log")
    file_handler = logging.FileHandler(file_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)

    logger.info("Structured logger initialized.")
    return logger


def _jsonl_path_for_today() -> str:
    return os.path.join(JSONL_DIR, now_jst().strftime("%Y%m%d") + ".jsonl")


def write_jsonl(event: Dict[str, Any]) -> None:
    payload = dict(event or {})
    payload.setdefault("ts_jst", now_jst().isoformat(timespec="seconds"))
    path = _jsonl_path_for_today()
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def log_api_call(
    logger: logging.Logger,
    name: str,
    request: Dict[str, Any],
    response: Dict[str, Any],
    ok: bool,
    latency_ms: float,
) -> None:
    level = logging.INFO if ok else logging.WARNING
    logger.log(level, "%s ok=%s latency_ms=%.1f", name, ok, latency_ms)
    write_jsonl(
        {
            "type": "api_call",
            "name": name,
            "ok": ok,
            "latency_ms": latency_ms,
            "request": request,
            "response": response,
        }
    )


def log_exception(logger: logging.Logger, context: str, exc: Exception) -> None:
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error("Exception in %s: %s", context, exc)
    write_jsonl(
        {
            "type": "exception",
            "context": context,
            "error": str(exc),
            "traceback": trace,
        }
    )


def append_trade_log(
    side: str,
    price: float,
    size: float,
    mode: str = "paper",
    symbol: str = "XRP/JPY",
    fee_jpy: float = 0.0,
    slippage_bps: Any = None,
    taker_fee_bps: Any = None,
    order_id: Any = None,
    extra: Dict[str, Any] | None = None,
) -> None:
    _ensure_tradelog()
    path = os.path.join(TRADES_DIR, "trades.csv")
    row = {
        "ts_jst": now_jst().isoformat(timespec="seconds"),
        "mode": mode,
        "symbol": symbol,
        "side": side,
        "size": round(float(size), 8),
        "price": round(float(price), 8),
        "notional_jpy": round(float(price) * float(size), 2),
        "fee_jpy": round(float(fee_jpy), 2),
        "slippage_bps": slippage_bps,
        "taker_fee_bps": taker_fee_bps,
        "tp": None,
        "sl": None,
        "order_id": order_id or f"LOG-{int(now_jst().timestamp())}",
        "raw": json.dumps(extra or {}, ensure_ascii=False),
    }
    df = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame()
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(path, index=False, encoding="utf-8")
    write_jsonl({"type": "trade_log", **row})


__all__ = [
    "setup_structured_logger",
    "write_jsonl",
    "log_api_call",
    "log_exception",
    "append_trade_log",
    "LOG_DIR",
    "JSONL_DIR",
]
