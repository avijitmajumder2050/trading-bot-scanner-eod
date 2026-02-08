# ==========================================================
# File: app/scanners/EMA_10_20_breakout.py
# ==========================================================

import time
import logging
import pandas as pd
from ta.trend import EMAIndicator
from datetime import datetime

# Activate global logging
from app.config import logging_config

from app.config.dhan_auth import dhan
from app.config.aws_s3 import read_csv_from_s3
from app.config.settings import (
    IST,
    S3_BUCKET,
    MAP_FILE_KEY,
    EOD_DATA_PREFIX
)

logger = logging.getLogger(__name__)


# ==============================
# UPDATE TODAY CANDLE
# ==============================
def update_today_candle(df, today, live):
    ohlc = live["ohlc"]

    df.loc[today] = {
        "open": ohlc["open"],
        "high": ohlc["high"],
        "low": ohlc["low"],
        "close": live["last_price"],
        "volume": live["volume"],
    }

    df.sort_index(inplace=True)

    # Keep only recent candles for performance
    return df.tail(120)


# ==============================
# EMA PRICE CROSS SCANNER
# ==============================
def ema_price_cross():

    logger.info("🚀 EMA PRICE CROSS SCANNER STARTED")

    today = pd.Timestamp(datetime.now(IST).date())

    # ---- Load Mapping ----
    df_map = read_csv_from_s3(S3_BUCKET, MAP_FILE_KEY)

    if df_map.empty:
        logger.error("Mapping file empty or not found")
        return "Mapping file not available"

    df_map = df_map.dropna(subset=["Stock Name", "Instrument ID", "Market Cap"])
    df_map["Instrument ID"] = df_map["Instrument ID"].astype(int)

    instrument_ids = df_map["Instrument ID"].tolist()

    # ---- Fetch Live Quotes ----
    live_data = {}

    for i in range(0, len(instrument_ids), 1000):
        batch = instrument_ids[i:i + 1000]
        try:
            resp = dhan.quote_data(securities={"NSE_EQ": batch})
            live_data.update(resp["data"]["data"].get("NSE_EQ", {}))
        except Exception as e:
            logger.error(f"Quote API error: {e}")
        time.sleep(0.4)

    matched = []

    # ---- Scan Each Stock ----
    for _, row in df_map.iterrows():

        stock = row["Stock Name"]
        instrument_id = row["Instrument ID"]
        market_cap = float(row["Market Cap"])

        if str(instrument_id) not in live_data:
            continue

        eod_key = f"{EOD_DATA_PREFIX}/{instrument_id}.csv"
        df = read_csv_from_s3(S3_BUCKET, eod_key)

        if df.empty:
            continue

        try:
            df.columns = df.columns.str.lower()
            df["date"] = pd.to_datetime(df["date"])
            df.set_index("date", inplace=True)

            live = live_data[str(instrument_id)]
            df = update_today_candle(df, today, live)

            if len(df) < 50:
                continue

            # ---- EMA Calculation ----
            df["ema10"] = EMAIndicator(df["close"], 10).ema_indicator()
            df["ema20"] = EMAIndicator(df["close"], 20).ema_indicator()
            df["ema50"] = EMAIndicator(df["close"], 50).ema_indicator()

            latest = df.iloc[-1]
            prev = df.iloc[-2]

            # =============================
            # SIGNAL CONDITIONS
            # =============================

            # ✔ Price fresh cross above EMA10 or EMA20
            cond_price_cross = (
                (prev["close"] <= prev["ema10"] and latest["close"] > latest["ema10"]) or
                (prev["close"] <= prev["ema20"] and latest["close"] > latest["ema20"])
            )

            # ✔ EMA alignment 10 > 20 > 50
            cond_alignment = (
                latest["ema10"] > latest["ema20"] > latest["ema50"]
            )

            # ✔ Market Cap > 500 Cr
            # ✔ Volume > 70,000
            cond_filters = (
                market_cap > 500 and
                latest["volume"] > 70000
            )

            # ---- Final Signal ----
            if cond_price_cross and cond_alignment and cond_filters:

                logger.info(f"🚀 PRICE EMA CROSS → {stock}")

                matched.append({
                    "Stock": stock,
                    "Price": round(latest["close"], 2),
                    "Volume": int(latest["volume"]),
                    "MarketCap": market_cap
                })

        except Exception as e:
            logger.error(f"{stock} failed: {e}")

    if matched:
        return pd.DataFrame(matched).to_csv(index=False)
    else:
        return "No EMA price cross signals today"


# ==============================
# ENTRY POINT
# ==============================
if __name__ == "__main__":
    print(ema_price_cross())
