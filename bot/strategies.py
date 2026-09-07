"""Strategy library.

A strategy maps an OHLCV frame to a target position series in ``{0, 1}``
(spot is long-only: 1 means "hold the asset", 0 means "hold quote"). The value
at bar ``t`` is decided from data up to and including bar ``t``'s close, and the
backtester acts on it at bar ``t + 1``'s open, so nothing peeks ahead.

``grid()`` returns the parameter combinations the research engine sweeps.

Each strategy also exposes ``indicators()``, the named series behind its
decision. ``signal()`` and ``indicators()`` both derive from a private
``_series()`` so the trade log can never drift from what actually traded.
"""

from __future__ import annotations

import itertools
from typing import Any, Iterable

import numpy as np
import pandas as pd

from . import indicators as ta

REGISTRY: dict[str, type["Strategy"]] = {}


def register(cls: type["Strategy"]) -> type["Strategy"]:
    REGISTRY[cls.key] = cls
    return cls


def expand(grid: dict[str, Iterable[Any]]) -> list[dict[str, Any]]:
    """Cartesian product of a parameter grid, as a list of kwarg dicts."""
    keys = list(grid)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(grid[k] for k in keys))]


class Strategy:
    key: str = "base"
    label: str = "Base"
    description: str = ""
    family: str = "other"
    entry_rule: str = ""
    exit_rule: str = ""

    def __init__(self, **params: Any) -> None:
        self.params = {**self.defaults(), **params}

    @classmethod
    def defaults(cls) -> dict[str, Any]:
        return {}

    @classmethod
    def grid(cls) -> list[dict[str, Any]]:
        return [cls.defaults()]

    def signal(self, df: pd.DataFrame) -> pd.Series:  # pragma: no cover - interface
        raise NotImplementedError

    def indicators(self, df: pd.DataFrame) -> dict[str, pd.Series]:
        """Named series behind the decision, for the trade log."""
        return {}

    # -- shared helpers ----------------------------------------------------

    @staticmethod
    def _hold(entry: pd.Series, exit_: pd.Series) -> pd.Series:
        """Turn entry/exit pulses into a held 0/1 position.

        Entry wins on a bar where both fire, which keeps trend strategies from
        being shaken out by the same bar that triggered them.
        """
        state = np.zeros(len(entry), dtype=float)
        entry_arr = entry.fillna(False).to_numpy(dtype=bool)
        exit_arr = exit_.fillna(False).to_numpy(dtype=bool)
        holding = False
        for i in range(len(state)):
            if entry_arr[i]:
                holding = True
            elif exit_arr[i]:
                holding = False
            state[i] = 1.0 if holding else 0.0
        return pd.Series(state, index=entry.index)

    def trigger(self, kind: str = "entry") -> tuple[str, str, str | float] | None:
        """The two numbers whose comparison is the whole decision.

        ``indicators`` returns everything behind a trade, which is right for an
        audit and wrong for a glance: one of those numbers crossed another and
        the rest are context. Naming the pair lets the interface print the
        decision itself - "1.3911 above 1.3600" - instead of a list the reader
        has to reduce in their head. The left side is a measured series; the
        right side is either another series or a fixed level.
        """
        return None

    def reading(self, df: pd.DataFrame, kind: str = "entry", *, bar: int = -1,
                series: dict[str, pd.Series] | None = None) -> dict[str, Any] | None:
        """The trigger pair, evaluated on one bar of ``df``.

        ``distance_pct`` is how far the measured side sits from the level it
        has to cross, as a share of that level. It answers "how close is this
        to firing", which is the question an operator watching seventeen
        allocations actually has.

        ``bar`` defaults to the last row, which is what a live reading wants.
        Replaying a history of trades asks the same question at dozens of past
        bars over the same frame, so ``series`` takes an indicator set that has
        already been computed instead of recomputing it once per trade.
        """
        pair = self.trigger(kind)
        if not pair:
            return None
        left_name, operator, right = pair
        if series is None:
            series = self.indicators(df)
        if left_name not in series:
            return None
        try:
            left_value = float(series[left_name].iloc[bar])
        except (TypeError, ValueError, IndexError):
            return None
        if isinstance(right, str):
            if right not in series:
                return None
            try:
                right_value = float(series[right].iloc[bar])
            except (TypeError, ValueError, IndexError):
                return None
            right_name: str | None = right
        else:
            right_value, right_name = float(right), None
        if left_value != left_value or right_value != right_value:  # NaN
            return None
        met = {">": left_value > right_value, ">=": left_value >= right_value,
               "<": left_value < right_value, "<=": left_value <= right_value,
               }[operator]
        # A level of zero has no percentage to be far from, and a strategy
        # that crosses zero - MACD, momentum, Supertrend - is exactly the case
        # where a percentage would read as 100% no matter how close the call
        # was. There the raw gap is the only honest number.
        gap = left_value - right_value
        return {
            "left": left_name, "left_value": round(left_value, 8),
            "operator": operator,
            "right": right_name, "right_value": round(right_value, 8),
            "met": met,
            "gap": round(gap, 8),
            "distance_pct": (round(gap / abs(right_value) * 100, 2)
                             if right_value else None),
        }

    def explain(self, df: pd.DataFrame, kind: str = "entry") -> dict[str, Any]:
        """Indicator values at the last bar, recorded against each trade.

        This is what turns "it bought" into "it bought because price closed at
        1.39 with the upper band at 1.36".
        """
        values: dict[str, float] = {}
        for name, series in self.indicators(df).items():
            try:
                value = float(series.iloc[-1])
            except (TypeError, ValueError, IndexError):
                continue
            if value == value:  # NaN fails this
                values[name] = round(value, 8)
        return {
            "strategy": self.label if isinstance(self.label, str) else str(self.label),
            "entry_rule": self.entry_rule,
            "exit_rule": self.exit_rule,
            "params": self.params,
            "values": values,
            "trigger": self.reading(df, kind),
        }

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "family": self.family,
            "description": self.description,
            "entry_rule": self.entry_rule,
            "exit_rule": self.exit_rule,
            "params": self.params,
        }


