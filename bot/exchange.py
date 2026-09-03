"""Thin Binance Spot REST client.

Two bases on purpose:

* market data always comes from the public production endpoint, because the
  testnet only keeps a shallow, partly synthetic history and research on it
  would be worthless;
* account and order calls go to whatever base ``settings`` points at, which is
  the testnet unless the operator explicitly turns it off.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.parse
from decimal import Decimal
from typing import Any, NamedTuple

import httpx
import pandas as pd

from .config import settings

MARKET_BASE = "https://data-api.binance.vision"
KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
]
INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


class BinanceError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class Fill(NamedTuple):
    """One order's effect on the account, net of what the exchange took.

    ``price`` is the effective cost or proceeds per coin actually moved, which
    is what makes ``quote / qty`` and the stored entry price agree and keeps a
    live trade comparable to a backtested one - the backtest charges its fee
    into the fill price too.
    """
    price: float
    qty: float
    quote: float
    fee: float
    fee_asset: str | None


class Exchange:
    def __init__(self) -> None:
        self._client = httpx.Client(timeout=20.0)
        self._time_offset = 0
        self._offset_synced_at = 0.0
        self._filters: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- helpers

    def _sync_time(self, force: bool = False) -> None:
        """Binance rejects requests whose timestamp drifts from its clock."""
        if not force and time.time() - self._offset_synced_at < 300:
            return
        data = self._client.get(f"{settings.rest_base}/api/v3/time").json()
        self._time_offset = int(data["serverTime"]) - int(time.time() * 1000)
        self._offset_synced_at = time.time()

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self._time_offset

    def _signed(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if not settings.has_keys:
            raise BinanceError("No API keys configured. Fill in .env")
        with self._lock:
            self._sync_time()
            payload = dict(params or {})
            payload["timestamp"] = self._timestamp()
            payload["recvWindow"] = 10_000
            query = urllib.parse.urlencode(payload)
            signature = hmac.new(
                settings.api_secret.encode(), query.encode(), hashlib.sha256
            ).hexdigest()
            url = f"{settings.rest_base}{path}?{query}&signature={signature}"
            response = self._client.request(
                method, url, headers={"X-MBX-APIKEY": settings.api_key}
            )
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                raise BinanceError(response.text) from None
            # -1021 is clock drift; resync so the next call has a fresh offset.
            if body.get("code") == -1021:
                self._sync_time(force=True)
            raise BinanceError(body.get("msg", response.text), body.get("code"))
        return response.json()

    def _public(self, path: str, params: dict[str, Any], base: str | None = None) -> Any:
        response = self._client.get(f"{base or MARKET_BASE}{path}", params=params)
        if response.status_code >= 400:
            raise BinanceError(response.text)
        return response.json()

    # ------------------------------------------------------------ market data

    def klines(self, symbol: str, interval: str, limit: int = 1000,
               end_time: int | None = None) -> list[list[Any]]:
        params: dict[str, Any] = {
            "symbol": symbol, "interval": interval, "limit": min(limit, 1000),
        }
        if end_time is not None:
            params["endTime"] = end_time
        return self._public("/api/v3/klines", params)

    def history(self, symbol: str, interval: str, candles: int = 3000) -> pd.DataFrame:
        """Page backwards until ``candles`` bars are collected."""
        rows: list[list[Any]] = []
        end_time: int | None = None
        while len(rows) < candles:
            batch = self.klines(symbol, interval, 1000, end_time)
            if not batch:
                break
            rows = batch + rows
            end_time = int(batch[0][0]) - 1
            if len(batch) < 1000:
                break
        return self._to_frame(rows[-candles:])

    @staticmethod
    def _to_frame(rows: list[list[Any]]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        frame = pd.DataFrame(rows, columns=KLINE_COLUMNS)
        numeric = ["open", "high", "low", "close", "volume"]
        frame[numeric] = frame[numeric].astype(float)
        frame["time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
        return frame[["time", *numeric]].drop_duplicates("time").reset_index(drop=True)

    def price(self, symbol: str) -> float:
        return float(self._public("/api/v3/ticker/price", {"symbol": symbol})["price"])

    def prices(self, symbols: list[str]) -> dict[str, float]:
        params = {"symbols": json.dumps(symbols, separators=(",", ":"))}
        data = self._public("/api/v3/ticker/price", params)
        return {row["symbol"]: float(row["price"]) for row in data}

    # ------------------------------------------------------------ trading

    def symbol_filters(self, symbol: str) -> dict[str, float]:
        """LOT_SIZE / NOTIONAL rules, read from the *trading* base, not market."""
        if symbol in self._filters:
            return self._filters[symbol]
        info = self._public(
            "/api/v3/exchangeInfo", {"symbol": symbol}, base=settings.rest_base
        )
        entry = info["symbols"][0]
        rules = {"step": 0.0, "min_qty": 0.0, "min_notional": 0.0, "tick": 0.0}
        for spec in entry["filters"]:
            kind = spec["filterType"]
            if kind == "LOT_SIZE":
                rules["step"] = float(spec["stepSize"])
                rules["min_qty"] = float(spec["minQty"])
            elif kind in ("NOTIONAL", "MIN_NOTIONAL"):
                rules["min_notional"] = float(spec.get("minNotional") or 0)
            elif kind == "PRICE_FILTER":
                rules["tick"] = float(spec["tickSize"])
        self._filters[symbol] = rules
        return rules

    def round_qty(self, symbol: str, qty: float) -> float:
        """Floor to the symbol's lot step, exactly, without float drift."""
        step = self.symbol_filters(symbol)["step"]
        if step <= 0:
            return qty
        step_dec = Decimal(str(step))
        floored = (Decimal(str(qty)) // step_dec) * step_dec
        return float(floored)

    def format_qty(self, symbol: str, qty: float) -> str:
        step = self.symbol_filters(symbol)["step"]
        decimals = max(0, -Decimal(str(step)).normalize().as_tuple().exponent) if step else 8
        return f"{qty:.{decimals}f}"

    def account(self) -> dict[str, Any]:
        return self._signed("GET", "/api/v3/account")

    def balances(self) -> dict[str, float]:
        return {
            entry["asset"]: float(entry["free"]) + float(entry["locked"])
            for entry in self.account()["balances"]
            if float(entry["free"]) + float(entry["locked"]) > 0
        }

    def free_balance(self, asset: str) -> float:
        for entry in self.account()["balances"]:
            if entry["asset"] == asset:
                return float(entry["free"])
        return 0.0

    def market_order(self, symbol: str, side: str, quantity: float) -> dict[str, Any]:
        return self._signed("POST", "/api/v3/order", {
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": self.format_qty(symbol, quantity),
        })

    @staticmethod
    def fill_summary(order: dict[str, Any]) -> Fill:
        """What the account actually gained and lost on one order.

        ``executedQty`` and ``cummulativeQuoteQty`` are both gross: they are
        what the trade matched, before the exchange takes its cut. Binance
        charges a spot buy in the coin bought and a spot sell in the quote
        asset, so a gross reading overstates the coins held after a buy and the
        cash received after a sell - by about a tenth of a percent each way,
        which is a fifth of a percent per round trip and is precisely the size
        of the edge these strategies are being judged on.

        The Spot Testnet charges nothing, so a gross reading is exactly right
        there and stays exactly right for as long as the bot is only ever run
        against the testnet. That is what makes this worth doing before the
        switch rather than after: the defect is invisible in every number the
        forward test will produce, and appears for the first time on the
        account where it costs money.

        Commission in a third asset (BNB, when the account is set up to pay
        that way) cannot honestly be folded into either side, so it is reported
        separately rather than silently dropped or wrongly netted.
        """
        qty = float(order.get("executedQty") or 0)
        quote = float(order.get("cummulativeQuoteQty") or 0)
        symbol = str(order.get("symbol") or "")
        side = str(order.get("side") or "BUY").upper()
        base = (symbol[:-len(settings.quote_asset)]
                if symbol.endswith(settings.quote_asset) else "")

        base_fee = quote_fee = other_fee = 0.0
        other_asset = None
        for fill in order.get("fills") or []:
            amount = float(fill.get("commission") or 0)
            if not amount:
                continue
            asset = fill.get("commissionAsset")
            if asset and asset == base:
                base_fee += amount
            elif asset == settings.quote_asset:
                quote_fee += amount
            else:
                other_fee += amount
                other_asset = asset

        # Coins taken as commission never arrive, so they were never held.
        qty = max(qty - base_fee, 0.0)
        # A buy is debited the trade plus its fee; a sell is credited the trade
        # minus its fee.
        quote = quote + quote_fee if side == "BUY" else max(quote - quote_fee, 0.0)

        fee_asset = None
        fee = 0.0
        if base_fee:
            fee, fee_asset = base_fee, base
        if quote_fee:
            fee, fee_asset = quote_fee, settings.quote_asset
        if other_fee:
            fee, fee_asset = other_fee, other_asset

        return Fill(price=quote / qty if qty else 0.0, qty=qty, quote=quote,
                    fee=fee, fee_asset=fee_asset)

    def ping(self) -> dict[str, Any]:
        """Connectivity + credential check used by the dashboard header."""
        status: dict[str, Any] = {
            "market_data": False, "account": False, "testnet": settings.testnet,
        }
        try:
            self._public("/api/v3/ping", {})
            status["market_data"] = True
        except Exception as exc:
            status["market_error"] = str(exc)[:200]
        try:
            account = self.account()
            status["account"] = True
            status["can_trade"] = account.get("canTrade", False)
            status["quote_balance"] = next(
                (float(b["free"]) for b in account["balances"]
                 if b["asset"] == settings.quote_asset), 0.0,
            )
        except Exception as exc:
            status["account_error"] = str(exc)[:200]
        return status


exchange = Exchange()
