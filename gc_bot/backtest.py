"""Backtesting utilities for the GC bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import math
import pandas as pd

from .config import OrderParams, SignalParams
from .data import add_sma_columns, detect_golden_cross_latest
from .orders import compute_tp_sl, decide_order_size_jpy_to_amount


@dataclass
class BacktestTrade:
    """Represents a single completed trade in the backtest."""

    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_price: float
    exit_price: float
    size: float
    pnl_jpy: float
    reason: str
    duration_bars: int
    entry_fee_jpy: float
    exit_fee_jpy: float
    notional_jpy: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_ts": self.entry_ts.isoformat() if hasattr(self.entry_ts, "isoformat") else self.entry_ts,
            "exit_ts": self.exit_ts.isoformat() if hasattr(self.exit_ts, "isoformat") else self.exit_ts,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "size": self.size,
            "pnl_jpy": self.pnl_jpy,
            "reason": self.reason,
            "duration_bars": self.duration_bars,
            "entry_fee_jpy": self.entry_fee_jpy,
            "exit_fee_jpy": self.exit_fee_jpy,
            "notional_jpy": self.notional_jpy,
        }


@dataclass
class BacktestResult:
    """Container for backtest output."""

    trades: List[BacktestTrade]
    equity_curve: pd.Series
    summary: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trades": [trade.to_dict() for trade in self.trades],
            "equity_curve": self.equity_curve.to_dict() if hasattr(self.equity_curve, "to_dict") else self.equity_curve,
            "summary": self.summary,
        }


@dataclass
class BacktestConfig:
    """Configuration for running a GC-bot backtest."""

    signal: SignalParams = field(default_factory=SignalParams)
    order: OrderParams = field(default_factory=OrderParams)
    force_close_last: bool = True
    prefer_take_profit_when_overlap: bool = True
    initial_capital: float = 100000.0
    notional_fraction: Optional[float] = None


def _apply_slippage(price: float, slippage_bps: float, side: str) -> float:
    slip = slippage_bps / 10000.0
    if side == "buy":
        return price * (1.0 + slip)
    if side == "sell":
        return price * (1.0 - slip)
    raise ValueError("side must be 'buy' or 'sell'")


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    rolling_max = equity.cummax()
    drawdown = equity - rolling_max
    return float(abs(drawdown.min()))


def run_backtest(df: pd.DataFrame, cfg: Optional[BacktestConfig] = None) -> BacktestResult:
    """Run a golden-cross backtest over historical OHLC data."""

    if cfg is None:
        cfg = BacktestConfig()

    required_cols = {"open", "high", "low", "close"}
    if not required_cols.issubset(df.columns):
        missing = ", ".join(sorted(required_cols - set(df.columns)))
        raise ValueError(f"DataFrame missing required columns: {missing}")

    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("DataFrame index must be a pandas.DatetimeIndex")

    if not df.index.is_monotonic_increasing:
        df = df.sort_index()

    data = add_sma_columns(df, cfg.signal)
    trades: List[BacktestTrade] = []
    equity_points: List[float] = []
    equity_index: List[pd.Timestamp] = []
    trade_returns: List[float] = []
    open_trade: Optional[Dict[str, Any]] = None
    last_signaled_bar_ts: Optional[pd.Timestamp] = None
    fee_rate = cfg.order.taker_fee_bps / 10000.0
    slippage_bps = cfg.order.slippage_bps
    cumulative_pnl = 0.0

    for idx, ts in enumerate(data.index):
        if idx < 1:
            continue

        window = data.iloc[: idx + 1]
        try:
            signal = detect_golden_cross_latest(window, cfg.signal, last_signaled_bar_ts)
        except ValueError:
            continue

        if signal["is_gc"]:
            last_signaled_bar_ts = signal["bar_ts"]

        bar = data.iloc[idx]

        if open_trade is None and signal["is_gc"] and not signal["already_signaled"]:
            ref_price = float(bar["close"])
            current_capital = cfg.initial_capital + cumulative_pnl
            effective_notional = cfg.order.notional_jpy
            if cfg.notional_fraction is not None:
                effective_notional = max(current_capital * cfg.notional_fraction, 0.0)
            if effective_notional <= 0:
                continue
            size = decide_order_size_jpy_to_amount(ref_price, effective_notional)
            if size <= 0:
                continue
            entry_price = _apply_slippage(ref_price, slippage_bps, side="buy")
            tp, sl = compute_tp_sl(entry_price)
            entry_notional = entry_price * size
            entry_fee = entry_notional * fee_rate
            open_trade = {
                "entry_ts": ts,
                "entry_index": idx,
                "entry_price": entry_price,
                "size": size,
                "tp": tp,
                "sl": sl,
                "entry_fee": entry_fee,
                "notional_jpy": effective_notional,
            }
            continue

        if open_trade is None:
            continue

        if idx <= open_trade["entry_index"]:
            continue

        high = float(bar["high"])
        low = float(bar["low"])
        reason: Optional[str] = None
        exit_reference_price: Optional[float] = None

        hits_tp = high >= open_trade["tp"]
        hits_sl = low <= open_trade["sl"]

        if hits_tp and hits_sl:
            if cfg.prefer_take_profit_when_overlap:
                reason = "TP"
                exit_reference_price = open_trade["tp"]
            else:
                reason = "SL"
                exit_reference_price = open_trade["sl"]
        elif hits_tp:
            reason = "TP"
            exit_reference_price = open_trade["tp"]
        elif hits_sl:
            reason = "SL"
            exit_reference_price = open_trade["sl"]
        elif cfg.force_close_last and idx == len(data.index) - 1:
            reason = "EOD"
            exit_reference_price = float(bar["close"])

        if reason is None or exit_reference_price is None:
            continue

        exit_price = _apply_slippage(exit_reference_price, slippage_bps, side="sell")
        size = open_trade["size"]
        exit_notional = exit_price * size
        exit_fee = exit_notional * fee_rate
        pnl = (exit_price - open_trade["entry_price"]) * size - open_trade["entry_fee"] - exit_fee
        cumulative_pnl += pnl

        trade = BacktestTrade(
            entry_ts=open_trade["entry_ts"],
            exit_ts=ts,
            entry_price=float(open_trade["entry_price"]),
            exit_price=float(exit_price),
            size=float(size),
            pnl_jpy=float(pnl),
            reason=reason,
            duration_bars=idx - open_trade["entry_index"],
            entry_fee_jpy=float(open_trade["entry_fee"]),
            exit_fee_jpy=float(exit_fee),
            notional_jpy=float(open_trade.get("notional_jpy", 0.0)),
        )
        trades.append(trade)
        equity_points.append(cumulative_pnl)
        equity_index.append(ts)
        if trade.notional_jpy > 0:
            trade_returns.append(trade.pnl_jpy / trade.notional_jpy)
        open_trade = None

    equity_curve = (
        pd.Series(equity_points, index=pd.DatetimeIndex(equity_index)) if equity_index else pd.Series(dtype=float)
    )

    wins = sum(1 for t in trades if t.pnl_jpy >= 0)
    losses = sum(1 for t in trades if t.pnl_jpy < 0)
    total = len(trades)
    win_rate = (wins / total * 100.0) if total else 0.0
    max_dd = _max_drawdown(equity_curve)

    if trade_returns:
        mean_return = sum(trade_returns) / len(trade_returns)
        if len(trade_returns) > 1:
            variance = sum((r - mean_return) ** 2 for r in trade_returns) / (len(trade_returns) - 1)
            std_dev = math.sqrt(variance)
        else:
            std_dev = 0.0
        sharpe_ratio = (mean_return / std_dev * math.sqrt(len(trade_returns))) if std_dev > 0 else 0.0
    else:
        mean_return = 0.0
        std_dev = 0.0
        sharpe_ratio = 0.0

    final_capital = cfg.initial_capital + cumulative_pnl
    total_return_pct = ((final_capital / cfg.initial_capital) - 1.0) * 100.0 if cfg.initial_capital > 0 else 0.0
    max_dd_pct = (max_dd / cfg.initial_capital * 100.0) if cfg.initial_capital > 0 else 0.0

    summary = {
        "trades": total,
        "win": wins,
        "loss": losses,
        "win_rate": round(win_rate, 2),
        "pnl_total": round(cumulative_pnl, 2),
        "pnl_per_trade": round(cumulative_pnl / total, 2) if total else 0.0,
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "capital_initial": round(cfg.initial_capital, 2),
        "capital_final": round(final_capital, 2),
        "total_return_pct": round(total_return_pct, 2),
        "sharpe_ratio": round(sharpe_ratio, 4),
        "mean_trade_return": round(mean_return, 4),
        "trade_return_std": round(std_dev, 4),
        "notional_fraction": cfg.notional_fraction,
        "notional_static": cfg.order.notional_jpy,
    }

    return BacktestResult(trades=trades, equity_curve=equity_curve, summary=summary)


__all__ = [
    "BacktestConfig",
    "BacktestTrade",
    "BacktestResult",
    "run_backtest",
]
