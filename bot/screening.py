"""Symbol profiling — and the record of a screen that did not work.

Research run 3 measured something the strategy comparison had hidden. Validation
rates per symbol ranged from 0% to 38.9%, an eightfold spread far wider than the
spread between strategies. Which coin a strategy runs on matters more than which
strategy it is. The obvious inference was that a cheap measure of price shape
could predict that spread and point sweeps at the symbols worth sweeping.

**It could not.** Two attempts, both falsified:

1. Ranking on how cleanly a symbol trended - choppiness, Hurst exponent, ADX
   share. Against run 3 it correlated 0.03 with the validation rate, and its top
   half validated *less* often (7.4%) than its bottom half (9.6%).

2. Ranking on the two measures that did correlate on run 3 - realised
   volatility (rho 0.53) and lag-1 autocorrelation (rho 0.33). Run on twenty
   symbols absent from run 3, with the ranking written to disk *before* the
   sweep started, it scored Spearman -0.17 against validation rate and -0.33
   against median out-of-sample alpha. Top half 4.44%, bottom half 4.58%. Both
   correlations point the wrong way and neither is distinguishable from noise.

The second attempt is the instructive one. Those correlations were real in run
3's data and they were still real when re-measured; they were simply picked
*because* they correlated, out of eight candidate measures against three
outcomes, at n=20. That is roughly two dozen chances for something to look
significant, and something did. Registering the prediction before running the
sweep is the only reason this is known rather than believed.

What survives is this module as a *descriptive* profiler. The numbers below are
honest measurements of a price series and useful for understanding a symbol.
None of them forecasts whether a strategy will validate on it, and nothing here
filters, ranks or skips a sweep.

``drift_ratio``
    The displacement a random walk of the same per-bar volatility would be
    expected to cover, divided by what the price actually did. Below 1.0 the
    series trended further than chance explains; above 1.0 it churned. Scale
    free, unlike raw choppiness, which grows with the bar count and therefore
    calls every long sample choppy.

``hurst``
    A rescaled-range style exponent, above 0.5 persistent and below 0.5
    anti-persistent, derived from a different property of the series than the
    other measures.

``autocorr_1``
    Correlation of each bar's return with the one before.

``vol_annual_pct``
    Mean absolute return per bar. The name is historical; it is per bar, not
    annualised.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from . import indicators as ta
from . import research

SCREEN_CANDLES = 720

# Every family, always. Kept as a named constant so the call sites read as a
# deliberate choice rather than an oversight.
ALL_FAMILIES = ["trend", "breakout", "momentum", "reversion", "ensemble"]


def choppiness(close: pd.Series) -> float:
    """Total distance travelled per unit of net movement. 1.0 = a straight line.

    Not comparable between samples of different length - use ``drift_ratio``.
    """
    values = close.to_numpy(dtype=float)
    path = float(np.abs(np.diff(values)).sum())
    net = abs(float(values[-1] - values[0]))
    if net <= 0:
        return float("inf")
    return path / net


def drift_ratio(close: pd.Series) -> float:
    """Expected random-walk displacement divided by the realised displacement.

    A random walk of n steps with per-step deviation s is expected to end about
    ``s * sqrt(n)`` from where it started. Dividing that expectation by what the
    price actually did removes both the sample size and the volatility, leaving
    a number that means the same thing on any symbol and any timeframe.
    """
    values = np.log(close.to_numpy(dtype=float))
    steps = np.diff(values)
    if len(steps) < 30:
        return 1.0
    expected = float(np.std(steps)) * math.sqrt(len(steps))
    net = abs(float(values[-1] - values[0]))
    if net <= 0 or expected <= 0:
        return float("inf")
    return expected / net


def autocorrelation(close: pd.Series, lag: int = 1) -> float:
    returns = close.pct_change().dropna()
    if len(returns) < lag + 30:
        return 0.0
    value = returns.autocorr(lag=lag)
    return 0.0 if value is None or value != value else float(value)


def hurst(close: pd.Series, max_lag: int = 60) -> float:
    """Hurst exponent from the growth of dispersion with the sampling lag."""
    values = np.log(close.to_numpy(dtype=float))
    lags = range(2, min(max_lag, len(values) // 3))
    spreads = [np.std(values[lag:] - values[:-lag]) for lag in lags]
    if len(spreads) < 5 or min(spreads) <= 0:
        return 0.5
    slope = np.polyfit(np.log(list(lags)), np.log(spreads), 1)[0]
    return round(float(slope), 4)


def profile(symbol: str, interval: str, candles: int = SCREEN_CANDLES) -> dict[str, Any]:
    """Everything measurable about a symbol's shape, before any strategy runs."""
    df = research.load_history(symbol, interval, candles)
    close = df["close"]
    chop = choppiness(close)
    drift = drift_ratio(close)
    persistence = hurst(close)
    lag1 = autocorrelation(close)
    strength = ta.adx(df["high"], df["low"], df["close"], 14).dropna()
    volatility = ta.realised_vol(close, 20).dropna()

    return {
        "symbol": symbol,
        "interval": interval,
        "bars": len(df),
        "buy_hold_pct": round((float(close.iloc[-1]) / float(close.iloc[0]) - 1) * 100, 2),
        "choppiness": round(chop, 2) if math.isfinite(chop) else None,
        "drift_ratio": round(drift, 3) if math.isfinite(drift) else None,
        "hurst": persistence,
        "autocorr_1": round(lag1, 4),
        "adx_mean": round(float(strength.mean()), 2) if len(strength) else 0.0,
        "trending_share_pct": round(float((strength > 25).mean()) * 100, 1) if len(strength) else 0.0,
        "vol_annual_pct": round(float(volatility.mean()) * 100, 2) if len(volatility) else 0.0,
        "quote_volume": round(float((df["close"] * df["volume"]).tail(90).mean()), 0),
    }


