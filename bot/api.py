"""FastAPI application: JSON API plus the static dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import backtest as bt
from . import report, research, storage
from . import portfolio, screening, walkforward
from . import strategies as st
from .config import WEB_DIR, settings
from .exchange import BinanceError, exchange
from .live import bot, get_config, save_config


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # A research thread dies with the process, so never leave a run 'running'.
    storage.execute(
        "UPDATE research_runs SET status = 'interrupted', finished_at = ? "
        "WHERE status = 'running'", (storage.now(),)
    )
    config = get_config()
    if config.get("enabled") and config.get("allocations"):
        bot.start()
        storage.log_event("info", "Bot resumed after restart")
    yield
    bot.stop()


app = FastAPI(title="Trader Bot", version="1.0.0", docs_url="/api/docs", lifespan=lifespan)


@app.exception_handler(BinanceError)
async def binance_error_handler(_request, exc: BinanceError):
    return JSONResponse(status_code=502, content={"detail": str(exc), "code": exc.code})


# ------------------------------------------------------------------- schemas

class ResearchRequest(BaseModel):
    symbols: list[str] | None = None
    intervals: list[str] | None = None
    candles: int = Field(default=research.DEFAULT_CANDLES, ge=500, le=10_000)
    strategies: list[str] | None = None


class BacktestRequest(BaseModel):
    symbol: str = "BTCUSDT"
    interval: str = "1h"
    strategy: str = "ema_cross"
    params: dict[str, Any] | None = None
    risk: dict[str, float] | None = None
    candles: int = Field(default=2000, ge=200, le=10_000)


class AllocationRequest(BaseModel):
    result_ids: list[int] | None = None
    allocations: list[dict[str, Any]] | None = None
    quote_per_trade: float | None = None


class RiskRequest(BaseModel):
    max_drawdown_pct: float = Field(0.0, ge=0, le=90)
    resume_drawdown_pct: float = Field(0.0, ge=0, le=90)
    volatility_sizing: bool = False
    max_correlation: float = Field(0.0, ge=0, le=1)


class ConfigRequest(BaseModel):
    mode: str | None = None
    poll_seconds: int | None = Field(default=None, ge=10, le=3600)
    max_positions: int | None = Field(default=None, ge=1, le=20)
    quote_per_trade: float | None = Field(default=None, gt=0)
    start_capital: float | None = Field(default=None, gt=0)


# -------------------------------------------------------------------- status

@app.get("/api/status")
def status() -> dict[str, Any]:
    return {
        "exchange": exchange.ping(),
        "bot": bot.status(),
        "settings": {
            "testnet": settings.testnet,
            "quote_asset": settings.quote_asset,
            "fee_rate": settings.fee_rate,
            "slippage_rate": settings.slippage_rate,
            "symbols": settings.symbols,
            "intervals": settings.intervals,
        },
        "research": research.run_status(),
    }


@app.get("/api/overview")
def overview() -> dict[str, Any]:
    return report.overview()


@app.get("/api/equity")
def equity(limit: int = 500) -> list[dict[str, Any]]:
    return report.equity_curve(limit)


@app.get("/api/trades")
def trades(limit: int = 100) -> list[dict[str, Any]]:
    return report.trades(limit)


@app.get("/api/orders")
def orders(limit: int = 200) -> dict[str, Any]:
    """Raw buy/sell ledger, with the running cash totals underneath it."""
    rows = report.orders(limit)
    return {"orders": rows, "totals": report.ledger_totals(rows)}


@app.get("/api/trades/history")
def trade_history(bars: int = report.HISTORY_BARS) -> list[dict[str, Any]]:
    """Simulated trade-by-trade history of the allocations currently running."""
    return report.allocation_history(bars)


@app.get("/api/risk")
def risk() -> dict[str, Any]:
    """What the portfolio-level controls are set to, and what they are doing."""
    config = get_config()
    symbols = sorted({p["symbol"] for p in bot.open_positions()})
    return portfolio.state(config, symbols)


@app.post("/api/risk")
def update_risk(request: RiskRequest) -> dict[str, Any]:
    config = save_config({"risk_controls": request.model_dump()})
    return portfolio.state(config)


@app.get("/api/validation")
def validation(refresh: bool = False) -> dict[str, Any]:
    """Walk-forward verdict on every allocation, on its deployed parameters."""
    return walkforward.validation_state(get_config()["allocations"], refresh=refresh)


@app.get("/api/readiness")
def readiness() -> dict[str, Any]:
    """Gates that decide whether this book has earned a real-money account."""
    return report.readiness()


@app.get("/api/screen")
def screen(symbols: str, interval: str = "1d",
           candles: int = screening.SCREEN_CANDLES) -> list[dict[str, Any]]:
    """Describe the price shape of symbols. Descriptive only - see screening.py."""
    wanted = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    if not wanted:
        raise HTTPException(status_code=400, detail="no symbols given")
    return screening.screen(wanted[:40], interval, candles)


@app.get("/api/breakdown")
def breakdown() -> dict[str, Any]:
    return report.breakdown()


@app.get("/api/events")
def events(limit: int = 60) -> list[dict[str, Any]]:
    return storage.recent_events(limit)


@app.get("/api/strategies")
def strategies() -> list[dict[str, Any]]:
    return st.catalog()


# ------------------------------------------------------------------ research

@app.post("/api/research/start")
def research_start(request: ResearchRequest) -> dict[str, Any]:
    return research.start_run(
        request.symbols, request.intervals, request.candles, request.strategies
    )


@app.get("/api/research/status")
def research_status(run_id: int | None = None) -> dict[str, Any] | None:
    return research.run_status(run_id)


@app.get("/api/research/leaderboard")
def leaderboard(run_id: int | None = None, limit: int = 40,
                only_validated: bool = False) -> list[dict[str, Any]]:
    return research.leaderboard(run_id, limit, only_validated)


@app.get("/api/research/result/{result_id}")
def research_result(result_id: int) -> dict[str, Any]:
    result = research.result_by_id(result_id)
    if not result:
        raise HTTPException(404, "result not found")
    return result


@app.get("/api/research/runs")
def research_runs(limit: int = 20) -> list[dict[str, Any]]:
    return storage.query(
        "SELECT id, created_at, finished_at, status, progress, total, stage "
        "FROM research_runs ORDER BY id DESC LIMIT ?", (limit,)
    )


@app.post("/api/backtest")
def run_backtest(request: BacktestRequest) -> dict[str, Any]:
    if request.strategy not in st.REGISTRY:
        raise HTTPException(400, f"unknown strategy: {request.strategy}")
    frame = research.load_history(request.symbol, request.interval, request.candles)
    strategy = st.build(request.strategy, request.params)
    risk = request.risk or {}
    result = research.evaluate(frame, strategy, risk)
    benchmark = bt.run(frame, st.build("buy_hold").signal(frame))
    return {
        "symbol": request.symbol,
        "interval": request.interval,
        "strategy": strategy.describe(),
        "risk": risk,
        "metrics": result.metrics,
        "score": bt.robust_score(result.metrics),
        "curve": result.curve(),
        "benchmark_curve": benchmark.curve(),
        "trades": result.trades[-100:],
    }


# ----------------------------------------------------------------------- bot

@app.get("/api/bot/config")
def bot_config() -> dict[str, Any]:
    return get_config()


@app.post("/api/bot/config")
def update_config(request: ConfigRequest) -> dict[str, Any]:
    patch = {k: v for k, v in request.model_dump().items() if v is not None}
    if patch.get("mode") not in (None, "testnet", "paper"):
        raise HTTPException(400, "mode must be 'testnet' or 'paper'")
    return save_config(patch)


@app.post("/api/bot/allocations")
def set_allocations(request: AllocationRequest) -> dict[str, Any]:
    allocations: list[dict[str, Any]] = list(request.allocations or [])
    for result_id in request.result_ids or []:
        result = research.result_by_id(result_id)
        if not result:
            raise HTTPException(404, f"result {result_id} not found")
        allocations.append({
            "symbol": result["symbol"],
            "interval": result["interval"],
            "strategy": result["strategy"],
            "label": result["label"],
            "params": result["params"],
            "risk": result["risk"],
            "source_result_id": result_id,
        })
    # One live allocation per symbol: two strategies on the same asset would
    # fight over the same spot balance.
    unique: dict[str, dict[str, Any]] = {}
    for allocation in allocations:
        unique.setdefault(allocation["symbol"], allocation)
    patch: dict[str, Any] = {"allocations": list(unique.values())}
    if request.quote_per_trade:
        patch["quote_per_trade"] = request.quote_per_trade
    config = save_config(patch)
    storage.log_event("info", f"Allocations set: {len(config['allocations'])} strategies")
    return config


@app.post("/api/bot/start")
def bot_start() -> dict[str, Any]:
    return bot.start()


@app.post("/api/bot/stop")
def bot_stop() -> dict[str, Any]:
    return bot.stop()


@app.post("/api/bot/tick")
def bot_tick() -> dict[str, Any]:
    return bot.tick()


@app.post("/api/bot/close-all")
def bot_close_all() -> dict[str, Any]:
    return {"closed": bot.close_all("manual")}


@app.post("/api/bot/reset")
def bot_reset() -> dict[str, Any]:
    """Wipe trading history. Open positions are left untouched on the exchange."""
    bot.stop()
    for table in ("positions", "orders", "equity_snapshots", "events"):
        storage.execute(f"DELETE FROM {table}")
    storage.set_state("position_peaks", {})
    storage.set_state("risk_halted", False)
    # Equity history is gone, so the kill switch and the post-stop stand-aside
    # flags have nothing left to refer to.
    for allocation in (get_config().get("allocations") or []):
        storage.set_state(f"standaside:{allocation['symbol']}", False)
    storage.log_event("info", "Trading history reset")
    return {"reset": True}


# -------------------------------------------------------------------- static

app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
