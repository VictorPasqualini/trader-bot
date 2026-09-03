"""How much of the market the bot was actually awake for.

The bot runs on a machine that gets turned off. Every candle close it sleeps
through is a trade it could not take, and that absence is invisible in every
other report: a strategy that never fired and a strategy that fired while the
process was down produce the same empty result. This module makes the
difference explicit, so a live run can be read as "what the book did" rather
than "what the book did, minus an unknown amount of downtime".

Coverage is derived from equity snapshots, which are written once per tick.
That makes them a truthful record of when the process was alive without needing
a second table for it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import storage
from .exchange import INTERVAL_MS

# A tick this soon after a candle close is treated as prompt. The backtest
# assumes a fill at the next candle's open; a tick a few minutes in fills close
# enough to that price for the comparison to hold. Beyond it, the fill drifts
# away from the assumption and the trade is worth flagging even though it
# happened.
PROMPT_MINUTES = 15


# Closes missed before this moment are listed but left out of the percentage.
#
# The count is cumulative and the misses never expire, which makes the gate
# arithmetic brutal: twenty-two misses on record means 220 closes have to
# accumulate before 90% is even reachable, and every further day of downtime
# pushes that target about ten days further out. A machine that spent its first
# weeks as a development laptop can therefore never clear the gate, however
# reliable the deployment it later moves to - which measures the wrong thing.
# The gate exists to describe the presence of the deployment running now.
#
# So the baseline moves when the deployment changes, and only then. It is set
# through set_baseline(), deliberately has no button in the interface, and the
# closes it sets aside stay in the report under their own heading rather than
# disappearing. Moving it because the number is unflattering is the one use
# that defeats the purpose: the number would then measure nothing at all.
#
# Same device as the parity guard in bot/parity.py, for the same reason.
BASELINE_KEY = "coverage_baseline"


def baseline() -> datetime | None:
    """The oldest close the deployment running now is willing to be judged on."""
    value = storage.get_state(BASELINE_KEY, None)
    return _parse(value) if value else None


def set_baseline(moment: datetime | None = None) -> dict[str, Any]:
    """Start counting coverage from now, or from `moment`.

    Call this when the deployment actually changes - moving off a laptop onto a
    host that stays up, for instance. What is being set aside is summarised into
    the event log first, so the old record survives the reset.
    """
    now = datetime.now(timezone.utc)
    moment = moment or now
    previous = report()
    storage.set_state(BASELINE_KEY, moment.isoformat(timespec="seconds"))
    storage.log_event(
        "info",
        f"Marco de cobertura movido para {moment.isoformat(timespec='seconds')}",
        {"previous_coverage_pct": previous.get("coverage_pct"),
         "previous_missed": previous.get("closes", 0) - previous.get("covered", 0),
         "previous_since": previous.get("since")},
    )
    return {"baseline": moment.isoformat(timespec="seconds"),
            "set_aside": {
                "coverage_pct": previous.get("coverage_pct"),
                "closes": previous.get("closes", 0),
                "missed": previous.get("closes", 0) - previous.get("covered", 0),
                "since": previous.get("since"),
            }}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def tick_times(since: datetime | None = None) -> list[datetime]:
    """When the process was alive, one entry per tick."""
    sql = "SELECT ts FROM equity_snapshots"
    params: tuple[Any, ...] = ()
    if since is not None:
        sql += " WHERE ts >= ?"
        params = (since.isoformat(),)
    return [_parse(row["ts"]) for row in storage.query(sql + " ORDER BY ts", params)]


def closes(interval: str, start: datetime, end: datetime) -> list[datetime]:
    """Candle close instants of `interval` inside the window.

    Binance candles are aligned to the epoch, so the boundaries are found by
    rounding rather than by walking from an arbitrary origin.
    """
    step_ms = INTERVAL_MS.get(interval)
    if not step_ms:
        return []
    step = timedelta(milliseconds=step_ms)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    first = epoch + step * -(-int((start - epoch) / step) // 1)
    if first < start:
        first += step
    out, moment = [], first
    while moment <= end:
        out.append(moment)
        moment += step
    return out


def interval_coverage(interval: str, ticks: list[datetime],
                      start: datetime, end: datetime) -> dict[str, Any]:
    """Which closes of one interval the bot was present for.

    A close counts as covered when a tick happened between it and the next one:
    that is exactly the span in which the newly closed candle is the latest
    closed candle, so a tick inside it sees the signal the backtest saw.
    """
    step = timedelta(milliseconds=INTERVAL_MS.get(interval) or 86_400_000)
    boundaries = closes(interval, start, end)
    covered, prompt, delays, missed = 0, 0, [], []
    cursor = 0
    for moment in boundaries:
        limit = moment + step
        while cursor < len(ticks) and ticks[cursor] < moment:
            cursor += 1
        if cursor < len(ticks) and ticks[cursor] < limit:
            covered += 1
            delay = (ticks[cursor] - moment).total_seconds() / 60
            delays.append(delay)
            if delay <= PROMPT_MINUTES:
                prompt += 1
        else:
            missed.append(moment.isoformat())
    total = len(boundaries)
    delays.sort()
    return {
        "interval": interval,
        "closes": total,
        "covered": covered,
        "missed": total - covered,
        "coverage_pct": round(covered / total * 100, 1) if total else 0.0,
        "prompt": prompt,
        "prompt_pct": round(prompt / covered * 100, 1) if covered else 0.0,
        "median_delay_minutes": round(delays[len(delays) // 2], 1) if delays else None,
        "missed_closes": missed[-40:],
    }


def report(allocations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Coverage per traded interval, since the baseline or the first tick."""
    ticks = tick_times()
    if not ticks:
        return {"since": None, "intervals": [], "coverage_pct": 0.0,
                "hours_live": 0.0, "hours_elapsed": 0.0}

    from .live import get_config
    allocations = allocations if allocations is not None else \
        get_config().get("allocations", [])
    end = datetime.now(timezone.utc)
    mark = baseline()
    start = max(ticks[0], mark) if mark else ticks[0]
    names = sorted({a["interval"] for a in allocations})

    intervals = [interval_coverage(name, ticks, start, end) for name in names]

    # What the baseline set aside, kept where it can still be read. A percentage
    # that silently excludes half its own history is worse than the
    # unflattering one it replaced.
    excluded = None
    if mark and ticks[0] < start:
        before = [interval_coverage(name, ticks, ticks[0], start) for name in names]
        excluded = {
            "since": ticks[0].isoformat(),
            "until": start.isoformat(),
            "closes": sum(item["closes"] for item in before),
            "missed": sum(item["missed"] for item in before),
        }

    # Weighted by candle count, because a missed daily close costs six times
    # what a missed 4h close costs and averaging the percentages would hide it.
    total_closes = sum(item["closes"] for item in intervals)
    covered = sum(item["covered"] for item in intervals)

    # Wall-clock presence, counting a tick as covering the poll window it
    # started. Independent of any interval, and the number that answers "was
    # the machine on".
    poll = timedelta(seconds=max(60, int(get_config().get("poll_seconds", 60))) * 3)
    live = timedelta()
    for index, moment in enumerate(ticks):
        gap = (ticks[index + 1] - moment) if index + 1 < len(ticks) else timedelta()
        live += min(gap, poll)
    elapsed = end - start

    return {
        "since": start.isoformat(),
        "baseline": mark.isoformat() if mark else None,
        "excluded": excluded,
        "intervals": intervals,
        "closes": total_closes,
        "covered": covered,
        "coverage_pct": round(covered / total_closes * 100, 1) if total_closes else 0.0,
        "hours_live": round(live.total_seconds() / 3600, 1),
        "hours_elapsed": round(elapsed.total_seconds() / 3600, 1),
        "uptime_pct": round(live / elapsed * 100, 1) if elapsed else 0.0,
        "ticks": len(ticks),
    }