@register
class EmaCross(Strategy):
    key = "ema_cross"
    label = "EMA Crossover"
    family = "trend"
    description = "Buys when the fast EMA crosses above the slow EMA, exits on the reverse cross."
    entry_rule = "fast EMA rises above the slow EMA"
    exit_rule = "fast EMA falls back below the slow EMA"

    @classmethod
    def defaults(cls):
        return {"fast": 12, "slow": 50, "trend": 200}

    @classmethod
    def grid(cls):
        return [
            p for p in expand({"fast": [8, 12, 21, 34], "slow": [50, 80, 120], "trend": [0, 200]})
            if p["fast"] < p["slow"]
        ]

    def _series(self, df):
        fast = ta.ema(df["close"], self.params["fast"])
        slow = ta.ema(df["close"], self.params["slow"])
        period = self.params.get("trend", 0)
        trend = ta.ema(df["close"], period) if period else None
        return fast, slow, trend


    def trigger(self, kind="entry"):
        fast = f"EMA {self.params['fast']}"
        slow = f"EMA {self.params['slow']}"
        return (fast, ">", slow) if kind == "entry" else (fast, "<", slow)

    def indicators(self, df):
        fast, slow, trend = self._series(df)
        out = {
            "price": df["close"],
            f"EMA {self.params['fast']}": fast,
            f"EMA {self.params['slow']}": slow,
        }
        if trend is not None:
            out[f"EMA {self.params['trend']} (trend filter)"] = trend
        return out

    def signal(self, df):
        fast, slow, trend = self._series(df)
        long = fast > slow
        if trend is not None:
            long &= df["close"] > trend
        return long.astype(float).fillna(0.0)


@register
class MacdTrend(Strategy):
    key = "macd_trend"
    label = "MACD Trend"
    family = "trend"
    description = "Holds while the MACD histogram is positive and price sits above its trend EMA."
    entry_rule = "MACD histogram turns positive"
    exit_rule = "MACD histogram turns negative"

    @classmethod
    def defaults(cls):
        return {"fast": 12, "slow": 26, "signal": 9, "trend": 200}

    @classmethod
    def grid(cls):
        return expand({
            "fast": [8, 12], "slow": [21, 26, 34], "signal": [9], "trend": [0, 100, 200],
        })

    def _series(self, df):
        line, sig, hist = ta.macd(
            df["close"], self.params["fast"], self.params["slow"], self.params["signal"]
        )
        period = self.params.get("trend", 0)
        trend = ta.ema(df["close"], period) if period else None
        return line, sig, hist, trend


    def trigger(self, kind="entry"):
        return ("histogram", ">", 0.0) if kind == "entry" else ("histogram", "<", 0.0)

    def indicators(self, df):
        line, sig, hist, trend = self._series(df)
        out = {"price": df["close"], "MACD": line, "signal line": sig, "histogram": hist}
        if trend is not None:
            out[f"EMA {self.params['trend']} (trend filter)"] = trend
        return out

    def signal(self, df):
        _, _, hist, trend = self._series(df)
        long = hist > 0
        if trend is not None:
            long &= df["close"] > trend
        return long.astype(float).fillna(0.0)


