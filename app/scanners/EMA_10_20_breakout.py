# ==========================================================
# File: app/scanners/EMA_10_20_breakout.py
# ==========================================================

import time
import logging
import pandas as pd
from ta.trend import EMAIndicator
from datetime import datetime, timedelta

# Activate global logging (DO NOT reconfigure logging here)
from app.config import logging_config

from app.config.dhan_auth import dhan
from app.config.aws_s3 import read_csv_from_s3, upload_csv_to_s3
from app.config.settings import (
    IST,
    S3_BUCKET,
    MAP_FILE_KEY,
    EOD_DATA_PREFIX
)

logger = logging.getLogger(__name__)

OUTPUT_KEY = "uploads/ema_momentum_EOD.csv"


# ==============================
# UPDATE TODAY CANDLE
# ==============================
def update_today_candle(df, today, live):

    if "ohlc" not in live:
        logger.warning("Live data missing OHLC")
        return df

    ohlc = live["ohlc"]

    df.loc[today] = {
        "open": ohlc.get("open", 0),
        "high": ohlc.get("high", 0),
        "low": ohlc.get("low", 0),
        "close": live.get("last_price", 0),
        "volume": live.get("volume", 0),
    }

    df.sort_index(inplace=True)
    return df.tail(120)


# ==============================
# EMA PRICE CROSS SCANNER
# ==============================
def ema_price_cross():

    logger.info("🚀 EMA PRICE CROSS SCANNER STARTED")

    scan_time = datetime.now(IST)
    scan_time_str = scan_time.strftime("%Y-%m-%d %H:%M:%S")
    today = pd.Timestamp(scan_time.date())

    # ---- Load Mapping ----
    df_map = read_csv_from_s3(S3_BUCKET, MAP_FILE_KEY)

    logger.info(f"Mapping file loaded. Rows: {len(df_map)}")

    if df_map.empty:
        logger.error("Mapping file empty or not found")
        upload_csv_to_s3(pd.DataFrame(), S3_BUCKET, OUTPUT_KEY)
        return "Mapping file not available"

    df_map = df_map.dropna(
        subset=["Stock Name", "Instrument ID", "Market Cap", "Setup_Case"]
    )

    df_map["Instrument ID"] = df_map["Instrument ID"].astype(int)
    instrument_ids = df_map["Instrument ID"].tolist()

    # ---- Fetch Live Quotes ----
    live_data = {}

    for i in range(0, len(instrument_ids), 1000):
        batch = instrument_ids[i:i + 1000]
        try:
            resp = dhan.quote_data(securities={"NSE_EQ": batch})
            live_data.update(resp["data"]["data"].get("NSE_EQ", {}))
            logger.info(f"Fetched live batch size: {len(batch)}")
        except Exception as e:
            logger.error(f"Quote API error: {e}")
        time.sleep(0.4)

    logger.info(f"Total live quotes received: {len(live_data)}")

    matched = []

    # ---- Scan Each Stock ----
    for _, row in df_map.iterrows():

        stock = row["Stock Name"]
        instrument_id = row["Instrument ID"]
        market_cap = float(row["Market Cap"])
        setup_case = row["Setup_Case"]

        live = live_data.get(str(instrument_id))
        if not live:
            logger.warning(f"{stock} skipped — No live data")
            continue

        eod_key = f"{EOD_DATA_PREFIX}/{instrument_id}.csv"
        df = read_csv_from_s3(S3_BUCKET, eod_key)

        if df.empty:
            logger.warning(f"{stock} skipped — No EOD data")
            continue

        try:
            df.columns = df.columns.str.lower()
            df["date"] = pd.to_datetime(df["date"])
            df.set_index("date", inplace=True)

            df = update_today_candle(df, today, live)

            if len(df) < 50:
                logger.warning(f"{stock} skipped — Not enough candles")
                continue

            # ---- EMA Calculation ----
            df["ema10"] = EMAIndicator(df["close"], 10).ema_indicator()
            df["ema20"] = EMAIndicator(df["close"], 20).ema_indicator()
            df["ema50"] = EMAIndicator(df["close"], 50).ema_indicator()

            latest = df.iloc[-1]
            prev = df.iloc[-2]

            # ---- Conditions ----
            cross_ema10 = prev["close"] <= prev["ema10"] and latest["close"] > latest["ema10"]
            cross_ema20 = prev["close"] <= prev["ema20"] and latest["close"] > latest["ema20"]

            cond_price_cross = cross_ema10 or cross_ema20
            cond_alignment = latest["ema10"] > latest["ema20"] > latest["ema50"]
            cond_filters = market_cap > 500 and latest["volume"] > 70000

            logger.info(
                f"{stock} | Cross10={cross_ema10} | "
                f"Cross20={cross_ema20} | "
                f"Align={cond_alignment} | "
                f"MCap={market_cap} | "
                f"Vol={latest['volume']}"
            )

            if cond_price_cross and cond_alignment and cond_filters:

                logger.info(f"🚀 EMA MOMENTUM SIGNAL → {stock}")

                matched.append({
                    "Stock Name": stock,
                    "Security ID": instrument_id,
                    "Market Cap": market_cap,
                    "Open": round(latest["open"], 2),
                    "Price": round(latest["close"], 2),
                    "High": round(latest["high"], 2),
                    "Low": round(latest["low"], 2),
                    "Setup_Case": setup_case,
                    "Scan Time": scan_time_str
                })

        except Exception as e:
            logger.error(f"{stock} failed: {e}")

    logger.info(f"Total matched stocks today: {len(matched)}")

    result_df = pd.DataFrame(matched)

    # =============================
    # WEEKLY ROLLING + NO DUPLICATES
    # =============================
    try:
        existing_df = read_csv_from_s3(S3_BUCKET, OUTPUT_KEY)

        if not existing_df.empty and "Scan Time" in existing_df.columns:
            existing_df["Scan Time"] = pd.to_datetime(existing_df["Scan Time"])
            result_df["Scan Time"] = pd.to_datetime(result_df["Scan Time"])

            today_date = scan_time.date()
            week_start = today_date - timedelta(days=today_date.weekday())

            # Keep only this week data
            existing_df = existing_df[
                existing_df["Scan Time"].dt.date >= week_start
            ]

            combined_df = pd.concat([existing_df, result_df], ignore_index=True)

            # Latest first
            combined_df.sort_values("Scan Time", ascending=False, inplace=True)

            # Drop duplicates (latest kept)
            combined_df.drop_duplicates(
                subset=["Security ID"],
                keep="first",
                inplace=True
            )

            result_df = combined_df

    except Exception as e:
        logger.warning(f"Weekly merge skipped: {e}")

    # Enforce column order
    result_df = result_df.reindex(columns=[
        "Stock Name",
        "Security ID",
        "Market Cap",
        "Open",
        "Price",
        "High",
        "Low",
        "Setup_Case",
        "Scan Time"
    ])

    upload_csv_to_s3(result_df, S3_BUCKET, OUTPUT_KEY)

    logger.info(
        f"✅ EMA momentum file updated | "
        f"Records={len(result_df)} | "
        f"Bucket={S3_BUCKET} | Key={OUTPUT_KEY}"
    )

    return f"{len(result_df)} stocks saved to S3"


# ==============================
# ENTRY POINT
# ==============================
if __name__ == "__main__":
    print(ema_price_cross())
