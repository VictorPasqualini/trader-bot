"""What the book was expected to do, frozen at the moment it was deployed.

Validation produces a distribution of quarterly results per allocation. That
distribution is the prediction, and a prediction is only worth something if it
is written down before the outcome arrives. Recomputing an expectation from
today's walk-forward and comparing it against today's results is not a forward
test: the history the walk-forward runs on has grown by however long the bot has
been live, so the "expectation" quietly absorbs the very period it is supposed
to be judging.

So the expectation is snapshotted into a table when the book changes, and never
recomputed. Everything here reads those frozen rows.

The comparison is a band rather than a line, because a single expected number is
useless: the walk-forward says the median quarter is +9% and the worst is −12%,
and a realised −3% is entirely normal against that pair and alarming against the
median alone. The band is scaled to the elapsed time by the square root of the
horizon, which is how dispersion actually accumulates - linear scaling would
claim the worst plausible first day is a thirtieth of the worst plausible month,
and no market behaves that way.

The point of the exercise is early warning. Waiting for 270 days to compare one
number against one number wastes the 269 days in between; a realised curve that
leaves the bottom of its band in week six is worth knowing about in week six.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from . import storage

# The band is anchored on the walk-forward's test window, which is a quarter.
# Everything shorter is scaled down from it.
BAND_DAYS = 90

# Below this the realised curve is noise: a couple of trades either side of
# nothing. The band still renders, but no verdict is issued against it.
MIN_DAYS_FOR_VERDICT = 14


def fingerprint(allocations: list[dict[str, Any]]) -> str:
    """A stable identity for one book.

    Two configurations with the same symbols, intervals, strategies and
    parameters make the same prediction, whatever order they are listed in, so
    they share a baseline. Any change to any of those is a different book and
    earns its own row - including a parameter tweak, which is the change most
    likely to be made casually and least likely to be remembered later.
    """
    parts = sorted(
        json.dumps({
            "symbol": item.get("symbol"),
            "interval": item.get("interval"),
            "strategy": item.get("strategy"),
            "params": item.get("params") or {},
        }, sort_keys=True)
        for item in allocations
    )
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def _parse(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def baselines() -> list[dict[str, Any]]:
    """Every recorded expectation, oldest first, with its detail decoded."""
    rows = storage.query(
        "SELECT * FROM expectations ORDER BY effective_from, id")
    for row in rows:
        row["detail"] = json.loads(row["detail"] or "[]")
    return rows


def latest() -> dict[str, Any] | None:
    rows = baselines()
    return rows[-1] if rows else None


def expectation(config: dict[str, Any],
                reports: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Scale each allocation's walk-forward down to the size it actually trades.

    An allocation that returned a median +18% per quarter in the walk-forward
    was trading the whole account in that test. Live it trades ``quote_per_trade``
    of ``start_capital``, so its contribution is that median times its share.
    Summing the shares is what turns seventeen single-strategy backtests into
    one prediction about one account.
    """
    allocations = config.get("allocations") or []
    if not allocations or not reports:
        return None

    quote = float(config.get("quote_per_trade", 200.0))
    start = float(config.get("start_capital", 10_000.0))
    share = quote / start if start else 0.0
    by_symbol = {r["symbol"]: r for r in reports if r.get("window_count")}

    trades_month, return_month, worst_quarter = 0.0, 0.0, 0.0
    detail: list[dict[str, Any]] = []
    for allocation in allocations:
        found = by_symbol.get(allocation["symbol"])
        if not found:
            continue
        days = found["window_count"] * found["test_days"]
        rate = found["total_trades"] / days * 30 if days else 0.0
        median = found["median_return_pct"] / 3 * share
        worst = found["worst_window_pct"] * share
        trades_month += rate
        return_month += median
        worst_quarter += worst
        detail.append({
            "symbol": allocation["symbol"],
            "interval": allocation.get("interval"),
            "strategy": allocation.get("strategy"),
            "windows": found["window_count"],
            "median_quarter_pct": found["median_return_pct"],
            "worst_quarter_pct": found["worst_window_pct"],
            "return_pct_month": round(median, 3),
            "worst_quarter_share_pct": round(worst, 3),
            "trades_per_month": round(rate, 2),
        })

    if not detail:
        return None
    return {
        "book": fingerprint(allocations),
        "start_capital": start,
        "quote_per_trade": quote,
        "allocations": len(allocations),
        "return_pct_month": round(return_month, 4),
        "worst_quarter_pct": round(worst_quarter, 4),
        "trades_month": round(trades_month, 3),
        "detail": detail,
    }


