"""The parallel experiment: a model allowed to be wrong, on its own money.

The live book is a forward test of strategies that survived a walk-forward, and
its whole value is that nothing has interfered with it. So this module shares no
table with it. Separate positions, separate equity curve, separate ledger, paper
fills only. Isolation by schema rather than by a flag, because a flag is
something a future query forgets to filter on.

What it is allowed to use
-------------------------
Everything with real history, today:

* price and volume, five years per symbol;
* funding rate, back to 2020, collected in full;
* Fear and Greed, back to 2018;
* headline sentiment, from the day collection started - and therefore mostly
  absent, which the model is told about honestly rather than by imputing zero.

Open interest and long/short are deliberately absent. They have three weeks of
history. Adding a feature that is NULL for 99% of the training sample teaches
the model nothing except how to key off the NULL, which is a date in disguise.

What it is being asked
----------------------
Not "will the price go up". That question was tried first and it does not work
here: eighteen coins with a correlation around 0.8 give one market opinion
dressed as eighteen, and a model that fires only when it is confident fired on
twenty days in six years - four crashes, unfalsifiable.

The question asked instead is cross-sectional. Given that the book is in the
market anyway, holding three coins out of eighteen, does the model pick better
than a coin toss? The label is whether a coin beat *that day's median coin*, so
market direction is on both sides of the comparison and cancels. The benchmark
is not zero and not cash; it is holding the whole universe equally weighted.
Beating zero in a bull market is not a skill.

Timing follows the live engine exactly: decide on the close of day t, fill at
the open of day t+1, and the return being ranked is open[t+1] to open[t+2].
Labelling on the close of day t would let the model buy at the price it just
used to make the decision, which is the classic way a backtest invents an edge.

Cost is charged where it is actually paid - on turnover, not in the label.
Whether a position pays for itself depends on whether it was already held,
which is a fact about the book that day and not about the coin. This is why the
basket is sticky and rebalances weekly: the ranking is worth about +0.09% a day
and a round trip costs 0.30%, so a model that is right every day and acts every
day loses money.

How it is validated
-------------------
Purged walk-forward. Train on everything before a cut, skip an embargo, test on
what follows. The embargo is not decoration: the label at day t reaches into day
t+2, so a training row three days before the test start still overlaps the test
period's outcome. Random k-fold on this panel would score beautifully and mean
nothing - it would train on Tuesday to predict Monday, on eighteen correlated
symbols at once.

Every statistic is one number per calendar day, never one per row, and every
run is scored beside a control whose labels have been shuffled within the day.
Both of those exist because the first version of this file reported a 72% hit
rate that turned out to be four crash days counted eighteen times each.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from . import storage
from .config import settings
from .exchange import BinanceError, exchange

COST_PCT = (settings.fee_rate + settings.slippage_rate) * 2 * 100

# How many coins the experiment holds at a time. Positions are chosen by rank
# rather than by a probability threshold, and that difference is the design.
#
# A threshold fires when the model is confident, which on this data meant "after
# a crash": twenty trading days in six years, spread over four events, which no
# amount of arithmetic can tell apart from luck. Ranking always fires. The book
# trades every day, the sample grows by a day every day, and the question
# becomes the one a cross-sectional model can actually answer - given that we
# are in the market anyway, does the model pick better coins than a coin toss?
#
# Market direction cancels out of both sides of that comparison, which is the
# point. Predicting the direction of the market is a different problem and this
# design refuses to let a lucky bull run pretend to have solved it.
#
# Three, four and seven come out of a sweep over basket size, stickiness and
# rebalance cadence. That sweep is not evidence - it tried 120 combinations and
# the winner's t-statistic of 2.19 is about what the best of 120 draws produces
# from nothing. What it does show is a mechanism: the ranking is worth roughly
# +0.09% a day gross, a full round trip costs 0.30%, and every configuration
# high in that table was there for the same reason, which is that it barely
# trades. The evidence for the signal itself is the information coefficient,
# +0.093 with a t of 11.6 over 1 319 days and positive in all six folds, and
# that number does not depend on any of these three.
TOP_K = 3

# Every event this module writes carries this tag. Three books sharing one
# activity feed makes the feed useless: what a reader wants from it is what
# *this* book just did.
SOURCE = "lab"
# A held coin keeps its place while it stays inside the top STICKY * TOP_K.
# Without it the basket churns on ranking noise: two coins swapping third and
# fourth place is not a change of opinion, and acting on it costs 0.30%.
STICKY = 4.0
# Days between rebalances. The label reaches two days forward, so weekly looks
# like a mismatch - it is not, because the ranking persists a good deal longer
# than the horizon it was fitted on, and holding through six days costs nothing
# while trading through them costs six round trips.
REBALANCE_DAYS = 7

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    # Half the book, by the same accounting the live side uses: a starting
    # number that the profit and loss is measured against.
    "capital": 5_000.0,
    "quote_per_trade": 100.0,
    # A hard ceiling on top of the model's own basket size, so a bad config
    # cannot quietly widen the book. At 100 USDT a position and a basket of
    # three, the experiment deploys 300 of its 5 000 - the binding constraint is
    # concentration, not capital, and the concentration is the point: spreading
    # the same signal over eighteen coins is the same as not having it.
    "max_positions": TOP_K,
    "rebalance_days": REBALANCE_DAYS,
    "poll_seconds": 900,
    "retrain_days": 7,
    "top_k": None,   # None: use whatever the active model was validated at
}

# The label reaches two days forward, so training rows within two days of a test
# fold share an outcome with it. Three days of embargo covers that with a day to
# spare, and costs three rows per fold.
EMBARGO_DAYS = 3
CV_FOLDS = 6
MIN_TRAIN_ROWS = 2_000
# Distinct calendar days, not rows. Eighteen coins that fire together on one
# crash are one observation wearing eighteen hats: they are 0.8 correlated, they
# all bounce or all do not, and counting them separately turns four lucky days
# into "171 trades, 72% hit rate". That number was produced by an earlier
# version of this file and it was not real.
MIN_DAYS_PER_FOLD = 40
HISTORY_DAYS = 2_200


FEATURES = [
    "ret_1", "ret_3", "ret_7", "ret_14", "ret_30",
    "vol_7", "vol_30", "vol_ratio_7_30",
    "rsi_14", "dist_sma20", "dist_sma50", "dist_sma200",
    "range_pct", "volume_ratio",
    "funding_bp", "funding_7d", "funding_z90",
    "fng", "fng_chg_7",
    "btc_ret_1", "btc_ret_7", "rel_ret_7", "breadth",
    "rank_ret_1", "rank_ret_3", "rank_ret_7", "rank_ret_30",
    "rank_vol_30", "rank_rsi_14", "rank_dist_sma20", "rank_dist_sma200",
    "rank_range_pct", "rank_volume_ratio", "rank_funding_bp", "rank_funding_z90",
    "dow",
    "sent_mean", "sent_count", "sent_chg",
]

# A feature that is missing for almost the whole sample is not a feature, it is
# a date. The model would learn "sentiment is present" as a proxy for "this is
# September 2026 onward" and key off it. Headline sentiment starts life below
# this floor and crosses it on its own, roughly a year from now, at which point
# it enters the model without anyone editing a list.
MIN_COVERAGE = 0.30


def usable_features(data: pd.DataFrame) -> list[str]:
    present = [f for f in FEATURES
               if f in data.columns and data[f].notna().mean() >= MIN_COVERAGE]
    return present


LGB_PARAMS: dict[str, Any] = {
    # Small on purpose. Thirty thousand rows of financial panel data with a
    # signal-to-noise ratio near zero will happily support a tree that memorises
    # every drawdown in the sample. Depth four and a hundred-row minimum leaf
    # make that impossible rather than merely discouraged.
    "objective": "binary",
    "num_leaves": 15,
    "max_depth": 4,
    "learning_rate": 0.03,
    "n_estimators": 300,
    "min_child_samples": 100,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.7,
    "reg_lambda": 5.0,
    "verbose": -1,
    "n_jobs": 2,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_config() -> dict[str, Any]:
    return {**DEFAULT_CONFIG, **(storage.get_state("lab_config") or {})}


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    config = {**get_config(), **patch}
    storage.set_state("lab_config", config)
    return config


def universe() -> list[str]:
    """The same coins the live book watches. Shared universe, separate books."""
    from .feeds import book_symbols

    return book_symbols()


# --------------------------------------------------------------- the features

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


def _funding_daily() -> pd.DataFrame:
    """Funding summed per day per symbol, in basis points.

    Summed rather than averaged because three settlements a day is what a
    position actually pays, and the daily total is the number that shows up in
    a holder's balance.
    """
    rows = storage.query(
        "SELECT symbol, substr(source_ts, 1, 10) AS day, SUM(value) * 10000 AS funding_bp"
        "  FROM feed_observations WHERE feed = 'funding'"
        " GROUP BY symbol, day")
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["symbol", "day", "funding_bp"])


def _fng_daily() -> pd.DataFrame:
    rows = storage.query(
        "SELECT substr(source_ts, 1, 10) AS day, value AS fng"
        "  FROM feed_observations WHERE feed = 'fear_greed'")
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["day", "fng"])


def _sentiment_daily() -> pd.DataFrame:
    """Headline sentiment by the day we saw it, not the day it was published.

    Grouped on ``observed_at`` deliberately. See bot/sentiment.py: the
    publisher's timestamp is routinely earlier than the moment the item was
    readable, so grouping on it hands the model a few hours of hindsight.
    """
    rows = storage.query(
        "SELECT substr(observed_at, 1, 10) AS day, AVG(sentiment) AS sent_mean,"
        "       COUNT(*) AS sent_count"
        "  FROM feed_headlines WHERE sentiment IS NOT NULL GROUP BY day")
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["day", "sent_mean", "sent_count"])


def _symbol_frame(symbol: str, days: int) -> pd.DataFrame | None:
    from . import research

    try:
        raw = research.load_history(symbol, "1d", days)
    except BinanceError:
        return None
    if raw is None or len(raw) < 260:
        return None

    frame = raw.copy()
    frame["day"] = pd.to_datetime(frame["time"]).dt.strftime("%Y-%m-%d")
    frame["symbol"] = symbol
    close = frame["close"]

    for span in (1, 3, 7, 14, 30):
        frame[f"ret_{span}"] = close.pct_change(span) * 100
    daily = close.pct_change()
    frame["vol_7"] = daily.rolling(7).std() * 100
    frame["vol_30"] = daily.rolling(30).std() * 100
    # Volatility relative to its own recent normal. A coin that is twice as
    # jumpy as usual is in a different state from one that is merely volatile.
    frame["vol_ratio_7_30"] = frame["vol_7"] / frame["vol_30"].replace(0, np.nan)
    frame["rsi_14"] = _rsi(close)
    for span in (20, 50, 200):
        sma = close.rolling(span).mean()
        # Divided by volatility, so "far from the average" means the same thing
        # on a stablecoin-quiet week and a 20%-a-day week.
        frame[f"dist_sma{span}"] = (close / sma - 1) * 100 / frame["vol_30"].replace(0, np.nan)
    frame["range_pct"] = (frame["high"] - frame["low"]) / close * 100
    frame["volume_ratio"] = frame["volume"] / frame["volume"].rolling(20).mean().replace(0, np.nan)
    frame["dow"] = pd.to_datetime(frame["time"]).dt.dayofweek

    # The trade this is predicting: filled at tomorrow's open, closed at the
    # open after. Never at today's close, which is the price the decision was
    # made on.
    open_next = frame["open"].shift(-1)
    open_after = frame["open"].shift(-2)
    frame["target_return"] = (open_after / open_next - 1) * 100
    frame["entry_open"] = open_next

    keep = ["symbol", "day", "time", "close", "target_return", "entry_open",
            "ret_1", "ret_3", "ret_7", "ret_14", "ret_30",
            "vol_7", "vol_30", "vol_ratio_7_30", "rsi_14",
            "dist_sma20", "dist_sma50", "dist_sma200",
            "range_pct", "volume_ratio", "dow"]
    return frame[keep]


def panel(symbols: list[str] | None = None, days: int = HISTORY_DAYS,
          for_training: bool = True) -> pd.DataFrame:
    """One row per symbol per day, every feature aligned to that day's close.

    ``for_training`` drops the rows whose outcome is not known yet. Scoring
    wants exactly those rows and nothing else, so it asks for them.
    """
    symbols = symbols or universe()
    frames = [f for f in (_symbol_frame(s, days) for s in symbols) if f is not None]
    if not frames:
        return pd.DataFrame()
    data = pd.concat(frames, ignore_index=True)

    funding = _funding_daily()
    if not funding.empty:
        data = data.merge(funding, on=["symbol", "day"], how="left")
    else:
        data["funding_bp"] = np.nan
    data = data.sort_values(["symbol", "day"]).reset_index(drop=True)
    grouped = data.groupby("symbol")["funding_bp"]
    data["funding_7d"] = grouped.transform(lambda s: s.rolling(7, min_periods=3).mean())
    mean90 = grouped.transform(lambda s: s.rolling(90, min_periods=30).mean())
    std90 = grouped.transform(lambda s: s.rolling(90, min_periods=30).std())
    data["funding_z90"] = (data["funding_bp"] - mean90) / std90.replace(0, np.nan)

    fng = _fng_daily()
    if not fng.empty:
        data = data.merge(fng, on="day", how="left")
    else:
        data["fng"] = np.nan
    data = data.sort_values(["symbol", "day"]).reset_index(drop=True)
    data["fng_chg_7"] = data.groupby("symbol")["fng"].transform(lambda s: s.diff(7))

    sentiment = _sentiment_daily()
    if not sentiment.empty:
        data = data.merge(sentiment, on="day", how="left")
    else:
        data["sent_mean"] = np.nan
        data["sent_count"] = np.nan
    data = data.sort_values(["symbol", "day"]).reset_index(drop=True)
    data["sent_chg"] = data.groupby("symbol")["sent_mean"].transform(lambda s: s.diff(1))
    # Left NaN where there is no coverage rather than filled with zero. Zero is
    # a real sentiment reading - neutral news - and pretending an unobserved day
    # was neutral is a lie the model will happily learn.

    # Market factor and cross-section, both computed across the universe on the
    # same day. A coin up 3% on a day the whole market is up 3% has done
    # nothing, and a model without this feature cannot tell the two apart.
    btc = data[data["symbol"] == "BTCUSDT"][["day", "ret_1", "ret_7"]]
    btc = btc.rename(columns={"ret_1": "btc_ret_1", "ret_7": "btc_ret_7"})
    data = data.merge(btc, on="day", how="left")
    data["rel_ret_7"] = data["ret_7"] - data["btc_ret_7"]
    # Ranked within the day, not levels. The book buys the top five of that
    # day's eighteen, so what matters about an RSI of 70 is whether it is high
    # for the day - in a market where everything is at 70, it is not.
    for column in ("ret_1", "ret_3", "ret_7", "ret_30", "vol_30", "rsi_14",
                   "dist_sma20", "dist_sma200", "range_pct", "volume_ratio",
                   "funding_bp", "funding_z90"):
        data[f"rank_{column}"] = data.groupby("day")[column].rank(pct=True)
    data["breadth"] = data.groupby("day")["ret_1"].transform(
        lambda s: (s > 0).mean())

    if for_training:
        data = data.dropna(subset=["target_return"])
        # Relative to the day's median, not to a fixed cost. This is the
        # correction that matters most: with an absolute label every coin is a
        # zero on a down day and a one on an up day, so the model learns to
        # forecast the market and the ranking inherits nothing. Eighteen
        # correlated coins make that a very easy lesson and a useless one.
        #
        # The cost does not belong in the label either. Whether a position pays
        # for itself depends on whether it was already held, which is a fact
        # about the book on that day and not about the coin.
        median = data.groupby("day")["target_return"].transform("median")
        data["y"] = (data["target_return"] > median).astype(int)
    data = data.sort_values(["day", "symbol"]).reset_index(drop=True)
    return data


# -------------------------------------------------------------- the validation

def _fit(train: pd.DataFrame, features: list[str]) -> Any:
    import lightgbm as lgb

    model = lgb.LGBMClassifier(**LGB_PARAMS)
    model.fit(train[features], train["y"])
    return model


def _select(symbols: np.ndarray, probability: np.ndarray, top_k: int,
            previous: set[str], sticky: float = STICKY
            ) -> tuple[list[str], float]:
    """The day's basket: the ``top_k`` highest probabilities, held stickily.

    A coin already in the book keeps its place while it stays inside the top
    ``sticky * top_k``, so the basket only changes when the model's opinion
    changes rather than every time two adjacent ranks swap. Returns the basket
    and the fraction of it that is new, which is the fraction that pays.
    """
    ranked = [symbols[i] for i in np.argsort(-probability)]
    keep = [s for s in ranked[:int(top_k * sticky)] if s in previous][:top_k]
    chosen = keep + [s for s in ranked if s not in keep][:top_k - len(keep)]
    turnover = len(set(chosen) - previous) / max(len(chosen), 1)
    return chosen, turnover


def _simulate(test: pd.DataFrame, probability: np.ndarray, top_k: int,
              sticky: float = STICKY, every: int = REBALANCE_DAYS
              ) -> dict[str, Any]:
    """Walk a test fold one day at a time, holding the model's basket.

    Long only and always invested, against a benchmark of holding the same
    day's universe equally weighted. Both sides feel the same market, so the
    only thing the difference between them can measure is selection.

    Cost is charged on turnover alone: a coin held through a rebalance pays
    nothing, which is the entire reason for the stickiness above. On the days
    between rebalances the basket is simply held, and those days are free.
    """
    frame = test[["day", "symbol", "target_return"]].copy()
    frame["probability"] = probability
    net, baseline, turnovers = [], [], []
    previous: set[str] = set()
    elapsed = 0
    for _, group in frame.groupby("day", sort=True):
        if len(group) < top_k:
            continue
        if previous and elapsed % every != 0:
            chosen, turnover = list(previous), 0.0
        else:
            chosen, turnover = _select(group["symbol"].to_numpy(),
                                       group["probability"].to_numpy(),
                                       top_k, previous, sticky)
        elapsed += 1
        picked = float(group[group["symbol"].isin(chosen)]["target_return"].mean())
        net.append(picked - COST_PCT * turnover)
        baseline.append(float(group["target_return"].mean()))
        turnovers.append(turnover)
        previous = set(chosen)
    return {
        "net": np.array(net),
        "baseline": np.array(baseline),
        "turnover": float(np.mean(turnovers)) if turnovers else 0.0,
    }


def _compound(daily: np.ndarray) -> float:
    """What the money did, rather than what the average day did."""
    return float(np.prod(1 + daily / 100) - 1) * 100


def _daily_ic(test: pd.DataFrame, probability: np.ndarray) -> list[float]:
    """Spearman correlation between predicted and realised order, per day.

    This is the cleanest measure of whether there is anything here, because it
    is free of basket size, stickiness, cadence and fees - all the parameters a
    search can quietly fit. Those parameters decide how much of the signal
    survives contact with the fee schedule; this decides whether there is a
    signal to survive.
    """
    frame = test[["day", "target_return"]].copy()
    frame["probability"] = probability
    out = []
    for _, group in frame.groupby("day", sort=True):
        if len(group) < 10:
            continue
        rho = group["probability"].corr(group["target_return"], method="spearman")
        if pd.notna(rho):
            out.append(float(rho))
    return out


def _information_coefficient(results: list[dict[str, Any]]) -> dict[str, Any]:
    values = [v for fold in results for v in fold.pop("_ic", [])]
    if len(values) < 30:
        return {"mean": None, "days": len(values), "t_stat": None}
    array = np.array(values)
    error = float(array.std(ddof=1) / np.sqrt(len(array)))
    return {
        "mean": round(float(array.mean()), 4),
        "days": len(array),
        "t_stat": round(float(array.mean() / error), 2) if error else None,
    }


def cross_validate(data: pd.DataFrame, features: list[str],
                   folds: int = CV_FOLDS, top_k: int = TOP_K,
                   sticky: float = STICKY,
                   every: int = REBALANCE_DAYS) -> dict[str, Any]:
    """Purged walk-forward over the panel, split on the calendar.

    Split on dates, never on rows: eighteen symbols share every day, so a row
    split would put BTC's Monday in training and ETH's Monday in test. They are
    0.8 correlated. That is not a test.

    Every statistic here is one number per calendar day. The same trades scored
    per row gave a t-statistic over 6 and scored per day gave 0.87, because
    eighteen correlated coins entered on one day are one bet wearing eighteen
    hats.
    """
    days = sorted(data["day"].unique())
    if len(days) < 400:
        return {"folds": [], "error": "not enough history"}

    # Fold boundaries over the last 60% of the calendar: the first 40% is train
    # for everything, because a model fitted on 200 days is not the model that
    # will trade.
    start_index = int(len(days) * 0.40)
    edges = np.linspace(start_index, len(days), folds + 1).astype(int)

    results = []
    for index in range(folds):
        test_days = days[edges[index]:edges[index + 1]]
        if len(test_days) < MIN_DAYS_PER_FOLD:
            continue
        embargo_before = (datetime.strptime(test_days[0], "%Y-%m-%d")
                          - timedelta(days=EMBARGO_DAYS)).strftime("%Y-%m-%d")
        train_set = data[data["day"] < embargo_before]
        test = data[data["day"].isin(test_days)]
        if len(train_set) < MIN_TRAIN_ROWS or test.empty:
            continue

        model = _fit(train_set, features)
        run = _simulate(test, model.predict_proba(test[features])[:, 1],
                        top_k, sticky, every)
        if len(run["net"]) < MIN_DAYS_PER_FOLD:
            continue

        difference = run["net"] - run["baseline"]
        spread = float(difference.std(ddof=1) / np.sqrt(len(difference)))
        ic = _daily_ic(test, model.predict_proba(test[features])[:, 1])
        results.append({
            "_ic": ic,
            "ic": round(float(np.mean(ic)), 4) if ic else None,
            "test_from": test_days[0], "test_to": test_days[-1],
            "train_rows": int(len(train_set)),
            "days": int(len(run["net"])),
            "turnover": round(run["turnover"], 3),
            "net_per_day": round(float(run["net"].mean()), 4),
            "baseline_per_day": round(float(run["baseline"].mean()), 4),
            "edge": round(float(difference.mean()), 4),
            # How often the basket beat holding everything. Not a hit rate
            # against zero - beating zero in a bull market is not a skill.
            "win_rate": round(float((difference > 0).mean() * 100), 2),
            "t_stat": round(float(difference.mean() / spread), 2) if spread else 0.0,
            "total_pct": round(_compound(run["net"]), 2),
            "baseline_total_pct": round(_compound(run["baseline"]), 2),
            "beat_baseline": bool(_compound(run["net"]) > _compound(run["baseline"])),
        })

    if not results:
        return {"folds": [], "error": "no fold long enough to judge"}

    per_fold = [f["edge"] for f in results]
    return {
        "folds": results,
        "usable_folds": len(results),
        "positive_folds": sum(1 for e in per_fold if e > 0),
        "beat_baseline_folds": sum(1 for f in results if f["beat_baseline"]),
        "median_edge": round(float(np.median(per_fold)), 4),
        "mean_net_per_day": round(float(np.mean([f["net_per_day"] for f in results])), 4),
        "median_win_rate": round(float(np.median([f["win_rate"] for f in results])), 2),
        "median_t_stat": round(float(np.median([f["t_stat"] for f in results])), 2),
        "total_trade_days": sum(f["days"] for f in results),
        "mean_turnover": round(float(np.mean([f["turnover"] for f in results])), 3),
        "top_k": top_k,
        "sticky": sticky,
        "rebalance_days": every,
        "information_coefficient": _information_coefficient(results),
    }


def null_test(data: pd.DataFrame, features: list[str], top_k: int = TOP_K,
              sticky: float = STICKY, every: int = REBALANCE_DAYS
              ) -> dict[str, Any]:
    """Run the same validation on deliberately destroyed labels.

    Each day's outcomes are shuffled among that day's coins, which leaves every
    day-level fact intact - if the whole market rose 5%, it still did - and
    destroys only the question of *which* coin to hold. Since the benchmark is
    that same day's universe, the control has nothing left to find and should
    come back at zero.

    This is not decoration. The first version of this file reported a 72% hit
    rate and a +3.26% edge, and the control reported +3.40% - which is how the
    result was discovered to be four crash days counted eighteen times each.
    """
    rng = np.random.default_rng(7)
    scrambled = data.copy()
    scrambled["target_return"] = scrambled.groupby("day")["target_return"].transform(
        lambda s: rng.permutation(s.to_numpy()))
    median = scrambled.groupby("day")["target_return"].transform("median")
    scrambled["y"] = (scrambled["target_return"] > median).astype(int)
    outcome = cross_validate(scrambled, features, top_k=top_k, sticky=sticky,
                             every=every)
    return {
        "median_edge": outcome.get("median_edge"),
        "positive_folds": outcome.get("positive_folds"),
        "usable_folds": outcome.get("usable_folds"),
        "error": outcome.get("error"),
    }


def train(symbols: list[str] | None = None, activate: bool = True) -> dict[str, Any]:
    """Validate, then fit on everything, then store both.

    Both numbers are kept because they answer different questions. The
    cross-validation says whether the approach works; the final model is the one
    that trades, and it has seen data the cross-validated versions had not.
    Reporting the second one's training accuracy as if it were evidence is the
    oldest mistake in the subject, so it is not reported at all.
    """
    data = panel(symbols)
    if data.empty or len(data) < MIN_TRAIN_ROWS:
        return {"error": f"only {len(data)} rows of panel data"}

    features = usable_features(data)
    cv = cross_validate(data, features)
    if cv.get("error"):
        return {"error": cv["error"], "cv": cv}
    # Always run, always stored. A model whose control scores as well as it does
    # has not found anything, and the only way to know is to have measured it at
    # the same moment on the same folds.
    cv["null"] = null_test(data, features)
    real, fake = cv["median_edge"], cv["null"].get("median_edge")
    cv["skill_is_coin_picking"] = bool(fake is not None and real > fake)

    model = _fit(data, features)
    probabilities = model.predict_proba(data[features])[:, 1]
    importance = dict(sorted(
        zip(features, (int(v) for v in model.booster_.feature_importance("gain"))),
        key=lambda kv: -kv[1]))

    row = storage.execute(
        "INSERT INTO lab_models(trained_at, rows, features, params, cv, accuracy,"
        " edge_pct, top_k, active, blob) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (_now(), len(data), json.dumps(features), json.dumps(LGB_PARAMS),
         json.dumps({**cv, "importance": importance}),
         cv["median_win_rate"], cv["median_edge"], cv["top_k"], 0,
         model.booster_.model_to_string()),
    ).lastrowid

    if activate:
        storage.execute("UPDATE lab_models SET active = 0")
        storage.execute("UPDATE lab_models SET active = 1 WHERE id = ?", (row,))
        _cache.clear()

    storage.log_event(
        "info",
        f"Lab model {row} treinado: {len(data)} linhas, vantagem mediana "
        f"{cv['median_edge']:+.3f}% por dia sobre a carteira igual em "
        f"{cv['usable_folds']} janelas"
        f" (controle embaralhado: {cv['null'].get('median_edge')})",
        {"model_id": row, "cv": cv}, source=SOURCE)
    return {"model_id": row, "rows": len(data), "cv": cv, "top_k": cv["top_k"],
            "features": features, "importance": importance,
            "train_probability_mean": round(float(probabilities.mean()), 4)}


_training: dict[str, Any] = {"running": False, "started_at": None,
                             "finished_at": None, "result": None, "error": None}


def training_status() -> dict[str, Any]:
    return dict(_training)


def train_async(symbols: list[str] | None = None) -> dict[str, Any]:
    """Train on a background thread.

    A fit over six folds plus the shuffled control takes about a minute, which
    is long enough that a browser gives up on the request and short enough that
    a job queue would be silly.
    """
    if _training["running"]:
        return {"running": True, "message": "already training"}

    def work() -> None:
        _training.update(running=True, started_at=_now(), finished_at=None,
                         result=None, error=None)
        try:
            _training["result"] = train(symbols)
        except Exception as exc:
            _training["error"] = f"{type(exc).__name__}: {exc}"
            storage.log_event("error", f"Treino do laboratório falhou: {exc}",
                              {"trace": traceback.format_exc()[-600:]}, source=SOURCE)
        finally:
            _training.update(running=False, finished_at=_now())

    threading.Thread(target=work, daemon=True).start()
    return {"running": True, "message": "started"}


# ------------------------------------------------------------------- the model

_cache: dict[str, Any] = {}


def active_model() -> dict[str, Any] | None:
    if "model" in _cache:
        return _cache["model"]
    row = storage.query_one(
        "SELECT * FROM lab_models WHERE active = 1 ORDER BY id DESC LIMIT 1")
    if not row:
        return None
    import lightgbm as lgb

    loaded = {
        "id": row["id"],
        "trained_at": row["trained_at"],
        "features": json.loads(row["features"]),
        "top_k": int(row["top_k"] or TOP_K),
        "cv": json.loads(row["cv"]),
        "booster": lgb.Booster(model_str=row["blob"]),
    }
    _cache["model"] = loaded
    return loaded


def models(limit: int = 20) -> list[dict[str, Any]]:
    rows = storage.query(
        "SELECT id, trained_at, rows, accuracy, edge_pct, top_k, active, cv"
        "  FROM lab_models ORDER BY id DESC LIMIT ?", (limit,))
    for row in rows:
        row["cv"] = json.loads(row["cv"])
    return rows


def score_today(symbols: list[str] | None = None) -> dict[str, Any]:
    """Probability per symbol on the latest closed daily candle.

    The row used is the last one whose candle has closed, which is also the last
    row with no known outcome. That is the correct row and the only one: any
    earlier row is a decision that should already have been taken.
    """
    model = active_model()
    if model is None:
        return {"error": "no active model", "rows": []}

    data = panel(symbols, for_training=False)
    if data.empty:
        return {"error": "no panel data", "rows": []}
    latest = data["day"].max()
    current = data[data["day"] == latest].copy()
    current["probability"] = model["booster"].predict(current[model["features"]])
    current = current.sort_values("probability", ascending=False)
    top_k = model["top_k"]
    wanted = set(current["symbol"].to_numpy()[:top_k])

    return {
        "model_id": model["id"],
        "day": latest,
        "top_k": top_k,
        "rows": [
            {
                "symbol": row["symbol"],
                "probability": round(float(row["probability"]), 4),
                "close": float(row["close"]),
                "ret_7": None if pd.isna(row["ret_7"]) else round(float(row["ret_7"]), 2),
                "rsi_14": None if pd.isna(row["rsi_14"]) else round(float(row["rsi_14"]), 1),
                "funding_bp": None if pd.isna(row["funding_bp"]) else round(float(row["funding_bp"]), 2),
                "rank": rank + 1,
                "wanted": row["symbol"] in wanted,
            }
            for rank, (_, row) in enumerate(current.iterrows())
        ],
    }


# ------------------------------------------------------------------ the ledger

def open_positions() -> list[dict[str, Any]]:
    rows = storage.query(
        "SELECT * FROM lab_positions WHERE status = 'open' ORDER BY id")
    for row in rows:
        row["features"] = json.loads(row["features"] or "null")
    return rows


def closed_positions(limit: int = 200) -> list[dict[str, Any]]:
    return storage.query(
        "SELECT * FROM lab_positions WHERE status = 'closed'"
        " ORDER BY exit_time DESC LIMIT ?", (limit,))


def _buy(symbol: str, price: float, probability: float, model_id: int,
         quote: float, context: dict[str, Any] | None = None) -> dict[str, Any]:
    # Same fee and slippage the backtester charges, so the experiment's numbers
    # stay comparable with the research that justified the live book.
    fill = price * (1 + settings.fee_rate + settings.slippage_rate)
    # An exchange sells in lot steps, so $100 of a coin is almost never $100 of
    # the coin. Flooring to the step here means entry_quote is what the order
    # would really have cost rather than what it asked for - and every return
    # in this book is divided by that figure. Without it the panel shows a round
    # $100 on every line, which is the one number guaranteed to be wrong.
    try:
        qty = exchange.round_qty(symbol, quote / fill) or quote / fill
    except Exception:
        qty = quote / fill
    spent = qty * fill
    storage.execute(
        "INSERT INTO lab_positions(symbol, status, qty, entry_price, entry_time,"
        " entry_quote, entry_prob, model_id, features)"
        " VALUES(?,'open',?,?,?,?,?,?,?)",
        (symbol, qty, fill, _now(), spent, probability, model_id,
         json.dumps(context) if context else None))
    return {"action": "buy", "symbol": symbol, "price": fill, "qty": qty,
            "quote": spent, "probability": probability}


def _sell(position: dict[str, Any], price: float, reason: str,
          probability: float | None = None) -> dict[str, Any]:
    fill = price * (1 - settings.fee_rate - settings.slippage_rate)
    proceeds = position["qty"] * fill
    pnl = proceeds - position["entry_quote"]
    storage.execute(
        "UPDATE lab_positions SET status='closed', exit_price=?, exit_time=?,"
        " exit_quote=?, exit_prob=?, pnl=?, return_pct=?, reason=? WHERE id=?",
        (fill, _now(), proceeds, probability, pnl,
         (proceeds / position["entry_quote"] - 1) * 100, reason, position["id"]))
    return {"action": "sell", "symbol": position["symbol"], "price": fill,
            "pnl": pnl, "reason": reason}


def snapshot_equity(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or get_config()
    realised = storage.query_one(
        "SELECT COALESCE(SUM(pnl), 0) AS pnl FROM lab_positions WHERE status='closed'"
    )["pnl"]
    positions = open_positions()
    unrealised, invested = 0.0, 0.0
    if positions:
        try:
            prices = exchange.prices(sorted({p["symbol"] for p in positions}))
        except BinanceError:
            prices = {}
        for position in positions:
            mark = prices.get(position["symbol"], position["entry_price"])
            unrealised += position["qty"] * mark - position["entry_quote"]
            invested += position["entry_quote"]

    start = float(config.get("capital", 5_000.0))
    total = start + realised + unrealised
    storage.execute(
        "INSERT INTO lab_equity(ts, total_value, free_quote, positions_value,"
        " open_positions) VALUES(?,?,?,?,?)"
        " ON CONFLICT(ts) DO UPDATE SET total_value = excluded.total_value",
        (_now(), total, total - invested - unrealised, invested + unrealised,
         len(positions)))
    return {"capital": start, "realised_pnl": realised, "unrealised_pnl": unrealised,
            "invested": invested, "total_value": total}


def equity_curve(limit: int = 500) -> list[dict[str, Any]]:
    rows = storage.query(
        "SELECT ts, total_value, open_positions FROM lab_equity"
        " ORDER BY ts DESC LIMIT ?", (limit,))
    return list(reversed(rows))


# -------------------------------------------------------------------- the loop

class LabTrader:
    """Rebalances once a day, on the close of the daily candle.

    Daily because that is the only interval where the arithmetic permits a
    directional model at all: at one hour the round trip exceeds the average
    move on the largest coin in the book, so break-even would need an accuracy
    above 100%.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self.last_tick: str | None = None
        self.last_error: str | None = None
        self.last_action: dict[str, Any] | None = None
        self.tick_count = 0

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.running:
                return {"running": True, "message": "already running"}
            if active_model() is None:
                return {"running": False, "message": "no trained model yet"}
            save_config({"enabled": True})
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            storage.log_event("info", "Laboratório iniciado", source=SOURCE)
            return {"running": True, "message": "started"}

    def stop(self) -> dict[str, Any]:
        save_config({"enabled": False})
        self._stop.set()
        storage.log_event("info", "Laboratório parado", source=SOURCE)
        return {"running": False, "message": "stopped"}

    def _loop(self) -> None:
        while not self._stop.is_set():
            config = get_config()
            try:
                self.tick(config)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                storage.log_event("error", f"Lab tick falhou: {exc}",
                                  {"trace": traceback.format_exc()[-600:]}, source=SOURCE)
            self.last_tick = _now()
            self.tick_count += 1
            self._stop.wait(max(60, int(config.get("poll_seconds", 900))))

    def tick(self, config: dict[str, Any] | None = None,
             force: bool = False) -> dict[str, Any]:
        with self._tick_lock:
            return self._tick(config, force)

    def _tick(self, config: dict[str, Any] | None, force: bool) -> dict[str, Any]:
        config = config or get_config()
        model = active_model()
        if model is None:
            return {"skipped": "no active model"}

        scored = score_today()
        if scored.get("error"):
            return {"skipped": scored["error"]}
        day = scored["day"]

        # One rebalance per daily candle. Ticking every fifteen minutes is about
        # not missing the close by half a day; acting every fifteen minutes
        # would pay the round trip 96 times for one day of opinion.
        if not force and storage.get_state("lab_last_day") == day:
            return {"skipped": "already rebalanced today", "day": day}

        top_k = int(config.get("top_k") or model["top_k"])
        top_k = min(top_k, int(config.get("max_positions", TOP_K)))
        # Between rebalances the book is simply held. Skipping the decision is
        # the decision: the sweep put weekly ahead of daily by a wide margin,
        # and the whole margin is the round trips not paid.
        every = max(1, int(config.get("rebalance_days", REBALANCE_DAYS)))
        last = storage.get_state("lab_last_rebalance")
        due = force or not last or (
            datetime.strptime(day, "%Y-%m-%d")
            - datetime.strptime(last, "%Y-%m-%d")).days >= every
        if not due and open_positions():
            storage.set_state("lab_last_day", day)
            equity = snapshot_equity(config)
            return {"day": day, "actions": [], "held": True,
                    "next_rebalance_in": every - (
                        datetime.strptime(day, "%Y-%m-%d")
                        - datetime.strptime(last, "%Y-%m-%d")).days,
                    "equity": equity}
        actions: list[dict[str, Any]] = []

        held = {p["symbol"]: p for p in open_positions()}
        ranked = [r["symbol"] for r in scored["rows"]]     # already sorted
        by_symbol = {r["symbol"]: r for r in scored["rows"]}
        # The same basket the validation used, including the stickiness: a coin
        # in the book keeps its place while it stays inside the top 2 * k, so
        # the book only turns over when the opinion changes.
        basket, _ = _select(np.array(ranked),
                            np.array([by_symbol[s]["probability"] for s in ranked]),
                            top_k, set(held))

        symbols = sorted(set(held) | set(basket))
        try:
            prices = exchange.prices(symbols) if symbols else {}
        except BinanceError as exc:
            return {"skipped": f"prices unavailable: {exc}"}

        for symbol, position in held.items():
            price = prices.get(symbol)
            if price is None or symbol in basket:
                continue
            actions.append(_sell(position, price, "saiu do ranking",
                                 by_symbol.get(symbol, {}).get("probability")))

        held = {p["symbol"]: p for p in open_positions()}
        room = int(config.get("max_positions", TOP_K)) - len(held)
        quote = float(config.get("quote_per_trade", 100.0))
        # Cash is checked because a book that silently skips entries for lack of
        # funds looks exactly like a model that declined to trade.
        free = float(config.get("capital", 5_000.0)) - sum(
            p["entry_quote"] for p in held.values())

        for row in (by_symbol[s] for s in basket if s not in held):
            if room <= 0:
                break
            if free < quote * (1 + settings.fee_rate + settings.slippage_rate):
                storage.log_event(
                    "warn", f"Laboratório sem caixa para {row['symbol']}",
                    {"free": round(free, 2), "needed": quote}, source=SOURCE)
                break
            price = prices.get(row["symbol"])
            if price is None:
                continue
            actions.append(_buy(row["symbol"], price, row["probability"],
                                model["id"], quote, row))
            room -= 1
            free -= quote

        storage.set_state("lab_last_day", day)
        storage.set_state("lab_last_rebalance", day)
        equity = snapshot_equity(config)
        if actions:
            storage.log_event(
                "trade",
                f"Laboratório: {sum(1 for a in actions if a['action'] == 'buy')} compras,"
                f" {sum(1 for a in actions if a['action'] == 'sell')} vendas",
                {"day": day, "model_id": model["id"]}, source=SOURCE)
        self.last_action = {"day": day, "actions": actions, "basket": basket}
        self._maybe_retrain(config)
        return {"day": day, "actions": actions, "basket": basket,
                "top_k": top_k, "equity": equity}

    def _maybe_retrain(self, config: dict[str, Any]) -> None:
        """Refit on a schedule, and keep the old model's row either way.

        Retraining is not obviously good - the live book measured fixed
        parameters beating refitted ones by a wide margin - so this exists to
        make the comparison possible rather than because it is known to help.
        Every version keeps its own row, and the trades it took carry its id.
        """
        every = int(config.get("retrain_days", 7))
        if every <= 0:
            return
        last = storage.get_state("lab_last_train")
        if last:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
            if age < timedelta(days=every):
                return
        storage.set_state("lab_last_train", _now())
        try:
            train()
        except Exception as exc:
            storage.log_event("error", f"Retreino do laboratório falhou: {exc}", source=SOURCE)

    def close_all(self, reason: str = "manual") -> list[dict[str, Any]]:
        positions = open_positions()
        if not positions:
            return []
        prices = exchange.prices(sorted({p["symbol"] for p in positions}))
        out = []
        for position in positions:
            price = prices.get(position["symbol"], position["entry_price"])
            out.append(_sell(position, price, reason))
        snapshot_equity()
        return out

    def status(self) -> dict[str, Any]:
        config = get_config()
        model = active_model()
        return {
            "running": self.running,
            "enabled": config.get("enabled"),
            "capital": config.get("capital"),
            "quote_per_trade": config.get("quote_per_trade"),
            "max_positions": config.get("max_positions"),
            "poll_seconds": config.get("poll_seconds"),
            "last_tick": self.last_tick,
            "last_error": self.last_error,
            "tick_count": self.tick_count,
            "last_day": storage.get_state("lab_last_day"),
            "last_rebalance": storage.get_state("lab_last_rebalance"),
            "rebalance_days": config.get("rebalance_days"),
            "model": None if model is None else {
                "id": model["id"], "trained_at": model["trained_at"],
                "top_k": model["top_k"],
                "median_edge": model["cv"].get("median_edge"),
                "median_win_rate": model["cv"].get("median_win_rate"),
                "information_coefficient": model["cv"].get("information_coefficient"),
                "mean_turnover": model["cv"].get("mean_turnover"),
                "median_t_stat": model["cv"].get("median_t_stat"),
                "usable_folds": model["cv"].get("usable_folds"),
                "positive_folds": model["cv"].get("positive_folds"),
                "beat_baseline_folds": model["cv"].get("beat_baseline_folds"),
                "total_trade_days": model["cv"].get("total_trade_days"),
                "null_edge": (model["cv"].get("null") or {}).get("median_edge"),
                "skill_is_coin_picking": model["cv"].get("skill_is_coin_picking"),
            },
        }


