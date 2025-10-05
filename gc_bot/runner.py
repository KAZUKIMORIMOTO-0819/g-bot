"""Hourly runner orchestration."""

from __future__ import annotations

import os
import time
from dataclasses import asdict
from typing import Any, Dict

from .config import CCXTConfig, OrderParams, RunnerConfig, SignalParams, SlackConfig
from .data import add_sma_columns, detect_golden_cross_latest, fetch_ohlcv_latest_ccxt, update_state_after_signal
from .logging_utils import log_api_call, log_exception, setup_structured_logger, write_jsonl
from .notifications import notify_close, notify_entry, notify_error, notify_gc
from .orders import close_if_reached_and_update, place_market_buy
from .state import StateStore, should_open_from_signal, set_entry_from_order


def _env_or(default: Any, *keys: str) -> Any:
    for key in keys:
        val = os.getenv(key)
        if val:
            return val
    return default


def _effective_notional(cfg: RunnerConfig, state) -> float:
    """Determine notional size (JPY) for the next position."""
    base_notional = cfg.notional_jpy
    if cfg.notional_fraction is None:
        return base_notional
    current_capital = (cfg.initial_capital or 0.0) + float(getattr(state, "pnl_cum", 0.0) or 0.0)
    notional = max(current_capital * cfg.notional_fraction, 0.0)
    return notional if notional > 0 else base_notional


def run_hourly_cycle(cfg: RunnerConfig) -> Dict[str, Any]:
    """Execute single hourly cycle: fetch data, detect signals, trade actions, notifications."""
    logger = setup_structured_logger("runner")
    slack = SlackConfig()
    summary: Dict[str, Any] = {"stage": None}

    store = StateStore(cfg.state_path)
    try:
        with store as s:
            st = s.state

            t0 = time.time()
            ccxt_cfg = CCXTConfig(symbol=cfg.symbol, api_key=cfg.api_key, secret=cfg.secret)
            try:
                df = fetch_ohlcv_latest_ccxt(ccxt_cfg)
                ok = True
                resp_summary = {"rows": len(df)}
            except Exception as exc:
                ok = False
                resp_summary = {"error": str(exc)}
                log_exception(logger, "fetch_ohlcv_latest_ccxt", exc)
                notify_error(slack, "fetch", str(exc))
                raise
            finally:
                latency_ms = (time.time() - t0) * 1000.0
                log_api_call(
                    logger,
                    "ccxt.fetch_ohlcv_or_trades",
                    {"symbol": ccxt_cfg.symbol, "timeframe": ccxt_cfg.timeframe},
                    resp_summary,
                    ok,
                    latency_ms,
                )

            summary["stage"] = "fetched"
            write_jsonl({"type": "stage", "name": "fetched", "rows": len(df)})

            sig_params = SignalParams()
            if len(df) < sig_params.long_window:
                msg = (
                    f"Insufficient data rows ({len(df)}) for SMA computation; "
                    f"need at least {sig_params.long_window}."
                )
                logger.warning(msg)
                write_jsonl({"type": "stage", "name": "insufficient_data", "rows": len(df)})
                notify_error(slack, "signal", msg)
                summary["stage"] = "insufficient_data"
                summary["reason"] = msg
                return summary

            df_feat = add_sma_columns(df, sig_params)

            last_gc_ts = st.last_gc_bar_ts
            signal = detect_golden_cross_latest(df_feat, sig_params, last_signaled_bar_ts=last_gc_ts)
            summary["signal"] = {
                "is_gc": signal["is_gc"],
                "already": signal["already_signaled"],
                "bar_ts": str(signal["bar_ts"]),
                "price": signal["price"],
            }
            write_jsonl({"type": "signal", **summary["signal"]})

            if signal["is_gc"] and not signal["already_signaled"]:
                try:
                    notify_gc(
                        slack,
                        bar_ts=str(signal["bar_ts"]),
                        price=signal["price"],
                        sma_s=signal["sma_short"],
                        sma_l=signal["sma_long"],
                    )
                except Exception as exc:
                    log_exception(logger, "notify_gc", exc)

            order_res = None
            close_res = None

            if should_open_from_signal(asdict(st), signal):
                effective_notional = _effective_notional(cfg, st)
                order_params = OrderParams(
                    mode=cfg.mode,
                    notional_jpy=effective_notional,
                    slippage_bps=cfg.slippage_bps,
                    taker_fee_bps=cfg.taker_fee_bps,
                    api_key=_env_or(cfg.api_key, "BFX_API_KEY", "BITFLYER_API_KEY"),
                    secret=_env_or(cfg.secret, "BFX_API_SECRET", "BITFLYER_API_SECRET"),
                )
                try:
                    order_res = place_market_buy(cfg.symbol, signal["price"], order_params)
                    st = set_entry_from_order(st, order_res)
                    s.save(st)
                    summary["order"] = {k: order_res[k] for k in ["mode", "price", "size", "tp", "sl", "order_id", "notional_jpy"]}
                    write_jsonl({"type": "entry", **summary["order"]})
                    try:
                        notify_entry(slack, cfg.symbol, order_res["price"], order_res["size"], order_res["tp"], order_res["sl"])
                    except Exception as exc:
                        log_exception(logger, "notify_entry", exc)
                except Exception as exc:
                    log_exception(logger, "place_market_buy", exc)
                    notify_error(slack, "order_entry", str(exc))
                    raise
            else:
                current_close = float(df_feat.iloc[-1]["close"])
                effective_notional = _effective_notional(cfg, st)
                order_params = OrderParams(
                    mode=cfg.mode,
                    notional_jpy=effective_notional,
                    slippage_bps=cfg.slippage_bps,
                    taker_fee_bps=cfg.taker_fee_bps,
                    api_key=_env_or(cfg.api_key, "BFX_API_KEY", "BITFLYER_API_KEY"),
                    secret=_env_or(cfg.secret, "BFX_API_SECRET", "BITFLYER_API_SECRET"),
                )
                try:
                    res = close_if_reached_and_update(
                        current_price=current_close,
                        symbol=cfg.symbol,
                        params=order_params,
                        store=s,
                    )
                    if res is not None:
                        close_res = res
                        summary["close"] = {
                            "reason": res["reason"],
                            "price": res["close_result"]["price"],
                            "size": res["close_result"]["size"],
                            "pnl_jpy": res["pnl_jpy"],
                            "pnl_cum": res["state"].pnl_cum,
                        }
                        write_jsonl({"type": "close", **summary["close"]})
                        try:
                            notify_close(
                                slack,
                                res["reason"],
                                res["close_result"]["price"],
                                res["close_result"]["size"],
                                res["pnl_jpy"],
                                res["state"].pnl_cum,
                            )
                        except Exception as exc:
                            log_exception(logger, "notify_close", exc)
                except Exception as exc:
                    log_exception(logger, "close_if_reached_and_update", exc)
                    notify_error(slack, "order_close", str(exc))
                    raise

            st_dict = asdict(st)
            st_new_dict = update_state_after_signal(st_dict, signal)
            st.last_gc_bar_ts = st_new_dict.get("last_gc_bar_ts", st.last_gc_bar_ts)
            s.save(st)

            summary["state_meta"] = {
                "position": st.position,
                "pnl_cum": st.pnl_cum,
                "last_gc_bar_ts": st.last_gc_bar_ts,
            }
            write_jsonl({"type": "stage", "name": "done", **summary["state_meta"]})
            return summary
    except Exception as exc:
        try:
            notify_error(SlackConfig(), "runner", str(exc))
        except Exception:
            pass
        raise


__all__ = ["run_hourly_cycle"]
