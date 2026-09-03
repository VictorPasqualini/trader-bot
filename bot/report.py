"""Aggregations for the dashboard: live P&L, risk stats, per-strategy breakdown."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from . import research
from . import storage
from . import portfolio
from . import walkforward
from .config import settings
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

    # Fees are already inside every realised number; this is only so the
    # dashboard can say how much of the result the exchange took.
    turnover = storage.query_one(
        "SELECT COALESCE(SUM(quote), 0) AS total FROM orders")["total"]

    return {
        "mode": config.get("mode", "testnet"),
        "turnover": round(turnover, 2),
        "fees_estimate": round(turnover * settings.fee_rate, 2),
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


def orders(limit: int = 200) -> list[dict[str, Any]]:
    """Every buy and every sell, newest first, as a plain ledger.

    The trade log answers "how did this position do"; this answers "what did the
    bot actually send to the exchange, and when". They are different questions:
    one position is two orders, and a sale is the only row that carries money
    coming back. Each sell is joined to the position it closed so the ledger can
    state the result of the round trip on the line where it was realised.
    """
    rows = storage.query(
        "SELECT o.id, o.ts, o.symbol, o.side, o.qty, o.price, o.quote, o.order_id, "
        "o.status, o.strategy, o.note, o.position_id, "
        "p.entry_quote, p.entry_time, p.pnl, p.return_pct, p.interval "
        "FROM orders o LEFT JOIN positions p ON p.id = o.position_id "
        "ORDER BY o.ts DESC, o.id DESC LIMIT ?",
        (limit,),
    )
    labels = {item["key"]: item["label"] for item in st.catalog()}
    now = datetime.now(timezone.utc)

    for row in rows:
        row["strategy_label"] = labels.get(row["strategy"], row["strategy"])
        row["is_buy"] = row["side"] == "BUY"
        # Money in on a buy, money out on a sell: the ledger should read like a
        # bank statement, so the sign is on the cash, not on the asset.
        row["cash_delta"] = round(-row["quote"] if row["is_buy"] else row["quote"], 4)
        row["fee_estimate"] = round(row["quote"] * settings.fee_rate, 4)
        if row["is_buy"]:
            row["pnl"] = None
            row["return_pct"] = None
        entered = _parse_time(row["entry_time"])
        left = _parse_time(row["ts"])
        row["duration_seconds"] = (
            round((left - entered).total_seconds()) if entered and left and not row["is_buy"]
            else None)
        row["age_seconds"] = round((now - left).total_seconds()) if left else None
    return rows


def ledger_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Running cash view of the ledger: what went out, what came back."""
    spent = sum(r["quote"] for r in rows if r["is_buy"])
    received = sum(r["quote"] for r in rows if not r["is_buy"])
    realised = sum(r["pnl"] or 0.0 for r in rows if not r["is_buy"])
    return {
        "orders": len(rows),
        "buys": sum(1 for r in rows if r["is_buy"]),
        "sells": sum(1 for r in rows if not r["is_buy"]),
        "spent": round(spent, 2),
        "received": round(received, 2),
        "realised_pnl": round(realised, 2),
        "fees_estimate": round(sum(r["fee_estimate"] for r in rows), 2),
    }


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


# How much live evidence is enough to stop guessing. Both numbers are chosen,
# not derived, and the reasoning is the point: under about 30 closed trades a
# win rate carries a confidence interval close to +/-18 points, which cannot
# separate a good book from a bad one; and under 90 days there is not one full
# walk-forward test window to compare against, so "realised" and "expected" are
# not the same unit.
MIN_LIVE_TRADES = 30
MIN_LIVE_DAYS = 90


