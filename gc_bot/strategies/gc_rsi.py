"""Golden-cross strategy extended with RSI confirmation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import math
import pandas as pd

from ..config import OrderParams, SignalParams
from ..data import add_sma_columns, detect_golden_cross_latest
from ..orders import compute_tp_sl, decide_order_size_jpy_to_amount
from ..backtest import BacktestResult, BacktestTrade


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Compute RSI values for a close price series."""
    if period <= 0:
        raise ValueError("RSI period must be positive")
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(to_replace=0.0, value=pd.NA)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.fillna(0.0)
    return rsi


@dataclass
class GCAndRSIStrategyParams:
    """Parameters combining GC detection with RSI filtering."""

    signal: SignalParams = field(default_factory=SignalParams)
    rsi_period: int = 14
    min_rsi: Optional[float] = 50.0
    max_rsi: Optional[float] = None
    rsi_column: str = "rsi"


@dataclass
class GCAndRSIBacktestConfig:
    """Backtest configuration for the GC + RSI strategy."""

    strategy: GCAndRSIStrategyParams = field(default_factory=GCAndRSIStrategyParams)
    order: OrderParams = field(default_factory=OrderParams)
    force_close_last: bool = True
    prefer_take_profit_when_overlap: bool = True
    initial_capital: float = 100000.0
    notional_fraction: Optional[float] = None


def add_gc_rsi_features(df: pd.DataFrame, params: GCAndRSIStrategyParams) -> pd.DataFrame:
    """Attach SMA and RSI columns required by the combined strategy."""
    if "close" not in df.columns:
        raise KeyError("DataFrame must include a 'close' column")
    df_feat = add_sma_columns(df, params.signal)
    df_feat[params.rsi_column] = compute_rsi(df_feat["close"], period=params.rsi_period)
    return df_feat


def _passes_rsi_filter(value: float, params: GCAndRSIStrategyParams) -> bool:
    if params.min_rsi is not None and value < params.min_rsi:
        return False
    if params.max_rsi is not None and value > params.max_rsi:
        return False
    return True


def evaluate_gc_rsi_signal(
    df: pd.DataFrame,
    last_signaled_bar_ts: Optional[pd.Timestamp],
    params: GCAndRSIStrategyParams,
) -> Dict[str, Any]:
    """Compute GC signal with RSI confirmation based on the provided data."""
    df_feat = add_gc_rsi_features(df, params)
    signal = detect_golden_cross_latest(df_feat, params.signal, last_signaled_bar_ts)
    latest_rsi = float(df_feat.iloc[-1][params.rsi_column])
    passes_filter = _passes_rsi_filter(latest_rsi, params)
    signal_details = dict(signal)
    signal_details["rsi"] = latest_rsi
    signal_details["passes_rsi_filter"] = passes_filter
    signal_details["should_enter"] = (
        signal_details["is_gc"]
        and not signal_details["already_signaled"]
        and passes_filter
    )
    return signal_details


def run_backtest_gc_rsi(
    df: pd.DataFrame,
    cfg: Optional[GCAndRSIBacktestConfig] = None,
) -> BacktestResult:
    """Run a backtest for the GC + RSI strategy using OHLC data."""
    if cfg is None:
        cfg = GCAndRSIBacktestConfig()

    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("DataFrame index must be a pandas.DatetimeIndex")
    if not df.index.is_monotonic_increasing:
        df = df.sort_index()

    data = add_gc_rsi_features(df, cfg.strategy)
    trades: list[BacktestTrade] = []
    equity_points: list[float] = []
    equity_index: list[pd.Timestamp] = []
    trade_returns: list[float] = []
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
            signal = detect_golden_cross_latest(window, cfg.strategy.signal, last_signaled_bar_ts)
        except ValueError:
            continue

        if signal["is_gc"]:
            last_signaled_bar_ts = signal["bar_ts"]

        bar = data.iloc[idx]
        latest_rsi = float(bar[cfg.strategy.rsi_column])
        passes_filter = _passes_rsi_filter(latest_rsi, cfg.strategy)

        if (
            open_trade is None
            and signal["is_gc"]
            and not signal["already_signaled"]
            and passes_filter
        ):
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
            slip = slippage_bps / 10000.0
            entry_price = ref_price * (1.0 + slip)
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
                "entry_rsi": latest_rsi,
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

        slip = slippage_bps / 10000.0
        exit_price = exit_reference_price * (1.0 - slip)
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
    rolling_max = equity_curve.cummax() if not equity_curve.empty else pd.Series(dtype=float)
    drawdown = equity_curve - rolling_max if not equity_curve.empty else pd.Series(dtype=float)
    max_dd = float(abs(drawdown.min())) if not drawdown.empty else 0.0

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
        "rsi_period": cfg.strategy.rsi_period,
        "min_rsi": cfg.strategy.min_rsi,
        "max_rsi": cfg.strategy.max_rsi,
    }

    return BacktestResult(trades=trades, equity_curve=equity_curve, summary=summary)


__all__ = [
    "compute_rsi",
    "add_gc_rsi_features",
    "evaluate_gc_rsi_signal",
    "GCAndRSIStrategyParams",
    "GCAndRSIBacktestConfig",
    "run_backtest_gc_rsi",
]
