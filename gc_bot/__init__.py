"""GC bot package exposing high level entry points."""

from .config import CCXTConfig, SignalParams, OrderParams, SlackConfig, RunnerConfig, load_env_settings
from .state import BotState, StateStore
from .data import fetch_ohlcv_latest_ccxt, load_latest_cached_ccxt, add_sma_columns, detect_golden_cross_latest, update_state_after_signal
from .orders import place_market_buy, place_market_sell, close_if_reached_and_update
from .notifications import notify_gc, notify_entry, notify_close, notify_error, notify_daily_summary
from .metrics import write_daily_metrics
from .runner import run_hourly_cycle

__all__ = [
    "CCXTConfig",
    "SignalParams",
    "OrderParams",
    "SlackConfig",
    "RunnerConfig",
    "load_env_settings",
    "BotState",
    "StateStore",
    "fetch_ohlcv_latest_ccxt",
    "load_latest_cached_ccxt",
    "add_sma_columns",
    "detect_golden_cross_latest",
    "update_state_after_signal",
    "place_market_buy",
    "place_market_sell",
    "close_if_reached_and_update",
    "notify_gc",
    "notify_entry",
    "notify_close",
    "notify_error",
    "notify_daily_summary",
    "write_daily_metrics",
    "run_hourly_cycle",
]
