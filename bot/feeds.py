"""Point-in-time collection of everything the candle history does not carry.

A predictor built on market context - funding, positioning, sentiment, news -
can only be tested honestly against data that was *available* at the moment the
decision would have been made. Almost no public source preserves that. Two
failures make historical context unusable for backtesting, and they are
different problems:

**Retention.** Binance keeps only 30 days of open interest and long/short
ratio. A walk-forward needs eight quarterly windows. The history simply is not
there to be downloaded, at any price, so the only way to obtain it is to start
writing it down.

**Backdating.** News APIs return a ``published_at`` set by the publisher, not
the moment the item became visible to a reader. Articles get edited, backdated,
and indexed hours late. Train a model on ``published_at`` and it learns from
headlines that had not appeared yet, scores beautifully out of sample, and
collapses live. This is the single most common way a sentiment model is wrong,
and it is invisible in every metric until real money is on it.

So every row here carries two timestamps and never conflates them:

``source_ts``    what the source says the observation is about;
``observed_at``  when this process actually received it.

``observed_at`` is the one a model may condition on. It is accurate to one poll
interval and it can never run ahead of reality, which is the only property that
matters. ``source_ts`` is kept because it is needed to align a series to a
candle, and because the gap between the two is itself worth studying.

Collection runs on the server process rather than the trading loop, deliberately.
The dataset's whole value is being unbroken; stopping the bot to change a
strategy must not put a hole in it.
"""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable

import httpx

from . import storage

FUTURES_BASE = "https://fapi.binance.com"
FNG_URL = "https://api.alternative.me/fng/"

# Public RSS, no key, no terms that forbid reading them. Bitcoin Magazine is
# deliberately absent: its feed contains undefined XML entities and fails to
# parse, and a source that needs hand-repair is a source that will break
# silently at three in the morning.
NEWS_SOURCES: dict[str, str] = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "decrypt": "https://decrypt.co/feed",
    "theblock": "https://www.theblock.co/rss.xml",
}

# Every request asks for far more history than one interval's worth. That is
# the gap-healing mechanism: come back after a week down and the same call that
# collects the newest point also fills what was missed, because the unique index
# turns the overlap into a no-op. The cost is re-sending rows already held,
# which is a few thousand ignored inserts an hour - nothing against the price of
# a permanent hole in a series that cannot be re-downloaded.
OPEN_INTEREST_PERIOD = "1h"
LOOKBACK_POINTS = 168          # a week of gap tolerance on the routine poll
DEEP_LOOKBACK_POINTS = 500     # the API's ceiling, used once at backfill

# One poll of a feed that has not moved is cheap; one missed hour is permanent.
# The intervals below are all comfortably faster than the sources update.
CADENCE_SECONDS: dict[str, int] = {
    "funding": 3600,          # published every 8h
    "open_interest": 3600,    # 1h buckets
    "long_short": 3600,       # 1h buckets
    "fear_greed": 21600,      # once a day, checked four times
    "headlines": 600,         # the tighter this is, the truer observed_at is
}

