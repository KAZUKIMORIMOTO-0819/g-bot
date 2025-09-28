"""Metrics and analytics utilities."""

from __future__ import annotations

import json
import os
from typing import Any, Dict

import pandas as pd

from .logging_utils import write_jsonl
from .timeutils import TZ_JST, now_jst

METRICS_DIR = os.path.join(os.getenv("GC_METRICS_DIR", "./data/metrics"))
os.makedirs(METRICS_DIR, exist_ok=True)


def _ensure_metrics_log() -> str:
    path = os.path.join(METRICS_DIR, "metrics.csv")
    if not os.path.exists(path):
        cols = ["date", "trades", "win", "loss", "win_rate", "pnl_day", "pnl_cum", "max_dd"]
        pd.DataFrame(columns=cols).to_csv(path, index=False, encoding="utf-8")
    return path


def _append_metrics_row(row: Dict[str, Any]) -> None:
    path = _ensure_metrics_log()
    df = pd.read_csv(path)
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(path, index=False, encoding="utf-8")


def build_daily_summary(trades_csv_path: str, today_str: str | None = None) -> Dict[str, Any]:
    if not os.path.exists(trades_csv_path):
        return {"trades": 0, "win": 0, "loss": 0, "win_rate": 0.0, "pnl_day": 0.0}

    today = today_str or now_jst().strftime("%Y-%m-%d")
    df = pd.read_csv(trades_csv_path)
    if "ts_jst" not in df.columns:
        return {"trades": 0, "win": 0, "loss": 0, "win_rate": 0.0, "pnl_day": 0.0}

    df["date"] = pd.to_datetime(df["ts_jst"], errors="coerce").dt.tz_localize(None).dt.strftime("%Y-%m-%d")
    dfd = df[df["date"] == today]

    pnl_day = 0.0
    trades = 0
    win = 0
    loss = 0

    if not dfd.empty and "mode" in dfd.columns and (dfd["mode"] == "summary").any():
        sdf = dfd[dfd["mode"] == "summary"]
        for _, row in sdf.iterrows():
            try:
                raw = json.loads(row.get("raw", "{}"))
                pnl = float(raw.get("pnl_jpy", 0.0))
            except Exception:
                pnl = 0.0
            pnl_day += pnl
            trades += 1
            if pnl >= 0:
                win += 1
            else:
                loss += 1
    else:
        sells = dfd[dfd.get("side") == "sell"]
        trades = len(sells)
        for _, row in sells.iterrows():
            try:
                raw = json.loads(row.get("raw", "{}"))
                pnl = float(raw.get("pnl_jpy", 0.0))
            except Exception:
                pnl = 0.0
            pnl_day += pnl
            if pnl >= 0:
                win += 1
            else:
                loss += 1

    win_rate = (win / trades * 100.0) if trades > 0 else 0.0
    return {"trades": trades, "win": win, "loss": loss, "win_rate": win_rate, "pnl_day": pnl_day}


def _equity_curve_from_trades(trades_csv_path: str) -> pd.Series:
    if not os.path.exists(trades_csv_path):
        return pd.Series(dtype=float)
    df = pd.read_csv(trades_csv_path)
    if df.empty or "mode" not in df.columns:
        return pd.Series(dtype=float)
    sdf = df[df["mode"] == "summary"].copy()
    if sdf.empty:
        return pd.Series(dtype=float)

    pnl_list = []
    ts_list = []
    for _, row in sdf.iterrows():
        try:
            raw = json.loads(row.get("raw", "{}"))
            pnl = float(raw.get("pnl_jpy", 0.0))
        except Exception:
            pnl = 0.0
        pnl_list.append(pnl)
        try:
            ts_list.append(pd.to_datetime(row.get("ts_jst")))
        except Exception:
            ts_list.append(pd.NaT)
    series = pd.Series(pnl_list, index=pd.to_datetime(ts_list)).sort_index()
    return series.cumsum()


def _max_drawdown_from_equity(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    rolling_max = equity.cummax()
    drawdown = equity - rolling_max
    return float(abs(drawdown.min()))


def write_daily_metrics(trades_csv_path: str, state_path: str) -> Dict[str, Any]:
    today = now_jst().strftime("%Y-%m-%d")
    summary = build_daily_summary(trades_csv_path, today)
    trades = summary["trades"]
    win = summary["win"]
    loss = summary["loss"]
    pnl_day = summary["pnl_day"]
    win_rate = summary["win_rate"]

    pnl_cum = 0.0
    try:
        with open(state_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        pnl_cum = float(state.get("pnl_cum", 0.0))
    except Exception:
        pass

    equity = _equity_curve_from_trades(trades_csv_path)
    try:
        day_equity = equity[equity.index.tz_localize(None).strftime("%Y-%m-%d") == today]
    except Exception:
        day_equity = pd.Series(dtype=float)
    max_dd = _max_drawdown_from_equity(day_equity)

    _ensure_metrics_log()
    path = os.path.join(METRICS_DIR, "metrics.csv")
    df = pd.read_csv(path)
    df = df[df["date"] != today]
    row = {
        "date": today,
        "trades": trades,
        "win": win,
        "loss": loss,
        "win_rate": round(win_rate, 2),
        "pnl_day": round(pnl_day, 2),
        "pnl_cum": round(pnl_cum, 2),
        "max_dd": round(max_dd, 2),
    }
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(path, index=False, encoding="utf-8")

    write_jsonl({"type": "daily_metrics", **row})
    return row


__all__ = [
    "build_daily_summary",
    "write_daily_metrics",
    "METRICS_DIR",
]
