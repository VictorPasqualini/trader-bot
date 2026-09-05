"""SQLite persistence. One file under ``data/``, no server, no migrations tool."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import DB_PATH

_local = threading.local()
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    config      TEXT NOT NULL,
    progress    INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    stage       TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS research_results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL,
    symbol     TEXT NOT NULL,
    interval   TEXT NOT NULL,
    strategy   TEXT NOT NULL,
    label      TEXT NOT NULL,
    family     TEXT NOT NULL,
    params     TEXT NOT NULL,
    risk       TEXT NOT NULL,
    train      TEXT NOT NULL,
    test       TEXT NOT NULL,
    full       TEXT NOT NULL,
    curve      TEXT NOT NULL,
    score      REAL NOT NULL,
    validated  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_run ON research_results(run_id, score DESC);

CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    interval    TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    params      TEXT NOT NULL,
    risk        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    qty         REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_time  TEXT NOT NULL,
    entry_quote REAL NOT NULL,
    exit_price  REAL,
    exit_time   TEXT,
    exit_quote  REAL,
    pnl         REAL,
    return_pct  REAL,
    reason      TEXT,
    mode        TEXT NOT NULL DEFAULT 'testnet',
    -- Why the bot acted: the strategy rule plus the indicator values that
    -- satisfied it, snapshotted at the bar that triggered the order.
    entry_context TEXT,
    exit_context  TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status, entry_time DESC);

CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL,
    qty        REAL NOT NULL,
    price      REAL NOT NULL,
    quote      REAL NOT NULL,
    order_id   TEXT,
    status     TEXT NOT NULL,
    strategy   TEXT,
    note       TEXT,
    position_id INTEGER,
    -- What the exchange actually took, in whichever asset it took it. Measured,
    -- not derived from the configured fee rate: the testnet charges nothing and
    -- a real account charges in the coin bought, so the difference between the
    -- two is exactly the thing a forward test on the testnet cannot see.
    fee        REAL,
    fee_asset  TEXT
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts             TEXT PRIMARY KEY,
    total_value    REAL NOT NULL,
    free_quote     REAL NOT NULL,
    positions_value REAL NOT NULL,
    open_positions INTEGER NOT NULL
);

-- An identical event repeating is one fact, not many. A two-hour network
-- outage polling once a minute writes 120 rows of the same sentence, which
-- pushes everything that happened before it out of any readable window and
-- makes the log least useful exactly when something is wrong. Consecutive
-- repeats collapse onto one row instead: `first_ts` keeps when it started,
-- `ts` moves to the latest, `repeats` counts them.
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    first_ts TEXT,
    level    TEXT NOT NULL,
    message  TEXT NOT NULL,
    repeats  INTEGER NOT NULL DEFAULT 1,
    context  TEXT
);

-- What the book was predicted to do, written when the book changed and never
-- recomputed. A prediction recalculated after the fact is not a prediction:
-- the walk-forward it comes from would by then include the very period being
-- judged. See bot/tracking.py.
CREATE TABLE IF NOT EXISTS expectations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at     TEXT NOT NULL,
    -- When the book this row describes started trading. Normally the same as
    -- recorded_at; earlier only for the first row ever written, which has to
    -- cover a run that predates the table.
    effective_from  TEXT NOT NULL,
    book            TEXT NOT NULL,
    start_capital   REAL NOT NULL,
    quote_per_trade REAL NOT NULL,
    allocations     INTEGER NOT NULL,
    return_pct_month  REAL NOT NULL,
    worst_quarter_pct REAL NOT NULL,
    trades_month      REAL NOT NULL,
    detail          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expectations_from ON expectations(effective_from);

-- Market context that the candle history does not carry, captured with the
-- one property no public archive preserves: when we actually saw it.
-- `source_ts` is what the source says the observation is about, `observed_at`
-- is when this process received it. Only the second is safe to condition a
-- model on. See bot/feeds.py for why the distinction decides whether any of
-- this is usable.
CREATE TABLE IF NOT EXISTS feed_observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    feed        TEXT NOT NULL,
    -- NULL for market-wide series such as Fear and Greed.
    symbol      TEXT,
    source_ts   TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    value       REAL,
    detail      TEXT
);
-- Polls overlap on purpose: every request asks for far more history than one
-- interval, so a gap heals itself on the next successful call. This index is
-- what makes that free - the overlap collapses into no-ops instead of
-- duplicates. COALESCE because SQLite treats NULLs as distinct in a unique
-- index, which would let every poll re-insert the whole Fear and Greed series.
CREATE UNIQUE INDEX IF NOT EXISTS idx_feed_point
    ON feed_observations(feed, COALESCE(symbol, ''), source_ts);
CREATE INDEX IF NOT EXISTS idx_feed_symbol
    ON feed_observations(feed, symbol, source_ts);

-- Headlines are stored separately because the point-in-time question is
-- sharper here: `published_at` comes from the publisher and is routinely
-- earlier than the moment the item was readable, so a model that trains on it
-- is reading news from the future. `observed_at` is the poll that first
-- returned the item, and is the only timestamp that cannot run ahead.
CREATE TABLE IF NOT EXISTS feed_headlines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    guid         TEXT NOT NULL,
    published_at TEXT,
    observed_at  TEXT NOT NULL,
    title        TEXT NOT NULL,
    link         TEXT,
    summary      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_headline_guid
    ON feed_headlines(source, guid);
CREATE INDEX IF NOT EXISTS idx_headline_seen
    ON feed_headlines(observed_at);

-- The parallel experiment: a model that is allowed to be wrong, kept in its
-- own tables rather than sharing the live book's. The live book is a forward
-- test whose value is that nothing has touched it, so the experiment gets no
-- write path into positions, orders or equity_snapshots at all. Isolation by
-- schema, not by a flag someone can forget to filter on.
CREATE TABLE IF NOT EXISTS lab_positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    qty         REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_time  TEXT NOT NULL,
    entry_quote REAL NOT NULL,
    -- The model's probability at entry, and the model that produced it. Two
    -- trades taken at 0.51 and 0.80 are not the same trade, and a book that
    -- cannot tell them apart cannot tell whether the probability means
    -- anything.
    entry_prob  REAL,
    model_id    INTEGER,
    exit_price  REAL,
    exit_time   TEXT,
    exit_quote  REAL,
    exit_prob   REAL,
    pnl         REAL,
    return_pct  REAL,
    reason      TEXT,
    features    TEXT
);
CREATE INDEX IF NOT EXISTS idx_lab_positions ON lab_positions(status, entry_time DESC);

CREATE TABLE IF NOT EXISTS lab_equity (
    ts              TEXT PRIMARY KEY,
    total_value     REAL NOT NULL,
    free_quote      REAL NOT NULL,
    positions_value REAL NOT NULL,
    open_positions  INTEGER NOT NULL
);

-- One row per training run, kept forever. A model that is retrained weekly and
-- overwritten leaves no way to ask whether this week's version is better than
-- the one that took last month's trades, which is the only question that
-- matters about retraining.
CREATE TABLE IF NOT EXISTS lab_models (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trained_at   TEXT NOT NULL,
    rows         INTEGER NOT NULL,
    features     TEXT NOT NULL,
    params       TEXT NOT NULL,
    -- Purged walk-forward results, measured against holding the whole universe
    -- equally weighted rather than against zero.
    cv           TEXT NOT NULL,
    -- Share of days the model's basket beat that benchmark.
    accuracy     REAL NOT NULL,
    edge_pct     REAL NOT NULL,
    -- How many coins the basket holds. Positions are chosen by rank, so there
    -- is no probability cut-off to store.
    top_k        INTEGER NOT NULL,
    active       INTEGER NOT NULL DEFAULT 0,
    blob         TEXT
);
CREATE INDEX IF NOT EXISTS idx_lab_models ON lab_models(trained_at DESC);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connection() -> sqlite3.Connection:
    """One connection per thread; SQLite objects are not thread-safe."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


