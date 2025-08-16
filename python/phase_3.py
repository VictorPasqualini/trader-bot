import pandas as pd
import numpy as np
from binance.client import Client
from sklearn.linear_model import LogisticRegression
import time
import datetime as dt
import os

# ===== CONFIG =====
API_KEY = "evWKMdTqLnWUXtAWRiulkEOZcQUsLJW0TP2ReeEkB9WLVPanJfpVcqcnnGH8qVgA"
API_SECRET = "MjicvRc5KYJRwFExGk4D7c2rOTemZccHpva0lUBOjGcEiliMmzqMfI5sMHZfHjla"
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
LIMIT = 100
TRADE_QUANTITY = 0.001  # BTC amount
TESTNET = True
LOG_FILE = "trade_log.csv"
# ==================

# Connect to Binance
client = Client(API_KEY, API_SECRET, testnet=TESTNET)
client.ping()  # quick check
client.get_server_time()  # force time sync

client.TIME_OFFSET = int((client.get_server_time()['serverTime']) - int(time.time() * 1000))

server_time = client.get_server_time()
server_timestamp = server_time['serverTime'] / 1000
local_timestamp = time.time()
time_offset = server_timestamp - local_timestamp

def get_binance_timestamp():

    return int((time.time() + time_offset) * 1000)

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

def prepare_features(df):

    """Create AI features"""
    df["return"] = df["close"].pct_change()
    df["MA5"] = df["close"].rolling(5).mean()
    df["MA10"] = df["close"].rolling(10).mean()
    df["MA20"] = df["close"].rolling(20).mean()
    df["volatility"] = df["close"].rolling(10).std()
    df["target"] = (df["close"].shift(-1) > df["close"]).astype(int)
    df = df.dropna()
    features = ["return", "MA5", "MA10", "MA20", "volatility"]

    return df, features

def train_model():

    """Train AI model on past data"""
    df = get_klines(limit=500)
    df, features = prepare_features(df)
    X = df[features]
    y = df["target"]
    model = LogisticRegression()
    model.fit(X, y)

    return model, features

def get_last_signal(model, features):
    
    df = get_klines(limit=50)
    df, _ = prepare_features(df)
    X = df[features].iloc[-1:].values
    pred = model.predict(X)[0]

    return pred, df.iloc[-1]["close"]

def get_usdt_balance():

    balance = client.get_asset_balance(asset="USDT")

    return float(balance["free"])

def get_btc_balance():

    balance = client.get_asset_balance(asset="BTC")

    return float(balance["free"])

def log_trade(side, price):

    trade_data = {
        "timestamp": dt.datetime.now(),
        "side": side,
        "price": price,
        "quantity": TRADE_QUANTITY,
        "balance_usdt": get_usdt_balance(),
        "balance_btc": get_btc_balance()
    }
    df_log = pd.DataFrame([trade_data])
    if not os.path.exists(LOG_FILE):
        df_log.to_csv(LOG_FILE, index=False)
    else:
        df_log.to_csv(LOG_FILE, mode="a", header=False, index=False)

def place_order(signal, price):
    if signal == 1:
        print(f"🟢 BUY at {price}")
        client.order_market_buy(
            symbol=SYMBOL, 
            quantity=TRADE_QUANTITY,
            timestamp=get_binance_timestamp()
        )
        log_trade("BUY", price)
    else:
        print(f"🔴 SELL at {price}")
        client.order_market_sell(
            symbol=SYMBOL,
            quantity=TRADE_QUANTITY,
            timestamp=get_binance_timestamp()
        )
        log_trade("SELL", price)

# ===== MAIN LOOP =====
model, features = train_model()
print("Model trained. Starting live trading loop...")

try:
    while True:
        signal, price = get_last_signal(model, features)
        place_order(signal, price)
        time.sleep(60)  # Wait until next candle

except KeyboardInterrupt:
    print("\nBot stopped by user.")
    print(f"Final USDT balance: {get_usdt_balance():.2f}")
    print(f"Final BTC balance: {get_btc_balance():.6f}")
    print(f"Trade log saved to {LOG_FILE}")