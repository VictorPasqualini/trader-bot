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

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL,
    context TEXT
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
    execute(
        "INSERT INTO events(ts, level, message, context) VALUES(?, ?, ?, ?)",
        (now(), level, message, json.dumps(context) if context else None),
    )


def recent_events(limit: int = 60) -> list[dict[str, Any]]:
    rows = query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    for row in rows:
        row["context"] = json.loads(row["context"]) if row["context"] else None
    return rows


init()