_client = httpx.Client(
    timeout=20.0,
    follow_redirects=True,
    # Some publishers reject the default httpx agent outright.
    headers={"User-Agent": "Mozilla/5.0 (compatible; Pouch/1.0; research collector)"},
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp(milliseconds: Any) -> str:
    return datetime.fromtimestamp(int(milliseconds) / 1000, timezone.utc).isoformat(
        timespec="seconds")


# ------------------------------------------------------------------- writing

def record(rows: Iterable[dict[str, Any]]) -> int:
    """Insert observations, ignoring any already held.

    Returns the number of genuinely new rows, which is what makes a poll's log
    line worth reading: 0 means the source has not moved, and a sudden 40 means
    the process was asleep.
    """
    return storage.execute_many(
        "INSERT OR IGNORE INTO feed_observations"
        "(feed, symbol, source_ts, observed_at, value, detail)"
        " VALUES(:feed, :symbol, :source_ts, :observed_at, :value, :detail)",
        rows,
    )


def record_headlines(rows: Iterable[dict[str, Any]]) -> int:
    return storage.execute_many(
        "INSERT OR IGNORE INTO feed_headlines"
        "(source, guid, published_at, observed_at, title, link, summary)"
        " VALUES(:source, :guid, :published_at, :observed_at, :title, :link, :summary)",
        rows,
    )


# ----------------------------------------------------------------- collectors

def _futures(path: str, params: dict[str, Any]) -> Any:
    response = _client.get(f"{FUTURES_BASE}{path}", params=params)
    if response.status_code >= 400:
        raise RuntimeError(f"{path} HTTP {response.status_code}: {response.text[:200]}")
    return response.json()


def collect_funding(symbols: list[str], start_ms: int | None = None) -> int:
    """Perpetual funding rate: what longs pay shorts, or the reverse.

    The one futures series Binance will hand over in full - it pages back to
    2020 - so it is the only piece of positioning data that can be researched
    today instead of in a year.
    """
    observed = _now()
    rows = []
    for symbol in symbols:
        params: dict[str, Any] = {"symbol": symbol, "limit": 1000}
        if start_ms is not None:
            params["startTime"] = start_ms
        for entry in _futures("/fapi/v1/fundingRate", params):
            rows.append({
                "feed": "funding", "symbol": symbol,
                "source_ts": _stamp(entry["fundingTime"]),
                "observed_at": observed,
                "value": float(entry["fundingRate"]),
                "detail": None,
            })
    return record(rows)


def collect_open_interest(symbols: list[str], limit: int = LOOKBACK_POINTS) -> int:
    """Total value of open perpetual contracts. Retained for 30 days only."""
    observed = _now()
    rows = []
    for symbol in symbols:
        entries = _futures("/futures/data/openInterestHist", {
            "symbol": symbol, "period": OPEN_INTEREST_PERIOD, "limit": limit,
        })
        for entry in entries:
            rows.append({
                "feed": "open_interest", "symbol": symbol,
                "source_ts": _stamp(entry["timestamp"]),
                "observed_at": observed,
                "value": float(entry["sumOpenInterestValue"]),
                "detail": storage.dumps({"contracts": float(entry["sumOpenInterest"])}),
            })
    return record(rows)


def collect_long_short(symbols: list[str], limit: int = LOOKBACK_POINTS) -> int:
    """Share of top accounts positioned long. Retained for 30 days only."""
    observed = _now()
    rows = []
    for symbol in symbols:
        entries = _futures("/futures/data/topLongShortAccountRatio", {
            "symbol": symbol, "period": OPEN_INTEREST_PERIOD, "limit": limit,
        })
        for entry in entries:
            rows.append({
                "feed": "long_short", "symbol": symbol,
                "source_ts": _stamp(entry["timestamp"]),
                "observed_at": observed,
                "value": float(entry["longAccount"]),
                "detail": storage.dumps({"ratio": float(entry["longShortRatio"])}),
            })
    return record(rows)


def collect_fear_greed(limit: int = 30) -> int:
    """The Fear and Greed index: one market-wide number, daily, back to 2018.

    Measured against next-day returns it explains nothing - correlation +0.015
    on BTC over 1497 days - so it is collected as a control rather than as a
    hope. A feature that is known to be inert is useful: a model that finds
    signal in it has found overfitting, and that is worth being able to detect.
    """
    observed = _now()
    payload = _client.get(FNG_URL, params={"limit": limit})
    if payload.status_code >= 400:
        raise RuntimeError(f"fng HTTP {payload.status_code}")
    rows = []
    for entry in payload.json()["data"]:
        rows.append({
            "feed": "fear_greed", "symbol": None,
            "source_ts": _stamp(int(entry["timestamp"]) * 1000),
            "observed_at": observed,
            "value": float(entry["value"]),
            "detail": storage.dumps({"classification": entry["value_classification"]}),
        })
    return record(rows)


def _published(item: ET.Element) -> str | None:
    raw = item.findtext("pubDate") or item.findtext("date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat(
            timespec="seconds")
    except (TypeError, ValueError):
        return None


def collect_headlines() -> int:
    """Headlines from public RSS, stamped with when this process saw them.

    Only ``observed_at`` is safe to train on. ``published_at`` comes from the
    publisher and is routinely earlier than the moment the item was actually
    readable; the feeds also carry pieces edited and re-dated after the fact.
    Both are stored so the discrepancy can be measured, but a model that
    conditions on the publisher's timestamp is reading tomorrow's paper.

    A source that fails is skipped, not fatal. Four independent publishers
    exist here precisely so that one of them going down is a gap in one column
    rather than a gap in the dataset.
    """
    observed = _now()
    rows = []
    failures = []
    for source, url in NEWS_SOURCES.items():
        try:
            response = _client.get(url)
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except Exception as exc:
            failures.append(f"{source}: {type(exc).__name__}")
            continue
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or link or title).strip()
            if not title or not guid:
                continue
            summary = (item.findtext("description") or "").strip()
            rows.append({
                "source": source, "guid": guid,
                "published_at": _published(item),
                "observed_at": observed,
                "title": title, "link": link or None,
                # Descriptions run to full articles on some feeds; the first
                # paragraph is what a headline model would use anyway.
                "summary": summary[:1000] or None,
            })
    written = record_headlines(rows)
    if failures and not rows:
        raise RuntimeError("every news source failed: " + ", ".join(failures))
    if written:
        # Scored here rather than in bulk later, and deliberately. A model run
        # over an archive knows how the archive turned out; one run at
        # collection time cannot. See bot/sentiment.py. A scorer that is
        # missing or broken leaves the column NULL and is not allowed to take
        # the collector down with it.
        try:
            from . import sentiment

            sentiment.score_pending()
        except Exception as exc:
            storage.log_event("warn", f"Sentimento não pontuado: {exc}")
    return written