def record(config: dict[str, Any],
           reports: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Freeze today's expectation, if the book is not already on record.

    Called from the readiness report, which already holds both a config and a
    finished walk-forward, and is polled by the dashboard. That makes recording
    a side effect of somebody looking, which sounds fragile and is not: the
    fingerprint makes it idempotent, and a book nobody has ever looked at has
    also never been compared against anything.
    """
    expected = expectation(config, reports)
    if not expected:
        return None

    previous = latest()
    if previous and previous["book"] == expected["book"]:
        return None

    now = datetime.now(timezone.utc)
    if previous:
        effective = now
    else:
        # The first baseline ever written has to cover a run that started before
        # this table existed, otherwise the only live data in hand is discarded
        # to preserve a formality. The book it describes is the one deployed
        # now, so the attribution is only wrong for whatever part of that run
        # used a different book - which is why every later change starts its
        # segment at the moment of the change instead.
        first = storage.query_one("SELECT MIN(ts) AS ts FROM orders")
        effective = _parse(first["ts"]) if first and first["ts"] else now

    storage.execute(
        "INSERT INTO expectations(recorded_at, effective_from, book, start_capital,"
        " quote_per_trade, allocations, return_pct_month, worst_quarter_pct,"
        " trades_month, detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (now.isoformat(timespec="seconds"), effective.isoformat(timespec="seconds"),
         expected["book"], expected["start_capital"], expected["quote_per_trade"],
         expected["allocations"], expected["return_pct_month"],
         expected["worst_quarter_pct"], expected["trades_month"],
         json.dumps(expected["detail"])),
    )
    storage.log_event(
        "info",
        f"Expectativa registrada: {expected['allocations']} alocações,"
        f" {expected['return_pct_month']:+.2f}%/mês esperado",
        {"book": expected["book"], "effective_from": effective.isoformat()},
    )
    return expected


def _band_width(row: dict[str, Any], days: float) -> float:
    """How far below the median the worst plausible result sits after `days`.

    The walk-forward measured that distance over a quarter. Dispersion grows
    with the square root of time, so a fortnight's worth is not a fortnight's
    share of it.
    """
    median_quarter = row["return_pct_month"] * 3
    spread = abs(median_quarter - row["worst_quarter_pct"])
    return spread * math.sqrt(max(days, 0.0) / BAND_DAYS)


def _segments() -> list[dict[str, Any]]:
    """Baselines with an end date, so each one owns a stretch of the timeline."""
    rows = baselines()
    now = datetime.now(timezone.utc)
    out = []
    for index, row in enumerate(rows):
        start = _parse(row["effective_from"])
        end = _parse(rows[index + 1]["effective_from"]) if index + 1 < len(rows) else now
        if end <= start:
            continue
        out.append({**row, "start": start, "end": end})
    return out


def _equity_series() -> list[tuple[datetime, float]]:
    return [(_parse(row["ts"]), float(row["total_value"]))
            for row in storage.query(
                "SELECT ts, total_value FROM equity_snapshots ORDER BY ts")]


def _equity_at(series: list[tuple[datetime, float]],
               moment: datetime) -> float | None:
    """The last equity reading at or before `moment`."""
    found = None
    for stamp, value in series:
        if stamp > moment:
            break
        found = value
    return found


def report() -> dict[str, Any]:
    """The realised curve against the band that was predicted for it.

    Realised is measured as a *difference* in equity from the start of each
    segment, not as an absolute. The exchange account holds assets the bot never
    bought and started with slightly more than the configured capital, so the
    absolute equity level is off by a constant - and a constant vanishes from a
    difference. What the bot did is the change; what the account happens to
    contain is not.
    """
    segments = _segments()
    if not segments:
        return {"status": "sem expectativa registrada", "points": [],
                "baselines": [], "current": None}

    equity = _equity_series()
    if not equity:
        return {"status": "sem histórico de patrimônio", "points": [],
                "baselines": baselines(), "current": None}

    start_capital = float(segments[0]["start_capital"]) or 10_000.0
    points: list[dict[str, Any]] = []
    # Both curves are cumulative across book changes: a segment starts where the
    # previous one ended, so switching allocations does not reset the score.
    carried_expected, carried_realised = 0.0, 0.0

    for segment in segments:
        anchor = _equity_at(equity, segment["start"])
        if anchor is None:
            anchor = equity[0][1]
        rate_per_day = segment["return_pct_month"] / 30.0
        # One point per day, plus the segment's final moment, which is what the
        # "now" reading is read from.
        span = (segment["end"] - segment["start"]).total_seconds() / 86400
        marks = [segment["start"] + timedelta(days=step)
                 for step in range(int(span) + 1)] + [segment["end"]]
        for moment in marks:
            days = (moment - segment["start"]).total_seconds() / 86400
            expected = carried_expected + rate_per_day * days
            width = _band_width(segment, days)
            value = _equity_at(equity, moment)
            realised = (carried_realised + (value - anchor) / start_capital * 100
                        if value is not None else None)
            points.append({
                "time": moment.isoformat(timespec="seconds"),
                "book": segment["book"],
                "days": round(days, 3),
                "expected_pct": round(expected, 3),
                "lower_pct": round(expected - width, 3),
                "upper_pct": round(expected + width, 3),
                "realised_pct": None if realised is None else round(realised, 3),
            })
        last = _equity_at(equity, segment["end"])
        carried_expected += rate_per_day * span
        if last is not None:
            carried_realised += (last - anchor) / start_capital * 100

    # Duplicate timestamps appear where one segment ends and the next begins.
    seen, unique = set(), []
    for point in points:
        if point["time"] in seen:
            continue
        seen.add(point["time"])
        unique.append(point)

    live = [p for p in unique if p["realised_pct"] is not None]
    current = dict(live[-1]) if live else None
    total_days = sum((s["end"] - s["start"]).total_seconds() / 86400
                     for s in segments)

    if current:
        current["days_live"] = round(total_days, 1)
        current["divergence_pct"] = round(
            current["realised_pct"] - current["expected_pct"], 3)
        current["inside_band"] = (
            current["lower_pct"] <= current["realised_pct"] <= current["upper_pct"])
        current["verdict"] = _verdict(current, total_days)

    return {
        "status": "ok",
        "points": unique,
        "baselines": baselines(),
        "segments": len(segments),
        "start_capital": start_capital,
        "days_live": round(total_days, 1),
        "current": current,
    }


def _verdict(current: dict[str, Any], days: float) -> str:
    """One phrase for where the realised curve sits inside its band."""
    if days < MIN_DAYS_FOR_VERDICT:
        return f"cedo demais para julgar ({days:.0f} de {MIN_DAYS_FOR_VERDICT} dias)"
    if current["realised_pct"] < current["lower_pct"]:
        return "abaixo do pior caso esperado"
    if current["realised_pct"] > current["upper_pct"]:
        return "acima da faixa esperada"
    if current["realised_pct"] < current["expected_pct"]:
        return "dentro da faixa, abaixo da mediana"
    return "dentro da faixa, acima da mediana"
