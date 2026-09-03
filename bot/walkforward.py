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

import math
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


# How far a window has to travel, measured in its own volatility, before the
# move counts as a direction rather than noise. A fixed percentage cannot do
# this job: +15% over a quarter is a strong trend in a coin that moves 20% a
# quarter and is indistinguishable from noise in one that moves 60%. Dividing
# the move by the window's own standard deviation asks the question that
# actually matters to a strategy - was there a direction to catch - in units
# that mean the same thing for every symbol.
#
# 0.75 is chosen so the three labels come out roughly balanced across the book;
# at 1.0 almost everything reads as chop, and at 0.5 almost nothing does.
REGIME_SNR = 0.75

REGIME_LABELS = {"bull": "alta", "bear": "baixa", "chop": "lateral"}


def window_volatility_pct(frame: pd.DataFrame) -> float:
    """Standard deviation of the window's total return, in percent.

    Per-bar volatility scaled by the square root of the bar count, which is how
    dispersion accumulates under a random walk. This is the yardstick the move
    is measured against, not a forecast of anything.
    """
    returns = frame["close"].pct_change().dropna()
    if len(returns) < 3:
        return 0.0
    return float(returns.std() * math.sqrt(len(returns)) * 100)


def label_regime(buy_hold_pct: float, volatility_pct: float) -> tuple[str, float]:
    """(regime, signal-to-noise) for one test window.

    Deliberately derived from buy-and-hold and volatility only - never from the
    strategy's own result. A regime labelled by how the strategy did would make
    "this strategy wins in trends" true by construction.
    """
    if volatility_pct <= 0:
        return "chop", 0.0
    snr = buy_hold_pct / volatility_pct
    if snr >= REGIME_SNR:
        return "bull", snr
    if snr <= -REGIME_SNR:
        return "bear", snr
    return "chop", snr


def regime_summary(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-regime performance, in the order alta / baixa / lateral.

    This is the slice the aggregate hides. A strategy with eight windows, five
    profitable and a positive median, can be a trend follower that made all of
    it in two bull windows and bled through every flat one - which is a
    different proposition from one that grinds out a small win in all three
    conditions, and matters enormously when the next quarter is flat.
    """
    out = []
    for key in ("bull", "bear", "chop"):
        group = [w for w in windows if w.get("regime") == key]
        if not group:
            continue
        returns = [w["return_pct"] for w in group]
        alphas = [w["alpha_pct"] for w in group]
        positive = sum(1 for value in returns if value > 0)
        out.append({
            "regime": key,
            "label": REGIME_LABELS[key],
            "windows": len(group),
            "profitable_windows": positive,
            "profitable_pct": round(positive / len(group) * 100, 1),
            "median_return_pct": round(statistics.median(returns), 2),
            "median_alpha_pct": round(statistics.median(alphas), 2),
            "worst_pct": round(min(returns), 2),
            "best_pct": round(max(returns), 2),
            "median_buy_hold_pct": round(
                statistics.median(w["buy_hold_pct"] for w in group), 2),
            "trades": sum(w["trades"] for w in group),
        })
    return out


def regime_verdict(regimes: list[dict[str, Any]]) -> str:
    """One line on how the edge is distributed across conditions.

    Two different failures are worth separating, because they call for opposite
    responses. Losing money in a regime is a reason to stop trading it. Making
    money in a regime while holding the coin would have made more is not - the
    strategy is not broken there, it is simply not what it is for - but it does
    mean the profit from that regime is not evidence of an edge, and reporting
    it as one is how a bull market gets mistaken for a signal.
    """
    scored = [r for r in regimes if r["windows"] >= 2]
    if len(scored) < 2:
        return "poucas janelas por regime para separar"
    losing = [r["label"] for r in scored if r["median_return_pct"] <= 0]
    if losing:
        if len(losing) == len(scored):
            return "não ganha em nenhum regime com amostra suficiente"
        return "perde em " + " e ".join(losing)
    lagging = [r["label"] for r in scored if r["beat_buy_hold_pct"] < 50]
    if not lagging:
        return "ganha em todos os regimes medidos"
    if len(lagging) == len(scored):
        return "lucra em todo regime, mas segurar rende mais em todos"
    return ("lucra em todos, mas fica atrás de segurar em "
            + " e ".join(lagging))


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
        volatility = window_volatility_pct(test)
        regime, snr = label_regime(result.metrics["buy_hold_return_pct"], volatility)
        windows.append({
            "train_start": str(train["time"].iloc[0]),
            "test_start": str(test["time"].iloc[0]),
            "test_end": str(test["time"].iloc[-1]),
            "volatility_pct": round(volatility, 2),
            "signal_to_noise": round(snr, 2),
            "regime": regime,
            "regime_label": REGIME_LABELS[regime],
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
    summary["regimes"] = regime_summary(windows)
    summary["regime_verdict"] = regime_verdict(summary["regimes"])
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
        "regimes": report.get("regimes", []),
        "regime_verdict": report.get("regime_verdict", ""),
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


def book_regimes(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Pool every allocation's windows by regime, for the book as a whole.

    Each window is one observation of "this allocation, in this condition", so
    pooling across symbols is asking how often the *book* wins in a trend, a
    decline or a flat market. Seventeen allocations of eight windows is 136
    observations, which is enough to separate three buckets - one allocation's
    eight is not, which is why the same slice is worth showing twice at two
    different scales.

    The number to look at is the flat bucket. Reversion and breakout rules both
    tend to be profitable in a direction and both tend to bleed sideways, and a
    book assembled from strategies that all quietly depend on movement has a
    concentration that no correlation matrix shows.
    """
    pooled: dict[str, list[dict[str, Any]]] = {"bull": [], "bear": [], "chop": []}
    for report in reports:
        for window in report.get("windows") or []:
            key = window.get("regime")
            if key in pooled:
                pooled[key].append(window)

    rows, total = [], sum(len(group) for group in pooled.values())
    for key, group in pooled.items():
        if not group:
            continue
        returns = [w["return_pct"] for w in group]
        alphas = [w["alpha_pct"] for w in group]
        positive = sum(1 for value in returns if value > 0)
        beat = sum(1 for value in alphas if value > 0)
        rows.append({
            "regime": key,
            "label": REGIME_LABELS[key],
            "windows": len(group),
            "share_pct": round(len(group) / total * 100, 1) if total else 0.0,
            "profitable_pct": round(positive / len(group) * 100, 1),
            "beat_buy_hold_pct": round(beat / len(group) * 100, 1),
            "median_return_pct": round(statistics.median(returns), 2),
            "mean_return_pct": round(statistics.fmean(returns), 2),
            "median_alpha_pct": round(statistics.median(alphas), 2),
            "worst_pct": round(min(returns), 2),
            "median_buy_hold_pct": round(
                statistics.median(w["buy_hold_pct"] for w in group), 2),
            "median_volatility_pct": round(
                statistics.median(w.get("volatility_pct") or 0.0 for w in group), 2),
            "trades": sum(w["trades"] for w in group),
        })
    rows.sort(key=lambda row: ("bull", "bear", "chop").index(row["regime"]))
    return {"rows": rows, "windows": total, "verdict": regime_verdict(rows)}


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
