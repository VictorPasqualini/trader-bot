"""Vectorised technical indicators.

Every function returns a Series aligned to the input index and uses only past
data at each point, so a value at bar ``t`` is safe to act on at bar ``t + 1``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return true_range(high, low, close).ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean()


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return macd_line, signal_line, macd_line - signal_line


def bollinger(series: pd.Series, period: int = 20, mult: float = 2.0):
    mid = sma(series, period)
    dev = series.rolling(period, min_periods=period).std(ddof=0)
    return mid - mult * dev, mid, mid + mult * dev


def donchian(high: pd.Series, low: pd.Series, period: int = 20):
    """Prior-bar channel: shifted so the current bar cannot see its own extreme."""
    upper = high.rolling(period, min_periods=period).max().shift(1)
    lower = low.rolling(period, min_periods=period).min().shift(1)
    return lower, upper


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr_ = atr(high, low, close, period).replace(0.0, np.nan)
    alpha = 1 / period
    plus_di = 100 * pd.Series(plus_dm, index=high.index).ewm(
        alpha=alpha, adjust=False, min_periods=period).mean() / atr_
    minus_di = 100 * pd.Series(minus_dm, index=high.index).ewm(
        alpha=alpha, adjust=False, min_periods=period).mean() / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean().fillna(0.0)


def supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
               period: int = 10, mult: float = 3.0) -> pd.Series:
    """+1 while the trend is up, -1 while it is down.

    Written as an explicit loop because each band depends on the previous one;
    the recursion has no vectorised equivalent.
    """
    atr_ = atr(high, low, close, period)
    hl2 = (high + low) / 2
    upper = (hl2 + mult * atr_).to_numpy()
    lower = (hl2 - mult * atr_).to_numpy()
    close_arr = close.to_numpy()

    n = len(close)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    trend = np.ones(n)

    for i in range(1, n):
        if np.isnan(upper[i]):
            continue
        prev_upper = final_upper[i - 1] if not np.isnan(final_upper[i - 1]) else upper[i]
        prev_lower = final_lower[i - 1] if not np.isnan(final_lower[i - 1]) else lower[i]

        final_upper[i] = (upper[i] if upper[i] < prev_upper or close_arr[i - 1] > prev_upper
                          else prev_upper)
        final_lower[i] = (lower[i] if lower[i] > prev_lower or close_arr[i - 1] < prev_lower
                          else prev_lower)

        if trend[i - 1] == 1:
            trend[i] = -1 if close_arr[i] < final_lower[i] else 1
        else:
            trend[i] = 1 if close_arr[i] > final_upper[i] else -1

    return pd.Series(trend, index=close.index)


def roc(series: pd.Series, period: int = 10) -> pd.Series:
    return series.pct_change(period) * 100


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series,
               period: int = 14, smooth: int = 3):
    lowest = low.rolling(period, min_periods=period).min()
    highest = high.rolling(period, min_periods=period).max()
    k = 100 * (close - lowest) / (highest - lowest).replace(0.0, np.nan)
    return k, k.rolling(smooth, min_periods=smooth).mean()


def zscore(series: pd.Series, period: int) -> pd.Series:
    mean = series.rolling(period, min_periods=period).mean()
    std = series.rolling(period, min_periods=period).std(ddof=0).replace(0.0, np.nan)
    return (series - mean) / std


def realised_vol(close: pd.Series, period: int = 20) -> pd.Series:
    """Annualisation is deliberately skipped: only relative level is used."""
    return close.pct_change().rolling(period, min_periods=period).std(ddof=0)
