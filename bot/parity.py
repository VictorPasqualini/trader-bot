"""Does the live bot do what the backtest said it would?

This is the one question a forward test answers that no amount of historical
data can. A backtest describes an edge; it also assumes an execution model -
signal read on a closed candle, fill at the open of the next candle, a fixed
cost on both sides. If the live engine departs from that model, every
historical number justifying the book is measuring a different bot.

The comparison is per trade, against its own twin, which is why it needs so few
of them: a systematic timing or pricing defect shows up in the first two or
three trades, while estimating a win rate from live results alone would need
dozens. Ten to fifteen matched trades is enough to trust the execution; the
edge itself is established by walk-forward, on hundreds.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from . import research, storage
from . import strategies as st
from .config import settings

# A fill is never exactly the modelled price - the backtest fills at the candle
# open and the bot fills at whatever the book offers when it wakes - so the
# question is whether the difference stays inside the cost assumption, not
# whether it is zero.
SLIPPAGE_TOLERANCE_BPS = (settings.fee_rate + settings.slippage_rate) * 10_000
HISTORY_BARS = 1000


def _series(symbol: str, interval: str, strategy: st.Strategy) -> tuple[Any, Any]:
    """Closed candles and the position series the strategy holds over them."""
    frame = research.load_history(symbol, interval, HISTORY_BARS)
    closed = frame.iloc[:-1].reset_index(drop=True)
    return closed, strategy.signal(closed).to_numpy(dtype=float) > 0


def _bar_at(times: list[Any], moment: datetime) -> int | None:
    """Index of the newest candle already closed at `moment`."""
    matches = [i for i, value in enumerate(times) if value <= moment]
    return matches[-1] if matches else None


def _transition(long: Any, upto: int, *, into: bool) -> int | None:
    """Last bar at or before `upto` where the signal turned on (or off).

    The backtest never enters mid-signal: it enters on the bar the rule starts
    firing and exits on the bar it stops. Finding that bar is what makes a live
    fill comparable to a modelled one.
    """
    for index in range(upto, 0, -1):
        if bool(long[index]) == into and bool(long[index - 1]) != into:
            return index
    return None


def _fill_price(closed: Any, decision: int) -> float | None:
    """What the backtest would have paid: the open after the decision."""
    if decision + 1 >= len(closed):
        return None
    return round(float(closed["open"].iloc[decision + 1]), 8)


def _bps(actual: float, expected: float) -> float:
    return round((actual / expected - 1) * 10_000, 1)


def _compare(position: dict[str, Any]) -> dict[str, Any]:
    params = json.loads(position["params"] or "{}")
    strategy = st.build(position["strategy"], params)
    closed, long = _series(position["symbol"], position["interval"], strategy)
    times = list(closed["time"])

    out: dict[str, Any] = {
        "position_id": position["id"],
        "symbol": position["symbol"],
        "interval": position["interval"],
        "strategy": position["strategy"],
        "status": position["status"],
        "reason": position["reason"],
        "entry_time": position["entry_time"],
        "exit_time": position["exit_time"],
        "actual_return_pct": (round(position["return_pct"], 2)
                              if position["return_pct"] is not None else None),
        "notes": [],
    }

    entry_bar = _bar_at(times, datetime.fromisoformat(position["entry_time"]))
    if entry_bar is None:
        out["notes"].append("histórico carregado não alcança a entrada")
        out["verdict"] = "sem histórico"
        return out

    decision = _transition(long, entry_bar, into=True)
    if decision is None:
        out["notes"].append("nenhuma virada de sinal antes da compra")
        out["verdict"] = "divergente"
        return out

    expected_entry = _fill_price(closed, decision)
    out["entry_decision_bar"] = str(times[decision])
    out["entry_bars_late"] = entry_bar - decision
    out["expected_entry_price"] = expected_entry
    out["actual_entry_price"] = position["entry_price"]
    if expected_entry:
        out["entry_slippage_bps"] = _bps(position["entry_price"], expected_entry)

    if position["status"] != "closed":
        out["verdict"] = _verdict(out)
        return out

    exit_bar = _bar_at(times, datetime.fromisoformat(position["exit_time"]))
    if position["reason"] == "signal" and exit_bar is not None:
        leave = _transition(long, exit_bar, into=False)
        if leave is not None:
            expected_exit = _fill_price(closed, leave)
            out["exit_decision_bar"] = str(times[leave])
            out["exit_bars_late"] = exit_bar - leave
            out["expected_exit_price"] = expected_exit
            out["actual_exit_price"] = position["exit_price"]
            if expected_exit:
                out["exit_slippage_bps"] = _bps(position["exit_price"], expected_exit)
            if expected_entry and expected_exit:
                cost = (settings.fee_rate + settings.slippage_rate) * 2
                out["expected_return_pct"] = round(
                    (expected_exit / expected_entry - 1 - cost) * 100, 2)
                out["return_gap_pct"] = round(
                    out["actual_return_pct"] - out["expected_return_pct"], 2)
    elif position["reason"] != "signal":
        # Stops and targets fire inside a candle, which the backtest models with
        # an assumption rather than a price it observed. There is nothing honest
        # to compare a live intrabar fill against, so it is reported, not scored.
        out["notes"].append(
            "saída por risco: preenchimento dentro da vela, não comparável")

    out["verdict"] = _verdict(out)
    return out


def _verdict(row: dict[str, Any]) -> str:
    """One phrase for whether this trade matched its model."""
    late = max(row.get("entry_bars_late") or 0, row.get("exit_bars_late") or 0)
    slip = max(abs(row.get("entry_slippage_bps") or 0),
               abs(row.get("exit_slippage_bps") or 0))
    if late == 0 and slip <= SLIPPAGE_TOLERANCE_BPS:
        return "igual ao modelo"
    if late == 0:
        return f"preço fora da premissa ({slip:.0f} bps)"
    if late == 1:
        return "1 vela de atraso"
    return f"{late} velas de atraso"


def report(limit: int = 50) -> dict[str, Any]:
    """Every live trade next to the trade the backtest would have made."""
    positions = storage.query(
        "SELECT * FROM positions ORDER BY entry_time DESC LIMIT ?", (limit,))
    rows = []
    for position in positions:
        try:
            rows.append(_compare(position))
        except Exception as exc:
            rows.append({
                "position_id": position["id"], "symbol": position["symbol"],
                "verdict": "não avaliada", "notes": [str(exc)],
            })

    matched = [r for r in rows if r.get("verdict") == "igual ao modelo"]
    scored = [r for r in rows if r.get("return_gap_pct") is not None]
    late = [r for r in rows if (r.get("entry_bars_late") or 0) > 0]
    slips = sorted(abs(r["entry_slippage_bps"]) for r in rows
                   if r.get("entry_slippage_bps") is not None)

    return {
        "trades": rows,
        "totals": {
            "evaluated": len(rows),
            "matched": len(matched),
            "late": len(late),
            "median_entry_slippage_bps": (round(slips[len(slips) // 2], 1)
                                          if slips else None),
            "tolerance_bps": round(SLIPPAGE_TOLERANCE_BPS, 1),
            "mean_return_gap_pct": (round(sum(r["return_gap_pct"] for r in scored)
                                          / len(scored), 2) if scored else None),
        },
    }
