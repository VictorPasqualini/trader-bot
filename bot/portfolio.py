"""Portfolio-level risk control.

Phase 4 measured what per-trade stops do to these strategies and the answer was
unambiguous: not one of 31 validated candidates improved under any pure stop,
and the tighter the stop the worse both the return and the drawdown. The reason
is structural — for a trend or breakout strategy the exit signal *is* the stop,
and a price stop bolted on top is a second exit rule that fires on noise, banks
the loss, and re-enters into the same decline paying fees each way.

That closes the trade-level door and opens this one. The controls here act on
the book as a whole, where they cannot pre-empt any individual strategy's own
exit logic:

``kill switch``
    Stop opening positions once portfolio drawdown from its peak crosses a
    threshold. Existing positions keep their own exit rules — closing them all
    at the bottom is precisely the behaviour the stop study showed to be
    destructive.

``volatility-scaled sizing``
    A flat 200 USDT is a different amount of risk in XRP than in INJ. Sizing on
    recent realised volatility equalises what each position can actually cost.

``correlation cap``
    Six positions in assets that move together is one position with six sets of
    fees. Refuse a new entry that is too correlated with what is already open.

All three are off by default. Each one narrows what the bot may do, and a
control that silently blocks trades is worse than no control if its owner did
not choose it.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from . import storage
from .exchange import INTERVAL_MS, exchange

# Sizing is expressed relative to a reference volatility so that the configured
# quote amount keeps its plain meaning: a symbol at the reference gets exactly
# that amount. Measured, not guessed - the median mean-absolute daily return
# across twelve liquid USDT pairs over the last 90 days is 2.28%, and the first
# version of this used 4%, which pushed almost every symbol against the ceiling
# and turned the control into a flat 1.6x multiplier.
REFERENCE_VOL = 0.023
SIZE_FLOOR = 0.4
SIZE_CEILING = 1.6
VOL_BARS = 30
CORRELATION_BARS = 90

DEFAULTS: dict[str, Any] = {
    # Percent of peak equity lost before new entries stop. 0 disables.
    "max_drawdown_pct": 0.0,
    # Drawdown the book must recover to before entries resume. Re-entering at
    # the exact threshold would flip the switch on and off on every tick.
    "resume_drawdown_pct": 0.0,
    # Scale position size by realised volatility. Off by default.
    "volatility_sizing": False,
    # Refuse an entry correlating above this with an open position. 0 disables.
    "max_correlation": 0.0,
}


def settings_for(config: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULTS, **(config.get("risk_controls") or {})}


# ------------------------------------------------------------- kill switch

def peak_equity() -> float:
    row = storage.query_one("SELECT MAX(total_value) AS peak FROM equity_snapshots")
    return float(row["peak"] or 0.0) if row else 0.0


def current_equity(config: dict[str, Any]) -> float:
    row = storage.query_one(
        "SELECT total_value FROM equity_snapshots ORDER BY ts DESC LIMIT 1")
    if row:
        return float(row["total_value"])
    return float(config.get("start_capital", 10_000.0))


def drawdown_state(config: dict[str, Any]) -> dict[str, Any]:
    """Where the book stands against its own high-water mark."""
    settings = settings_for(config)
    peak = peak_equity()
    equity = current_equity(config)
    drawdown = (equity / peak - 1) * 100 if peak > 0 else 0.0

    limit = float(settings["max_drawdown_pct"] or 0.0)
    resume = float(settings["resume_drawdown_pct"] or 0.0) or limit / 2
    was_halted = bool(storage.get_state("risk_halted"))

    if limit <= 0:
        halted = False
    elif was_halted:
        # Hysteresis: having stopped at -20%, do not resume until -10%, or the
        # switch chatters on every tick that crosses the line.
        halted = drawdown < -resume
    else:
        halted = drawdown <= -limit

    if halted != was_halted:
        storage.set_state("risk_halted", halted)
        storage.log_event(
            "risk",
            f"Entries halted: drawdown {drawdown:.1f}% past the {limit:.0f}% limit"
            if halted else
            f"Entries resumed: drawdown recovered to {drawdown:.1f}%",
            {"drawdown_pct": round(drawdown, 2), "peak": round(peak, 2),
             "equity": round(equity, 2)},
        )

    return {
        "peak": round(peak, 2),
        "equity": round(equity, 2),
        "drawdown_pct": round(drawdown, 2),
        "limit_pct": limit,
        "resume_pct": resume,
        "halted": halted,
        "enabled": limit > 0,
    }


# --------------------------------------------------------- volatility sizing

def realised_volatility(symbol: str, interval: str, bars: int = VOL_BARS) -> float:
    """Recent volatility, expressed per day whatever the interval.

    Per-bar volatility is not comparable across timeframes: a 4h bar moves far
    less than a daily one, so sizing on the raw figure would hand every 4h
    allocation a larger position for no reason other than its clock. Scaling by
    the square root of the bars per day puts every symbol on the same axis,
    which is the axis ``REFERENCE_VOL`` is quoted on.
    """
    frame = exchange.history(symbol, interval, bars + 5)
    if len(frame) < 10:
        return REFERENCE_VOL
    returns = frame["close"].pct_change().dropna().tail(bars)
    value = float(returns.abs().mean())
    if not (value > 0 and math.isfinite(value)):
        return REFERENCE_VOL
    step_ms = INTERVAL_MS.get(interval) or 86_400_000
    return value * math.sqrt(86_400_000 / step_ms)


def size_for(symbol: str, interval: str, base_quote: float,
             config: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """The quote amount to spend, scaled by how violent this symbol is.

    Returns the amount and the reasoning behind it, so a position that was sized
    down can say why rather than looking like an arbitrary number.
    """
    settings = settings_for(config)
    if not settings["volatility_sizing"]:
        return base_quote, {"scaled": False}

    volatility = realised_volatility(symbol, interval)
    # Clamped: a symbol three times as volatile gets a third of the size, but
    # the bounds stop one quiet week from concentrating the whole book in one
    # asset, and stop one violent day from sizing a position out of existence.
    factor = max(SIZE_FLOOR, min(SIZE_CEILING, REFERENCE_VOL / volatility))
    return round(base_quote * factor, 2), {
        "scaled": True,
        "volatility_pct": round(volatility * 100, 3),
        "reference_pct": round(REFERENCE_VOL * 100, 3),
        "factor": round(factor, 3),
    }


# ----------------------------------------------------------- correlation cap

def _returns(symbol: str, interval: str, bars: int) -> np.ndarray:
    frame = exchange.history(symbol, interval, bars + 5)
    values = frame["close"].pct_change().dropna().tail(bars).to_numpy(dtype=float)
    return values


def correlation(symbol_a: str, symbol_b: str, interval: str,
                bars: int = CORRELATION_BARS) -> float:
    a, b = _returns(symbol_a, interval, bars), _returns(symbol_b, interval, bars)
    size = min(len(a), len(b))
    if size < 30:
        return 0.0
    value = float(np.corrcoef(a[-size:], b[-size:])[0, 1])
    return value if math.isfinite(value) else 0.0


def correlation_block(symbol: str, interval: str, open_symbols: list[str],
                      config: dict[str, Any]) -> dict[str, Any] | None:
    """Whether this entry duplicates exposure the book already carries."""
    settings = settings_for(config)
    limit = float(settings["max_correlation"] or 0.0)
    if limit <= 0 or not open_symbols:
        return None

    for other in open_symbols:
        if other == symbol:
            continue
        try:
            value = correlation(symbol, other, interval)
        except Exception:
            continue
        if value >= limit:
            return {"blocked_by": other, "correlation": round(value, 3),
                    "limit": limit}
    return None


# ------------------------------------------------------------------ summary

def state(config: dict[str, Any], open_symbols: list[str] | None = None) -> dict[str, Any]:
    """Everything the dashboard needs to show what the controls are doing."""
    settings = settings_for(config)
    summary = {"settings": settings, "drawdown": drawdown_state(config)}

    open_symbols = open_symbols or []
    pairs: list[dict[str, Any]] = []
    if settings["max_correlation"] and len(open_symbols) > 1:
        for index, first in enumerate(open_symbols):
            for second in open_symbols[index + 1:]:
                try:
                    value = correlation(first, second, "1d")
                except Exception:
                    continue
                pairs.append({"a": first, "b": second, "correlation": round(value, 3)})
    summary["correlations"] = sorted(pairs, key=lambda p: -p["correlation"])
    return summary
