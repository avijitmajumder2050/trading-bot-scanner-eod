# ==========================================================
# File: app/scanners/EMA_10_20_breakout.py
# ==========================================================

import logging
import pandas as pd
from ta.trend import EMAIndicator
from datetime import datetime, timedelta

# Activate global logging
from app.config import logging_config

from app.config.aws_s3 import read_csv_from_s3, upload_csv_to_s3,S3_BUCKET
from app.config.settings import (
    IST,
    MAP_FILE_KEY,
    EOD_DATA_PREFIX
)
from app.config.dhan_auth import dhan
from app.broker.market_data import get_quotes_with_retry
from app.utils.nse_delivery import get_delivery_pct

logger = logging.getLogger(__name__)

OUTPUT_KEY = "uploads/ema_momentum_EOD.csv"

# ---- Rule thresholds ----
MAX_DAY_MOVE_PCT = 6.0     # skip if |close vs prev close| on detection day >= this %
MIN_PRICE_STRENGTH = 80    # mapping.csv "Price Strength"
MIN_EPS_STRENGTH = 80      # mapping.csv "EPS Strength"
MIN_DELIVERY_PCT = 47.0    # NSE delivery % on the signal candle's day
DELIVERY_WAIT_MINUTES = 75 # NSE posts the file ~17:50-18:30 IST; scan starts ~17:31


# ==============================
# Check if market day
# ==============================
def is_market_day(timestamp):
    # Monday=0, Sunday=6
    return timestamp.weekday() < 5

# ------------------------------
# Update Today Candle
# ------------------------------
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

# ------------------------------
# Delivery % filter
# ------------------------------
def apply_delivery_filter(matched):
    """
    Adds "Delivery %" and keeps rows with delivery >= MIN_DELIVERY_PCT.
    A symbol missing from the NSE file is rejected. If the whole file is
    unavailable (NSE late/down) the rows are kept with a blank Delivery %,
    so a missing file never wipes the day's list.
    """
    if not matched:
        return matched

    kept = []
    for day in sorted({m["_candle_date"] for m in matched}):
        delivery = get_delivery_pct(day, wait_minutes=DELIVERY_WAIT_MINUTES)

        for m in (m for m in matched if m["_candle_date"] == day):
            if delivery is None:
                logger.warning(f"{m['Stock Name']} kept — delivery % unavailable for {day}")
                m["Delivery %"] = None
                kept.append(m)
                continue

            pct = delivery.get(m["Stock Name"])
            if pct is None or pd.isna(pct) or pct < MIN_DELIVERY_PCT:
                logger.info(f"{m['Stock Name']} skipped — delivery {pct}% < {MIN_DELIVERY_PCT}%")
                continue

            m["Delivery %"] = round(float(pct), 2)
            logger.info(f"✅ {m['Stock Name']} delivery {m['Delivery %']}%")
            kept.append(m)

    for m in kept:
        m.pop("_candle_date", None)
    return kept


