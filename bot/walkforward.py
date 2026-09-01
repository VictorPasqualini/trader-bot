"""Walk-forward validation.

The single train/test split used by the research sweep answers one question:
"did this edge survive one regime change?". That is a low bar. A strategy can
clear it because the held-out slice happened to be a bull market of the kind it
likes, and nothing about the split can tell the difference.

Walk-forward asks the question repeatedly. Fit on a rolling window, trade the
window that immediately follows, roll forward, repeat. What comes out is not a
number but a distribution: how many of those out-of-sample windows made money,
how bad the worst one was, and whether the fitted parameters kept changing.

The last point is the one most people skip. A strategy whose best parameters
are different in every window has no stable edge; it has a curve fitter. The
``param_stability_pct`` figure below is there to catch exactly that.
"""

from __future__ import annotations

import statistics
import threading
import time
from typing import Any

import pandas as pd

from . import backtest as bt
from . import research
from . import strategies as st
from .exchange import INTERVAL_MS

# Defaults in calendar days, converted to bars per interval. A year of fitting
# and a quarter of trading is the usual compromise: long enough to see more
# than one regime, short enough to leave several windows in three years of data.
TRAIN_DAYS = 365
TEST_DAYS = 90
MIN_WINDOWS = 3


def bars_for(interval: str, days: int) -> int:
    step_ms = INTERVAL_MS.get(interval)
    if not step_ms:
        raise ValueError(f"unknown interval: {interval}")
    return max(1, round(days * 86_400_000 / step_ms))


def _fit(train: pd.DataFrame, cls: type[st.Strategy],
         risk_grid: list[dict[str, float]]) -> tuple[dict[str, Any], dict[str, float], float]:
    """Best (params, risk) on this window, by the same score research uses."""
    best_params: dict[str, Any] = cls.defaults()
    best_risk: dict[str, float] = risk_grid[0]
    best_score = float("-inf")
    for params in cls.grid():
        for risk in risk_grid:
            try:
                result = research.evaluate(train, cls(**params), risk)
            except Exception:
                continue
            score = bt.robust_score(result.metrics)
            if score > best_score:
                best_score, best_params, best_risk = score, params, risk
    return best_params, best_risk, best_score


