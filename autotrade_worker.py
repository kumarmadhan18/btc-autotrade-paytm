import os
import time
from datetime import datetime
from dotenv import load_dotenv
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
    log_inr_transaction,
)

load_dotenv()

REAL_TRADING = False
ENABLE_NOTIFICATIONS = True
AUTO_REFRESH_INTERVAL = 15  # seconds

def background_autotrade_loop():
    """Persistent background auto-trade worker: DB-driven, idle-safe."""
    while True:
        try:
            autotrade_active = get_autotrade_active_from_db()

            if autotrade_active:
                # --- Idle Monitoring (stop if no trade > 60 mins) ---
                last_trade_time = get_last_trade_time_from_logs()
                if last_trade_time:
                    idle_minutes = (datetime.now() - last_trade_time).total_seconds() / 60
                    if idle_minutes > 60:
                        update_wallet_daily_summary(auto_end=True)
                        update_autotrade_status_db(0)

                        msg = f"⏰ Auto-Trade auto-stopped after {idle_minutes:.0f} minutes of inactivity (background)"
                        print(msg)
                        if ENABLE_NOTIFICATIONS:
                            send_telegram(msg)

                        # --- Fetch balances safely from DB ---
                        btc_balance, _ = get_last_wallet_balance()
                        btc_balance = btc_balance or 0.0

                        inr_balance = get_last_inr_balance()
                        if inr_balance is None:
                            inr_balance = 10000.0 if not REAL_TRADING else 0.0

                        log_wallet_transaction("AUTO_IDLE_STOP", 0, btc_balance, 0, "AUTO_IDLE_STOP")
                        log_inr_transaction("AUTO_IDLE_STOP", 0, inr_balance, "LIVE" if REAL_TRADING else "TEST")
                        continue  # Skip this loop cycle

                # --- Fetch current BTC price and run trade logic ---
                price_inr = cd_get_market_price("BTCINR")
                if price_inr is not None and price_inr > 0:
                    check_auto_trading(price_inr)
                else:
                    print("⚠️ Background loop skipped: Invalid market price")
                    if ENABLE_NOTIFICATIONS:
                        send_telegram("⚠️ Background AutoTrade skipped: Invalid market price")

            else:
                # Auto-trade inactive → short sleep
                time.sleep(5)

        except Exception as e:
            err_msg = f"⚠️ Background auto-trade error: {str(e)}"
            print(err_msg)
            if ENABLE_NOTIFICATIONS:
                send_telegram(err_msg)

        time.sleep(AUTO_REFRESH_INTERVAL)


# ----------------- Flask Wrapper for Render Health Check -----------------
app = Flask(__name__)

@app.route("/")
def health():
    return "🚀 Autotrade worker is running", 200


if __name__ == "__main__":
    print("🚀 Autotrade worker started")

    # Start background loop in a separate daemon thread
    t = threading.Thread(target=background_autotrade_loop, daemon=True)
    t.start()

    # Use Render-provided port (fallback to 10000 locally)
    # port = int(os.environ.get("PORT", 10000))
    port = int(os.environ.get["PORT"])
    app.run(host="0.0.0.0", port=port)
