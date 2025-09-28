"""State management primitives for the GC bot."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from .timeutils import TZ_JST, now_jst

logger = logging.getLogger(__name__)


@dataclass
class BotState:
    """Serializable bot state persisted between runs."""

    position: str = "FLAT"  # "FLAT" or "LONG"
    entry_price: float = 0.0
    size: float = 0.0
    tp: float = 0.0
    sl: float = 0.0
    pnl_cum: float = 0.0
    streak_loss: int = 0
    last_gc_bar_ts: Optional[str] = None
    entry_ts_jst: Optional[str] = None
    last_updated_jst: Optional[str] = None
    last_daily_summary_date: Optional[str] = None


class StateStore:
    """File-backed state store with naive lock file for mutual exclusion."""

    def __init__(self, path: str):
        self.path = path
        self.backup_path = f"{path}.bak"
        self.lock_path = f"{path}.lock"
        self._lock_token: Optional[str] = None
        self.state: BotState = BotState()
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)

    def acquire_lock(self, timeout_sec: float = 5.0) -> bool:
        """Try to obtain the lock file within the timeout."""
        start = time.time()
        token = str(uuid.uuid4())
        while time.time() - start < timeout_sec:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as handle:
                    handle.write(token)
                self._lock_token = token
                return True
            except FileExistsError:
                time.sleep(0.1)
        return False

    def release_lock(self) -> None:
        """Release the lock if owned."""
        try:
            if not os.path.exists(self.lock_path):
                return
            with open(self.lock_path, "r", encoding="utf-8") as handle:
                current = handle.read().strip()
            if current == (self._lock_token or ""):
                os.remove(self.lock_path)
        except Exception:  # best effort
            pass
        finally:
            self._lock_token = None

    def load(self) -> BotState:
        """Load persisted state or return defaults."""
        if not os.path.exists(self.path):
            return BotState(last_updated_jst=now_jst().isoformat(timespec="seconds"))
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            base = asdict(BotState())
            base.update(payload or {})
            return BotState(**base)
        except Exception as exc:  # fallback to backup
            logger.warning("state.json read failed: %s. Attempting backup restore.", exc)
            if os.path.exists(self.backup_path):
                with open(self.backup_path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                base = asdict(BotState())
                base.update(payload or {})
                return BotState(**base)
            return BotState(last_updated_jst=now_jst().isoformat(timespec="seconds"))

    def save(self, state: BotState) -> None:
        """Atomically persist state to disk and refresh backup."""
        payload = asdict(state)
        payload["last_updated_jst"] = now_jst().isoformat(timespec="seconds")
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        if os.path.exists(self.path):
            shutil.copy2(self.path, self.backup_path)
        os.replace(tmp_path, self.path)

    def __enter__(self) -> "StateStore":
        if not self.acquire_lock(timeout_sec=5.0):
            raise RuntimeError("Failed to acquire state.json lock")
        self.state = self.load()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release_lock()


def ensure_single_position(state: BotState) -> None:
    """Validate that the state obeys the single position constraints."""
    if state.position not in ("FLAT", "LONG"):
        raise ValueError(f"Unexpected position value: {state.position}")
    if state.position == "FLAT":
        if any(getattr(state, key) for key in ("size", "entry_price", "tp", "sl")):
            logger.warning("State FLAT but entry fields populated; they will be cleared later.")
    else:
        required = {
            "entry_price": state.entry_price,
            "size": state.size,
            "tp": state.tp,
            "sl": state.sl,
        }
        for key, value in required.items():
            if value is None or float(value) <= 0:
                raise ValueError(f"LONG position missing required field {key}: {value}")


def set_entry_from_order(state: BotState, order_result: Dict[str, Any]) -> BotState:
    """Apply order result to state when opening a position."""
    if state.position != "FLAT":
        raise ValueError("Cannot open new position when already in LONG state")
    state.position = "LONG"
    state.entry_price = float(order_result["price"])
    state.size = float(order_result["size"])
    state.tp = float(order_result["tp"])
    state.sl = float(order_result["sl"])
    state.entry_ts_jst = now_jst().isoformat(timespec="seconds")
    ensure_single_position(state)
    return state


def clear_to_flat(state: BotState) -> BotState:
    """Reset state to flat after closing a position."""
    state.position = "FLAT"
    state.entry_price = 0.0
    state.size = 0.0
    state.tp = 0.0
    state.sl = 0.0
    state.entry_ts_jst = None
    ensure_single_position(state)
    return state


def bump_streak(state: BotState, won: bool) -> BotState:
    """Update losing streak counter based on trade outcome."""
    if won:
        state.streak_loss = 0
    else:
        state.streak_loss = int(state.streak_loss or 0) + 1
    return state


def touch_daily_summary_marker(state: BotState) -> BotState:
    """Mark that today's daily summary was already sent."""
    state.last_daily_summary_date = now_jst().strftime("%Y-%m-%d")
    return state


def update_state_on_entry(state_dict: Dict[str, Any], order_result: Dict[str, Any]) -> Dict[str, Any]:
    """Dictionary-based state helper retained for compatibility with legacy flows."""
    new_state = dict(state_dict or {})
    new_state["position"] = "LONG"
    new_state["entry_price"] = float(order_result["price"])
    new_state["size"] = float(order_result["size"])
    new_state["tp"] = float(order_result["tp"])
    new_state["sl"] = float(order_result["sl"])
    new_state["pnl_cum"] = float(new_state.get("pnl_cum", 0.0))
    new_state["entry_ts_jst"] = now_jst().isoformat(timespec="seconds")
    return new_state


def should_open_from_signal(state_dict: Dict[str, Any], signal: Dict[str, Any]) -> bool:
    """Return True when GC signal suggests opening a new position."""
    position = (state_dict or {}).get("position", "FLAT")
    return position == "FLAT" and bool(signal.get("is_gc")) and not bool(signal.get("already_signaled"))


__all__ = [
    "BotState",
    "StateStore",
    "ensure_single_position",
    "set_entry_from_order",
    "clear_to_flat",
    "bump_streak",
    "touch_daily_summary_marker",
    "update_state_on_entry",
    "should_open_from_signal",
]
