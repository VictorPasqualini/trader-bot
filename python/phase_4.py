import os
import time
import math
import datetime as dt
import pandas as pd
import numpy as np
from binance.client import Client
from binance.enums import *
from datetime import datetime
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import GradientBoostingRegressor
import matplotlib.pyplot as plt

# ===== CONFIG =====
API_KEY = "evWKMdTqLnWUXtAWRiulkEOZcQUsLJW0TP2ReeEkB9WLVPanJfpVcqcnnGH8qVgA"
API_SECRET = "MjicvRc5KYJRwFExGk4D7c2rOTemZccHpva0lUBOjGcEiliMmzqMfI5sMHZfHjla"
SYMBOL = "BTCUSDT"
INTERVAL = "15m"
LIMIT = 1000
TRADE_AMOUNT_USDT = 100
STOP_LOSS_PCT = 0.01    # 1%
TAKE_PROFIT_PCT = 0.02  # 2%
LOG_FILE = "trade_log_new_way.csv"
TESTNET = True
# ==================

# Connect to Binance
client = Client(API_KEY, API_SECRET, testnet=TESTNET)
client.TIME_OFFSET = int((client.get_server_time()['serverTime']) - int(time.time() * 1000))

def get_binance_timestamp():
    return int(time.time() * 1000 + client.TIME_OFFSET)

def get_step_size(symbol):
    info = client.get_symbol_info(symbol)
    lot_size = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
    return float(lot_size["stepSize"]), float(lot_size["minQty"])

def adjust_quantity(symbol, quantity):
    step_size, _ = get_step_size(symbol)
    precision = int(round(-math.log(step_size, 10), 0))
    return float(f"{quantity:.{precision}f}")

def get_klines(symbol=SYMBOL, interval=INTERVAL, limit=LIMIT):
    klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore"
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df

def rsi(series, period=14):
    delta = series.diff()
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(up, index=series.index).rolling(period).mean()
    roll_down = pd.Series(down, index=series.index).rolling(period).mean()
    rs = roll_up / (roll_down + 1e-12)
    return 100 - (100 / (1 + rs))

def atr(df, period=14):
    high = df['high']; low = df['low']; close = df['close']
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def build_features(df):
    # df has columns: open, high, low, close, volume (float), indexed by time
    df = df.copy()
    df['log_ret'] = np.log(df['close']).diff()
    df['ma5']  = df['close'].rolling(5).mean()
    df['ma20'] = df['close'].rolling(20).mean()
    df['ma60'] = df['close'].rolling(60).mean()
    df['ma_gap_5_20'] = df['ma5'] - df['ma20']
    df['ma_gap_20_60'] = df['ma20'] - df['ma60']
    df['vol_20'] = df['close'].pct_change().rolling(20).std()
    df['rsi14'] = rsi(df['close'], 14)
    df['bb_width'] = (df['close'].rolling(20).std() * 2) / (df['close'].rolling(20).mean())
    df['atr14'] = atr(df, 14)

    # Target: next-bar log return
    df['y_next'] = df['log_ret'].shift(-1)

    # Clean
    df = df.dropna()
    feature_cols = ['log_ret','ma_gap_5_20','ma_gap_20_60','vol_20','rsi14','bb_width']
    return df, feature_cols, 'y_next'

def train_model_on_history(df, feature_cols, target_col):
    X = df[feature_cols].values
    y = df[target_col].values
    # Simple split: last 10% for validation (or skip for speed)
    split = int(len(df) * 0.9)
    X_train, y_train = X[:split], y[:split]
    model = GradientBoostingRegressor(random_state=42)
    model.fit(X_train, y_train)
    return model

def predict_next_return(model, df, feature_cols):
    x_latest = df[feature_cols].iloc[-1:].values
    pred_log_return = float(model.predict(x_latest)[0])  # predicted next-bar log return
    return pred_log_return

def should_enter_long(pred_log_return, fee_bp=7, buffer_bp=5):
    """
    Enter only if predicted edge > fees + tiny buffer.
    fee_bp: total round-trip bps (e.g., 7 bps = 0.07%)
    buffer_bp: extra cushion to reduce churn
    """
    edge_bp = pred_log_return * 10000  # approx bps for small returns

    print("edge_bp = ", edge_bp)

    return edge_bp > (fee_bp + buffer_bp)

def compute_sl_tp_levels(entry_price, atr_value, sl_atr=1.0, tp_atr=2.0):
    """
    ATR-based stops: scales with volatility.
    Common starters: sl_atr = 0.8–1.5, tp_atr = 1.5–3.0
    """
    stop_loss = entry_price - sl_atr * atr_value
    take_profit = entry_price + tp_atr * atr_value
    return stop_loss, take_profit

