"""The exit study: same entries, different ways of getting out.

The live book holds until its strategy says to leave. The obvious objection is
that this hands back gains that were already on the screen - a position up 3%
becomes a position down 1% while the rule waits for its own condition. The
obvious answer is to sell at a fixed profit instead.

Backtests can argue about that and this file does not. It runs the argument
forward, on the live book's own trades, as they happen.

Every arm here shadows the same live positions: same coin, same entry price,
same entry moment, same size. One arm - `rule` - exits exactly when the live
book exits, and exists to prove the harness is faithful rather than to test an
idea. The others sell the moment the position shows their target profit, and
otherwise fall back to the rule's exit. Nothing else varies, so a difference
between arms is the exit and can be nothing else.

Isolation is by schema. This module reads `positions` and writes only
`mirror_positions` and `mirror_equity`. There is no code path from here into
`positions`, `orders`, `equity_snapshots` or any `lab_` table. It cannot
disturb the forward test or the ranking experiment because it has nowhere to
write that would.
"""

from __future__ import annotations

import threading
import traceback
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from . import research, storage
from .config import settings
from .exchange import BinanceError, exchange

# The targets are fixed here rather than tuned, and they are written down
# before any of them has traded. Three of them, spread wide enough to show a
# shape rather than a point: if selling at a profit helps, the help should not
# arrive only at one magic percentage. Picking the winner afterwards from a
# grid of many would be the same selection error the lab's parameter sweep is
# careful not to claim as evidence.
TARGETS: tuple[float, ...] = (2.0, 5.0, 10.0)
CONTROL = "rule"

# Every line this module writes to the event log carries this tag, so the
# activity panel can show what the study did without three books talking over
# each other.
SOURCE = "mirror"

# Same size on every arm, so the comparison is not quietly a comparison of
# position sizes. Eleven is the live book's own limit on concurrent positions,
# so an arm can always mirror everything the live book holds and never has to
# skip a trade for lack of room - a skipped trade would break the pairing that
# the whole study rests on.
QUOTE_PER_TRADE = 100.0
MAX_POSITIONS = 11
POLL_SECONDS = 300

# The arms share one capital figure because they are alternative histories of
# the same money, not four books running at once. Only one of them can be true.
# Multiplying the base by the number of arms would invent capital that never
# existed and shrink every reported return by a factor of four.
#
# It is a denominator and nothing else. The study places no orders - it shadows
# trades the live book already made - so it consumes no exchange balance, and
# this number is not taken from any book's start_capital either. Moving that
# figure would shift the whole equity curve of whichever book it came from.
CAPITAL = 2_500.0

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "quote_per_trade": QUOTE_PER_TRADE,
    "capital": CAPITAL,
    "targets": list(TARGETS),
    "poll_seconds": POLL_SECONDS,
    # Adopting the positions that were already open when the study started
    # gets it moving today instead of waiting for the next entry, which on a
    # book of daily strategies can be weeks. Their outcomes are tagged, and
    # every report separates them, because a trade this book did not choose is
    # not evidence about this book.
    "adopt_open": True,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_config() -> dict[str, Any]:
    return {**DEFAULTS, **(storage.get_state("mirror_config") or {})}


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    config = {**get_config(), **patch}
    storage.set_state("mirror_config", config)
    return config


def arms(config: dict[str, Any] | None = None) -> list[str]:
    config = config or get_config()
    return [CONTROL] + [f"t{target:g}" for target in config["targets"]]


def target_of(arm: str) -> float | None:
    """The profit an arm sells at, or None for the control."""
    return None if arm == CONTROL else float(arm[1:])


def _arm_label(arm: str) -> str:
    target = target_of(arm)
    return "regra decide" if target is None else f"alvo +{target:g}%"


# ------------------------------------------------------------------ the ledger

def open_positions(arm: str | None = None) -> list[dict[str, Any]]:
    if arm:
        return storage.query(
            "SELECT * FROM mirror_positions WHERE status='open' AND arm=? ORDER BY id",
            (arm,))
    return storage.query(
        "SELECT * FROM mirror_positions WHERE status='open' ORDER BY arm, id")