@register
class Supertrend(Strategy):
    key = "supertrend"
    label = "Supertrend"
    family = "trend"
    description = "ATR-banded trend follower; long while the Supertrend flips bullish."
    entry_rule = "Supertrend flips bullish"
    exit_rule = "Supertrend flips bearish"

    @classmethod
    def defaults(cls):
        return {"period": 10, "mult": 3.0}

    @classmethod
    def grid(cls):
        return expand({"period": [7, 10, 14, 20], "mult": [2.0, 2.5, 3.0, 4.0]})

    def _series(self, df):
        return ta.supertrend(df["high"], df["low"], df["close"],
                             self.params["period"], self.params["mult"])


    def trigger(self, kind="entry"):
        name = "Supertrend direction"
        return (name, ">", 0.0) if kind == "entry" else (name, "<", 0.0)

    def indicators(self, df):
        return {
            "price": df["close"],
            "Supertrend direction": self._series(df),
            f"ATR {self.params['period']}": ta.atr(
                df["high"], df["low"], df["close"], self.params["period"]),
        }

    def signal(self, df):
        return (self._series(df) > 0).astype(float)


@register
class DonchianBreakout(Strategy):
    key = "donchian_breakout"
    label = "Donchian Breakout"
    family = "breakout"
    description = "Turtle-style: buys N-bar highs, exits on M-bar lows."
    entry_rule = "price closes above the N-bar high"
    exit_rule = "price closes below the M-bar low"

    @classmethod
    def defaults(cls):
        return {"entry": 20, "exit": 10}

    @classmethod
    def grid(cls):
        return [
            p for p in expand({"entry": [20, 30, 55, 80], "exit": [10, 15, 20, 30]})
            if p["exit"] <= p["entry"]
        ]

    def _series(self, df):
        _, upper = ta.donchian(df["high"], df["low"], self.params["entry"])
        lower, _ = ta.donchian(df["high"], df["low"], self.params["exit"])
        return upper, lower


    def trigger(self, kind="entry"):
        if kind == "entry":
            return ("price", ">", f"{self.params['entry']}-bar high")
        return ("price", "<", f"{self.params['exit']}-bar low")

    def indicators(self, df):
        upper, lower = self._series(df)
        return {
            "price": df["close"],
            f"{self.params['entry']}-bar high": upper,
            f"{self.params['exit']}-bar low": lower,
        }

    def signal(self, df):
        upper, lower = self._series(df)
        return self._hold(df["close"] > upper, df["close"] < lower)


@register
class BollingerBreakout(Strategy):
    key = "bollinger_breakout"
    label = "Bollinger Breakout"
    family = "breakout"
    description = "Rides volatility expansion: enters above the upper band, exits back at the mean."
    entry_rule = "price closes above the upper Bollinger band"
    exit_rule = "price falls back below the moving average"

    @classmethod
    def defaults(cls):
        return {"period": 20, "mult": 2.0}

    @classmethod
    def grid(cls):
        return expand({"period": [14, 20, 30, 50], "mult": [1.5, 2.0, 2.5]})

    def _series(self, df):
        return ta.bollinger(df["close"], self.params["period"], self.params["mult"])


    def trigger(self, kind="entry"):
        if kind == "entry":
            return ("price", ">", "upper band")
        return ("price", "<", f"MA {self.params['period']}")

    def indicators(self, df):
        lower, mid, upper = self._series(df)
        return {"price": df["close"], "upper band": upper,
                f"MA {self.params['period']}": mid, "lower band": lower}

    def signal(self, df):
        _, mid, upper = self._series(df)
        return self._hold(df["close"] > upper, df["close"] < mid)