def check_exits(current_price, stop_loss, take_profit, trailing=False, trail_mult=1.0, best_price=None, atr_val=None):
    """
    trailing=True: trail stop at (best_price - trail_mult*atr)
    """
    #if current_price <= stop_loss:
    #    return "stop"
    if current_price >= take_profit:
        return "tp"
    if trailing and best_price is not None and atr_val is not None:
        trail_stop = best_price - trail_mult * atr_val
        if current_price <= trail_stop:
            return "trail"
    return None

def get_usdt_balance():

    balance = client.get_asset_balance(asset="USDT")

    return float(balance["free"])

def get_btc_balance():

    balance = client.get_asset_balance(asset="BTC")

    return float(balance["free"])

def log_trade(side, price, quantity_btc, quantity_usdt):

    trade_data = {
        "timestamp": dt.datetime.now(),
        "side": side,
        "price": price,
        "quantity_btc": quantity_btc,
        "quantity_usdt": quantity_usdt,
        "balance_usdt": get_usdt_balance(),
        "balance_btc": get_btc_balance()
    }
    df_log = pd.DataFrame([trade_data])
    if not os.path.exists(LOG_FILE):
        df_log.to_csv(LOG_FILE, index=False)
    else:
        df_log.to_csv(LOG_FILE, mode="a", header=False, index=False)

# ===== MAIN LOOP =====
try:

    # 1) fetch klines as you do now
    df_raw = get_klines(limit=600)  # a bit more history for features
    df_feat, feats, target = build_features(df_raw)

    # 2) (Re)train occasionally (e.g., every 50 bars) or once at start
    #   For simplicity, retrain each loop is fine on 1m data & small model
    model = train_model_on_history(df_feat, feats, target)

    position_usdt = TRADE_AMOUNT_USDT
    position_btc = 0
    entry_price = None
    stop_loss = None
    take_profit = None
    best_price = None
    last_trade_usdt = 0

    while True:
        
        # 1) fetch klines as you do now
        df_raw = get_klines(limit=600)  # a bit more history for features
        df_feat, feats, target = build_features(df_raw)

        # 2) (Re)train occasionally (e.g., every 50 bars) or once at start
        #   For simplicity, retrain each loop is fine on 1m data & small model
        model = train_model_on_history(df_feat, feats, target)
        
        # 3) Predict next-bar return
        pred = predict_next_return(model, df_feat, feats)

        # pred_series = df_feat['log_ret'].tail(5)
        # pred = pred_series.mean()

        # 4) Current market info
        price = float(df_raw['close'].iloc[-1])
        curr_atr = float(df_feat['atr14'].iloc[-1])

        # 5) Entry decision (long/flat only)
        enter = should_enter_long(pred)

        # Your position tracking variables:
        # position_btc, position_usdt, entry_price, stop_loss, take_profit, best_price
        print("position_btc = ", position_btc)
        print("position_usdt = ", position_usdt)
        print("enter = ", enter)
        print("curr_atr = ", curr_atr)
        if position_btc == 0 and position_usdt > 0 and enter:
            # size = all your position_usdt (or fraction) -> convert to BTC respecting LOT_SIZE
            qty = adjust_quantity(SYMBOL, position_usdt / price)
            # Ensure >= minQty, else skip
            step_size, min_qty = get_step_size(SYMBOL)
            if qty >= min_qty:
                # place buy
                client.order_market_buy(symbol=SYMBOL, quantity=qty, timestamp=get_binance_timestamp())
                entry_price = price
                log_trade("BUY", price, qty, position_usdt)
                position_btc = qty
                position_usdt = 0
                stop_loss, take_profit = compute_sl_tp_levels(entry_price, curr_atr, sl_atr=1.0, tp_atr=10.0)
                best_price = entry_price  # for trailing
                print(f"🟢 BUY @ {price} pred={pred:.6f} SL={stop_loss:.2f} TP={take_profit:.2f}")
        elif position_btc > 0:
            # Manage exits if in a position
            best_price = max(best_price, price)  # for trailing
            exit_reason = check_exits(
                price, stop_loss, take_profit,
                trailing=True, trail_mult=1.0, best_price=best_price, atr_val=curr_atr
            )
            if exit_reason:
                qty = adjust_quantity(SYMBOL, position_btc)
                last_trade_usdt = price * qty
                client.order_market_sell(symbol=SYMBOL, quantity=qty, timestamp=get_binance_timestamp())
                # Update internal wallet by querying or by bookkeeping
                position_btc = 0
                # (Optionally refresh balances and set position_usdt accordingly)
                position_usdt = last_trade_usdt  # simplest
                log_trade(f"SELL-{exit_reason}", price, qty, position_usdt)
                print(f"🔴 SELL @ {price} because {exit_reason}")

        else:
            print("⚠️ No BTC to sell or no USDT to buy, skipping.")

        time.sleep(900)  # Wait until next candle

except KeyboardInterrupt:
    print("\nBot stopped by user.")
    print(f"Final USDT balance: {get_usdt_balance():.2f}")
    print(f"Final BTC balance: {get_btc_balance():.6f}")
    print(f"Trade log saved to {LOG_FILE}")