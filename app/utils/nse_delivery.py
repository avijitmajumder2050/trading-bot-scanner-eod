# ==========================================================
# File: app/utils/nse_delivery.py
# Delivery % per symbol from NSE's daily full bhavcopy
# (sec_bhavdata_full_DDMMYYYY.csv, column DELIV_PER).
# ==========================================================

import io
import time
import logging

import pandas as pd
import requests

logger = logging.getLogger(__name__)

NSE_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{:%d%m%Y}.csv"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"}


def _download(day):
    try:
        r = requests.get(NSE_URL.format(day), headers=HEADERS, timeout=30)
        if r.status_code == 200 and r.content.strip():
            return r.content
        logger.info(f"NSE delivery file for {day:%Y-%m-%d} not available yet (HTTP {r.status_code})")
    except Exception as e:
        logger.warning(f"NSE delivery download error for {day:%Y-%m-%d}: {e}")
    return None


def get_delivery_pct(day, wait_minutes=75, poll_seconds=180):
    """
    Returns {SYMBOL: delivery %} for trading day `day` (EQ series preferred,
    BE as fallback), or None if the file is still unavailable after
    `wait_minutes`. NSE usually publishes it around 17:50-18:30 IST.
    """
    deadline = time.time() + wait_minutes * 60
    content = _download(day)

    while content is None and time.time() + poll_seconds <= deadline:
        time.sleep(poll_seconds)
        content = _download(day)

    if content is None:
        logger.warning(f"⚠️ NSE delivery file for {day:%Y-%m-%d} unavailable after {wait_minutes} min")
        return None

    df = pd.read_csv(io.BytesIO(content), skipinitialspace=True)
    df.columns = df.columns.str.strip()
    df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip()
    df["SERIES"] = df["SERIES"].astype(str).str.strip()
    df["DELIV_PER"] = pd.to_numeric(df["DELIV_PER"], errors="coerce")

    df = df[df["SERIES"].isin(["EQ", "BE"])].sort_values("SERIES")  # EQ before BE
    df = df.drop_duplicates("SYMBOL")

    logger.info(f"NSE delivery loaded for {day:%Y-%m-%d} | symbols: {len(df)}")
    return dict(zip(df["SYMBOL"], df["DELIV_PER"]))