def classify(row: dict[str, Any]) -> tuple[str, float]:
    """Which strategy family the shape leans towards, and how strongly.

    Right on 12 of 20 symbols in run 3 and 12 of 20 in run 4 - a coin flip,
    twice. Reported because it is interesting to look at, used for nothing.
    """
    persistence = row.get("hurst", 0.5)
    lag1 = row.get("autocorr_1", 0.0)
    trending = row.get("trending_share_pct", 0.0)
    drift = row.get("drift_ratio")

    lean = (
        max(-1.0, min(1.0, (persistence - 0.5) * 5))
        + max(-1.0, min(1.0, lag1 * 10))
        + max(-1.0, min(1.0, (trending - 30) / 20))
    )
    if drift is not None:
        lean += max(-1.0, min(1.0, 1.0 - drift))
    lean /= 4

    if lean >= 0:
        return ("trend/breakout", round(lean, 3))
    return ("reversion", round(-lean, 3))


def screen(symbols: list[str], interval: str = "1d",
           candles: int = SCREEN_CANDLES) -> list[dict[str, Any]]:
    """Profile a list of symbols.

    Ordered by volatility purely so the output reads consistently. That is not a
    recommendation: volatility was the best of the measures tried and it still
    failed out of sample.
    """
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            row = profile(symbol, interval, candles)
        except Exception as exc:
            rows.append({"symbol": symbol, "interval": interval, "error": str(exc)})
            continue
        family, lean = classify(row)
        row["favours"] = family
        row["lean"] = lean
        rows.append(row)
    return sorted(rows, key=lambda item: item.get("vol_annual_pct", -1), reverse=True)


def families_for(row: dict[str, Any]) -> list[str]:
    """Strategy families worth sweeping on this symbol: all of them.

    Kept as a function because the day a screen is found that actually predicts
    something, this is where it goes.
    """
    return list(ALL_FAMILIES)