@register
class BollingerReversion(Strategy):
    key = "bollinger_reversion"
    label = "Bollinger Mean Reversion"
    family = "reversion"
    description = "Fades stretched moves: buys below the lower band, exits at the mean."
    entry_rule = "price closes below the lower Bollinger band"
    exit_rule = "price recovers above the moving average"

    @classmethod
    def defaults(cls):
        return {"period": 20, "mult": 2.0, "trend": 0}

    @classmethod
    def grid(cls):
        return expand({
            "period": [14, 20, 30], "mult": [1.5, 2.0, 2.5], "trend": [0, 200],
        })

    def _series(self, df):
        lower, mid, upper = ta.bollinger(df["close"], self.params["period"], self.params["mult"])
        period = self.params.get("trend", 0)
        trend = ta.ema(df["close"], period) if period else None
        return lower, mid, upper, trend


    def trigger(self, kind="entry"):
        if kind == "entry":
            return ("price", "<", "lower band")
        return ("price", ">", f"MA {self.params['period']}")

    def indicators(self, df):
        lower, mid, upper, trend = self._series(df)
        out = {"price": df["close"], "lower band": lower,
               f"MA {self.params['period']}": mid, "upper band": upper}
        if trend is not None:
            out[f"EMA {self.params['trend']} (trend filter)"] = trend
        return out

    def signal(self, df):
        lower, mid, _, trend = self._series(df)
        entry = df["close"] < lower
        if trend is not None:
            entry &= df["close"] > trend
        return self._hold(entry, df["close"] > mid)


@register
class RsiReversion(Strategy):
    key = "rsi_reversion"
    label = "RSI Mean Reversion"
    family = "reversion"
    description = "Buys oversold RSI, exits once RSI recovers past the upper threshold."
    entry_rule = "RSI drops below the oversold threshold"
    exit_rule = "RSI recovers above the upper threshold"

    @classmethod
    def defaults(cls):
        return {"period": 14, "low": 30, "high": 55, "trend": 0}

    @classmethod
    def grid(cls):
        return expand({
            "period": [7, 14, 21], "low": [20, 25, 30, 35],
            "high": [50, 55, 65, 70], "trend": [0, 200],
        })

    def _series(self, df):
        rsi = ta.rsi(df["close"], self.params["period"])
        period = self.params.get("trend", 0)
        trend = ta.ema(df["close"], period) if period else None
        return rsi, trend


    def trigger(self, kind="entry"):
        rsi = f"RSI {self.params['period']}"
        if kind == "entry":
            return (rsi, "<", "oversold level")
        return (rsi, ">", "exit level")

    def indicators(self, df):
        rsi, trend = self._series(df)
        out = {
            "price": df["close"],
            f"RSI {self.params['period']}": rsi,
            "oversold level": pd.Series(float(self.params["low"]), index=df.index),
            "exit level": pd.Series(float(self.params["high"]), index=df.index),
        }
        if trend is not None:
            out[f"EMA {self.params['trend']} (trend filter)"] = trend
        return out

    def signal(self, df):
        rsi, trend = self._series(df)
        entry = rsi < self.params["low"]
        if trend is not None:
            entry &= df["close"] > trend
        return self._hold(entry, rsi > self.params["high"])


@register
class StochReversion(Strategy):
    key = "stoch_reversion"
    label = "Stochastic Reversion"
    family = "reversion"
    description = "Buys when %K crosses up out of oversold, exits in overbought territory."
    entry_rule = "%K crosses above %D while still near oversold"
    exit_rule = "%K reaches the overbought threshold"

    @classmethod
    def defaults(cls):
        return {"period": 14, "smooth": 3, "low": 20, "high": 80}

    @classmethod
    def grid(cls):
        return expand({
            "period": [9, 14, 21], "smooth": [3], "low": [10, 20, 30], "high": [70, 80, 90],
        })

    def _series(self, df):
        return ta.stochastic(df["high"], df["low"], df["close"],
                             self.params["period"], self.params["smooth"])


    def trigger(self, kind="entry"):
        if kind == "entry":
            return ("%K", ">", "%D")
        return ("%K", ">", "overbought level")

    def indicators(self, df):
        k, d = self._series(df)
        return {
            "price": df["close"], "%K": k, "%D": d,
            "oversold level": pd.Series(float(self.params["low"]), index=df.index),
            "overbought level": pd.Series(float(self.params["high"]), index=df.index),
        }

    def signal(self, df):
        k, d = self._series(df)
        entry = (k > d) & (k.shift(1) <= d.shift(1)) & (k < self.params["low"] + 15)
        return self._hold(entry, k > self.params["high"])


