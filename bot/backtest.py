"""Event-driven backtester and performance metrics.

Execution model, kept deliberately pessimistic so research does not flatter
itself:

* the position decided on bar ``t``'s close is filled at bar ``t + 1``'s open;
* every fill pays the taker fee plus a slippage assumption, on both sides;
* stops and targets are checked against the bar's own high/low, and when a bar
  touches both, the stop is assumed to hit first;
* after a protective exit the strategy stays flat until its own signal drops and
  turns long again, so a stop can never re-enter the position it just closed;
* ATR-based levels read the ATR of the *previous* bar, since the current bar's
  ATR includes the very high and low the stop is being tested against.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from . import indicators as ta
from .config import settings

INITIAL_EQUITY = 10_000.0


@dataclass
class BacktestResult:
    equity: pd.Series
    position: pd.Series
    trades: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def curve(self, max_points: int = 400) -> list[dict[str, Any]]:
        """Down-sampled equity curve, small enough to ship to the browser."""
        series = self.equity
        if len(series) > max_points:
            step = math.ceil(len(series) / max_points)
            series = series.iloc[::step]
        return [
            {"time": ts.isoformat(), "equity": round(float(value), 2)}
            for ts, value in series.items()
        ]


def bars_per_year(index: pd.Series | pd.DatetimeIndex) -> float:
    times = pd.to_datetime(pd.Series(index).reset_index(drop=True))
    if len(times) < 3:
        return 365.0
    seconds = times.diff().dt.total_seconds().median()
    if not seconds or math.isnan(seconds) or seconds <= 0:
        return 365.0
    return (365.25 * 24 * 3600) / seconds


def run(
    df: pd.DataFrame,
    signal: pd.Series,
    *,
    fee: float | None = None,
    slippage: float | None = None,
    stop_pct: float = 0.0,
    take_pct: float = 0.0,
    trail_pct: float = 0.0,
    atr_stop_mult: float = 0.0,
    atr_trail_mult: float = 0.0,
    atr_period: int = 14,
    initial_equity: float = INITIAL_EQUITY,
) -> BacktestResult:
    """Simulate ``signal`` over ``df`` and return the equity curve, trades and metrics.

    The percentage exits (``stop_pct``, ``take_pct``, ``trail_pct``) place levels
    a fixed distance from the entry. That distance means something different on
    every symbol and in every regime: 5% is a routine day in one asset and a
    crash in another. The ATR variants place the same levels at a multiple of
    recent true range instead, so the exit adapts to how much the symbol is
    actually moving. Percentage and ATR levels can be combined; whichever sits
    highest is the one that fires.
    """
    fee = settings.fee_rate if fee is None else fee
    slippage = settings.slippage_rate if slippage is None else slippage
    cost = fee + slippage

    open_ = df["open"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    times = pd.to_datetime(df["time"]).to_numpy()
    # Bar t acts on the signal decided at the close of bar t-1.
    target = signal.shift(1).fillna(0.0).to_numpy(dtype=float)

    # Same one-bar lag, for the same reason: the ATR of bar t is computed from
    # bar t's own high and low, and using it to place a level that bar t's high
    # and low are then tested against would be reading the answer first.
    if atr_stop_mult or atr_trail_mult:
        atr = ta.atr(df["high"], df["low"], df["close"], atr_period)
        atr_prev = atr.shift(1).to_numpy(dtype=float)
    else:
        atr_prev = np.zeros(len(df), dtype=float)

    n = len(df)
    equity = np.full(n, initial_equity, dtype=float)
    held = np.zeros(n, dtype=float)

    cash = initial_equity
    qty = 0.0
    entry_price = 0.0
    entry_index = 0
    peak_price = 0.0
    entry_atr = 0.0
    # Set when a stop or target closed a position while the entry signal was
    # still long. Without it the entry block below would re-buy on the same bar
    # at that bar's open, which is a guaranteed loss: the position is sold at
    # the stop level and bought back higher, paying both spreads for nothing.
    # A stop that fires would then be arithmetically incapable of helping, and a
    # study of stops built on that would find exactly what it built in.
    stand_aside = False
    trades: list[dict[str, Any]] = []

    def close_position(index: int, price: float, reason: str) -> None:
        nonlocal cash, qty, entry_price, peak_price, entry_atr
        proceeds = qty * price * (1 - cost)
        invested = qty * entry_price * (1 + cost)
        pnl = proceeds - invested
        trades.append({
            "entry_time": pd.Timestamp(times[entry_index]).isoformat(),
            "exit_time": pd.Timestamp(times[index]).isoformat(),
            "entry_price": round(entry_price, 8),
            "exit_price": round(price, 8),
            "qty": qty,
            "pnl": round(pnl, 4),
            "return_pct": round((proceeds / invested - 1) * 100, 4) if invested else 0.0,
            "bars": index - entry_index,
            # Decision bars: the signal acted on at bar i was decided at i - 1.
            "entry_bar": max(entry_index - 1, 0),
            "exit_bar": max(index - 1, 0),
            "reason": reason,
        })
        cash += proceeds
        qty = 0.0
        entry_price = 0.0
        peak_price = 0.0
        entry_atr = 0.0

    for i in range(n):
        price_open = open_[i]

        # The signal going flat both closes a position and clears the block: a
        # later long is a genuinely new entry, not a re-entry into the old one.
        if target[i] <= 0.0:
            stand_aside = False

        if qty > 0.0:
            # 1. Signal exit, filled at this bar's open.
            if target[i] <= 0.0:
                close_position(i, price_open, "signal")
            else:
                # 2. Protective exits, checked against the bar's own range.
                peak_price = max(peak_price, high[i])
                stop_price = 0.0
                if stop_pct:
                    stop_price = max(stop_price, entry_price * (1 - stop_pct))
                if trail_pct:
                    stop_price = max(stop_price, peak_price * (1 - trail_pct))
                # The ATR is frozen at entry rather than re-read each bar. A
                # live ATR widens the stop exactly as a position starts going
                # wrong, which is the opposite of what a stop is for.
                if atr_stop_mult and entry_atr > 0.0:
                    stop_price = max(stop_price, entry_price - atr_stop_mult * entry_atr)
                if atr_trail_mult and entry_atr > 0.0:
                    stop_price = max(stop_price, peak_price - atr_trail_mult * entry_atr)
                take_price = entry_price * (1 + take_pct) if take_pct else 0.0

                if stop_price and low[i] <= stop_price:
                    close_position(i, min(stop_price, price_open), "stop")
                    stand_aside = True
                elif take_price and high[i] >= take_price:
                    close_position(i, max(take_price, price_open), "target")
                    stand_aside = True

        if qty == 0.0 and not stand_aside and target[i] > 0.0 and i < n - 1:
            qty = cash / (price_open * (1 + cost))
            cash = 0.0
            entry_price = price_open
            entry_index = i
            peak_price = high[i]
            reference = atr_prev[i] if i < len(atr_prev) else 0.0
            entry_atr = float(reference) if reference == reference else 0.0

        held[i] = 1.0 if qty > 0.0 else 0.0
        equity[i] = cash + qty * close[i]

    if qty > 0.0:
        close_position(n - 1, close[n - 1], "end")
        equity[n - 1] = cash

    index = pd.DatetimeIndex(times, name="time")
    equity_series = pd.Series(equity, index=index)
    result = BacktestResult(
        equity=equity_series,
        position=pd.Series(held, index=index),
        trades=trades,
    )
    result.metrics = compute_metrics(equity_series, trades, held, df)
    return result


def consistency(equity: pd.Series, blocks: int = 8) -> float:
    """Share of equal-length sub-periods that ended in profit.

    A strategy whose whole edge comes from one lucky window scores low here even
    when its total return looks fine, which is exactly the case worth catching.
    """
    values = equity.to_numpy(dtype=float)
    if len(values) < blocks * 2:
        return 0.0
    edges = np.linspace(0, len(values) - 1, blocks + 1).astype(int)
    wins = sum(
        1 for i in range(blocks)
        if values[edges[i]] > 0 and values[edges[i + 1]] > values[edges[i]]
    )
    return round(wins / blocks * 100, 1)


def compute_metrics(
    equity: pd.Series, trades: list[dict[str, Any]], held: np.ndarray, df: pd.DataFrame
) -> dict[str, Any]:
    values = equity.to_numpy(dtype=float)
    start, end = float(values[0]), float(values[-1])
    returns = pd.Series(values).pct_change().fillna(0.0)
    periods = bars_per_year(df["time"])

    total_return = (end / start - 1) * 100 if start else 0.0
    years = max(len(values) / periods, 1e-9)
    cagr = ((end / start) ** (1 / years) - 1) * 100 if start > 0 and end > 0 else -100.0

    std = returns.std(ddof=0)
    sharpe = float(returns.mean() / std * math.sqrt(periods)) if std > 0 else 0.0
    downside = returns[returns < 0].std(ddof=0)
    sortino = float(returns.mean() / downside * math.sqrt(periods)) if downside > 0 else 0.0

    running_peak = np.maximum.accumulate(values)
    drawdowns = values / running_peak - 1
    max_dd = float(drawdowns.min() * 100)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)

    buy_hold = (float(df["close"].iloc[-1]) / float(df["close"].iloc[0]) - 1) * 100

    return {
        "initial_equity": round(start, 2),
        "final_equity": round(end, 2),
        "total_return_pct": round(total_return, 2),
        "buy_hold_return_pct": round(buy_hold, 2),
        "alpha_pct": round(total_return - buy_hold, 2),
        "cagr_pct": round(cagr, 2),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "max_drawdown_pct": round(max_dd, 2),
        "calmar": round(cagr / abs(max_dd), 3) if max_dd < 0 else 0.0,
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else (
            999.0 if gross_win > 0 else 0.0),
        "avg_trade_pct": round(float(np.mean([t["return_pct"] for t in trades])), 3)
        if trades else 0.0,
        "best_trade_pct": round(max((t["return_pct"] for t in trades), default=0.0), 2),
        "worst_trade_pct": round(min((t["return_pct"] for t in trades), default=0.0), 2),
        "avg_bars_held": round(float(np.mean([t["bars"] for t in trades])), 1) if trades else 0.0,
        "exposure_pct": round(float(held.mean()) * 100, 2),
        "consistency_pct": consistency(equity),
        "bars": len(values),
        "start": pd.Timestamp(df["time"].iloc[0]).isoformat(),
        "end": pd.Timestamp(df["time"].iloc[-1]).isoformat(),
    }


def robust_score(metrics: dict[str, Any]) -> float:
    """Rank candidates by risk-adjusted return, not by raw profit.

    Sharpe is the backbone. A sample-size penalty keeps a lucky three-trade run
    from topping the table, and drawdown is charged against the score so a
    strategy that doubles money through an 80% drawdown does not look good.
    """
    trades = metrics.get("trades", 0)
    if trades < 3:
        return -99.0
    sharpe = metrics.get("sharpe", 0.0)
    drawdown = abs(metrics.get("max_drawdown_pct", 0.0)) / 100
    confidence = min(1.0, math.sqrt(trades / 20))
    # Reward edges that show up across the period rather than in one window.
    steadiness = 0.6 + 0.8 * (metrics.get("consistency_pct", 50.0) / 100)
    return round((sharpe * confidence - drawdown * 1.5) * steadiness, 4)
