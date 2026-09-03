"""What every allocation is looking at right now.

The trade log explains a decision after the fact. This answers the question
that comes before it: the bot has not bought ARBUSDT — how close is it? An
operator watching seventeen allocations cannot hold seventeen indicator sets in
their head, and without this the interface can only say "nothing happened",
which is indistinguishable from "nothing is working".

Each allocation reports one comparison: the measured value, the level it has to
cross, and the distance between them. For a symbol already held the comparison
flips to the exit rule, because that is the decision actually pending.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from . import research, storage
from . import strategies as st

# Seventeen allocations means seventeen history loads and seventeen indicator
# passes. Research already caches the candles; this caches the arithmetic on
# top of them, so a dashboard poll every few seconds costs nothing between
# candle closes.
CACHE_SECONDS = 60
HISTORY_BARS = 400

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "rows": []}


def _open_symbols() -> dict[str, dict[str, Any]]:
    rows = storage.query("SELECT * FROM positions WHERE status = 'open'")
    return {row["symbol"]: row for row in rows}


def _row(allocation: dict[str, Any], held: dict[str, Any] | None) -> dict[str, Any]:
    symbol = allocation["symbol"]
    interval = allocation["interval"]
    strategy = st.build(allocation["strategy"], allocation.get("params") or {})
    frame = research.load_history(symbol, interval, HISTORY_BARS)
    # The forming candle is not a fact yet, so the reading is taken from the
    # last closed one - the same bar the engine will act on.
    closed = frame.iloc[:-1]
    kind = "exit" if held else "entry"
    reading = strategy.reading(closed, kind)
    return {
        "symbol": symbol,
        "interval": interval,
        "strategy": allocation["strategy"],
        "strategy_label": strategy.label if isinstance(strategy.label, str)
                          else str(strategy.label),
        "kind": kind,
        "rule": strategy.entry_rule if kind == "entry" else strategy.exit_rule,
        "holding": bool(held),
        "price": round(float(closed["close"].iloc[-1]), 8),
        "bar_time": str(closed["time"].iloc[-1]),
        "trigger": reading,
    }


def snapshot(allocations: list[dict[str, Any]], *, refresh: bool = False) -> dict[str, Any]:
    """One pending decision per allocation, sorted by how close it is."""
    with _lock:
        fresh = time.time() - _cache["at"] < CACHE_SECONDS
        if fresh and not refresh and _cache["rows"]:
            return {"rows": _cache["rows"], "checked_at": _cache["at"], "cached": True}

    held = _open_symbols()
    rows = []
    for allocation in allocations:
        try:
            rows.append(_row(allocation, held.get(allocation["symbol"])))
        except Exception as exc:
            rows.append({"symbol": allocation.get("symbol"),
                         "interval": allocation.get("interval"),
                         "strategy": allocation.get("strategy"),
                         "error": str(exc)})

    # Closest to firing first: a met trigger is already there, and among the
    # rest the smallest distance is the one worth watching.
    def nearness(row: dict[str, Any]) -> tuple[int, float]:
        trigger = row.get("trigger")
        if not trigger:
            return (2, 0.0)
        if trigger["met"]:
            return (0, 0.0)
        distance = trigger.get("distance_pct")
        return (1, abs(distance) if distance is not None else abs(trigger["gap"]))

    rows.sort(key=nearness)
    with _lock:
        _cache["at"] = time.time()
        _cache["rows"] = rows
    return {"rows": rows, "checked_at": _cache["at"], "cached": False}