# ==============================
# EMA PRICE CROSS SCANNER
# ==============================
def ema_price_cross():
    logger.info("🚀 EMA PRICE CROSS SCANNER STARTED")

    scan_time = datetime.now(IST)
    scan_time_str = scan_time.strftime("%Y-%m-%d %H:%M:%S")
    today = pd.Timestamp(scan_time.date())
    weekday = scan_time.weekday()  # Monday=0, Sunday=6

    # ---- Load Mapping ----
    df_map = read_csv_from_s3(S3_BUCKET, MAP_FILE_KEY)

    if df_map.empty:
        logger.error("Mapping file empty or not found")
        upload_csv_to_s3(pd.DataFrame(), S3_BUCKET, OUTPUT_KEY)
        return pd.DataFrame()

    df_map = df_map.dropna(
        subset=["Stock Name", "Instrument ID", "Market Cap", "Setup_Case"]
    )

    df_map["Instrument ID"] = df_map["Instrument ID"].astype(int)
    instrument_ids = df_map["Instrument ID"].tolist()
    logger.info(f"Mapping loaded | Total stocks: {len(instrument_ids)}")
     # ------------------------------
    # Get Live Data on Trading Days only
    # ------------------------------
    live_data = {}
    if weekday < 5:  # Monday=0 ... Friday=4
        live_data = get_quotes_with_retry(instrument_ids, "NSE_EQ") or {}
        logger.info(f"Total live quotes received: {len(live_data)}")
    else:
        logger.info("Weekend detected — using only EOD data")

    

    matched = []

    # ---- Scan Each Stock ----
    for _, row in df_map.iterrows():
        stock = row["Stock Name"]
        instrument_id = row["Instrument ID"]
        market_cap = float(row["Market Cap"])
        setup_case = row["Setup_Case"]
        price_strength = pd.to_numeric(row.get("Price Strength"), errors="coerce")
        eps_strength = pd.to_numeric(row.get("EPS Strength"), errors="coerce")

        # ---- Strength filter (from mapping, no candle data needed) ----
        if not (price_strength >= MIN_PRICE_STRENGTH and eps_strength >= MIN_EPS_STRENGTH):
            logger.info(f"{stock} skipped — PS={price_strength} EPS={eps_strength} below {MIN_PRICE_STRENGTH}/{MIN_EPS_STRENGTH}")
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
             # Update today candle only on trading days
            if weekday < 5:
                live = live_data.get(str(instrument_id))
                if live:
                    df = update_today_candle(df, today, live)

            # Only take last 120 candles
            df = df.tail(120)

            if len(df) < 50:
                logger.warning(f"{stock} skipped — Not enough candles")
                continue

            # ---- EMA Calculation ----
            df["ema10"] = EMAIndicator(df["close"], 10).ema_indicator()
            df["ema20"] = EMAIndicator(df["close"], 20).ema_indicator()
            df["ema50"] = EMAIndicator(df["close"], 50).ema_indicator()

            latest = df.iloc[-1]
            prev = df.iloc[-2]

            # ---- EMA CROSS CONDITIONS ----
            cross_ema10 = prev["close"] <= prev["ema10"] and latest["close"] > latest["ema10"]
            cross_ema20 = prev["close"] <= prev["ema20"] and latest["close"] > latest["ema20"]

            cond_price_cross = cross_ema10 or cross_ema20
            cond_alignment = latest["ema10"] > latest["ema20"] > latest["ema50"]
            cond_filters = market_cap > 500 and latest["volume"] > 70000

            day_change_pct = (latest["close"] / prev["close"] - 1) * 100
            cond_day_move = abs(day_change_pct) < MAX_DAY_MOVE_PCT

            logger.info(
                f"{stock} | Cross10={cross_ema10} | "
                f"Cross20={cross_ema20} | "
                f"Align={cond_alignment} | "
                f"MCap={market_cap} | "
                f"Vol={latest['volume']} | "
                f"DayChg={day_change_pct:.2f}%"
            )

            if cond_price_cross and cond_alignment and cond_filters and not cond_day_move:
                logger.info(f"{stock} skipped — day move {day_change_pct:.2f}% >= {MAX_DAY_MOVE_PCT}%")

            if cond_price_cross and cond_alignment and cond_filters and cond_day_move:
                logger.info(f"🚀 EMA MOMENTUM SIGNAL → {stock}")

                matched.append({
                    "Stock Name": stock,
                    "Security ID": instrument_id,
                    "Market Cap": market_cap,
                    "Open": round(latest["open"], 2),
                    "Price": round(latest["close"], 2),
                    "High": round(latest["high"], 2),
                    "Low": round(latest["low"], 2),
                    "Day Chg %": round(day_change_pct, 2),
                    "Price Strength": price_strength,
                    "EPS Strength": eps_strength,
                    "Setup_Case": setup_case,
                    "Scan Time": scan_time_str,
                    "_candle_date": df.index[-1].date(),
                })

        except Exception as e:
            logger.error(f"{stock} failed: {e}")

    logger.info(f"Technical + strength matches: {len(matched)}")

    # ---- Delivery % filter (NSE full bhavcopy of the signal candle's day) ----
    matched = apply_delivery_filter(matched)

    logger.info(f"Total matched stocks today: {len(matched)}")

    # ---- Prepare DataFrame ----
    today_df = pd.DataFrame(matched)

    # ---- Weekly storage ----
    result_df = today_df.copy()
    try:
        try:
            existing_df = read_csv_from_s3(S3_BUCKET, OUTPUT_KEY)
        except Exception as e:
            logger.warning(f"S3 key not found, creating new file: {e}")
            existing_df = pd.DataFrame()
            

        if not existing_df.empty and "Scan Time" in existing_df.columns:
            existing_df["Scan Time"] = pd.to_datetime(existing_df["Scan Time"])
            result_df["Scan Time"] = pd.to_datetime(result_df["Scan Time"])

            today_date = scan_time.date()
            week_start = today_date - timedelta(days=today_date.weekday())
            existing_df = existing_df[existing_df["Scan Time"].dt.date >= week_start]

            combined_df = pd.concat([existing_df, result_df], ignore_index=True)
            combined_df.sort_values("Scan Time", ascending=False, inplace=True)
            combined_df.drop_duplicates(subset=["Security ID"], keep="first", inplace=True)

            result_df = combined_df
    except Exception as e:
        logger.warning(f"Weekly merge skipped: {e}")

    # ---- Column order ----
    columns_order = [
        "Stock Name", "Security ID", "Market Cap",
        "Open", "Price", "High", "Low",
        "Day Chg %", "Price Strength", "EPS Strength", "Delivery %",
        "Setup_Case", "Scan Time"
    ]
    result_df = result_df.reindex(columns=columns_order)
    upload_csv_to_s3(result_df, S3_BUCKET, OUTPUT_KEY)

    logger.info(
        f"✅ EMA momentum file updated | Records={len(result_df)} | "
        f"Bucket={S3_BUCKET} | Key={OUTPUT_KEY}"
    )

    return today_df


# ==============================
# ENTRY POINT
# ==============================
if __name__ == "__main__":
    df = ema_price_cross()
    print(df)
