"""Strategy modules combining golden cross and oscillator filters."""

from .gc_rsi import (
    GCAndRSIBacktestConfig,
    GCAndRSIStrategyParams,
    add_gc_rsi_features,
    compute_rsi,
    evaluate_gc_rsi_signal,
    run_backtest_gc_rsi,
)

__all__ = [
    "compute_rsi",
    "add_gc_rsi_features",
    "evaluate_gc_rsi_signal",
    "GCAndRSIStrategyParams",
    "GCAndRSIBacktestConfig",
    "run_backtest_gc_rsi",
]