# --------------------------------------------------------------- the schedule

def book_symbols() -> list[str]:
    """Collect for what is traded, plus the two the whole market keys off.

    Imported lazily: bot.live imports plenty, and feeds is imported at server
    start whether or not anything is trading.
    """
    from .live import get_config

    symbols = {a["symbol"] for a in get_config().get("allocations", [])}
    symbols.update({"BTCUSDT", "ETHUSDT"})
    return sorted(symbols)


def _tasks() -> dict[str, Callable[[], int]]:
    return {
        "funding": lambda: collect_funding(book_symbols()),
        "open_interest": lambda: collect_open_interest(book_symbols()),
        "long_short": lambda: collect_long_short(book_symbols()),
        "fear_greed": collect_fear_greed,
        "headlines": collect_headlines,
    }


class Collector:
    """One thread, every feed on its own clock.

    Feeds are polled independently so that a source being down delays only
    itself. Failures are recorded on the feed rather than raised, because the
    collector going quiet is worse than any single feed going stale - and the
    event log already folds a repeating error onto one counted row, so an
    outage costs one line rather than one line per attempt.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.status: dict[str, dict[str, Any]] = {
            name: {"last_run": None, "last_error": None, "written": 0, "runs": 0}
            for name in CADENCE_SECONDS
        }

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.running:
                return {"running": True, "message": "already running"}
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="feeds")
            self._thread.start()
            return {"running": True, "message": "started"}

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        return {"running": False, "message": "stopped"}

    def run_once(self, only: str | None = None) -> dict[str, int | str]:
        """Poll every feed now. Used at startup and by the manual endpoint."""
        results: dict[str, int | str] = {}
        for name, task in _tasks().items():
            if only and name != only:
                continue
            state = self.status[name]
            try:
                written = task()
                state.update({"last_run": _now(), "last_error": None,
                              "written": written, "runs": state["runs"] + 1})
                results[name] = written
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:300]
                state.update({"last_run": _now(), "last_error": message,
                              "runs": state["runs"] + 1})
                results[name] = message
                storage.log_event("error", f"Feed {name} failed: {message}")
        return results

    def _loop(self) -> None:
        due = {name: 0.0 for name in CADENCE_SECONDS}
        tasks = _tasks()
        while not self._stop.is_set():
            now = time.monotonic()
            for name, interval in CADENCE_SECONDS.items():
                if now < due[name]:
                    continue
                self.run_once(only=name)
                due[name] = time.monotonic() + interval
            # Short sleep, long cadences: the thread wakes often enough to stop
            # promptly and does nothing on almost every wake.
            self._stop.wait(30)


collector = Collector()


# -------------------------------------------------------------------- reading

def coverage() -> dict[str, Any]:
    """What has been collected, and how far it reaches.

    ``usable_from`` is the honest answer to "when can this be researched": for
    a series that had to be collected forward it is the first row's timestamp,
    and everything before that does not exist and never will.
    """
    feeds = []
    for row in storage.query(
        "SELECT feed, COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols,"
        " MIN(source_ts) AS first_ts, MAX(source_ts) AS last_ts,"
        " MAX(observed_at) AS last_seen"
        " FROM feed_observations GROUP BY feed ORDER BY feed"
    ):
        entry = dict(row)
        entry["days"] = _span_days(entry["first_ts"], entry["last_ts"])
        entry["status"] = collector.status.get(entry["feed"], {})
        feeds.append(entry)

    news = storage.query_one(
        "SELECT COUNT(*) AS rows, COUNT(DISTINCT source) AS sources,"
        " MIN(observed_at) AS first_ts, MAX(observed_at) AS last_ts"
        " FROM feed_headlines") or {}
    news = dict(news)
    news.update({
        "feed": "headlines",
        "days": _span_days(news.get("first_ts"), news.get("last_ts")),
        "status": collector.status.get("headlines", {}),
    })

    return {
        "running": collector.running,
        "feeds": feeds,
        "news": news,
        "cadence_seconds": CADENCE_SECONDS,
    }


def _span_days(first: str | None, last: str | None) -> float:
    if not first or not last:
        return 0.0
    try:
        delta = datetime.fromisoformat(last) - datetime.fromisoformat(first)
    except ValueError:
        return 0.0
    return round(delta / timedelta(days=1), 1)


def series(feed: str, symbol: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    """One feed's observations, newest first."""
    if symbol:
        return storage.query(
            "SELECT * FROM feed_observations WHERE feed = ? AND symbol = ?"
            " ORDER BY source_ts DESC LIMIT ?", (feed, symbol, limit))
    return storage.query(
        "SELECT * FROM feed_observations WHERE feed = ?"
        " ORDER BY source_ts DESC LIMIT ?", (feed, limit))


