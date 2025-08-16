import pandas as pd
import numpy as np
from binance.client import Client
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score
import matplotlib.pyplot as plt
import datetime as dt

# ========== CONFIG ==========
API_KEY = "SuZsL51PpZcLtLJ5AzAoOEKEu8z157Fb7aqTjGO0o5gSShzNBqdosiJkhlB5fMT3"
API_SECRET = "LG9r2LBuSXNBtHX426vIzohq3X1d1PBxdqWOavc8dMVhwFlJFe9GKMPKNHDmwgfN"
SYMBOL = "BTCUSDT"
INTERVAL = "1h"
LIMIT = 1000
TESTNET = True
# ============================

# Connect to Binance
client = Client(API_KEY, API_SECRET, testnet=TESTNET)

def get_klines(symbol=SYMBOL, interval=INTERVAL, limit=LIMIT):

    """Fetch OHLCV data from Binance"""
    klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore"
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)

    return df

# 1️⃣ Get historical data
df = get_klines()

# 2️⃣ Create AI features
df["return"] = df["close"].pct_change()  # % change
df["MA5"] = df["close"].rolling(5).mean()
df["MA10"] = df["close"].rolling(10).mean()
df["MA20"] = df["close"].rolling(20).mean()
df["volatility"] = df["close"].rolling(10).std()

# Target: 1 if price goes up next candle, else 0
df["target"] = (df["close"].shift(-1) > df["close"]).astype(int)

# Drop rows with NaN
df = df.dropna()

# Features for AI
features = ["return", "MA5", "MA10", "MA20", "volatility"]
X = df[features]
y = df["target"]

# 3️⃣ Split into train & test
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, shuffle=False)

# 4️⃣ Train model
model = LogisticRegression()
model.fit(X_train, y_train)

# 5️⃣ Evaluate accuracy
y_pred = model.predict(X_test)
print("Accuracy:", accuracy_score(y_test, y_pred))

# 6️⃣ Backtest with AI predictions
df_test = df.iloc[len(X_train):].copy()
df_test["pred_signal"] = y_pred
initial_balance = 100
balance = initial_balance
position = 0

for i in range(len(df_test)):
    if df_test["pred_signal"].iloc[i] == 1 and position == 0:
        position = balance / df_test["close"].iloc[i]
        balance = 0
    elif df_test["pred_signal"].iloc[i] == 0 and position > 0:
        balance = position * df_test["close"].iloc[i]
        position = 0

if position > 0:
    balance = position * df_test["close"].iloc[-1]

print(f"AI Final Balance: ${balance:.2f} (Start: ${initial_balance})")

# 7️⃣ Save results
filename = f"ai_trading_results_{SYMBOL}_{dt.datetime.now().strftime('%Y%m%d_%H%M')}.csv"
df_test.to_csv(filename, index=False)
print(f"Results saved to {filename}")

# 8️⃣ Plot
plt.figure(figsize=(12,6))
plt.plot(df_test["timestamp"], df_test["close"], label="Close Price", alpha=0.5)
buy_signals = df_test[df_test["pred_signal"] == 1]
sell_signals = df_test[df_test["pred_signal"] == 0]
plt.scatter(buy_signals["timestamp"], buy_signals["close"], marker="^", color="green", label="Buy", alpha=0.8)
plt.scatter(sell_signals["timestamp"], sell_signals["close"], marker="v", color="red", label="Sell", alpha=0.8)
plt.legend()
plt.show()