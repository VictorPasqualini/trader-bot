"""Aggregations for the dashboard: live P&L, risk stats, per-strategy breakdown."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from . import research
from . import storage
from . import strategies as st
from .exchange import INTERVAL_MS, exchange
from .live import bot, get_config


def _drawdown_and_sharpe(values: list[float]) -> tuple[float, float]:
    if len(values) < 3:
        return 0.0, 0.0
    peak = values[0]
    max_dd = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_dd = min(max_dd, value / peak - 1)
    returns = [
        values[i] / values[i - 1] - 1
        for i in range(1, len(values)) if values[i - 1] > 0
    ]
    if len(returns) < 2:
        return max_dd * 100, 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    std = math.sqrt(variance)
    # Snapshots are taken once per tick, so this is a per-tick Sharpe scaled to
    # a nominal 365-observation year. Directional, not a precise annual figure.
    sharpe = (mean / std * math.sqrt(365)) if std > 0 else 0.0
    return max_dd * 100, sharpe


def overview() -> dict[str, Any]:
    config = get_config()
    closed = storage.query(
        "SELECT * FROM positions WHERE status = 'closed' ORDER BY exit_time"
    )
    positions = bot.open_positions()

    marks: dict[str, float] = {}
    if positions:
        try:
            marks = exchange.prices(sorted({p["symbol"] for p in positions}))
        except Exception:
            marks = {}

    unrealised = 0.0
    invested = 0.0
    for position in positions:
        mark = marks.get(position["symbol"], position["entry_price"])
        position["mark_price"] = mark
        position["value"] = position["qty"] * mark
        position["unrealised_pnl"] = position["value"] - position["entry_quote"]
        position["unrealised_pct"] = (
            (position["value"] / position["entry_quote"] - 1) * 100
            if position["entry_quote"] else 0.0
        )
        unrealised += position["unrealised_pnl"]
        invested += position["entry_quote"]

    realised = sum(p["pnl"] or 0.0 for p in closed)
    start = float(config.get("start_capital", 10_000.0))
    total = start + realised + unrealised

    wins = [p for p in closed if (p["pnl"] or 0) > 0]
    losses = [p for p in closed if (p["pnl"] or 0) <= 0]
    gross_win = sum(p["pnl"] for p in wins)
    gross_loss = -sum(p["pnl"] for p in losses)

    snapshots = storage.query(
        "SELECT ts, total_value FROM equity_snapshots ORDER BY ts"
    )
    max_dd, sharpe = _drawdown_and_sharpe([s["total_value"] for s in snapshots])

    return {
        "start_capital": round(start, 2),
        "total_value": round(total, 2),
        "total_pnl": round(realised + unrealised, 2),
        "total_return_pct": round((total / start - 1) * 100, 2) if start else 0.0,
        "realised_pnl": round(realised, 2),
        "unrealised_pnl": round(unrealised, 2),
        "invested": round(invested, 2),
        "cash": round(total - invested - unrealised, 2),
        "open_positions": len(positions),
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 2) if closed else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (
            999.0 if gross_win > 0 else 0.0),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "expectancy": round(realised / len(closed), 2) if closed else 0.0,
        "best_trade_pct": round(max((p["return_pct"] or 0 for p in closed), default=0.0), 2),
        "worst_trade_pct": round(min((p["return_pct"] or 0 for p in closed), default=0.0), 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "positions": positions,
    }


def equity_curve(limit: int = 500) -> list[dict[str, Any]]:
    rows = storage.query(
        "SELECT ts, total_value, open_positions FROM equity_snapshots ORDER BY ts DESC LIMIT ?",
        (limit,),
    )
    return list(reversed(rows))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def trades(limit: int = 100) -> list[dict[str, Any]]:
    """Detailed trade log, one row per position.

    Each row answers the same questions in the same order: which coin, which
    strategy and parameters, what triggered the entry, what triggered the exit,
    how much it made or lost, and how long that took. Open positions are marked
    to the current price so their running result is comparable to closed ones.
    """
    rows = storage.query(
        "SELECT id, symbol, strategy, interval, params, risk, status, qty, entry_price, "
        "entry_time, entry_quote, exit_price, exit_time, exit_quote, pnl, return_pct, "
        "reason, mode, entry_context, exit_context "
        "FROM positions ORDER BY COALESCE(exit_time, entry_time) DESC LIMIT ?",
        (limit,),
    )
    open_symbols = sorted({r["symbol"] for r in rows if r["status"] == "open"})
    marks: dict[str, float] = {}
    if open_symbols:
        try:
            marks = exchange.prices(open_symbols)
        except Exception:
            marks = {}

    catalog = {item["key"]: item for item in st.catalog()}
    labels = {key: item["label"] for key, item in catalog.items()}
    now = datetime.now(timezone.utc)

    for row in rows:
        row["params"] = json.loads(row["params"])
        row["risk"] = json.loads(row["risk"])
        row["entry_signal"] = json.loads(row.pop("entry_context") or "null")
        row["exit_signal"] = json.loads(row.pop("exit_context") or "null")
        row["strategy_label"] = labels.get(row["strategy"], row["strategy"])

        entered = _parse_time(row["entry_time"])
        finished = _parse_time(row["exit_time"])
        seconds = (finished - entered).total_seconds() if entered and finished else (
            (now - entered).total_seconds() if entered else None)
        row["duration_seconds"] = round(seconds) if seconds is not None else None
        bar_ms = INTERVAL_MS.get(row["interval"])
        row["bars_held"] = round(seconds / (bar_ms / 1000), 1) if seconds and bar_ms else None

        if row["status"] == "open":
            # An open position has no exit signal yet, so the card says what the
            # bot is waiting for instead of leaving the field blank. Protective
            # exits are not listed: whether one is armed depends on the risk
            # settings, which the row already carries.
            entry = catalog.get(row["strategy"])
            row["pending_exit_rule"] = entry["exit_rule"] if entry else None
            mark = marks.get(row["symbol"], row["entry_price"])
            value = row["qty"] * mark
            row["mark_price"] = mark
            row["pnl"] = round(value - row["entry_quote"], 4)
            row["return_pct"] = round(
                (value / row["entry_quote"] - 1) * 100, 4) if row["entry_quote"] else 0.0
    return rows


HISTORY_BARS = 900


def _snapshot(frame: Any, bar: int) -> dict[str, float]:
    """Indicator values at one bar, rounded for display."""
    values: dict[str, float] = {}
    for name, series in frame.items():
        try:
            value = float(series.iloc[bar])
        except (TypeError, ValueError, IndexError):
            continue
        if value == value:
            values[name] = round(value, 8)
    return values


def _decision_bar(frame: Any, bar: int) -> dict[str, Any]:
    """Time and close of the candle a decision was read from."""
    return {
        "bar_time": str(frame["time"].iloc[bar]),
        "bar_close": round(float(frame["close"].iloc[bar]), 8),
    }


def allocation_history(bars: int = HISTORY_BARS) -> list[dict[str, Any]]:
    """Replay each live allocation over recent history, trade by trade.

    Live trades on daily strategies arrive a few times a year, so the live log
    alone says very little about what a strategy actually does. This shows the
    same strategy, the same parameters and the same risk settings applied to
    real recent candles: every entry, every exit, what triggered each one, the
    result and how long it took. Simulated, and labelled as such — it is the
    strategy's behaviour, not money that was made.
    """
    from . import backtest as bt

    out: list[dict[str, Any]] = []
    config = get_config()
    # The backtester compounds one full account into a single strategy, which
    # makes its currency figures an order of magnitude larger than anything the
    # live book can produce. Restating each trade at the size the bot actually
    # sends keeps the two views comparable; the percentages are untouched.
    quote = float(config.get("quote_per_trade", 200.0))
    for allocation in config.get("allocations", []):
        symbol = allocation["symbol"]
        interval = allocation.get("interval", "1h")
        try:
            strategy = st.build(allocation["strategy"], allocation.get("params"))
            frame = exchange.history(symbol, interval, bars)
        except Exception as exc:
            out.append({"symbol": symbol, "interval": interval,
                        "strategy": allocation["strategy"], "error": str(exc), "trades": []})
            continue

        risk = allocation.get("risk") or {}
        result = bt.run(frame, strategy.signal(frame), **research.risk_kwargs(risk))
        indicator_frame = strategy.indicators(frame)
        bar_seconds = (INTERVAL_MS.get(interval) or 0) / 1000

        rows = []
        for trade in result.trades:
            rows.append({
                **trade,
                "symbol": symbol,
                "interval": interval,
                "strategy": allocation["strategy"],
                "strategy_label": strategy.label,
                "entry_rule": strategy.entry_rule,
                "exit_rule": strategy.exit_rule if trade["reason"] == "signal" else trade["reason"],
                "entry_values": _snapshot(indicator_frame, trade["entry_bar"]),
                "exit_values": _snapshot(indicator_frame, trade["exit_bar"]),
                # The decision candle, not the fill: an order is sent at the open
                # of the bar after the one the rule fired on, so the timestamp in
                # the row and the numbers in the card are one bar apart.
                "entry_signal": _decision_bar(frame, trade["entry_bar"]),
                "exit_signal": _decision_bar(frame, trade["exit_bar"]),
                "duration_seconds": round(trade["bars"] * bar_seconds) if bar_seconds else None,
                "pnl": round(trade["return_pct"] / 100 * quote, 4),
                "sized_at_quote": quote,
            })
        out.append({
            "symbol": symbol,
            "interval": interval,
            "strategy": allocation["strategy"],
            "strategy_label": strategy.label,
            "params": strategy.params,
            "risk": risk,
            "metrics": result.metrics,
            "trades": list(reversed(rows)),
        })
    return out


def breakdown() -> dict[str, list[dict[str, Any]]]:
    """Closed-trade performance sliced by strategy and by coin.

    Average holding time is included because two strategies with the same
    return are not equivalent if one takes a week and the other takes a month.
    """
    closed = storage.query("SELECT * FROM positions WHERE status = 'closed'")

    def group(field: str) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in closed:
            buckets[row[field]].append(row)
        out = []
        for name, rows in buckets.items():
            pnl = sum(r["pnl"] or 0 for r in rows)
            wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
            spans = [
                (_parse_time(r["exit_time"]) - _parse_time(r["entry_time"])).total_seconds()
                for r in rows
                if _parse_time(r["exit_time"]) and _parse_time(r["entry_time"])
            ]
            out.append({
                "name": name,
                "trades": len(rows),
                "pnl": round(pnl, 2),
                "win_rate_pct": round(wins / len(rows) * 100, 1) if rows else 0.0,
                "avg_return_pct": round(
                    sum(r["return_pct"] or 0 for r in rows) / len(rows), 2) if rows else 0.0,
                "best_return_pct": round(max((r["return_pct"] or 0 for r in rows), default=0.0), 2),
                "worst_return_pct": round(min((r["return_pct"] or 0 for r in rows), default=0.0), 2),
                "avg_duration_seconds": round(sum(spans) / len(spans)) if spans else None,
            })
        return sorted(out, key=lambda item: item["pnl"], reverse=True)

    return {"by_strategy": group("strategy"), "by_symbol": group("symbol")}
