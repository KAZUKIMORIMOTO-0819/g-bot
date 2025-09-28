"""Slack notification helpers."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from .config import SlackConfig

logger = logging.getLogger("slack_notify")


def _http_post_json(url: str, payload: Dict[str, Any], timeout: int) -> Tuple[int, str]:
    """POST JSON payload to a webhook and return status/text."""
    resp = requests.post(url, json=payload, timeout=timeout)
    return resp.status_code, resp.text


def send_slack_message(cfg: SlackConfig, text: str, blocks: Optional[List[Dict[str, Any]]] = None) -> bool:
    """Send a Slack message; noop success when webhook is missing."""
    url = cfg.resolved_url()
    if not url:
        logger.warning("SLACK_WEBHOOK_URL not set; skipping notification")
        return True

    payload: Dict[str, Any] = {
        "username": cfg.username,
        "icon_emoji": cfg.icon_emoji,
        "text": text,
    }
    if blocks:
        payload["blocks"] = blocks

    last_err: Optional[str] = None
    for attempt in range(1, cfg.max_retries + 1):
        try:
            status, body = _http_post_json(url, payload, timeout=cfg.timeout_sec)
            if 200 <= status < 300:
                return True
            last_err = f"HTTP {status}: {body}"
        except Exception as exc:
            last_err = str(exc)
        sleep_sec = cfg.backoff_factor ** (attempt - 1)
        logger.warning("[%s/%s] Slack send failed: %s -> sleep %.2fs", attempt, cfg.max_retries, last_err, sleep_sec)
        time.sleep(sleep_sec)
    logger.error("Slack send ultimately failed: %s", last_err)
    return False


def fmt_signal_gc(bar_ts: str, price: float, sma_s: float, sma_l: float) -> Tuple[str, List[Dict[str, Any]]]:
    text = f":vertical_traffic_light: *GC detected* @ {bar_ts}  price={price:.4f}  (SMA30={sma_s:.4f} > SMA60={sma_l:.4f})"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Golden Cross Detected"}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Time (JST)*: `{bar_ts}`\n"
                    f"*Price*: `{price:.4f}`\n"
                    f"*SMA30/60*: `{sma_s:.4f}` / `{sma_l:.4f}`"
                ),
            },
        },
    ]
    return text, blocks


def fmt_entry(symbol: str, price: float, size: float, tp: float, sl: float) -> Tuple[str, List[Dict[str, Any]]]:
    text = f":rocket: Entry LONG {symbol}  price={price:.4f} size={size:.4f}  TP={tp:.4f}  SL={sl:.4f}"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "New Entry"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Symbol:*\n{symbol}"},
                {"type": "mrkdwn", "text": f"*Price:*\n{price:.4f}"},
                {"type": "mrkdwn", "text": f"*Size:*\n{size:.4f}"},
                {"type": "mrkdwn", "text": f"*TP:*\n{tp:.4f}"},
                {"type": "mrkdwn", "text": f"*SL:*\n{sl:.4f}"},
            ],
        },
    ]
    return text, blocks


def fmt_close(reason: str, price: float, size: float, pnl_jpy: float, pnl_cum: float) -> Tuple[str, List[Dict[str, Any]]]:
    emoji = ":white_check_mark:" if pnl_jpy >= 0 else ":x:"
    text = f"{emoji} Close ({reason})  price={price:.4f} size={size:.4f}  PnL={pnl_jpy:.2f} JPY  Cum={pnl_cum:.2f}"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Close - {reason}"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Price:*\n{price:.4f}"},
                {"type": "mrkdwn", "text": f"*Size:*\n{size:.4f}"},
                {"type": "mrkdwn", "text": f"*PnL (JPY):*\n{pnl_jpy:.2f}"},
                {"type": "mrkdwn", "text": f"*PnL Cum (JPY):*\n{pnl_cum:.2f}"},
            ],
        },
    ]
    return text, blocks


def fmt_error(context: str, message: str) -> Tuple[str, List[Dict[str, Any]]]:
    text = f":warning: *Error* in `{context}` — {message}"
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":warning: *Error* in `{context}`\n```{message}```"},
        }
    ]
    return text, blocks


def fmt_daily_summary(summary: Dict[str, Any], pnl_cum: float) -> Tuple[str, List[Dict[str, Any]]]:
    text = (
        f":bar_chart: Daily Summary — Trades={summary['trades']}  "
        f"Win={summary['win']}  Loss={summary['loss']}  "
        f"WinRate={summary['win_rate']:.1f}%  PnL_day={summary['pnl_day']:.2f}  PnL_cum={pnl_cum:.2f}"
    )
    fields = [
        {"type": "mrkdwn", "text": f"*Trades:*\n{summary['trades']}"},
        {"type": "mrkdwn", "text": f"*Win/Loss:*\n{summary['win']} / {summary['loss']}"},
        {"type": "mrkdwn", "text": f"*WinRate:*\n{summary['win_rate']:.1f}%"},
        {"type": "mrkdwn", "text": f"*PnL (Day):*\n{summary['pnl_day']:.2f}"},
        {"type": "mrkdwn", "text": f"*PnL (Cum):*\n{pnl_cum:.2f}"},
    ]
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Daily Summary"}},
        {"type": "section", "fields": fields},
    ]
    return text, blocks


def notify_gc(cfg: SlackConfig, bar_ts: str, price: float, sma_s: float, sma_l: float) -> bool:
    text, blocks = fmt_signal_gc(bar_ts, price, sma_s, sma_l)
    return send_slack_message(cfg, text, blocks)


def notify_entry(cfg: SlackConfig, symbol: str, price: float, size: float, tp: float, sl: float) -> bool:
    text, blocks = fmt_entry(symbol, price, size, tp, sl)
    return send_slack_message(cfg, text, blocks)


def notify_close(cfg: SlackConfig, reason: str, price: float, size: float, pnl_jpy: float, pnl_cum: float) -> bool:
    text, blocks = fmt_close(reason, price, size, pnl_jpy, pnl_cum)
    return send_slack_message(cfg, text, blocks)


def notify_error(cfg: SlackConfig, context: str, message: str) -> bool:
    text, blocks = fmt_error(context, message)
    return send_slack_message(cfg, text, blocks)


def notify_daily_summary(
    cfg: SlackConfig,
    state_path: str = "./data/state/state.json",
    trades_csv_path: str = "./data/trades/trades.csv",
) -> bool:
    from .metrics import build_daily_summary

    pnl_cum = 0.0
    try:
        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            pnl_cum = float(state.get("pnl_cum", 0.0))
    except Exception as exc:
        logger.warning("state.json read error: %s", exc)

    summary = build_daily_summary(trades_csv_path)
    text, blocks = fmt_daily_summary(summary, pnl_cum)
    return send_slack_message(cfg, text, blocks)


def notify_runner_status(cfg: SlackConfig, title: str, message: str, emoji: str = ":information_source:") -> bool:
    text = f"{emoji} *{title}*\n{message}"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]
    return send_slack_message(cfg, text, blocks)


__all__ = [
    "send_slack_message",
    "notify_gc",
    "notify_entry",
    "notify_close",
    "notify_error",
    "notify_daily_summary",
    "notify_runner_status",
    "fmt_signal_gc",
    "fmt_entry",
    "fmt_close",
    "fmt_error",
    "fmt_daily_summary",
]
