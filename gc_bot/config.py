"""Configuration dataclasses and helpers for the GC bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency
    load_dotenv = None


def load_env_settings(dotenv_path: Optional[str] = None) -> None:
    """Load environment variables from a .env file if python-dotenv is available."""
    if load_dotenv is None:
        return
    if dotenv_path:
        load_dotenv(dotenv_path)
    else:
        load_dotenv()


@dataclass
class CCXTConfig:
    """Configuration for fetching market data via ccxt."""

    exchange_id: str = "binance"
    symbol: str = "XRP/JPY"
    timeframe: str = "1h"
    period_sec: int = 3600
    limit: int = 200
    max_retries: int = 3
    retry_backoff_sec: float = 1.5
    timeout_ms: int = 15000
    trades_page_limit: int = 500
    api_key: Optional[str] = None
    secret: Optional[str] = None


@dataclass
class SignalParams:
    """Parameters for computing SMAs and detecting golden crosses."""

    short_window: int = 30
    long_window: int = 60
    epsilon: float = 1e-12


@dataclass
class OrderParams:
    """Parameters describing order execution behavior."""

    mode: str = "paper"
    notional_jpy: float = 5000.0
    slippage_bps: float = 5.0
    taker_fee_bps: float = 15.0
    api_key: Optional[str] = None
    secret: Optional[str] = None


@dataclass
class SlackConfig:
    """Slack notification settings."""

    webhook_url: Optional[str] = None
    username: str = "XRP GC Bot"
    icon_emoji: str = ":robot_face:"
    timeout_sec: int = 10
    max_retries: int = 3
    backoff_factor: float = 1.6

    def resolved_url(self) -> Optional[str]:
        """Return webhook URL from explicit value or environment variable."""
        return self.webhook_url or os.getenv("SLACK_WEBHOOK_URL")


@dataclass
class RunnerConfig:
    """Configuration for hourly runner execution."""

    mode: str = "paper"
    symbol: str = "XRP/JPY"
    state_path: str = "./data/state/state.json"
    notional_jpy: float = 5000.0
    slippage_bps: float = 5.0
    taker_fee_bps: float = 15.0
    api_key: Optional[str] = None
    secret: Optional[str] = None
    initial_capital: float = 100000.0
    notional_fraction: Optional[float] = None


__all__ = [
    "CCXTConfig",
    "SignalParams",
    "OrderParams",
    "SlackConfig",
    "RunnerConfig",
    "load_env_settings",
]
