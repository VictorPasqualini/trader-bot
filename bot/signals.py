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
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import research, storage
from . import strategies as st

# Seventeen allocations means seventeen history loads and seventeen indicator
# passes. Research already caches the candles; this caches the arithmetic on
# top of them, so a dashboard poll every few seconds costs nothing between
# candle closes.
CACHE_SECONDS = 60
HISTORY_BARS = 400
WORKERS = 6

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "rows": []}


def _open_symbols() -> dict[str, dict[str, Any]]:
    rows = storage.query("SELECT * FROM positions WHERE status = 'open'")
    return {row["symbol"]: row for row in rows}


def crossing_price(strategy: st.Strategy, closed: Any, kind: str,
                   step: float = 0.01, reach: int = 40) -> float | None:
    """The nearest close that would flip this trigger, expressed in price.

    A reading of "ROC 3.94 against 0" is correct and unusable: nobody watches a
    rate of change on a chart, they watch the price. Rather than invert each
    strategy's arithmetic by hand - seventeen of them, each a chance to be
    subtly wrong - this asks the strategy itself. Replace the last closed bar's
    close, recompute the rule, and see which side of itself it lands on.

    The search walks outward from today's price rather than bisecting a wide
    bracket, because the level is not always unique. A reversion rule moves its
    own band when the close moves - lower the close and the VWAP follows it
    down - so the rule can flip, flip back, and flip again across a wide range.
    Bisection over such a range returns whichever root the halving happens to
    land on, which is not the one being watched. Walking out from the current
    price finds the first crossing in each direction and keeps the nearer one:
    the only level that can be reached without passing through another.

    Returns None when nothing flips within ``reach`` steps, the honest answer
    for a rule whose level is too far away to be worth watching.
    """
    here = float(closed["close"].iloc[-1])
    if here <= 0 or not strategy.reading(closed, kind):
        return None

    # One copy, mutated in place across the whole search. Copying a 400-bar
    # frame once per probe costs more than the indicator pass it exists to run.
    frame = closed.copy()
    last = frame.index[-1]
    high, low_wick = float(frame.loc[last, "high"]), float(frame.loc[last, "low"])

    def met_at(price: float) -> bool | None:
        frame.loc[last, "close"] = price
        # A candle whose close sits outside its own range is not a candle, and
        # any strategy reading highs or lows would be handed an impossibility.
        frame.loc[last, "high"] = max(high, price)
        frame.loc[last, "low"] = min(low_wick, price)
        result = strategy.reading(frame, kind)
        return result["met"] if result else None

    base = met_at(here)
    if base is None:
        return None

    def refine(low: float, high_price: float, low_met: bool) -> float:
        for _ in range(12):
            middle = (low + high_price) / 2
            if met_at(middle) == low_met:
                low = middle
            else:
                high_price = middle
        return round((low + high_price) / 2, 8)

    # Alternate the two directions at each distance, so the first crossing
    # found is the nearest one on either side.
    previous = {1: here, -1: here}
    for index in range(1, reach + 1):
        for direction in (-1, 1):
            price = here * (1 + direction * step * index)
            if price <= 0:
                continue
            met = met_at(price)
            if met is None:
                continue
            if met != base:
                low, high_price = sorted((previous[direction], price))
                return refine(low, high_price, base if previous[direction] < price else met)
            previous[direction] = price
    return None


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
        # The same trigger in the unit the operator actually watches. It moves
        # as the reference bars roll forward, so it is a level for the next
        # close and not a standing order.
        "trigger_price": crossing_price(strategy, closed, kind),
    }


def snapshot(allocations: list[dict[str, Any]], *, refresh: bool = False) -> dict[str, Any]:
    """One pending decision per allocation, sorted by how close it is."""
    with _lock:
        fresh = time.time() - _cache["at"] < CACHE_SECONDS
        if fresh and not refresh and _cache["rows"]:
            return {"rows": _cache["rows"], "checked_at": _cache["at"], "cached": True}

    held = _open_symbols()
    def build(allocation: dict[str, Any]) -> dict[str, Any]:
        try:
            return _row(allocation, held.get(allocation["symbol"]))
        except Exception as exc:
            return {"symbol": allocation.get("symbol"),
                    "interval": allocation.get("interval"),
                    "strategy": allocation.get("strategy"),
                    "error": str(exc)}

    # Seventeen independent indicator passes, each solving for its own price
    # level. They share nothing, and the numpy underneath drops the GIL, so a
    # small pool turns a serial wait into roughly one allocation's worth.
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        rows = list(pool.map(build, allocations))

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