def readiness() -> dict[str, Any]:
    """Is this book ready to be trusted with real money?

    Answers it as a set of gates rather than a score, because the interesting
    part is which gate is missing. The forward test is the whole point: a
    backtest says what a strategy did on data it was chosen against, and only
    live trading says what it does on data nobody has seen.
    """
    config = get_config()
    allocations = config.get("allocations", [])
    quote = float(config.get("quote_per_trade", 200.0))
    start = float(config.get("start_capital", 10_000.0))

    state = walkforward.validation_state(allocations)
    reports = state["reports"]
    # The cache starts empty and fills on a background thread, so a fresh
    # process has no expectation to compare against yet. Say that, rather than
    # printing zeros that read like a measured result.
    pending = not reports
    per_symbol = {r["symbol"]: r for r in reports if r.get("window_count")}

    # Expected rates, scaled from each walk-forward to the size the bot trades.
    expected_trades_month, expected_return_month, worst_quarter = 0.0, 0.0, 0.0
    for allocation in allocations:
        found = per_symbol.get(allocation["symbol"])
        if not found:
            continue
        days = found["window_count"] * found["test_days"]
        expected_trades_month += found["total_trades"] / days * 30
        share = quote / start
        expected_return_month += found["median_return_pct"] / 3 * share
        worst_quarter += found["worst_window_pct"] * share

    closed = storage.query("SELECT * FROM positions WHERE status = 'closed'")
    first = storage.query_one("SELECT MIN(ts) AS ts FROM orders")
    started = _parse_time(first["ts"]) if first and first["ts"] else None
    days_live = ((datetime.now(timezone.utc) - started).total_seconds() / 86400
                 if started else 0.0)

    snapshots = storage.query("SELECT total_value FROM equity_snapshots ORDER BY ts")
    observed_dd, _ = _drawdown_and_sharpe([s["total_value"] for s in snapshots])
    realised = sum(p["pnl"] or 0.0 for p in closed)

    max_dd_limit = float(portfolio.settings_for(config)["max_drawdown_pct"] or 0.0)

    failing = [r for r in reports if not r.get("passes")]
    gates = [
        {
            "key": "validation",
            "label": "Todas as estratégias passam no teste em janelas móveis",
            "ok": bool(reports) and not failing,
            "detail": ("calculando..." if pending else
                       f"{len(reports) - len(failing)} de {len(reports)} aprovadas"
                       + (f" — falham: {', '.join(r['symbol'] for r in failing)}"
                          if failing else "")),
        },
        {
            "key": "trades",
            "label": f"Pelo menos {MIN_LIVE_TRADES} operações encerradas ao vivo",
            "ok": len(closed) >= MIN_LIVE_TRADES,
            "detail": f"{len(closed)} encerrada" + ("" if len(closed) == 1 else "s"),
            "progress": min(len(closed) / MIN_LIVE_TRADES, 1.0),
        },
        {
            "key": "time",
            "label": f"Pelo menos {MIN_LIVE_DAYS} dias rodando",
            "ok": days_live >= MIN_LIVE_DAYS,
            "detail": f"{days_live:.0f} dias desde a primeira ordem" if started
                      else "nenhuma ordem enviada ainda",
            "progress": min(days_live / MIN_LIVE_DAYS, 1.0) if started else 0.0,
        },
        {
            "key": "tracking",
            # A book can be profitable and still be broken: what matters is
            # whether it behaves like the thing that was measured.
            "label": "Resultado realizado não pior que o pior trimestre esperado",
            "ok": (not pending and len(closed) >= MIN_LIVE_TRADES
                   and realised / start * 100 >= worst_quarter),
            "detail": ("calculando..." if pending else
                       f"realizado {realised / start * 100:+.2f}% do capital,"
                       f" pior trimestre esperado {worst_quarter:+.2f}%"
                       + ("" if len(closed) >= MIN_LIVE_TRADES
                          else " — amostra ainda pequena demais para comparar")),
        },
        {
            "key": "drawdown",
            "label": "Rebaixamento observado dentro do limite configurado",
            "ok": bool(max_dd_limit) and abs(observed_dd) <= max_dd_limit,
            "detail": (f"observado {observed_dd:.2f}%, limite {max_dd_limit:.0f}%"
                       if max_dd_limit else "trava de rebaixamento desligada"),
        },
    ]

    return {
        "ready": all(g["ok"] for g in gates),
        "gates": gates,
        "allocations": len(allocations),
        "mode": config.get("mode", "testnet"),
        "days_live": round(days_live, 1),
        "closed_trades": len(closed),
        "realised_pnl": round(realised, 2),
        "observed_drawdown_pct": round(observed_dd, 2),
        "pending": pending,
        "expected_trades_per_month": None if pending else round(expected_trades_month, 1),
        "expected_return_pct_month": None if pending else round(expected_return_month, 2),
        "expected_worst_quarter_pct": None if pending else round(worst_quarter, 2),
        "deployed": round(quote * len(allocations), 2),
        "start_capital": round(start, 2),
    }


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