def headlines(limit: int = 50) -> list[dict[str, Any]]:
    return storage.query(
        "SELECT * FROM feed_headlines ORDER BY observed_at DESC, id DESC LIMIT ?",
        (limit,))


# ------------------------------------------------------------------- backfill

# Funding exists from the launch of each perpetual; 2020 covers all of them and
# costs one wasted request on the ones that started later.
BACKFILL_FROM = datetime(2020, 1, 1, tzinfo=timezone.utc)


def backfill_funding(symbols: list[str] | None = None) -> dict[str, int]:
    """Page funding back to 2020, once.

    Everything else in this module has to be collected going forward. This one
    does not, so it is worth the several hundred requests to have it now: a
    positioning feature that can be walked forward over six years is available
    for research immediately rather than in twelve months.
    """
    symbols = symbols or book_symbols()
    written: dict[str, int] = {}
    for symbol in symbols:
        total = 0
        cursor = int(BACKFILL_FROM.timestamp() * 1000)
        while True:
            entries = _futures("/fapi/v1/fundingRate",
                               {"symbol": symbol, "startTime": cursor, "limit": 1000})
            if not entries:
                break
            observed = _now()
            total += record([{
                "feed": "funding", "symbol": symbol,
                "source_ts": _stamp(entry["fundingTime"]),
                # Backfilled rows were observed now, not when they happened.
                # Saying otherwise would forge exactly the property this module
                # exists to protect, so the two timestamps are simply far apart
                # here and a model that filters on observed_at will correctly
                # refuse to use them for anything before today.
                "observed_at": observed,
                "value": float(entry["fundingRate"]),
                "detail": None,
            } for entry in entries])
            last = int(entries[-1]["fundingTime"])
            if len(entries) < 1000:
                break
            cursor = last + 1
            # Binance's futures weight limit is generous, but a tight loop over
            # eighteen symbols is still a good way to be rate-limited.
            time.sleep(0.2)
        written[symbol] = total
    storage.log_event("info", f"Funding backfilled for {len(symbols)} symbols",
                      {"rows": sum(written.values())})
    return written


def backfill_fear_greed() -> int:
    """The whole index, 2018 to today. Same caveat as funding on observed_at."""
    written = collect_fear_greed(limit=0)
    storage.log_event("info", "Fear and Greed backfilled", {"rows": written})
    return written


def backfill_positioning(symbols: list[str] | None = None) -> dict[str, int]:
    """Take the whole retention window of open interest and long/short, once.

    Binance keeps about 30 days of these and the API returns at most 500 points,
    which at hourly resolution is 20 days. That is all there will ever be of the
    past for these two series - everything earlier is already gone, and
    everything later has to be waited for.
    """
    symbols = symbols or book_symbols()
    return {
        "open_interest": collect_open_interest(symbols, DEEP_LOOKBACK_POINTS),
        "long_short": collect_long_short(symbols, DEEP_LOOKBACK_POINTS),
    }