def closed_positions(arm: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if arm:
        return storage.query(
            "SELECT * FROM mirror_positions WHERE status='closed' AND arm=?"
            " ORDER BY exit_time DESC LIMIT ?", (arm, limit))
    return storage.query(
        "SELECT * FROM mirror_positions WHERE status='closed'"
        " ORDER BY exit_time DESC LIMIT ?", (limit,))


def _live_positions() -> list[dict[str, Any]]:
    """The live book's trades. Read only - this module never writes here."""
    return storage.query("SELECT * FROM positions ORDER BY id")


def _buyable(symbol: str, quote: float, price: float) -> tuple[float, float]:
    """The quantity an order would actually get, and what it would actually cost.

    An exchange sells in lot steps, so $100 of a coin is almost never $100 of
    the coin: the order is floored to the step and the real outlay is whatever
    that quantity costs. Reporting the requested amount instead would show a
    round $100 on every line and quietly misstate the money at work - and it is
    the money at work that every return here is divided by.

    If the exchange cannot be reached the request stands unrounded. A missing
    filter is not a reason to stop mirroring, and the error it would otherwise
    raise would take the whole tick down.
    """
    try:
        qty = exchange.round_qty(symbol, quote / price)
    except Exception:
        qty = quote / price
    if qty <= 0:
        qty = quote / price
    return qty, qty * price


def _open(arm: str, live: dict[str, Any], quote: float,
          adopted: bool) -> dict[str, Any] | None:
    """Shadow one live entry.

    The entry price is the live fill, unchanged and with no extra cost charged.
    That fill is already what the exchange gave the live book, and every arm
    inherits the same one, so entry cost is identical across arms and cancels
    out of every comparison drawn from them. Only the exits are charged, and
    they are charged the same way on every arm.
    """
    target = target_of(arm)
    price = float(live["entry_price"])
    if price <= 0:
        return None
    qty, spent = _buyable(live["symbol"], quote, price)
    try:
        storage.execute(
            "INSERT INTO mirror_positions(arm, source_id, symbol, interval, strategy,"
            " status, qty, entry_price, entry_time, entry_quote, target_pct,"
            " target_price, adopted) VALUES(?,?,?,?,?,'open',?,?,?,?,?,?,?)",
            (arm, int(live["id"]), live["symbol"], live["interval"], live["strategy"],
             qty, price, live["entry_time"], spent, target,
             price * (1 + target / 100) if target else None, int(adopted)))
    except Exception:
        # The unique index refused a duplicate. That is the index doing its job
        # on a retry or a restart mid-tick, not an error worth surfacing.
        return None
    storage.log_event(
        "info",
        f"{_arm_label(arm)}: espelhou {live['symbol']} a {price:.6g}"
        f" ({spent:,.2f} USDT){' — posicao herdada' if adopted else ''}",
        {"arm": arm, "symbol": live["symbol"], "source_id": int(live["id"]),
         "spent": spent, "adopted": adopted}, source=SOURCE)
    return {"action": "open", "arm": arm, "symbol": live["symbol"],
            "price": price, "quote": spent, "adopted": adopted}


def _close(position: dict[str, Any], price: float, reason: str,
           when: str | None = None) -> dict[str, Any]:
    """Charge the same fee and slippage the backtester charges, on every arm.

    A target is a limit order and arguably suffers no slippage, while the
    rule's exit is a market order and certainly does. Charging them differently
    would be a thumb on the scale in favour of the targets, so both pay the
    same. The cost is the round trip the research assumed, which keeps these
    numbers comparable with the study that justified the live book.
    """
    fill = price * (1 - settings.fee_rate - settings.slippage_rate)
    proceeds = position["qty"] * fill
    pnl = proceeds - position["entry_quote"]
    change = (proceeds / position["entry_quote"] - 1) * 100
    storage.execute(
        "UPDATE mirror_positions SET status='closed', exit_price=?, exit_time=?,"
        " exit_quote=?, pnl=?, return_pct=?, reason=? WHERE id=?",
        (fill, when or _now(), proceeds, pnl, change, reason, position["id"]))
    storage.log_event(
        "info",
        f"{_arm_label(position['arm'])}: saiu de {position['symbol']} a {fill:.6g}"
        f" por {reason} — {pnl:+,.2f} USDT ({change:+.2f}%)",
        {"arm": position["arm"], "symbol": position["symbol"],
         "source_id": int(position["source_id"]), "pnl": pnl, "reason": reason},
        source=SOURCE)
    return {"action": "close", "arm": position["arm"], "symbol": position["symbol"],
            "price": fill, "pnl": pnl, "reason": reason}


# ------------------------------------------------------------------- the tick

def _target_hit(position: dict[str, Any],
                mark: float | None = None) -> tuple[float, str] | None:
    """Whether the target was reached, and when.

    A target is a resting limit order: it fills when the price trades through
    it, which is a fact about the candle's high and not about where the candle
    happened to close. Reading closes instead would miss every target that was
    hit and given back inside one bar, which is exactly the case this study
    exists to settle.

    Candles are also what makes an adopted position honest. A trade that has
    been open since yesterday may have passed its target hours ago, and the
    high that proves it is on record.

    Two sources, in time order. Completed bars come first, because a fill on
    one of them happened earlier than any fill happening now, and the earliest
    fill is the one a resting order would have got. The bar still forming is
    excluded - its high is not final - but the current price stands in for it:
    if the market is trading at or above the target right now, the order is
    filled now, and waiting for the bar to close would be pretending otherwise.

    The bar the position entered on is excluded too. Its high may well have
    been printed before the entry, and crediting a fill to a price that traded
    before the position existed would invent profit.
    """
    if position["target_price"] is None:
        return None
    target = float(position["target_price"])
    frame = research.load_history(position["symbol"], position["interval"], 400)
    entry = pd.Timestamp(position["entry_time"])
    if entry.tzinfo is None:
        entry = entry.tz_localize("UTC")
    closed = frame.iloc[:-1]
    times = pd.to_datetime(closed["time"], utc=True)
    after = closed[times > entry]
    reached = after[after["high"].astype(float) >= target] if not after.empty else after
    if not reached.empty:
        return target, str(reached["time"].iloc[0])
    if mark is not None and mark >= target:
        return target, _now()
    return None


def tick(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """One pass: mirror new entries, take targets, follow the rule's exits."""
    config = config or get_config()
    quote = float(config["quote_per_trade"])
    live_rows = _live_positions()
    live_by_id = {int(row["id"]): row for row in live_rows}
    every_arm = arms(config)

    mirrored: set[tuple[str, int]] = {
        (row["arm"], int(row["source_id"]))
        for row in storage.query("SELECT arm, source_id FROM mirror_positions")}

    actions: list[dict[str, Any]] = []

    # 1. New live entries, plus - once, on the first run - the ones that were
    #    already open. A live trade that opened and closed before this book
    #    existed is skipped: shadowing it would be backfilling a result, and a
    #    forward test that starts by filling in the past is not a forward test.
    started = storage.get_state("mirror_started_at")
    if not started:
        started = _now()
        storage.set_state("mirror_started_at", started)
        storage.log_event("info", "Estudo de saida iniciado",
                          {"arms": every_arm}, source=SOURCE)
    adopt = bool(config.get("adopt_open"))

    for live in live_rows:
        fresh = str(live["entry_time"]) >= started
        if not fresh and not (adopt and live["status"] == "open"):
            continue
        for arm in every_arm:
            if (arm, int(live["id"])) in mirrored:
                continue
            if len(open_positions(arm)) >= MAX_POSITIONS:
                continue
            action = _open(arm, live, quote, adopted=not fresh)
            if action:
                actions.append(action)

    # 2. Targets, checked against candle highs. Done before the rule's exits so
    #    a position whose target was hit earlier in the same bar the rule fired
    #    is credited to the target, which is the order the two orders would
    #    actually have filled in.
    holding = open_positions()
    marks = _marks([p["symbol"] for p in holding])
    for position in holding:
        hit = _target_hit(position, marks.get(position["symbol"]))
        if hit:
            price, when = hit
            actions.append(_close(position, price, "alvo", when))

    # 3. Whatever the live book has closed, close here too, at its price and
    #    its moment. This is the control arm's only exit, and the fallback for
    #    a target that never came.
    for position in open_positions():
        live = live_by_id.get(int(position["source_id"]))
        if not live or live["status"] != "closed" or live["exit_price"] is None:
            continue
        reason = live["reason"] or "saida"
        actions.append(_close(position, float(live["exit_price"]),
                              "regra: " + str(reason), str(live["exit_time"])))

    snapshot_equity(config)
    storage.set_state("mirror_last_tick", _now())
    return {"actions": actions, "arms": every_arm,
            "live_open": sum(1 for row in live_rows if row["status"] == "open")}


# ------------------------------------------------------------------ the money

def _marks(symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    try:
        return exchange.prices(sorted(set(symbols)))
    except BinanceError:
        return {}


def snapshot_equity(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or get_config()
    capital = float(config["capital"])
    positions = open_positions()
    marks = _marks([p["symbol"] for p in positions])
    now = _now()

    out: dict[str, Any] = {}
    for arm in arms(config):
        realised = storage.query_one(
            "SELECT COALESCE(SUM(pnl), 0) AS pnl FROM mirror_positions"
            " WHERE status='closed' AND arm=?", (arm,))["pnl"]
        held = [p for p in positions if p["arm"] == arm]
        unrealised = sum(p["qty"] * marks.get(p["symbol"], p["entry_price"])
                         - p["entry_quote"] for p in held)
        invested = sum(p["entry_quote"] for p in held)
        total = capital + realised + unrealised
        storage.execute(
            "INSERT INTO mirror_equity(arm, ts, total_value, positions_value,"
            " open_positions) VALUES(?,?,?,?,?)"
            " ON CONFLICT(arm, ts) DO UPDATE SET total_value = excluded.total_value",
            (arm, now, total, invested + unrealised, len(held)))
        out[arm] = {"capital": capital, "realised_pnl": realised,
                    "unrealised_pnl": unrealised, "invested": invested,
                    "total_value": total}
    return out


def equity_curve(arm: str, limit: int = 500) -> list[dict[str, Any]]:
    rows = storage.query(
        "SELECT ts, total_value, open_positions FROM mirror_equity"
        " WHERE arm=? ORDER BY ts DESC LIMIT ?", (arm, limit))
    return list(reversed(rows))


# ----------------------------------------------------------------- the report

def _stats(arm: str, capital: float, marks: dict[str, float]) -> dict[str, Any]:
    closed = storage.query(
        "SELECT * FROM mirror_positions WHERE status='closed' AND arm=?", (arm,))
    held = open_positions(arm)
    realised = sum(float(row["pnl"]) for row in closed)
    unrealised = sum(p["qty"] * marks.get(p["symbol"], p["entry_price"])
                     - p["entry_quote"] for p in held)
    wins = [row for row in closed if float(row["pnl"]) > 0]
    losses = [row for row in closed if float(row["pnl"]) <= 0]
    gains = sum(float(row["pnl"]) for row in wins)
    pains = -sum(float(row["pnl"]) for row in losses)
    # Trades this book chose, as opposed to inherited. Reported separately
    # because a study that started by adopting a winner would otherwise show
    # that winner as its own result.
    chosen = [row for row in closed if not row["adopted"]]
    target = target_of(arm)
    return {
        "arm": arm,
        "target_pct": target,
        "label": "regra decide" if arm == CONTROL else f"vende em +{target:g}%",
        "capital": capital,
        "realised_pnl": round(realised, 2),
        "unrealised_pnl": round(unrealised, 2),
        "total_pnl": round(realised + unrealised, 2),
        "total_value": round(capital + realised + unrealised, 2),
        "return_pct": round((realised + unrealised) / capital * 100, 3) if capital else 0.0,
        "closed_trades": len(closed),
        "open_positions": len(held),
        "invested": round(sum(p["entry_quote"] for p in held), 2),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 2) if closed else 0.0,
        "profit_factor": round(gains / pains, 3) if pains else None,
        "avg_trade_pct": round(sum(float(r["return_pct"]) for r in closed) / len(closed), 3)
                         if closed else 0.0,
        "hit_target": sum(1 for row in closed if row["reason"] == "alvo"),
        "adopted_trades": len(closed) - len(chosen),
        "chosen_trades": len(chosen),
        "chosen_pnl": round(sum(float(row["pnl"]) for row in chosen), 2),
    }


def overview() -> dict[str, Any]:
    """Every arm side by side, plus what can and cannot yet be concluded."""
    config = get_config()
    capital = float(config["capital"])
    positions = open_positions()
    marks = _marks([p["symbol"] for p in positions])
    rows = [_stats(arm, capital, marks) for arm in arms(config)]
    control = next((row for row in rows if row["arm"] == CONTROL), None)

    for row in rows:
        row["vs_rule_pct"] = (round(row["return_pct"] - control["return_pct"], 3)
                              if control else None)

    # The paired comparison, trade by trade. Two arms that shadow the same live
    # position differ only in the exit, so their difference is the effect being
    # measured - and pairing removes the variance from which coins happened to
    # be traded, which on a handful of trades is most of the variance there is.
    control_closed = {
        int(row["source_id"]): row for row in
        storage.query("SELECT * FROM mirror_positions WHERE status='closed' AND arm=?",
                      (CONTROL,))}
    pairs: dict[str, Any] = {}
    for arm in arms(config):
        if arm == CONTROL:
            continue
        both = [(float(row["return_pct"]),
                 float(control_closed[int(row["source_id"])]["return_pct"]))
                for row in storage.query(
                    "SELECT * FROM mirror_positions WHERE status='closed' AND arm=?",
                    (arm,))
                if int(row["source_id"]) in control_closed]
        if not both:
            pairs[arm] = {"trades": 0}
            continue
        deltas = [rule - target for target, rule in both]
        pairs[arm] = {
            "trades": len(both),
            "rule_minus_target_pp": round(sum(deltas) / len(deltas), 3),
            "rule_ahead": sum(1 for delta in deltas if delta > 0),
            "target_ahead": sum(1 for delta in deltas if delta < 0),
            "identical": sum(1 for delta in deltas if delta == 0),
        }

    closed_total = sum(row["closed_trades"] for row in rows)
    needed = 30 * len(rows)
    return {
        "enabled": bool(config.get("enabled")),
        "running": trader.running,
        "started_at": storage.get_state("mirror_started_at"),
        "last_tick": storage.get_state("mirror_last_tick"),
        "capital": capital,
        # Money actually deployed when every slot is full, reported beside the
        # capital for the same reason the other two books report it: a return
        # computed on capital is mostly a statement about idle cash.
        "capital_at_work": float(config["quote_per_trade"]) * MAX_POSITIONS,
        "quote_per_trade": float(config["quote_per_trade"]),
        "targets": list(config["targets"]),
        "arms": rows,
        "paired": pairs,
        # Said in the response rather than left for the reader to work out. A
        # handful of trades cannot separate these arms, and the number below is
        # the honest reason the panel should not be read as a verdict yet.
        "conclusive": closed_total >= needed,
        "closed_trades": closed_total,
        "trades_needed": needed,
        "note": ("Amostra pequena demais para concluir qualquer coisa."
                 if closed_total < needed else
                 "Amostra suficiente para uma primeira leitura."),
    }


# -------------------------------------------------------------------- the loop

class MirrorTrader:
    """Polls the live ledger. Never writes to it."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self.last_tick: str | None = None
        self.last_error: str | None = None
        self.tick_count = 0

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.running:
                return {"running": True, "message": "already running"}
            save_config({"enabled": True})
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            storage.log_event("info", "Estudo de saida iniciado", source=SOURCE)
            return {"running": True, "message": "started"}

    def stop(self) -> dict[str, Any]:
        save_config({"enabled": False})
        self._stop.set()
        storage.log_event("info", "Estudo de saida parado", source=SOURCE)
        return {"running": False, "message": "stopped"}

    def _loop(self) -> None:
        while not self._stop.is_set():
            config = get_config()
            try:
                self.safe_tick(config)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                storage.log_event("error", f"Tick do estudo de saida falhou: {exc}",
                                  {"trace": traceback.format_exc()[-600:]},
                                  source=SOURCE)
            self.last_tick = _now()
            self.tick_count += 1
            self._stop.wait(max(60, int(config.get("poll_seconds", POLL_SECONDS))))

    def safe_tick(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._tick_lock:
            return tick(config)


trader = MirrorTrader()


def reset() -> dict[str, Any]:
    """Wipe the study and start over. Touches nothing outside its own tables."""
    storage.execute("DELETE FROM mirror_positions")
    storage.execute("DELETE FROM mirror_equity")
    storage.set_state("mirror_started_at", None)
    storage.set_state("mirror_last_tick", None)
    storage.log_event("info", "Estudo de saida zerado", source=SOURCE)
    return {"reset": True}