# Columns added after the first release. CREATE TABLE IF NOT EXISTS leaves an
# existing table alone, so they have to be added by hand on databases that
# predate them.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("positions", "entry_context", "TEXT"),
    ("positions", "exit_context", "TEXT"),
    ("orders", "position_id", "INTEGER"),
    ("orders", "fee", "REAL"),
    ("orders", "fee_asset", "TEXT"),
    ("events", "first_ts", "TEXT"),
    ("events", "repeats", "INTEGER NOT NULL DEFAULT 1"),
    # A score is only interpretable next to the model that produced it, so the
    # model name is stored per row rather than assumed. See bot/sentiment.py.
    ("feed_headlines", "sentiment", "REAL"),
    ("feed_headlines", "sentiment_model", "TEXT"),
    ("feed_headlines", "scored_at", "TEXT"),
)


def init() -> None:
    conn = connection()
    conn.executescript(SCHEMA)
    for table, column, kind in ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    conn.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    with _write_lock:
        conn = connection()
        cursor = conn.execute(sql, tuple(params))
        conn.commit()
        return cursor


def execute_many(sql: str, rows: Iterable[Any]) -> int:
    """Run one statement over many rows, returning how many actually landed.

    The count comes from the connection's change counter rather than from
    ``cursor.rowcount``, which reports the number of statements attempted. The
    difference matters for the ``INSERT OR IGNORE`` that the feed collectors
    use: what is worth knowing there is how much of the batch was new, and
    every poll deliberately re-sends rows it already holds.
    """
    rows = list(rows)
    if not rows:
        return 0
    with _write_lock:
        conn = connection()
        before = conn.total_changes
        conn.executemany(sql, rows)
        conn.commit()
        return conn.total_changes - before


def dumps(value: Any) -> str | None:
    """JSON for a column that holds it, or NULL for nothing worth storing."""
    return json.dumps(value) if value else None


def query(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection().execute(sql, tuple(params)).fetchall()]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    row = connection().execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None


# ----------------------------------------------------------------- key/value

def set_state(key: str, value: Any) -> None:
    execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def get_state(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value FROM kv WHERE key = ?", (key,))
    return json.loads(row["value"]) if row else default


# --------------------------------------------------------------------- events

def log_event(level: str, message: str, context: dict[str, Any] | None = None) -> None:
    """Record one event, folding it into the previous row if it is the same one.

    Only the immediately preceding row is considered, so an event that recurs
    with anything in between still gets its own line and the sequence stays
    readable. The context of the first occurrence is kept rather than the
    latest: for the case this exists for - a stack trace repeating every poll -
    they are the same text, and the first one is the one with the timestamp
    that matters.
    """
    stamp = now()
    previous = query_one("SELECT * FROM events ORDER BY id DESC LIMIT 1")
    if previous and previous["level"] == level and previous["message"] == message:
        execute(
            "UPDATE events SET ts = ?, repeats = repeats + 1,"
            " first_ts = COALESCE(first_ts, ts) WHERE id = ?",
            (stamp, previous["id"]),
        )
        return
    execute(
        "INSERT INTO events(ts, first_ts, level, message, repeats, context)"
        " VALUES(?, ?, ?, ?, 1, ?)",
        (stamp, stamp, level, message, json.dumps(context) if context else None),
    )


def recent_events(limit: int = 60) -> list[dict[str, Any]]:
    rows = query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    for row in rows:
        row["context"] = json.loads(row["context"]) if row["context"] else None
    return rows


init()
