import logging
import os

from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
)

from app.bot.handlers import handle_message
from app.bot.scheduler import (
    terminate_at,
    run_nifty_breakout_trade,
)
from app.config.aws_ssm import get_param

from app.scanners.EMA_10_20_breakout import ema_price_cross
from app.bot.telegram_sender import send_telegram_message


# ───────────────────────────────
# Logging (FORCED – DO NOT USE basicConfig)
# ───────────────────────────────
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "bot.log")

os.makedirs(LOG_DIR, exist_ok=True)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# 🔥 CRITICAL: remove PTB / preloaded handlers
if root_logger.handlers:
    root_logger.handlers.clear()

file_handler = logging.FileHandler(LOG_FILE, mode="a")
file_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
)

console_handler = logging.StreamHandler()
console_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )
)

root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)

logger.info("✅ Logging system initialized")


# ───────────────────────────────
# Silence noisy libraries
# ───────────────────────────────
for lib in [
    "telegram",
    "telegram.ext",
    "httpx",
    "asyncio",
    "boto3",
    "botocore",
    "s3transfer",
    "urllib3",
]:
    logging.getLogger(lib).setLevel(logging.WARNING)


# ───────────────────────────────
# Load secrets
# ───────────────────────────────
BOT_TOKEN = get_param("/trading-bot/telegram/BOT_TOKEN")


# ───────────────────────────────
# Background jobs (PTB SAFE)
# ───────────────────────────────


async def post_init(app):
    logger.info("🚀 Starting background jobs")

    # Existing jobs
    #app.create_task(run_nifty_breakout_trade())
    #app.create_task(terminate_at(target_hour=12, target_minute=30))

    # 🔥 RUN EMA SCANNER IMMEDIATELY ON START
    try:
        logger.info("📊 Running EMA EOD scan on startup")

        today_df = ema_price_cross(
            save_to_s3=True,
            return_df=True
        )

        if today_df is not None and not today_df.empty:

            message = "📊 <b>EMA Momentum Stocks (BUY Setup)</b>\n\n"
            symbols_for_copy = []

            for _, row in today_df.iterrows():
                message += (
                    f"🔹 <b>{row['Stock Name']}</b>\n"
                    f"Price: ₹{row['Price']}\n"
                    f"Setup: {row['Setup_Case']}\n\n"
                )

                symbols_for_copy.append(
                    f"NSE:{row['Stock Name'].replace(' ', '').upper()}-EQ"
                )

            copy_line = ",".join(symbols_for_copy)

            message += (
                "📋 <b>FYERS Copy:</b>\n"
                f"<code>{copy_line}</code>"
            )

            await send_telegram_message(message)
            logger.info("✅ EMA startup alert sent")

        else:
            await send_telegram_message(
                "📊 EMA Scan Completed\nNo momentum signals found."
            )
            logger.info("ℹ️ No EMA signals found on startup")

    except Exception as e:
        logger.error(f"❌ EMA startup error: {e}")
        await send_telegram_message(f"❌ EMA Scan Error: {e}")

    


# ───────────────────────────────
# Main
# ───────────────────────────────
def main():
    logger.info("🤖 Building Telegram application")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("🤖 Telegram bot started")
    app.run_polling()


# ───────────────────────────────
# Entry
# ───────────────────────────────
if __name__ == "__main__":
    main()
