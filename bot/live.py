"""Live trading engine.

Runs the strategies that survived research against the live market, one worker
thread for the whole bot. Two execution modes:

``testnet``  real signed orders on Binance Spot Testnet (fake money, real API);
``paper``    fills simulated at the last trade price, nothing sent anywhere.

Signals are always read from the **last closed candle**. Acting on the candle
still forming is the classic way to backtest a profit that live trading never
sees, so the forming bar is dropped before the signal is taken.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from . import portfolio, storage
from . import strategies as st
from .config import settings
from .exchange import BinanceError, exchange

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "mode": "testnet",
    "poll_seconds": 60,
    "max_positions": 3,
    "quote_per_trade": 200.0,
    "start_capital": 10_000.0,
    "allocations": [],
    # Portfolio-level controls, all off until switched on. See bot/portfolio.py
    # for why risk is managed here rather than with per-trade stops.
    "risk_controls": dict(portfolio.DEFAULTS),
}

# How many candles may pass between the signal turning long and the bot buying.
# The backtest decides on a closed candle and fills at the next open, so a bot
# that is awake sees a lag of zero. Anything larger means it was asleep while
# the move started, and the entry it is about to take is not the entry that was
# measured: joining a run in progress costs roughly half a point of expected
# return per candle, and the whole run is priced in by then.
MAX_ENTRY_LAG_BARS = 1


def get_config() -> dict[str, Any]:
    return {**DEFAULT_CONFIG, **(storage.get_state("bot_config") or {})}


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    config = {**get_config(), **patch}
    storage.set_state("bot_config", config)
    return config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TraderBot:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # Serialises ticks: the poll thread and a manual /api/bot/tick would
        # otherwise both read 'no position' and open the symbol twice.
        self._tick_lock = threading.Lock()
        self.last_tick: str | None = None
        self.last_error: str | None = None
        self.tick_count = 0

    # ------------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.running:
                return {"running": True, "message": "already running"}
            config = get_config()
            if not config["allocations"]:
                return {"running": False, "message": "no strategies allocated"}
            save_config({"enabled": True})
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            storage.log_event("info", "Bot started", {"mode": config["mode"]})
            return {"running": True, "message": "started"}

    def stop(self) -> dict[str, Any]:
        save_config({"enabled": False})
        self._stop.set()
        storage.log_event("info", "Bot stopped")
        return {"running": False, "message": "stopped"}

    # ------------------------------------------------------------------ loop

    def _loop(self) -> None:
        while not self._stop.is_set():
            config = get_config()
            try:
                self.tick(config)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                storage.log_event("error", f"Tick failed: {exc}",
                                  {"trace": traceback.format_exc()[-600:]})
            self.last_tick = _now()
            self.tick_count += 1
            self._stop.wait(max(10, int(config.get("poll_seconds", 60))))

    def tick(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._tick_lock:
            return self._tick(config)

    def _tick(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        config = config or get_config()
        actions: list[dict[str, Any]] = []
        open_positions = {p["symbol"]: p for p in self.open_positions()}
        # Evaluated once per tick: it is a statement about the whole book, and
        # exits are never blocked by it - only new entries.
        halted = portfolio.drawdown_state(config)["halted"]

        for allocation in config.get("allocations", []):
            symbol = allocation["symbol"]
            try:
                action = self._evaluate(allocation, config, open_positions, halted)
            except BinanceError as exc:
                storage.log_event("error", f"{symbol}: {exc}")
                continue
            if action:
                actions.append(action)
                open_positions = {p["symbol"]: p for p in self.open_positions()}

        self.snapshot_equity(config)
        return {"actions": actions, "checked": len(config.get("allocations", [])),
                "halted": halted}

    def _evaluate(self, allocation: dict[str, Any], config: dict[str, Any],
                  open_positions: dict[str, dict[str, Any]],
                  halted: bool = False) -> dict[str, Any] | None:
        symbol = allocation["symbol"]
        interval = allocation.get("interval", "1h")
        strategy = st.build(allocation["strategy"], allocation.get("params"))
        risk = allocation.get("risk") or {}

        frame = exchange.history(symbol, interval, 400)
        if len(frame) < 60:
            return None
        # Drop the candle that is still forming.
        closed = frame.iloc[:-1]
        signal = strategy.signal(closed)
        want_long = bool(signal.iloc[-1] > 0)
        price = float(exchange.price(symbol))
        position = open_positions.get(symbol)

        if position:
            exit_reason = None
            if not want_long:
                exit_reason = "signal"
            else:
                exit_reason = self._risk_exit(position, price, risk)
            if exit_reason:
                context = self._context(
                    strategy, closed, "exit", price,
                    self._exit_rule(exit_reason, strategy, risk, position), signal)
                if exit_reason != "signal":
                    # A stop fires while the strategy still wants to be long, so
                    # without this the next tick buys straight back in - selling
                    # at the stop and repurchasing above it, over and over, for
                    # the cost of two fees each round. Stay out until the signal
                    # itself drops and turns long again.
                    storage.set_state(self._standaside_key(symbol), True)
                return self._sell(position, price, exit_reason, config, context)
            self._track_peak(position, price)
            return None

        key = self._standaside_key(symbol)
        if storage.get_state(key) is None:
            # First tick for this allocation. A strategy holds its position
            # between entry and exit pulses, so a signal that is already long
            # here belongs to a move that started days ago - and the backtest
            # that justified the allocation never bought at that point, it
            # bought at the transition. Entering now is an unmeasured trade at a
            # worse price, so wait for the next clean turn instead. The cost is
            # sitting out the tail of the current run.
            storage.set_state(key, bool(want_long))

        if not want_long:
            storage.set_state(key, False)

        if want_long:
            if storage.get_state(key):
                return None
            lag = self._bars_since_turn(signal)
            if lag > MAX_ENTRY_LAG_BARS:
                # The machine was off while this signal started. Take the miss
                # rather than the bad fill, and record it so the trade log can
                # tell "the strategy never fired" apart from "the strategy
                # fired while nobody was listening" - the two look identical
                # afterwards and mean opposite things.
                storage.set_state(key, True)
                storage.log_event(
                    "warning",
                    f"{symbol}: entrada perdida, sinal virou há {lag} velas"
                    f" de {interval} e o robô estava fora",
                    {"symbol": symbol, "interval": interval, "bars_late": lag,
                     "strategy": allocation["strategy"], "kind": "missed_entry"})
                return None
            if len(open_positions) >= int(config.get("max_positions", 3)):
                return None
            if halted:
                return None
            blocked = portfolio.correlation_block(
                symbol, interval, list(open_positions), config)
            if blocked:
                storage.log_event(
                    "risk",
                    f"{symbol} entry skipped: {blocked['correlation']:.2f} correlated"
                    f" with open {blocked['blocked_by']}", blocked)
                return None
            context = self._context(strategy, closed, "entry", price,
                                    strategy.entry_rule, signal)
            return self._buy(allocation, price, config, context)
        return None

    @staticmethod
    def _bars_since_turn(signal: Any) -> int:
        """Candles between the signal turning long and the latest closed one."""
        wanted = signal.to_numpy(dtype=float) > 0
        index = len(wanted) - 1
        while index > 0 and wanted[index] and wanted[index - 1]:
            index -= 1
        return len(wanted) - 1 - index

    # ----------------------------------------------------------- explanation

    @staticmethod
    def _context(strategy: st.Strategy, closed: Any, kind: str, price: float,
                 rule: str, signal: Any = None) -> dict[str, Any]:
        """Freeze why the bot acted, so the trade log can say it later.

        Taken from the same closed frame the signal was read from, so the
        numbers shown are exactly the ones that triggered the order.

        The bar reported is the one the rule fired on, which is not always the
        latest one. Strategies hold a position between their entry and exit
        pulses, so an allocation that is added while its signal is already long
        buys bars after the breakout that justified it. Reporting the latest bar
        there would print a rule next to numbers that do not satisfy it.
        """
        trigger = closed
        bars_since = 0
        if signal is not None and len(signal) == len(closed):
            wanted = signal.to_numpy(dtype=float) > 0
            if kind != "entry":
                wanted = ~wanted
            index = len(wanted) - 1
            while index > 0 and wanted[index] and wanted[index - 1]:
                index -= 1
            bars_since = len(wanted) - 1 - index
            if bars_since:
                trigger = closed.iloc[: index + 1]
        info = strategy.explain(trigger)
        context = {
            "kind": kind,
            "rule": rule,
            "strategy": info["strategy"],
            "params": info["params"],
            "bar_time": str(trigger["time"].iloc[-1]),
            "bar_close": round(float(trigger["close"].iloc[-1]), 8),
            "market_price": round(price, 8),
            "values": info["values"],
        }
        if bars_since:
            context["bars_since_trigger"] = bars_since
        return context

    @staticmethod
    def _exit_rule(reason: str, strategy: st.Strategy, risk: dict[str, Any],
                   position: dict[str, Any]) -> str:
        """Plain-language reason for an exit, protective exits included."""
        if reason == "signal":
            return strategy.exit_rule
        if reason == "stop":
            return f"price fell {risk.get('stop_pct', 0):.1%} below the entry price"
        if reason == "target":
            return f"price reached the +{risk.get('take_pct', 0):.1%} profit target"
        if reason == "trailing stop":
            peak = position.get("peak_price") or position["entry_price"]
            return (f"price fell {risk.get('trail_pct', 0):.1%} from the "
                    f"{peak:.6g} peak reached since entry")
        if reason == "manual":
            return "closed by hand from the dashboard"
        return reason

    # -------------------------------------------------------------- risk

    @staticmethod
    def _standaside_key(symbol: str) -> str:
        return f"standaside:{symbol}"

    @staticmethod
    def _risk_exit(position: dict[str, Any], price: float, risk: dict[str, Any]) -> str | None:
        entry = position["entry_price"]
        peak = max(position.get("peak_price") or entry, price)
        if risk.get("stop_pct") and price <= entry * (1 - risk["stop_pct"]):
            return "stop"
        if risk.get("take_pct") and price >= entry * (1 + risk["take_pct"]):
            return "target"
        if risk.get("trail_pct") and price <= peak * (1 - risk["trail_pct"]):
            return "trailing stop"
        return None

    @staticmethod
    def _track_peak(position: dict[str, Any], price: float) -> None:
        peak = position.get("peak_price") or position["entry_price"]
        if price > peak:
            state = storage.get_state("position_peaks", {}) or {}
            state[str(position["id"])] = price
            storage.set_state("position_peaks", state)

    # ------------------------------------------------------------ execution

    def _buy(self, allocation: dict[str, Any], price: float, config: dict[str, Any],
             context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        symbol = allocation["symbol"]
        quote = float(allocation.get("quote_amount") or config.get("quote_per_trade", 200.0))
        quote, sizing = portfolio.size_for(
            symbol, allocation.get("interval", "1h"), quote, config)
        if sizing.get("scaled"):
            context = {**(context or {}), "sizing": sizing}
        rules = exchange.symbol_filters(symbol)
        if quote < rules["min_notional"]:
            quote = rules["min_notional"] * 1.05
        qty = exchange.round_qty(symbol, quote / price)
        if qty <= 0 or qty < rules["min_qty"]:
            storage.log_event("warn", f"{symbol}: order size below exchange minimum")
            return None

        if config.get("mode") == "paper":
            # Charge the same fee + slippage the backtester assumes, so paper
            # results stay comparable to research results.
            fill_price = price * (1 + settings.fee_rate + settings.slippage_rate)
            filled, spent = qty, qty * fill_price
            order_id = None
            status = "PAPER"
        else:
            order = exchange.market_order(symbol, "BUY", qty)
            fill_price, filled, spent = exchange.fill_summary(order)
            order_id = str(order.get("orderId"))
            status = order.get("status", "FILLED")
            if filled <= 0:
                storage.log_event("error", f"{symbol}: buy did not fill", order)
                return None

        order_row = storage.execute(
            "INSERT INTO orders(ts, symbol, side, qty, price, quote, order_id, status, strategy, note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (_now(), symbol, "BUY", filled, fill_price, spent, order_id, status,
             allocation["strategy"], "entry"),
        ).lastrowid
        cursor = storage.execute(
            "INSERT INTO positions(symbol, interval, strategy, params, risk, status, qty, "
            "entry_price, entry_time, entry_quote, mode, entry_context) "
            "VALUES(?,?,?,?,?,'open',?,?,?,?,?,?)",
            (symbol, allocation.get("interval", "1h"), allocation["strategy"],
             json.dumps(allocation.get("params") or {}),
             json.dumps(allocation.get("risk") or {}),
             filled, fill_price, _now(), spent, config.get("mode", "testnet"),
             json.dumps(context) if context else None),
        )
        # The order is written before the position exists, so the link back is
        # set here. Without it a sale in the order book cannot say what it made.
        storage.execute("UPDATE orders SET position_id=? WHERE id=?",
                        (cursor.lastrowid, order_row))
        storage.log_event("trade", f"BUY {symbol} {filled:g} @ {fill_price:.6g}",
                          {"strategy": allocation["strategy"], "quote": round(spent, 2)})
        return {"action": "buy", "symbol": symbol, "qty": filled, "price": fill_price,
                "position_id": cursor.lastrowid}

    def _sell(self, position: dict[str, Any], price: float, reason: str,
              config: dict[str, Any],
              context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        symbol = position["symbol"]
        qty = exchange.round_qty(symbol, position["qty"])

        if position["mode"] == "paper" or config.get("mode") == "paper":
            fill_price = price * (1 - settings.fee_rate - settings.slippage_rate)
            filled, proceeds = position["qty"], position["qty"] * fill_price
            order_id, status = None, "PAPER"
        else:
            available = exchange.free_balance(symbol.replace(settings.quote_asset, ""))
            qty = exchange.round_qty(symbol, min(qty, available))
            if qty <= 0:
                storage.log_event("error", f"{symbol}: nothing to sell, closing position as stale")
                self._close_row(position, price, 0.0, "stale", context)
                return None
            order = exchange.market_order(symbol, "SELL", qty)
            fill_price, filled, proceeds = exchange.fill_summary(order)
            order_id, status = str(order.get("orderId")), order.get("status", "FILLED")

        storage.execute(
            "INSERT INTO orders(ts, symbol, side, qty, price, quote, order_id, status, strategy,"
            " note, position_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), symbol, "SELL", filled, fill_price, proceeds, order_id, status,
             position["strategy"], reason, position["id"]),
        )
        self._close_row(position, fill_price, proceeds, reason, context)
        pnl = proceeds - position["entry_quote"]
        storage.log_event("trade", f"SELL {symbol} {filled:g} @ {fill_price:.6g} ({reason})",
                          {"pnl": round(pnl, 2), "strategy": position["strategy"]})
        return {"action": "sell", "symbol": symbol, "qty": filled, "price": fill_price,
                "pnl": pnl, "reason": reason}

    @staticmethod
    def _close_row(position: dict[str, Any], price: float, proceeds: float, reason: str,
                   context: dict[str, Any] | None = None) -> None:
        pnl = proceeds - position["entry_quote"]
        return_pct = (proceeds / position["entry_quote"] - 1) * 100 if position["entry_quote"] else 0.0
        storage.execute(
            "UPDATE positions SET status='closed', exit_price=?, exit_time=?, exit_quote=?, "
            "pnl=?, return_pct=?, reason=?, exit_context=? WHERE id=?",
            (price, _now(), proceeds, pnl, return_pct, reason,
             json.dumps(context) if context else None, position["id"]),
        )
        peaks = storage.get_state("position_peaks", {}) or {}
        peaks.pop(str(position["id"]), None)
        storage.set_state("position_peaks", peaks)

    def close_all(self, reason: str = "manual") -> list[dict[str, Any]]:
        config = get_config()
        closed = []
        for position in self.open_positions():
            price = float(exchange.price(position["symbol"]))
            context = {"kind": "exit", "rule": self._exit_rule(reason, st.build(
                position["strategy"], position["params"]), position["risk"], position),
                "strategy": position["strategy"], "params": position["params"],
                "market_price": round(price, 8), "values": {}}
            result = self._sell(position, price, reason, config, context)
            if result:
                closed.append(result)
        return closed

    # -------------------------------------------------------------- reporting

    @staticmethod
    def open_positions() -> list[dict[str, Any]]:
        rows = storage.query("SELECT * FROM positions WHERE status = 'open' ORDER BY id")
        peaks = storage.get_state("position_peaks", {}) or {}
        for row in rows:
            row["params"] = json.loads(row["params"])
            row["risk"] = json.loads(row["risk"])
            row["entry_context"] = json.loads(row["entry_context"] or "null")
            row["peak_price"] = peaks.get(str(row["id"]))
        return rows

    def snapshot_equity(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        config = config or get_config()
        realised = storage.query_one(
            "SELECT COALESCE(SUM(pnl), 0) AS pnl FROM positions WHERE status = 'closed'"
        )["pnl"]
        positions = self.open_positions()
        unrealised, invested = 0.0, 0.0
        if positions:
            prices = exchange.prices(sorted({p["symbol"] for p in positions}))
            for position in positions:
                mark = prices.get(position["symbol"], position["entry_price"])
                unrealised += position["qty"] * mark - position["entry_quote"]
                invested += position["entry_quote"]

        start = float(config.get("start_capital", 10_000.0))
        total = start + realised + unrealised
        storage.execute(
            "INSERT INTO equity_snapshots(ts, total_value, free_quote, positions_value, open_positions)"
            " VALUES(?,?,?,?,?) ON CONFLICT(ts) DO UPDATE SET total_value = excluded.total_value",
            (_now(), total, total - invested - unrealised, invested + unrealised, len(positions)),
        )
        return {
            "start_capital": start,
            "realised_pnl": realised,
            "unrealised_pnl": unrealised,
            "invested": invested,
            "total_value": total,
        }

    def status(self) -> dict[str, Any]:
        config = get_config()
        return {
            "running": self.running,
            "mode": config.get("mode"),
            "enabled": config.get("enabled"),
            "poll_seconds": config.get("poll_seconds"),
            "allocations": config.get("allocations", []),
            "max_positions": config.get("max_positions"),
            "last_tick": self.last_tick,
            "tick_count": self.tick_count,
            "last_error": self.last_error,
        }


bot = TraderBot()
