"""Strategy research: parameter sweep, out-of-sample validation, ranking.

The pipeline exists to answer one question honestly — "does this edge survive on
data it was never fitted to?" So every candidate is optimised on the first slice
of history and then scored, untouched, on the held-out tail. Only the held-out
numbers decide the ranking; the in-sample numbers are kept for comparison, and a
wide gap between the two is the clearest overfitting tell there is.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from typing import Any, Callable

import pandas as pd

from . import backtest as bt
from . import storage
from . import strategies as st
from .config import settings
from .exchange import exchange

DEFAULT_CANDLES = 3000
TRAIN_FRACTION = 0.65
MIN_TRAIN_BARS = 300
TOP_PER_STRATEGY = 3

# Applied only to the finalists, so the sweep does not blow up combinatorially.
RISK_GRID: list[dict[str, float]] = [
    {"stop_pct": 0.0, "take_pct": 0.0, "trail_pct": 0.0},
    {"stop_pct": 0.03, "take_pct": 0.0, "trail_pct": 0.0},
    {"stop_pct": 0.05, "take_pct": 0.10, "trail_pct": 0.0},
    {"stop_pct": 0.0, "take_pct": 0.0, "trail_pct": 0.05},
    {"stop_pct": 0.0, "take_pct": 0.0, "trail_pct": 0.08},
]

_cache: dict[tuple[str, str, int], tuple[float, pd.DataFrame]] = {}
_cache_lock = threading.Lock()
CACHE_TTL = 300.0


def load_history(symbol: str, interval: str, candles: int = DEFAULT_CANDLES) -> pd.DataFrame:
    key = (symbol, interval, candles)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    frame = exchange.history(symbol, interval, candles)
    with _cache_lock:
        _cache[key] = (time.time(), frame)
    return frame


# The keys a risk dict may carry through to the backtester. Named explicitly so
# a stray key in a stored allocation cannot become a silent keyword argument.
RISK_KEYS = ("stop_pct", "take_pct", "trail_pct",
             "atr_stop_mult", "atr_trail_mult", "atr_period")


def risk_kwargs(risk: dict[str, float] | None) -> dict[str, float]:
    return {key: value for key, value in (risk or {}).items() if key in RISK_KEYS}


def evaluate(df: pd.DataFrame, strategy: st.Strategy, risk: dict[str, float]) -> bt.BacktestResult:
    return bt.run(df, strategy.signal(df), **risk_kwargs(risk))


def evaluate_slice(df: pd.DataFrame, begin: int, strategy: st.Strategy,
                   risk: dict[str, float]) -> bt.BacktestResult:
    """Backtest from ``begin`` onward, with the earlier bars used only as warmup.

    Indicators need history. Handing a strategy a bare test slice leaves a
    200-period EMA undefined for the first 200 bars of it, and a strategy that
    cannot compute its own filter simply sits out the window it was supposed to
    be judged on — an EMA-200 filter on a 90-bar window produces no trades at
    all. So the signal is computed on the whole frame and only the trading is
    restricted to the slice. No lookahead: the signal at bar t still uses only
    bars up to t.

    The slice starts flat by construction, so no position is inherited from the
    fitting period.
    """
    signal = strategy.signal(df)
    return bt.run(
        df.iloc[begin:].reset_index(drop=True),
        signal.iloc[begin:].reset_index(drop=True),
        **risk_kwargs(risk),
    )


def _split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    cut = int(len(df) * TRAIN_FRACTION)
    return df.iloc[:cut].reset_index(drop=True), df.iloc[cut:].reset_index(drop=True)


def scan_pair(
    symbol: str,
    interval: str,
    candles: int = DEFAULT_CANDLES,
    strategy_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Sweep every strategy on one symbol/interval and return scored candidates."""
    df = load_history(symbol, interval, candles)
    if len(df) < MIN_TRAIN_BARS:
        return []
    train, test = _split(df)
    cut = len(train)
    keys = strategy_keys or [k for k in st.REGISTRY if k != "buy_hold"]
    candidates: list[dict[str, Any]] = []

    for key in keys:
        cls = st.REGISTRY[key]
        scored: list[tuple[float, dict[str, Any]]] = []
        for params in cls.grid():
            try:
                result = evaluate(train, cls(**params), RISK_GRID[0])
            except Exception:
                continue
            scored.append((bt.robust_score(result.metrics), params))
        scored.sort(key=lambda item: item[0], reverse=True)

        for _, params in scored[:TOP_PER_STRATEGY]:
            best_risk, best_train = None, None
            best_risk_score = float("-inf")
            for risk in RISK_GRID:
                try:
                    result = evaluate(train, cls(**params), risk)
                except Exception:
                    continue
                score = bt.robust_score(result.metrics)
                if score > best_risk_score:
                    best_risk_score, best_risk, best_train = score, risk, result
            if best_risk is None or best_train is None:
                continue

            strategy = cls(**params)
            test_result = evaluate_slice(df, cut, strategy, best_risk)
            full_result = evaluate(df, strategy, best_risk)
            test_score = bt.robust_score(test_result.metrics)

            candidates.append({
                "symbol": symbol,
                "interval": interval,
                "strategy": key,
                "label": cls.label,
                "family": cls.family,
                "params": params,
                "risk": best_risk,
                "train": best_train.metrics,
                "test": test_result.metrics,
                "full": full_result.metrics,
                "curve": full_result.curve(),
                "score": test_score,
                "validated": is_validated(test_result.metrics, best_train.metrics),
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


def is_validated(test: dict[str, Any], train: dict[str, Any]) -> bool:
    """A candidate passes only if the held-out slice is genuinely profitable.

    Beating buy-and-hold is required too: on a market that simply went up, a
    strategy that trails it is worse than doing nothing. The consistency floor
    rejects edges that came from a single fortunate stretch.
    """
    return bool(
        test.get("trades", 0) >= 3
        and test.get("total_return_pct", 0) > 0
        and test.get("sharpe", 0) > 0.3
        and test.get("total_return_pct", 0) > test.get("buy_hold_return_pct", 0)
        and test.get("consistency_pct", 0) >= 50
        and train.get("total_return_pct", 0) > 0
    )


# --------------------------------------------------------------------- runner

class ResearchJob:
    """Runs a full scan on a worker thread and streams progress into SQLite."""

    def __init__(self, symbols: list[str], intervals: list[str], candles: int,
                 strategy_keys: list[str] | None = None):
        self.symbols = symbols
        self.intervals = intervals
        self.candles = candles
        self.strategy_keys = strategy_keys
        self.run_id: int | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        config = {
            "symbols": self.symbols,
            "intervals": self.intervals,
            "candles": self.candles,
            "strategies": self.strategy_keys,
            "train_fraction": TRAIN_FRACTION,
        }
        cursor = storage.execute(
            "INSERT INTO research_runs(created_at, status, config, progress, total, stage) "
            "VALUES(?, 'running', ?, 0, ?, ?)",
            (storage.now(), json.dumps(config),
             len(self.symbols) * len(self.intervals), "starting"),
        )
        self.run_id = int(cursor.lastrowid)
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()
        return self.run_id

    def _work(self) -> None:
        done = 0
        try:
            for symbol in self.symbols:
                for interval in self.intervals:
                    storage.execute(
                        "UPDATE research_runs SET stage = ? WHERE id = ?",
                        (f"{symbol} {interval}", self.run_id),
                    )
                    candidates = scan_pair(symbol, interval, self.candles, self.strategy_keys)
                    self._persist(candidates)
                    done += 1
                    storage.execute(
                        "UPDATE research_runs SET progress = ? WHERE id = ?",
                        (done, self.run_id),
                    )
            storage.execute(
                "UPDATE research_runs SET status = 'done', finished_at = ?, stage = 'complete' "
                "WHERE id = ?",
                (storage.now(), self.run_id),
            )
            storage.log_event("info", "Research run finished", {"run_id": self.run_id})
        except Exception as exc:  # keep the failure visible in the dashboard
            storage.execute(
                "UPDATE research_runs SET status = 'error', error = ?, finished_at = ? WHERE id = ?",
                (f"{exc}\n{traceback.format_exc()[-800:]}", storage.now(), self.run_id),
            )
            storage.log_event("error", f"Research run failed: {exc}", {"run_id": self.run_id})

    def _persist(self, candidates: list[dict[str, Any]]) -> None:
        for candidate in candidates:
            storage.execute(
                "INSERT INTO research_results(run_id, symbol, interval, strategy, label, family, "
                "params, risk, train, test, full, curve, score, validated, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.run_id, candidate["symbol"], candidate["interval"],
                    candidate["strategy"], candidate["label"], candidate["family"],
                    json.dumps(candidate["params"]), json.dumps(candidate["risk"]),
                    json.dumps(candidate["train"]), json.dumps(candidate["test"]),
                    json.dumps(candidate["full"]), json.dumps(candidate["curve"]),
                    candidate["score"], int(candidate["validated"]), storage.now(),
                ),
            )


_current: ResearchJob | None = None


def start_run(symbols: list[str] | None = None, intervals: list[str] | None = None,
              candles: int = DEFAULT_CANDLES,
              strategy_keys: list[str] | None = None) -> dict[str, Any]:
    global _current
    active = storage.query_one("SELECT id FROM research_runs WHERE status = 'running'")
    if active:
        return {"started": False, "run_id": active["id"], "reason": "a run is already active"}
    _current = ResearchJob(
        symbols or settings.symbols, intervals or settings.intervals, candles, strategy_keys
    )
    run_id = _current.start()
    storage.log_event("info", "Research run started", {"run_id": run_id})
    return {"started": True, "run_id": run_id}


def run_status(run_id: int | None = None) -> dict[str, Any] | None:
    if run_id:
        row = storage.query_one("SELECT * FROM research_runs WHERE id = ?", (run_id,))
    else:
        row = storage.query_one("SELECT * FROM research_runs ORDER BY id DESC LIMIT 1")
    if not row:
        return None
    row["config"] = json.loads(row["config"])
    row["results"] = storage.query_one(
        "SELECT COUNT(*) AS n FROM research_results WHERE run_id = ?", (row["id"],)
    )["n"]
    return row


def leaderboard(run_id: int | None = None, limit: int = 40,
                only_validated: bool = False) -> list[dict[str, Any]]:
    if run_id is None:
        latest = storage.query_one(
            "SELECT id FROM research_runs ORDER BY id DESC LIMIT 1"
        )
        if not latest:
            return []
        run_id = latest["id"]
    clause = " AND validated = 1" if only_validated else ""
    rows = storage.query(
        f"SELECT * FROM research_results WHERE run_id = ?{clause} "
        "ORDER BY score DESC LIMIT ?",
        (run_id, limit),
    )
    return [_hydrate(row) for row in rows]


def _hydrate(row: dict[str, Any]) -> dict[str, Any]:
    for field in ("params", "risk", "train", "test", "full", "curve"):
        row[field] = json.loads(row[field])
    row["validated"] = bool(row["validated"])
    return row


def result_by_id(result_id: int) -> dict[str, Any] | None:
    row = storage.query_one("SELECT * FROM research_results WHERE id = ?", (result_id,))
    return _hydrate(row) if row else None
