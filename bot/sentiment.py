"""Turning headlines into a number, at the moment they are collected.

The score is written when the headline arrives rather than in bulk afterwards,
and that ordering is deliberate for two reasons - one weak, one that actually
matters.

The weak reason is contamination. FinBERT's weights were trained in 2019 on a
financial news corpus that ends long before anything this project collects, so
it cannot possibly know how any of these stories turned out. Scoring today's
headlines with it tomorrow would be just as valid. The strong reason is the
model *after* this one: score a 2026 archive in 2028 with whatever is current
then, and the scorer knows how 2026 went. It will read a headline about a
collapse it remembers and rate it with a confidence no contemporary reader
could have had. That leak lives in the weights, not in the data, so no schema
and no timestamp discipline catches it - only scoring at collection time does.

Hence ``sentiment_model`` on every row. A score is only interpretable next to
the thing that produced it, and a mixed column where half the rows were scored
by one model and half by another is a feature that changes meaning halfway
through the sample.

The model is loaded lazily and every failure is swallowed. A headline without a
score is a headline; a collector that dies because a 400 MB download failed is
a permanent hole in the dataset, which is the one thing this project is trying
to avoid.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from . import storage

# ProsusAI/finbert: three-way positive/negative/neutral over financial news,
# which is what an RSS headline is. CryptoBERT was the alternative and was not
# taken - it is trained on social posts, and the register of a StockTwits
# message is not the register of a Coindesk lede.
MODEL_NAME = "ProsusAI/finbert"
MAX_TOKENS = 256          # headline plus first paragraph; the rest is boilerplate
BATCH_SIZE = 32

_lock = threading.Lock()
_pipeline: Any = None
_load_error: str | None = None
_load_attempted = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def available() -> bool:
    """Is the scorer usable? Never raises, so callers can just ask."""
    return _load() is not None


def status() -> dict[str, Any]:
    unscored = storage.query_one(
        "SELECT COUNT(*) AS n FROM feed_headlines WHERE sentiment IS NULL")["n"]
    scored = storage.query_one(
        "SELECT COUNT(*) AS n FROM feed_headlines WHERE sentiment IS NOT NULL")["n"]
    return {
        "model": MODEL_NAME,
        "loaded": _pipeline is not None,
        "error": _load_error,
        "scored": scored,
        "unscored": unscored,
    }


def _load() -> Any:
    """Import torch and fetch the weights, once, on whoever asks first.

    Both are heavy - the import alone costs seconds and the weights are a few
    hundred megabytes on first run - so neither happens at server start. A
    machine without them keeps collecting headlines with a NULL score.
    """
    global _pipeline, _load_error, _load_attempted
    with _lock:
        if _pipeline is not None or _load_attempted:
            return _pipeline
        _load_attempted = True
        try:
            from transformers import pipeline as hf_pipeline

            _pipeline = hf_pipeline(
                "text-classification", model=MODEL_NAME,
                top_k=None, truncation=True, max_length=MAX_TOKENS, device=-1,
            )
            _load_error = None
        except Exception as exc:
            _pipeline = None
            _load_error = f"{type(exc).__name__}: {exc}"
        return _pipeline


def retry_load() -> dict[str, Any]:
    """Clear a failed load so the next call tries again."""
    global _load_attempted, _load_error
    with _lock:
        _load_attempted = False
        _load_error = None
    _load()
    return status()


def _to_score(scores: list[dict[str, Any]]) -> float:
    """Collapse the three labels into one number in [-1, 1].

    Positive minus negative, ignoring neutral. Neutral is not a midpoint
    between the two, it is the absence of both, and a confidently neutral
    headline should land on zero rather than dragging the score anywhere.
    """
    by_label = {str(s["label"]).lower(): float(s["score"]) for s in scores}
    return by_label.get("positive", 0.0) - by_label.get("negative", 0.0)


def score_texts(texts: list[str]) -> list[float] | None:
    """Score a list of strings, or None if the model is unavailable."""
    model = _load()
    if model is None or not texts:
        return None
    out: list[float] = []
    for start in range(0, len(texts), BATCH_SIZE):
        chunk = texts[start:start + BATCH_SIZE]
        for result in model(chunk):
            out.append(_to_score(result))
    return out


def score_pending(limit: int = 500) -> dict[str, Any]:
    """Score headlines that have none yet.

    Called right after every headline poll, so in steady state this handles the
    handful of items that arrived in the last ten minutes. The limit exists for
    the first run after the model is installed, when a backlog is waiting.
    """
    rows = storage.query(
        "SELECT id, title, summary FROM feed_headlines"
        " WHERE sentiment IS NULL ORDER BY id LIMIT ?", (limit,))
    if not rows:
        return {"scored": 0, "pending": 0, "model": MODEL_NAME}

    # Title and lede together: a headline alone is often too short to place,
    # and the first sentence of the summary usually carries the direction.
    texts = [
        f"{row['title']}. {(row['summary'] or '')}".strip()[:1200]
        for row in rows
    ]
    scores = score_texts(texts)
    if scores is None:
        return {"scored": 0, "pending": len(rows), "model": MODEL_NAME,
                "error": _load_error}

    stamped = _now()
    written = storage.execute_many(
        "UPDATE feed_headlines SET sentiment=?, sentiment_model=?, scored_at=?"
        " WHERE id=?",
        [(score, MODEL_NAME, stamped, row["id"]) for score, row in zip(scores, rows)],
    )
    remaining = storage.query_one(
        "SELECT COUNT(*) AS n FROM feed_headlines WHERE sentiment IS NULL")["n"]
    return {"scored": written, "pending": remaining, "model": MODEL_NAME}


def daily_series(days: int = 400) -> list[dict[str, Any]]:
    """Mean sentiment per day, keyed on when we saw the item.

    Grouped by ``observed_at`` rather than ``published_at`` because the whole
    point is what a model could have read at the time. A story published at
    23:50 and first seen at 00:04 belongs to the following day's features, and
    grouping it under its publication date would hand the model an hour of
    hindsight for free.
    """
    return storage.query(
        "SELECT substr(observed_at, 1, 10) AS day, COUNT(*) AS n,"
        "       AVG(sentiment) AS mean_sentiment,"
        "       SUM(CASE WHEN sentiment >  0.25 THEN 1 ELSE 0 END) AS positive,"
        "       SUM(CASE WHEN sentiment < -0.25 THEN 1 ELSE 0 END) AS negative"
        "  FROM feed_headlines WHERE sentiment IS NOT NULL"
        " GROUP BY day ORDER BY day DESC LIMIT ?", (days,))
