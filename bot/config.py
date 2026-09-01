"""Runtime configuration, loaded once from the .env file at the project root."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
DB_PATH = DATA_DIR / "trader.db"

load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except ValueError:
        return default


@dataclass
class Settings:
    api_key: str = os.getenv("BINANCE_API_KEY", "")
    api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    testnet: bool = _bool("BINANCE_TESTNET", True)

    quote_asset: str = os.getenv("QUOTE_ASSET", "USDT")
    trade_quote_amount: float = _float("TRADE_QUOTE_AMOUNT", 200.0)
    max_open_positions: int = _int("MAX_OPEN_POSITIONS", 3)

    # Binance spot taker fee. Applied on both sides of every backtested trade.
    fee_rate: float = _float("FEE_RATE", 0.001)
    # Extra cost assumption per side, on top of the fee, to keep backtests honest.
    slippage_rate: float = _float("SLIPPAGE_RATE", 0.0005)

    # Universe the research engine scans by default.
    symbols: list[str] = field(
        default_factory=lambda: [
            "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
        ]
    )
    intervals: list[str] = field(default_factory=lambda: ["15m", "1h", "4h"])

    @property
    def rest_base(self) -> str:
        return (
            "https://testnet.binance.vision"
            if self.testnet
            else "https://api.binance.com"
        )

    @property
    def has_keys(self) -> bool:
        return bool(self.api_key and self.api_secret)


settings = Settings()
DATA_DIR.mkdir(exist_ok=True)