@register
class Momentum(Strategy):
    key = "momentum"
    label = "Momentum (ROC)"
    family = "momentum"
    description = "Holds while rate-of-change stays above a threshold; a plain time-series momentum sleeve."
    entry_rule = "rate of change rises above the threshold"
    exit_rule = "rate of change falls back below the threshold"

    @classmethod
    def defaults(cls):
        return {"period": 20, "threshold": 0.0, "vol_filter": 0}

    @classmethod
    def grid(cls):
        return expand({
            "period": [10, 20, 40, 60], "threshold": [0.0, 1.0, 3.0], "vol_filter": [0, 100],
        })

    def _series(self, df):
        momentum = ta.roc(df["close"], self.params["period"])
        window = self.params.get("vol_filter", 0)
        if not window:
            return momentum, None, None
        vol = ta.realised_vol(df["close"], 20)
        # Skip the noisiest regime: sit out when vol is in its own top quartile.
        cap = vol.rolling(window, min_periods=window).quantile(0.75)
        return momentum, vol, cap


    def trigger(self, kind="entry"):
        roc = f"ROC {self.params['period']}%"
        return (roc, ">", "threshold") if kind == "entry" else (roc, "<", "threshold")

    def indicators(self, df):
        momentum, vol, cap = self._series(df)
        out = {
            "price": df["close"],
            f"ROC {self.params['period']}%": momentum,
            "threshold": pd.Series(float(self.params["threshold"]), index=df.index),
        }
        if vol is not None:
            out["realised volatility"] = vol
            out["volatility cap"] = cap
        return out

    def signal(self, df):
        momentum, vol, cap = self._series(df)
        long = momentum > self.params["threshold"]
        if cap is not None:
            long &= vol < cap
        return long.astype(float).fillna(0.0)


@register
class AdxTrend(Strategy):
    key = "adx_trend"
    label = "ADX Filtered Trend"
    family = "trend"
    description = "EMA trend entries, taken only while ADX confirms a directional market."
    entry_rule = "fast EMA above slow EMA while ADX confirms a trending market"
    exit_rule = "EMA trend reverses or ADX drops below the minimum"

    @classmethod
    def defaults(cls):
        return {"fast": 20, "slow": 60, "adx_period": 14, "adx_min": 20}

    @classmethod
    def grid(cls):
        return [
            p for p in expand({
                "fast": [10, 20, 30], "slow": [50, 60, 100],
                "adx_period": [14], "adx_min": [15, 20, 25],
            }) if p["fast"] < p["slow"]
        ]

    def _series(self, df):
        fast = ta.ema(df["close"], self.params["fast"])
        slow = ta.ema(df["close"], self.params["slow"])
        strength = ta.adx(df["high"], df["low"], df["close"], self.params["adx_period"])
        return fast, slow, strength


    def trigger(self, kind="entry"):
        fast = f"EMA {self.params['fast']}"
        slow = f"EMA {self.params['slow']}"
        return (fast, ">", slow) if kind == "entry" else (fast, "<", slow)

    def indicators(self, df):
        fast, slow, strength = self._series(df)
        return {
            "price": df["close"],
            f"EMA {self.params['fast']}": fast,
            f"EMA {self.params['slow']}": slow,
            f"ADX {self.params['adx_period']}": strength,
            "ADX minimum": pd.Series(float(self.params["adx_min"]), index=df.index),
        }

    def signal(self, df):
        fast, slow, strength = self._series(df)
        return ((fast > slow) & (strength > self.params["adx_min"])).astype(float).fillna(0.0)


