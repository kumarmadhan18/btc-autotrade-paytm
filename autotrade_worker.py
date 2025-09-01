import time
from datetime import datetime
from dotenv import load_dotenv
import requests
import threading
from flask import Flask

from python_project_paytm import (
    get_last_wallet_balance,
    get_last_inr_balance,
    check_auto_trading,
    cd_get_market_price,
    send_telegram,
    get_autotrade_active_from_db,
    get_last_trade_time_from_logs,
    update_wallet_daily_summary,
    update_autotrade_status_db,
    log_wallet_transaction,
    log_inr_transaction
)

load_dotenv()

REAL_TRADING = False
ENABLE_NOTIFICATIONS = True
AUTO_REFRESH_INTERVAL = 15  # seconds


def background_autotrade_loop():
    """Runs continuously in the background, checking DB flag & auto-trading + idle timeout."""
    while True:
        try:
            if get_autotrade_active_from_db():  # ✅ DB flag

                # --- Idle Monitoring ---
                last_trade_time = get_last_trade_time_from_logs()
                if last_trade_time:
                    idle_minutes = (datetime.now() - last_trade_time).total_seconds() / 60
                    if idle_minutes > 60:
                        update_wallet_daily_summary(auto_end=True)
                        update_autotrade_status_db(0)

                        msg = f"⏰ Auto-Trade auto-stopped after {idle_minutes:.0f} minutes of inactivity (background)"
                        print(msg)
                        send_telegram(msg)

                        # Use DB balances instead of cached ones
                        btc_balance, _ = get_last_wallet_balance()
                        inr_balance = get_last_inr_balance()

                        log_wallet_transaction("AUTO_IDLE_STOP", 0, btc_balance, 0, "AUTO_IDLE_STOP")
                        log_inr_transaction("AUTO_IDLE_STOP", 0, inr_balance, "LIVE" if REAL_TRADING else "TEST")
                        continue

                # --- Run trade logic ---
                price_inr = cd_get_market_price("BTCINR")
                if price_inr:
                    check_auto_trading(price_inr)

            else:
                time.sleep(5)

        except Exception as e:
            print("⚠️ Background auto-trade error:", str(e))

        time.sleep(AUTO_REFRESH_INTERVAL)


# ----------------- Flask Wrapper for Render -----------------
app = Flask(__name__)

@app.route("/")
def health():
    return "🚀 Autotrade worker is running", 200


if __name__ == "__main__":
    print("🚀 Autotrade worker started")

    # Start background loop in a separate thread
    t = threading.Thread(target=background_autotrade_loop, daemon=True)
    t.start()

    # Keep Flask alive so Render detects an open port
    app.run(host="0.0.0.0", port=10000)