trader = LabTrader()


# ----------------------------------------------------------------- the report

def overview() -> dict[str, Any]:
    """The same keys ``report.overview`` returns, computed the same way.

    Deliberately identical down to the field names: the point of running the two
    books side by side is to compare them, and a comparison where each side is
    measured by its own favourite metric is not one. The extra keys at the end
    are about the model, which the live book does not have.
    """
    config = get_config()
    closed = storage.query(
        "SELECT * FROM lab_positions WHERE status = 'closed' ORDER BY exit_time")
    positions = open_positions()

    marks: dict[str, float] = {}
    if positions:
        try:
            marks = exchange.prices(sorted({p["symbol"] for p in positions}))
        except Exception:
            marks = {}

    unrealised, invested = 0.0, 0.0
    for position in positions:
        mark = marks.get(position["symbol"], position["entry_price"])
        position["mark_price"] = mark
        position["value"] = position["qty"] * mark
        position["unrealised_pnl"] = position["value"] - position["entry_quote"]
        position["unrealised_pct"] = (
            (position["value"] / position["entry_quote"] - 1) * 100
            if position["entry_quote"] else 0.0)
        unrealised += position["unrealised_pnl"]
        invested += position["entry_quote"]

    realised = sum(p["pnl"] or 0.0 for p in closed)
    start = float(config.get("capital", 5_000.0))
    total = start + realised + unrealised

    wins = [p for p in closed if (p["pnl"] or 0) > 0]
    losses = [p for p in closed if (p["pnl"] or 0) <= 0]
    gross_win = sum(p["pnl"] for p in wins)
    gross_loss = -sum(p["pnl"] for p in losses)

    curve = [row["total_value"] for row in equity_curve(5_000)]
    peak, max_dd = 0.0, 0.0
    for value in curve:
        peak = max(peak, value)
        if peak:
            max_dd = min(max_dd, (value / peak - 1) * 100)

    turnover = storage.query_one(
        "SELECT COALESCE(SUM(entry_quote), 0) AS total FROM lab_positions")["total"]
    # What the book can actually put to work, which is not what it was given.
    # A basket of three at 100 USDT is 300 whatever the capital line says, so a
    # return computed on the capital line understates the strategy by a factor
    # of seventeen. Both numbers are reported because both are true and they
    # answer different questions: one is "how is the money doing", the other is
    # "is the idea any good".
    ceiling = float(config.get("quote_per_trade", 100.0)) * int(
        config.get("max_positions", TOP_K))
    model = active_model()

    return {
        "mode": "paper",
        "turnover": round(turnover, 2),
        "fees_estimate": round(turnover * settings.fee_rate * 2, 2),
        "start_capital": round(start, 2),
        "capital_at_work": round(ceiling, 2),
        "total_value": round(total, 2),
        "total_pnl": round(realised + unrealised, 2),
        "total_return_pct": round((total / start - 1) * 100, 2) if start else 0.0,
        "return_on_capital_at_work_pct": round(
            (realised + unrealised) / ceiling * 100, 2) if ceiling else 0.0,
        "realised_pnl": round(realised, 2),
        "unrealised_pnl": round(unrealised, 2),
        "invested": round(invested, 2),
        "cash": round(total - invested - unrealised, 2),
        "open_positions": len(positions),
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 2) if closed else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (
            999.0 if gross_win > 0 else 0.0),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "expectancy": round(realised / len(closed), 2) if closed else 0.0,
        "best_trade_pct": round(max((p["return_pct"] or 0 for p in closed), default=0.0), 2),
        "worst_trade_pct": round(min((p["return_pct"] or 0 for p in closed), default=0.0), 2),
        "max_drawdown_pct": round(max_dd, 2),
        "cost_per_trade_pct": round(COST_PCT, 3),
        "positions": positions,
        "model": None if model is None else {
            "id": model["id"],
            "trained_at": model["trained_at"],
            "top_k": model["top_k"],
            "median_edge": model["cv"].get("median_edge"),
            "median_t_stat": model["cv"].get("median_t_stat"),
            "median_win_rate": model["cv"].get("median_win_rate"),
            "usable_folds": model["cv"].get("usable_folds"),
            "positive_folds": model["cv"].get("positive_folds"),
            "beat_baseline_folds": model["cv"].get("beat_baseline_folds"),
            "total_trade_days": model["cv"].get("total_trade_days"),
            "mean_turnover": model["cv"].get("mean_turnover"),
            "rebalance_days": model["cv"].get("rebalance_days"),
            "information_coefficient": model["cv"].get("information_coefficient"),
            "null_edge": (model["cv"].get("null") or {}).get("median_edge"),
            "skill_is_coin_picking": model["cv"].get("skill_is_coin_picking"),
            "folds": model["cv"].get("folds"),
            "importance": model["cv"].get("importance"),
        },
    }


def reset() -> dict[str, Any]:
    """Wipe the experiment's ledger. Models are kept.

    The trades are the disposable part - the whole point of the experiment is
    being able to throw them away and start again. The trained models are not:
    a model's cross-validation is evidence about the approach, and evidence is
    not improved by deleting it.
    """
    storage.execute("DELETE FROM lab_positions")
    storage.execute("DELETE FROM lab_equity")
    storage.set_state("lab_last_day", None)
    storage.set_state("lab_last_rebalance", None)
    storage.log_event("warning", "Ledger do laboratório zerado", source=SOURCE)
    return {"cleared": True}