@register
class VwapReversion(Strategy):
    key = "vwap_reversion"
    label = "Rolling VWAP Reversion"
    family = "reversion"
    description = "Buys dips a set z-score below rolling VWAP, exits once price returns to it."
    entry_rule = "price falls the entry z-score below rolling VWAP"
    exit_rule = "price returns to the exit z-score above VWAP"

    @classmethod
    def defaults(cls):
        return {"period": 48, "entry_z": -1.5, "exit_z": 0.0}

    @classmethod
    def grid(cls):
        return expand({
            "period": [24, 48, 96], "entry_z": [-1.0, -1.5, -2.0, -2.5], "exit_z": [0.0, 0.5],
        })

    def _series(self, df):
        period = self.params["period"]
        typical = (df["high"] + df["low"] + df["close"]) / 3
        pv = (typical * df["volume"]).rolling(period, min_periods=period).sum()
        vol = df["volume"].rolling(period, min_periods=period).sum().replace(0.0, np.nan)
        vwap = pv / vol
        spread = df["close"] / vwap - 1
        return vwap, ta.zscore(spread, period)


    def trigger(self, kind="entry"):
        if kind == "entry":
            return ("z-score", "<", "entry z-score")
        return ("z-score", ">", "exit z-score")

    def indicators(self, df):
        vwap, z = self._series(df)
        return {
            "price": df["close"],
            f"VWAP {self.params['period']}": vwap,
            "z-score": z,
            "entry z-score": pd.Series(float(self.params["entry_z"]), index=df.index),
            "exit z-score": pd.Series(float(self.params["exit_z"]), index=df.index),
        }

    def signal(self, df):
        _, z = self._series(df)
        return self._hold(z < self.params["entry_z"], z > self.params["exit_z"])


@register
class Ensemble(Strategy):
    key = "ensemble"
    label = "Ensemble Vote"
    family = "ensemble"
    description = (
        "Runs a trend, a breakout and a reversion sleeve and holds when at least "
        "`min_votes` of them agree. Diversifies away single-signal luck."
    )
    entry_rule = "enough member sleeves vote long at once"
    exit_rule = "votes fall back below the minimum"

    MEMBERS: tuple[tuple[str, dict[str, Any]], ...] = (
        ("ema_cross", {"fast": 12, "slow": 50, "trend": 0}),
        ("supertrend", {"period": 10, "mult": 3.0}),
        ("donchian_breakout", {"entry": 20, "exit": 10}),
        ("macd_trend", {"fast": 12, "slow": 26, "signal": 9, "trend": 0}),
        ("rsi_reversion", {"period": 14, "low": 30, "high": 55, "trend": 0}),
    )

    @classmethod
    def defaults(cls):
        return {"min_votes": 2, "trend": 200}

    @classmethod
    def grid(cls):
        return expand({"min_votes": [1, 2, 3], "trend": [0, 100, 200]})

    def _series(self, df):
        members = {key: REGISTRY[key](**params).signal(df) for key, params in self.MEMBERS}
        period = self.params.get("trend", 0)
        trend = ta.ema(df["close"], period) if period else None
        return members, trend


    def trigger(self, kind="entry"):
        if kind == "entry":
            return ("votes", ">=", "votes required")
        return ("votes", "<", "votes required")

    def indicators(self, df):
        members, trend = self._series(df)
        out: dict[str, pd.Series] = {"price": df["close"]}
        for key, series in members.items():
            out[f"vote: {key}"] = series
        out["votes"] = sum(members.values())
        out["votes required"] = pd.Series(float(self.params["min_votes"]), index=df.index)
        if trend is not None:
            out[f"EMA {self.params['trend']} (trend filter)"] = trend
        return out

    def signal(self, df):
        members, trend = self._series(df)
        long = sum(members.values()) >= self.params["min_votes"]
        if trend is not None:
            long &= df["close"] > trend
        return long.astype(float).fillna(0.0)


@register
class BuyAndHold(Strategy):
    key = "buy_hold"
    label = "Buy & Hold"
    family = "benchmark"
    description = "Benchmark. Any strategy that cannot beat this on the same data is noise."
    entry_rule = "always in"
    exit_rule = "never exits"

    def indicators(self, df):
        return {"price": df["close"]}

    def signal(self, df):
        return pd.Series(1.0, index=df.index)


def build(key: str, params: dict[str, Any] | None = None) -> Strategy:
    if key not in REGISTRY:
        raise KeyError(f"unknown strategy: {key}")
    return REGISTRY[key](**(params or {}))


def catalog() -> list[dict[str, Any]]:
    return [
        {
            "key": cls.key,
            "label": cls.label if isinstance(cls.label, str) else cls.label[0],
            "family": cls.family,
            "description": cls.description,
            "entry_rule": cls.entry_rule,
            "exit_rule": cls.exit_rule,
            "defaults": cls.defaults(),
            "grid_size": len(cls.grid()),
        }
        for cls in REGISTRY.values()
    ]
