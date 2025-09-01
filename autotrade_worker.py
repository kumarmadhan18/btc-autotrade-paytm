import time
from python_project_paytm import get_last_wallet_balance, get_last_inr_balance,check_auto_trading,cd_get_market_price,send_telegram,get_autotrade_active_from_db, get_last_trade_time_from_logs, update_wallet_daily_summary, update_autotrade_status_db,log_wallet_transaction,log_inr_transaction
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

last_trade_time = time.time()

# def background_autotrade_loop():
#     global last_trade_time
#     while True:
#         state = get_autotrade_state()
#         if state and state["active"]:
#             price_inr = cd_get_market_price()
#             if price_inr:
#                 check_auto_trading(price_inr)
#                 last_trade_time = time.time()
#             else:
#                 print("⚠️ Failed to fetch price.")
#         else:
#             time.sleep(5)

#         # Idle alert (30s no trades)
#         if time.time() - last_trade_time > 30:
#             send_telegram("⚠️ Idle Alert: No trades in last 30s!")
#             last_trade_time = time.time()

#         time.sleep(5)

# Initialize session state for BTC wallet
print("REAL_TRADING =", REAL_TRADING)

if REAL_TRADING:
    try:
        wallet = Wallet(BTC_WALLET_NAME)
    except:
        wallet = Wallet.create(BTC_WALLET_NAME)
    BALANCE_BTC = wallet.balance() / 1e8
    last_trade_time = None
else:
    print("Fetching last wallet balance from DB")
    BALANCE_BTC, last_trade_time = get_last_wallet_balance()
    print(f"Test Balance: {BALANCE_BTC}, Last Trade Time: {last_trade_time}")

BTC_WALLET = {"balance": BALANCE_BTC}
INR_WALLET = {"balance": get_last_inr_balance()}

AUTO_REFRESH_INTERVAL = 15  # seconds

def background_autotrade_loop():
    """Runs continuously in the background, checking DB flag & auto-trading + idle timeout."""
    while True:
        try:
            if get_autotrade_active_from_db():  # ✅ DB decides

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

                        log_wallet_transaction("AUTO_IDLE_STOP", 0, BTC_WALLET['balance'], 0, "AUTO_IDLE_STOP")
                        log_inr_transaction("AUTO_IDLE_STOP", 0, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
                        continue

                # --- Run trade logic ---
                price_inr = cd_get_market_price("BTCINR")
                if price_inr:
                    check_auto_trading(price_inr)

            else:
                time.sleep(5)

        except Exception as e:
            print("⚠️ Background auto-trade error:", str(e))

        time.sleep(AUTO_REFRESH_INTERVAL)  # e.g., 15 sec loop

if __name__ == "__main__":
    background_autotrade_loop()
