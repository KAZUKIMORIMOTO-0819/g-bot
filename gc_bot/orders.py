"""Order execution and trade bookkeeping utilities."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from .config import OrderParams
from .state import (
    BotState,
    StateStore,
    bump_streak,
    clear_to_flat,
)
from .timeutils import now_jst

TRADES_DIR = os.path.join(os.getenv("GC_TRADES_DIR", "./data/trades"))
os.makedirs(TRADES_DIR, exist_ok=True)


def decide_order_size_jpy_to_amount(price: float, notional_jpy: float) -> float:
    """Convert notional JPY value to instrument amount."""
    if price <= 0:
        raise ValueError("price must be positive")
    return float(notional_jpy / price)


def _fit_amount_to_market(exchange, symbol: str, amount: float) -> float:
    """Adjust amount to meet exchange precision/limits."""
    market = exchange.market(symbol)
    min_amt = (market.get("limits", {}).get("amount", {}) or {}).get("min", 0.0) or 0.0
    precision = (market.get("precision", {}) or {}).get("amount", None)
    if isinstance(precision, int):
        quant = 10 ** precision
        adj = (int(amount * quant)) / quant
    else:
        adj = amount
    if min_amt and adj < min_amt:
        return 0.0
    return float(adj)


def _ensure_tradelog() -> str:
    """Ensure trades.csv exists and return its path."""
    path = os.path.join(TRADES_DIR, "trades.csv")
    if not os.path.exists(path):
        cols = [
            "ts_jst",
            "mode",
            "symbol",
            "side",
            "size",
            "price",
            "notional_jpy",
            "fee_jpy",
            "slippage_bps",
            "taker_fee_bps",
            "tp",
            "sl",
            "order_id",
            "raw",
        ]
        pd.DataFrame(columns=cols).to_csv(path, index=False, encoding="utf-8")
    return path


def _append_trade_row(row: Dict[str, Any]) -> None:
    path = _ensure_tradelog()
    df = pd.read_csv(path)
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(path, index=False, encoding="utf-8")


def _append_trade_row_close(row: Dict[str, Any]) -> None:
    path = _ensure_tradelog()
    df = pd.read_csv(path)
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(path, index=False, encoding="utf-8")


def compute_tp_sl(entry_price: float, tp_pct: float = 0.02, sl_pct: float = 0.03) -> Tuple[float, float]:
    """Return take-profit and stop-loss price levels for long positions."""
    tp = entry_price * (1.0 + tp_pct)
    sl = entry_price * (1.0 - sl_pct)
    return float(tp), float(sl)


def _init_ccxt_for_real(api_key: Optional[str], secret: Optional[str]):
    import ccxt

    return ccxt.bitflyer(
        {
            "apiKey": api_key or "",
            "secret": secret or "",
            "enableRateLimit": True,
            "timeout": 15000,
        }
    )


def place_market_buy(
    symbol: str,
    ref_price: float,
    params: OrderParams,
    exchange_for_real: Optional[Any] = None,
) -> Dict[str, Any]:
    """Execute a market buy for paper or real trading modes."""
    ts_jst = now_jst().isoformat(timespec="seconds")
    side = "buy"

    size = decide_order_size_jpy_to_amount(ref_price, params.notional_jpy)

    if params.mode == "paper":
        slip = params.slippage_bps / 10000.0
        fee_rate = params.taker_fee_bps / 10000.0
        fill_price = ref_price * (1.0 + slip)
        notional = fill_price * size
        fee_jpy = notional * fee_rate
        order_id = f"PAPER-{int(time.time())}"
        raw = {"reason": "paper_fill", "ref_price": ref_price}
    elif params.mode == "real":
        if exchange_for_real is None:
            exchange_for_real = _init_ccxt_for_real(params.api_key, params.secret)
        exchange_for_real.load_markets()
        adj_size = _fit_amount_to_market(exchange_for_real, symbol, size)
        if adj_size <= 0:
            raise ValueError("Calculated order size below exchange min/precision")
        order = exchange_for_real.create_order(symbol=symbol, type="market", side="buy", amount=adj_size)
        order_id = order.get("id")
        filled = float(order.get("filled", adj_size))
        avg = float(order.get("average") or order.get("price") or ref_price)
        fill_price = avg
        size = filled
        notional = fill_price * size
        fee_cost = None
        if order.get("fees"):
            try:
                fee_cost = float(order["fees"][0].get("cost", 0.0))
            except Exception:
                fee_cost = None
        if fee_cost is None:
            fee_cost = notional * (params.taker_fee_bps / 10000.0)
        fee_jpy = fee_cost
        raw = order
    else:
        raise ValueError("OrderParams.mode must be 'paper' or 'real'")

    tp, sl = compute_tp_sl(fill_price, tp_pct=0.02, sl_pct=0.03)
    row = {
        "ts_jst": ts_jst,
        "mode": params.mode,
        "symbol": symbol,
        "side": side,
        "size": round(size, 8),
        "price": round(fill_price, 8),
        "notional_jpy": round(notional, 2),
        "fee_jpy": round(fee_jpy, 2),
        "slippage_bps": params.slippage_bps,
        "taker_fee_bps": params.taker_fee_bps,
        "tp": round(tp, 8),
        "sl": round(sl, 8),
        "order_id": order_id,
        "raw": json.dumps(raw, ensure_ascii=False),
    }
    _append_trade_row(row)

    return {
        "mode": params.mode,
        "symbol": symbol,
        "side": side,
        "size": float(row["size"]),
        "price": float(row["price"]),
        "notional_jpy": float(row["notional_jpy"]),
        "fee_jpy": float(row["fee_jpy"]),
        "tp": float(row["tp"]),
        "sl": float(row["sl"]),
        "order_id": order_id,
        "raw": raw,
    }


def place_market_sell(
    symbol: str,
    size: float,
    ref_price: float,
    params: OrderParams,
    exchange_for_real: Optional[Any] = None,
) -> Dict[str, Any]:
    """Execute a market sell for closing positions."""
    ts_jst = now_jst().isoformat(timespec="seconds")
    side = "sell"

    if params.mode == "paper":
        slip = params.slippage_bps / 10000.0
        fee_rate = params.taker_fee_bps / 10000.0
        fill_price = ref_price * (1.0 - slip)
        notional = fill_price * size
        fee_jpy = notional * fee_rate
        order_id = f"PAPER-{int(time.time())}"
        raw = {"reason": "paper_fill_close", "ref_price": ref_price}
    elif params.mode == "real":
        if exchange_for_real is None:
            exchange_for_real = _init_ccxt_for_real(params.api_key, params.secret)
        exchange_for_real.load_markets()
        adj_size = _fit_amount_to_market(exchange_for_real, symbol, size)
        if adj_size <= 0:
            raise ValueError("Calculated close size below exchange min/precision")
        order = exchange_for_real.create_order(symbol=symbol, type="market", side="sell", amount=adj_size)
        order_id = order.get("id")
        filled = float(order.get("filled", adj_size))
        avg = float(order.get("average") or order.get("price") or ref_price)
        fill_price = avg
        notional = fill_price * filled
        fee_cost = None
        if order.get("fees"):
            try:
                fee_cost = float(order["fees"][0].get("cost", 0.0))
            except Exception:
                fee_cost = None
        if fee_cost is None:
            fee_cost = notional * (params.taker_fee_bps / 10000.0)
        fee_jpy = fee_cost
        size = filled
        raw = order
    else:
        raise ValueError("OrderParams.mode must be 'paper' or 'real'")

    row = {
        "ts_jst": ts_jst,
        "mode": params.mode,
        "symbol": symbol,
        "side": side,
        "size": round(size, 8),
        "price": round(fill_price, 8),
        "notional_jpy": round(notional, 2),
        "fee_jpy": round(fee_jpy, 2),
        "slippage_bps": params.slippage_bps,
        "taker_fee_bps": params.taker_fee_bps,
        "tp": None,
        "sl": None,
        "order_id": order_id,
        "raw": json.dumps(raw, ensure_ascii=False),
    }
    _append_trade_row_close(row)

    return {
        "mode": params.mode,
        "symbol": symbol,
        "side": side,
        "size": float(row["size"]),
        "price": float(row["price"]),
        "notional_jpy": float(row["notional_jpy"]),
        "fee_jpy": float(row["fee_jpy"]),
        "order_id": order_id,
        "raw": raw,
    }


def is_exit_reached(current_price: float, tp: float, sl: float) -> Tuple[bool, Optional[str]]:
    """Return (True, reason) when TP or SL is hit."""
    if current_price >= tp > 0:
        return True, "TP"
    if 0 < sl >= current_price:
        return True, "SL"
    return False, None


def find_last_buy_fee_from_trades(symbol: str) -> float:
    """Fetch last buy fee from trades log for the given symbol."""
    path = _ensure_tradelog()
    df = pd.read_csv(path)
    df = df[(df["symbol"] == symbol) & (df["side"] == "buy")]
    if df.empty:
        return 0.0
    return float(df.iloc[-1]["fee_jpy"] or 0.0)


def append_trade_outcome_row(symbol: str, reason: str, entry_price: float, exit_price: float, size: float, pnl_jpy: float) -> None:
    """Append summary row capturing trade outcome."""
    row = {
        "ts_jst": now_jst().isoformat(timespec="seconds"),
        "mode": "summary",
        "symbol": symbol,
        "side": reason,
        "size": round(size, 8),
        "price": round(exit_price, 8),
        "notional_jpy": round(exit_price * size, 2),
        "fee_jpy": round(0.0, 2),
        "slippage_bps": None,
        "taker_fee_bps": None,
        "tp": None,
        "sl": None,
        "order_id": f"SUMMARY-{int(time.time())}",
        "raw": json.dumps(
            {"entry_price": entry_price, "exit_price": exit_price, "pnl_jpy": pnl_jpy},
            ensure_ascii=False,
        ),
    }
    _append_trade_row_close(row)


def realize_pnl_and_update_state(
    state: BotState,
    close_result: Dict[str, Any],
    buy_fee_jpy: float,
) -> Tuple[BotState, float]:
    """Compute realized PnL, update cumulative totals, reset position."""
    exit_price = float(close_result["price"])
    size = float(close_result["size"])
    sell_fee = float(close_result.get("fee_jpy", 0.0))
    entry_price = float(state.entry_price)

    pnl = (exit_price - entry_price) * size - (buy_fee_jpy + sell_fee)

    state.pnl_cum = float(state.pnl_cum or 0.0) + pnl
    won = pnl >= 0.0
    state = bump_streak(state, won=won)
    state = clear_to_flat(state)
    return state, float(pnl)


def close_if_reached_and_update(
    current_price: float,
    symbol: str,
    params: OrderParams,
    store: StateStore,
    exchange_for_real: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Check TP/SL and close position if required, updating state and logs."""
    st = store.state
    if st.position != "LONG":
        return None

    reached, reason = is_exit_reached(current_price, tp=st.tp, sl=st.sl)
    if not reached:
        return None

    close_res = place_market_sell(
        symbol=symbol,
        size=st.size,
        ref_price=current_price,
        params=params,
        exchange_for_real=exchange_for_real,
    )

    buy_fee = find_last_buy_fee_from_trades(symbol)
    st_after, pnl_jpy = realize_pnl_and_update_state(st, close_res, buy_fee_jpy=buy_fee)

    append_trade_outcome_row(
        symbol=symbol,
        reason=reason,
        entry_price=st.entry_price,
        exit_price=close_res["price"],
        size=st.size,
        pnl_jpy=pnl_jpy,
    )

    store.save(st_after)

    return {
        "reason": reason,
        "close_result": close_res,
        "pnl_jpy": pnl_jpy,
        "state": st_after,
    }


__all__ = [
    "decide_order_size_jpy_to_amount",
    "compute_tp_sl",
    "place_market_buy",
    "place_market_sell",
    "close_if_reached_and_update",
    "is_exit_reached",
    "find_last_buy_fee_from_trades",
    "append_trade_outcome_row",
    "realize_pnl_and_update_state",
    "TRADES_DIR",
]