def walk_forward(
    df: pd.DataFrame,
    strategy_key: str,
    *,
    train_bars: int,
    test_bars: int,
    risk_grid: list[dict[str, float]] | None = None,
    fixed_params: dict[str, Any] | None = None,
    fixed_risk: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Roll a fit/trade window across the whole history and report every step.

    With ``fixed_params`` the fitting step is skipped and one parameter set is
    carried through every window. That is the question worth asking about a
    strategy already running live: not "could a refit have made money here?"
    but "does the configuration actually deployed survive these periods?".
    """
    cls = st.REGISTRY[strategy_key]
    risk_grid = risk_grid or research.RISK_GRID
    windows: list[dict[str, Any]] = []

    start = 0
    while start + train_bars + test_bars <= len(df):
        window = df.iloc[start:start + train_bars + test_bars].reset_index(drop=True)
        train = window.iloc[:train_bars].reset_index(drop=True)
        test = window.iloc[train_bars:].reset_index(drop=True)
        if fixed_params is None:
            params, risk, train_score = _fit(train, cls, risk_grid)
        else:
            params, risk, train_score = fixed_params, (fixed_risk or risk_grid[0]), 0.0
        try:
            # The fitting window doubles as indicator warmup for the test window.
            result = research.evaluate_slice(window, train_bars, cls(**params), risk)
        except Exception:
            start += test_bars
            continue
        windows.append({
            "train_start": str(train["time"].iloc[0]),
            "test_start": str(test["time"].iloc[0]),
            "test_end": str(test["time"].iloc[-1]),
            "params": params,
            "risk": risk,
            "train_score": train_score,
            "return_pct": result.metrics["total_return_pct"],
            "buy_hold_pct": result.metrics["buy_hold_return_pct"],
            "alpha_pct": result.metrics["alpha_pct"],
            "sharpe": result.metrics["sharpe"],
            "max_drawdown_pct": result.metrics["max_drawdown_pct"],
            "trades": result.metrics["trades"],
        })
        start += test_bars

    summary = summarise(windows)
    summary["refit"] = fixed_params is None
    summary["verdict"] = verdict(summary)
    return {"strategy": strategy_key, "label": cls.label,
            "train_bars": train_bars, "test_bars": test_bars,
            "refit": fixed_params is None,
            "windows": windows, **summary}


def summarise(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Turn the per-window results into the numbers worth acting on."""
    if not windows:
        return {"window_count": 0, "verdict": "not enough history"}

    returns = [w["return_pct"] for w in windows]
    alphas = [w["alpha_pct"] for w in windows]

    # What you would actually have ended with, trading only the decisions each
    # window's fit produced — the honest headline number.
    compounded = 1.0
    for value in returns:
        compounded *= 1 + value / 100

    # How often the fit landed on the same parameters as the window before it.
    repeats = sum(1 for a, b in zip(windows, windows[1:]) if a["params"] == b["params"])
    stability = repeats / (len(windows) - 1) * 100 if len(windows) > 1 else 0.0

    positive = sum(1 for value in returns if value > 0)
    beat = sum(1 for value in alphas if value > 0)
    summary = {
        "window_count": len(windows),
        "profitable_windows": positive,
        "profitable_pct": round(positive / len(windows) * 100, 1),
        "beat_buy_hold_windows": beat,
        "beat_buy_hold_pct": round(beat / len(windows) * 100, 1),
        "compounded_return_pct": round((compounded - 1) * 100, 2),
        "median_return_pct": round(statistics.median(returns), 2),
        "mean_return_pct": round(statistics.fmean(returns), 2),
        "median_alpha_pct": round(statistics.median(alphas), 2),
        "worst_window_pct": round(min(returns), 2),
        "best_window_pct": round(max(returns), 2),
        "worst_drawdown_pct": round(min(w["max_drawdown_pct"] for w in windows), 2),
        "total_trades": sum(w["trades"] for w in windows),
        "param_stability_pct": round(stability, 1),
    }
    return summary


def verdict(summary: dict[str, Any]) -> str:
    """One line a human can act on, from the walk-forward summary.

    Deliberately harsh. Anything short of "most windows profitable, and the
    edge beat buy-and-hold more often than not" is not a strategy to fund.
    """
    if summary["window_count"] < MIN_WINDOWS:
        return "not enough windows to judge"
    if summary["compounded_return_pct"] <= 0:
        return "fails: loses money out of sample"
    if summary["profitable_pct"] < 50:
        return "fails: most windows lose, the total rests on a few outliers"
    if summary["beat_buy_hold_pct"] < 50:
        return "weak: profitable, but holding the coin beat it more often than not"
    if summary.get("refit", True) and summary["param_stability_pct"] < 34:
        return "fragile: the best parameters change almost every window"
    return "holds up out of sample"


def run(symbol: str, interval: str, strategy_key: str, *,
        train_days: int = TRAIN_DAYS, test_days: int = TEST_DAYS,
        candles: int | None = None,
        fixed_params: dict[str, Any] | None = None,
        fixed_risk: dict[str, float] | None = None) -> dict[str, Any]:
    """Load history and walk one strategy forward across all of it."""
    train_bars = bars_for(interval, train_days)
    test_bars = bars_for(interval, test_days)
    # Enough history for the first fit plus a useful number of trading windows.
    needed = candles or train_bars + test_bars * 8
    df = research.load_history(symbol, interval, needed)
    report = walk_forward(df, strategy_key, train_bars=train_bars, test_bars=test_bars,
                          fixed_params=fixed_params, fixed_risk=fixed_risk)
    report.update({
        "symbol": symbol, "interval": interval,
        "bars": len(df),
        "start": str(df["time"].iloc[0]), "end": str(df["time"].iloc[-1]),
        "train_days": train_days, "test_days": test_days,
    })
    return report


# ------------------------------------------------------- validating the live set

# Walking six allocations forward takes about ten seconds, which is too slow to
# do on every dashboard poll and far too fast to be worth a job queue. One
# background thread and a timestamp cover it.
CACHE_SECONDS = 30 * 60
_cache: dict[str, Any] = {"status": "idle", "reports": [], "checked_at": 0.0}
_lock = threading.Lock()


def _describe(report: dict[str, Any], allocation: dict[str, Any]) -> dict[str, Any]:
    """The fields the dashboard needs, without the per-window detail."""
    return {
        "symbol": report["symbol"],
        "interval": report["interval"],
        "strategy": report["strategy"],
        "label": report["label"],
        "verdict": report["verdict"],
        "passes": report["verdict"] == "holds up out of sample",
        "window_count": report.get("window_count", 0),
        "profitable_pct": report.get("profitable_pct", 0.0),
        "beat_buy_hold_pct": report.get("beat_buy_hold_pct", 0.0),
        "compounded_return_pct": report.get("compounded_return_pct", 0.0),
        "median_return_pct": report.get("median_return_pct", 0.0),
        "worst_window_pct": report.get("worst_window_pct", 0.0),
        "worst_drawdown_pct": report.get("worst_drawdown_pct", 0.0),
        "total_trades": report.get("total_trades", 0),
        "train_days": report["train_days"],
        "test_days": report["test_days"],
        "params": allocation.get("params"),
        "windows": report["windows"],
    }


def validate_allocations(allocations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Walk each live allocation forward on the parameters it actually trades.

    Not a refit. The question is whether the configuration currently deployed
    would have survived the last few years, which is a different and much less
    flattering question than whether some configuration would have.
    """
    reports = []
    for allocation in allocations:
        try:
            report = run(allocation["symbol"], allocation["interval"], allocation["strategy"],
                         fixed_params=allocation.get("params"),
                         fixed_risk=allocation.get("risk"))
        except Exception as exc:
            reports.append({
                "symbol": allocation.get("symbol"), "interval": allocation.get("interval"),
                "strategy": allocation.get("strategy"), "label": allocation.get("strategy"),
                "verdict": f"could not validate: {exc}", "passes": False,
                "window_count": 0, "windows": [],
            })
            continue
        reports.append(_describe(report, allocation))
    return reports


def validation_state(allocations: list[dict[str, Any]], *, refresh: bool = False) -> dict[str, Any]:
    """Cached validation, recomputed in the background when stale."""
    with _lock:
        fresh = time.time() - _cache["checked_at"] < CACHE_SECONDS
        running = _cache["status"] == "running"
        if running or (fresh and not refresh):
            return dict(_cache)
        _cache["status"] = "running"

    def work() -> None:
        try:
            reports = validate_allocations(allocations)
            with _lock:
                _cache.update({"status": "done", "reports": reports,
                               "checked_at": time.time()})
        except Exception as exc:
            with _lock:
                _cache.update({"status": "error", "error": str(exc),
                               "checked_at": time.time()})

    threading.Thread(target=work, daemon=True).start()
    with _lock:
        return dict(_cache)
