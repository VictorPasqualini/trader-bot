import pandas as pd
import matplotlib.pyplot as plt
from binance.client import Client
import datetime as dt

# ========== CONFIG ==========
API_KEY = "SuZsL51PpZcLtLJ5AzAoOEKEu8z157Fb7aqTjGO0o5gSShzNBqdosiJkhlB5fMT3"
API_SECRET = "LG9r2LBuSXNBtHX426vIzohq3X1d1PBxdqWOavc8dMVhwFlJFe9GKMPKNHDmwgfN"
SYMBOL = "BTCUSDT"
INTERVAL = "1h"
LIMIT = 500  # number of candles to fetch
SHORT_MA = 5
LONG_MA = 15
TESTNET = True
# ============================

# Connect to Binance
client = Client(API_KEY, API_SECRET, testnet=TESTNET)

def get_klines(symbol=SYMBOL, interval=INTERVAL, limit=LIMIT):

    """Fetch historical OHLCV data from Binance"""
    klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore"
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
    df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})

    return df

def moving_average_strategy(df, short_window=SHORT_MA, long_window=LONG_MA):

    """Apply Moving Average Crossover strategy"""
    df["MA_short"] = df["close"].rolling(short_window).mean()
    df["MA_long"] = df["close"].rolling(long_window).mean()
    df["signal"] = 0
    df.loc[df["MA_short"] > df["MA_long"], "signal"] = 1  # Buy
    df.loc[df["MA_short"] < df["MA_long"], "signal"] = -1 # Sell

    return df

def backtest(df, initial_balance=100):

    """Simple backtest for strategy"""
    balance = initial_balance
    position = 0
    for i in range(1, len(df)):
        if df["signal"].iloc[i-1] == 1 and position == 0:
            # Buy
            position = balance / df["close"].iloc[i]
            balance = 0
        elif df["signal"].iloc[i-1] == -1 and position > 0:
            # Sell
            balance = position * df["close"].iloc[i]
            position = 0
    # Final portfolio value
    if position > 0:
        balance = position * df["close"].iloc[-1]

    return balance

# Run the bot
print("Fetching data...")
data = get_klines()

print("Running strategy...")
data = moving_average_strategy(data)

print("Backtesting...")
final_balance = backtest(data)
print(f"Final balance from ${100} start: ${final_balance:.2f}")

# Save results
filename = f"trading_results_{SYMBOL}_{dt.datetime.now().strftime('%Y%m%d_%H%M')}.csv"
data.to_csv(filename, index=False)
print(f"Results saved to {filename}")

# Plot
plt.figure(figsize=(12,6))
plt.plot(data["timestamp"], data["close"], label="Close Price", alpha=0.5)
plt.plot(data["timestamp"], data["MA_short"], label=f"{SHORT_MA}-period MA", alpha=0.9)
plt.plot(data["timestamp"], data["MA_long"], label=f"{LONG_MA}-period MA", alpha=0.9)

# Buy/Sell markers
buy_signals = data[data["signal"] == 1]
sell_signals = data[data["signal"] == -1]
plt.scatter(buy_signals["timestamp"], buy_signals["close"], marker="^", color="green", label="Buy", alpha=0.8)
plt.scatter(sell_signals["timestamp"], sell_signals["close"], marker="v", color="red", label="Sell", alpha=0.8)

plt.title(f"{SYMBOL} Moving Average Strategy")
plt.xlabel("Date")
plt.ylabel("Price (USDT)")
plt.legend()
plt.show()