import smtplib
import requests
import pandas as pd
from datetime import datetime, timedelta
import streamlit as st
import plotly.graph_objects as go
import os
import time
import pymysql
import psycopg2
import psycopg2.extras
from bitcoinlib.services.services import ServiceError
from bitcoinlib.wallets import Wallet, WalletError
import qrcode
from PIL import Image
import io
from io import BytesIO
import streamlit.components.v1 as components
import csv
import base64
from flask import Flask, request, jsonify, send_file, redirect, render_template_string
import hmac
import hashlib
import threading
import uuid
import json
import traceback
import threading, time
from Crypto.Cipher import AES
from paytmchecksum import PaytmChecksum
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
# from webhook import generate_signature
load_dotenv()
# --- CONFIG ---
# API_KEY = "NTaqcuC3m8Z38Rrr1k2dMQuid7ImrvOrw0p43cctvvBMMYQfrEehTifq7ZrBfvnk"
# API_SECRET = "76loGIkq6MvLLPliAZVXd2I2XiokA5dzqbunOJ0ftM7uR3wAqMEA1fBBVK52cT01"
# BOT_TOKEN = "7828169838:AAGpO-3WSsFdjLWR8MnKY8HY6g6pNh5iDUg"
# CHAT_ID = "7916754073"

API_KEY = os.getenv("COINDCX_API_KEY", "")
API_SECRET = os.getenv("COINDCX_API_SECRET", "")
BASE_URL = "https://api.coindcx.com"

# API_KEY = os.getenv("BINANCE_API_KEY", "")
# API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

ALERT_THRESHOLD_UP = 70000
ALERT_THRESHOLD_DOWN = 60000
STOP_LOSS_THRESHOLD = 60000
REAL_TRADING = False
ENABLE_NOTIFICATIONS = True
AUTO_REFRESH_INTERVAL = 15  # seconds

# Telegram notifier embedded
def get_mysql_connection():
    try:
        return psycopg2.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            dbname=os.getenv("DB_NAME"),
            port=int(os.getenv("DB_PORT", 5432))
        )
    except psycopg2.Error as e:
        st.error(f"❌ PostgreSQL connection error: {e}")
        return None
    
def get_market_price(symbol="BTCINR"):
    try:
        res = requests.get(f"{BASE_URL}/exchange/ticker")
        data = res.json()
        for ticker in data:
            if ticker["market"] == symbol:
                return float(ticker["last_price"])
    except Exception as e:
        return None
    
def cd_get_market_price(symbol: str = "BTCINR") -> float | None:
    """Return last traded price as float from public ticker list."""
    try:
        r = requests.get(f"{BASE_URL}/exchange/ticker", timeout=10)
        data = r.json()
        for t in data:
            if t.get("market") == symbol:
                return float(t.get("last_price"))
        return None
    except Exception as e:
        st.error(f"❌ Price fetch failed: {e}")
        return None

def get_paytm_wallet_balance():
    url = "https://securegw.paytm.in/wallet-web/checkBalance"

    payload = {
        "mid": os.getenv("PAYTM_MID"),
        "orderId": "BALCHECK_" + datetime.now().strftime("%Y%m%d%H%M%S")
    }

    # ✅ Generate checksum with merchant key
    checksum = generate_signature(payload, os.getenv("PAYTM_MERCHANT_KEY"))

    headers = {
        "Content-Type": "application/json",
        "x-mid": os.getenv("PAYTM_MID"),
        "x-checksum": checksum,
    }

    response = requests.post(url, json=payload, headers=headers, timeout=10)
    data = response.json()

    if data.get("status") == "SUCCESS":
        return float(data["walletBalance"])
    else:
        raise Exception("Paytm API error: " + str(data))

# ==============================
# 🔹 DB Sync Logic
# ==============================
def sync_inr_wallet(mode="LIVE"):
    """
    Syncs INR wallet balance from Paytm API to database.
    """
    try:
        live_balance = get_paytm_wallet_balance()

        conn = get_mysql_connection()
        cursor = conn.cursor()

        # Insert sync transaction
        cursor.execute("""
            INSERT INTO inr_wallet_transactions
            (trade_type, amount, balance_after, status, mode)
            VALUES (%s, %s, %s, %s, %s)
        """, ("SYNC", 0, live_balance, "SUCCESS", mode))

        conn.commit()
        conn.close()

        print(f"✅ INR Wallet synced → Balance: ₹{live_balance:.2f}")
        return live_balance

    except Exception as e:
        print("❌ Sync Error:", e)
        return None
    
def init_mysql_tables():
    conn = get_mysql_connection()
    if not conn:
        return
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # inr_wallet_transactions
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS inr_wallet_transactions (
        id SERIAL PRIMARY KEY,
        trade_time TIMESTAMP,
        action VARCHAR(50),
        amount DOUBLE PRECISION,
        balance_after DOUBLE PRECISION,
        trade_mode VARCHAR(10) DEFAULT 'TEST',
        payment_id VARCHAR(255),
        status VARCHAR(20) DEFAULT 'PENDING',
        reversal_id VARCHAR(50) DEFAULT '',
        razorpay_order_id VARCHAR(50) DEFAULT ''
    )
    """)

    # live_trades
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS live_trades (
        id SERIAL PRIMARY KEY,
        trade_time TIMESTAMP,
        order_id VARCHAR(50),
        action VARCHAR(10),
        amount DOUBLE PRECISION,
        price DOUBLE PRECISION,
        status VARCHAR(20) DEFAULT 'PENDING',
        profit DOUBLE PRECISION DEFAULT 0,
        reason VARCHAR(50)
    )
    """)

    # payout_logs
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS payout_logs (
        id SERIAL PRIMARY KEY,
        recipient_name VARCHAR(100),
        method VARCHAR(10) CHECK (method IN ('bank','upi')),
        fund_account_id VARCHAR(50),
        amount NUMERIC(10,2) DEFAULT 0,
        status VARCHAR(50) DEFAULT 'PENDING',
        razorpay_payout_id VARCHAR(50),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # razorpay_payment_log
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS razorpay_payment_log (
        id SERIAL PRIMARY KEY,
        order_id VARCHAR(100),
        customer_id VARCHAR(50),
        name VARCHAR(50),
        method VARCHAR(50),
        account_number VARCHAR(50),
        ifsc VARCHAR(50),
        upi_id VARCHAR(50),
        amount NUMERIC(10,2) DEFAULT 0,
        status VARCHAR(100) DEFAULT 'PENDING',
        response VARCHAR(100),
        credited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        retry_count INT DEFAULT 0,
        last_attempt_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # saved_recipients
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS saved_recipients (
        id SERIAL PRIMARY KEY,
        name VARCHAR(100),
        method VARCHAR(20),
        account_number VARCHAR(50),
        ifsc VARCHAR(20),
        upi_id VARCHAR(50)
    )
    """)

    # saved_upi_recipients
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS saved_upi_recipients (
        id SERIAL PRIMARY KEY,
        name VARCHAR(100),
        email VARCHAR(100),
        phone VARCHAR(20),
        upi_id VARCHAR(100),
        contact_id VARCHAR(50),
        fund_account_id VARCHAR(50)
    )
    """)

    # user_wallets
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_wallets (
        id SERIAL PRIMARY KEY,
        user_email VARCHAR(100) NOT NULL,
        inr_balance NUMERIC(10,2) DEFAULT 0.00,
        customer_id VARCHAR(255) NOT NULL
    )
    """)

    # wallet_history
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS wallet_history (
        id SERIAL PRIMARY KEY,
        trade_date DATE,
        start_balance DOUBLE PRECISION DEFAULT 0,
        end_balance DOUBLE PRECISION DEFAULT 0,
        current_inr_value DOUBLE PRECISION DEFAULT 0,
        trade_count INT DEFAULT 0,
        auto_start_price DOUBLE PRECISION DEFAULT 0,
        auto_end_price DOUBLE PRECISION DEFAULT 0,
        auto_profit DOUBLE PRECISION DEFAULT 0,
        total_deposit_inr DOUBLE PRECISION DEFAULT 0,
        total_btc_received DOUBLE PRECISION DEFAULT 0,
        total_btc_sent DOUBLE PRECISION DEFAULT 0,
        profit_inr DOUBLE PRECISION DEFAULT 0,
        mode VARCHAR(10) DEFAULT 'TEST'

    )
    """)

    # wallet_transactions
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS wallet_transactions (
        id SERIAL PRIMARY KEY,
        trade_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        action VARCHAR(20),
        amount DOUBLE PRECISION DEFAULT 0,
        balance_after DOUBLE PRECISION DEFAULT 0,
        inr_value DOUBLE PRECISION DEFAULT 0,
        trade_type VARCHAR(200) DEFAULT 'MANUAL',
        autotrade_active BOOLEAN DEFAULT FALSE,
        status VARCHAR(20) DEFAULT 'PENDING',
        reversal_id VARCHAR(50) DEFAULT '',
        is_autotrade_marker BOOLEAN DEFAULT FALSE,
        last_price DOUBLE PRECISION DEFAULT 0,
        mode VARCHAR(10) DEFAULT 'TEST'
    )
    """)

    conn.commit()
    cursor.close()
    conn.close()
    st.success("✅ PostgreSQL tables initialized successfully!")
    
# init_mysql_tables()

def migrate_postgres_tables():
    conn = get_mysql_connection()
    if not conn:
        return
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # 🔹 inr_wallet_transactions
    cursor.execute("ALTER TABLE inr_wallet_transactions ALTER COLUMN status SET DEFAULT 'PENDING';")
    cursor.execute("ALTER TABLE inr_wallet_transactions ALTER COLUMN reversal_id SET DEFAULT '';")
    cursor.execute("ALTER TABLE inr_wallet_transactions ALTER COLUMN razorpay_order_id SET DEFAULT '';")
    cursor.execute("ALTER TABLE inr_wallet_transactions ADD COLUMN IF NOT EXISTS mode VARCHAR(10) DEFAULT 'TEST';")

    # 🔹 live_trades
    cursor.execute("ALTER TABLE live_trades ALTER COLUMN status SET DEFAULT 'PENDING';")
    cursor.execute("ALTER TABLE live_trades ALTER COLUMN profit SET DEFAULT 0;")

    # 🔹 payout_logs
    cursor.execute("ALTER TABLE payout_logs ALTER COLUMN status SET DEFAULT 'PENDING';")
    cursor.execute("ALTER TABLE payout_logs ALTER COLUMN amount SET DEFAULT 0;")
    cursor.execute("ALTER TABLE payout_logs ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP;")

    # 🔹 razorpay_payment_log
    cursor.execute("ALTER TABLE razorpay_payment_log ALTER COLUMN status SET DEFAULT 'PENDING';")
    cursor.execute("ALTER TABLE razorpay_payment_log ALTER COLUMN amount SET DEFAULT 0;")
    cursor.execute("ALTER TABLE razorpay_payment_log ALTER COLUMN retry_count SET DEFAULT 0;")
    cursor.execute("ALTER TABLE razorpay_payment_log ALTER COLUMN last_attempt_time SET DEFAULT CURRENT_TIMESTAMP;")

    # 🔹 wallet_history
    cursor.execute("ALTER TABLE wallet_history ALTER COLUMN start_balance SET DEFAULT 0;")
    cursor.execute("ALTER TABLE wallet_history ALTER COLUMN end_balance SET DEFAULT 0;")
    cursor.execute("ALTER TABLE wallet_history ALTER COLUMN current_inr_value SET DEFAULT 0;")
    cursor.execute("ALTER TABLE wallet_history ALTER COLUMN trade_count SET DEFAULT 0;")
    cursor.execute("ALTER TABLE wallet_history ALTER COLUMN auto_start_price SET DEFAULT 0;")
    cursor.execute("ALTER TABLE wallet_history ALTER COLUMN auto_end_price SET DEFAULT 0;")
    cursor.execute("ALTER TABLE wallet_history ALTER COLUMN auto_profit SET DEFAULT 0;")
    cursor.execute("ALTER TABLE wallet_history ALTER COLUMN total_deposit_inr SET DEFAULT 0;")
    cursor.execute("ALTER TABLE wallet_history ALTER COLUMN total_btc_received SET DEFAULT 0;")
    cursor.execute("ALTER TABLE wallet_history ALTER COLUMN total_btc_sent SET DEFAULT 0;")
    cursor.execute("ALTER TABLE wallet_history ALTER COLUMN profit_inr SET DEFAULT 0;")

    # 🔹 wallet_transactions
    cursor.execute("ALTER TABLE wallet_transactions ALTER COLUMN status SET DEFAULT 'PENDING';")
    cursor.execute("ALTER TABLE wallet_transactions ALTER COLUMN reversal_id SET DEFAULT '';")
    cursor.execute("ALTER TABLE wallet_transactions ALTER COLUMN amount SET DEFAULT 0;")
    cursor.execute("ALTER TABLE wallet_transactions ALTER COLUMN balance_after SET DEFAULT 0;")
    cursor.execute("ALTER TABLE wallet_transactions ALTER COLUMN inr_value SET DEFAULT 0;")
    cursor.execute("ALTER TABLE wallet_transactions ALTER COLUMN last_price SET DEFAULT 0;")
    cursor.execute("ALTER TABLE wallet_transactions ALTER COLUMN trade_time SET DEFAULT CURRENT_TIMESTAMP;")
    cursor.execute("ALTER TABLE wallet_transactions ALTER COLUMN autotrade_active TYPE BOOLEAN USING (autotrade_active::INTEGER <> 0);")
    cursor.execute("ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS mode VARCHAR(10) DEFAULT 'TEST';")
    cursor.execute("ALTER TABLE wallet_transactions ALTER COLUMN is_autotrade_marker TYPE BOOLEAN USING (is_autotrade_marker::INTEGER <> 0);")

#------ Temprary Cleaning purpose ------##
    cursor.execute("TRUNCATE wallet_history;")
    cursor.execute("TRUNCATE wallet_transactions;")
    cursor.execute("TRUNCATE inr_wallet_transactions;")
    cursor.execute("TRUNCATE user_wallets;")

    conn.commit()
    cursor.close()
    conn.close()
    st.success("✅ Migration completed! All tables updated with safe defaults.")

migrate_postgres_tables()

def get_btc_price():
    """
    Return BTC price as float. Handles float or dict return from get_market_price.
    """
    try:
        data = get_market_price("BTCUSDT")
        if isinstance(data, dict):
            return float(data.get("price") or data.get("last_price") or data.get("last"))
        elif isinstance(data, (int, float, str)):
            return float(data)
        else:
            return None
    except Exception as e:
        st.error(f"Price fetch failed: {e}")
        return None

def get_btc_price_bak_25_09_2025():
    try:
        # data = client.get_symbol_ticker(symbol="BTCUSDT")
        data = get_market_price("BTCINR")
        return float(data['price'])
    except Exception as e:
        st.error(f"Price fetch failed: {e}")
        return None

def usd_to_inr(usd):
    try:
        response = requests.get("https://api.exchangerate-api.com/v4/latest/USD")
        return usd * response.json()['rates']['INR']
    except:
        return usd * 83.0


# --- Simulated/Real Wallet Balances ---
def get_last_inr_balance(mode: str | None = None):
    """
    Returns (balance_after: float, ts: float|None) from inr_wallet_transactions.
    Column is named trade_mode (not mode).
    """
    if mode is None:
        mode = "LIVE" if REAL_TRADING else "TEST"

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT balance_after, EXTRACT(EPOCH FROM trade_time) AS ts
            FROM inr_wallet_transactions
            WHERE status = 'SUCCESS' AND trade_mode = %s
            ORDER BY trade_time DESC
            LIMIT 1
        """, (mode,))
        row = cursor.fetchone()
        if row:
            return float(row.get("balance_after") or 0.0), float(row.get("ts") or 0.0)

        # No INR tx found → default
        return (10000.0, None) if not REAL_TRADING else (0.0, None)
    finally:
        conn.close()

def get_last_inr_balance_bak_25_09_2025(mode: str):
    """
    Returns the last INR balance and trade_time for the given mode.
    Always returns a tuple: (balance: float, trade_time: float or None)
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT balance_after, EXTRACT(EPOCH FROM trade_time) AS ts
            FROM inr_wallet_transactions
            WHERE status = 'SUCCESS' AND mode = %s
            ORDER BY trade_time DESC
            LIMIT 1
        """, (mode,))
        row = cursor.fetchone()
        if row:
            balance = float(row.get("balance_after") or 0.0)
            ts = float(row.get("ts") or 0.0)
            return balance, ts
        else:
            return 10000.0, None
    finally:
        conn.close()

# inr_balance, _ = get_last_inr_balance(mode)
# inr_balance, _ = get_last_inr_balance(mode="LIVE" if REAL_TRADING else "TEST")
if REAL_TRADING:
    inr_balance, _ = sync_inr_wallet("LIVE")
else:
    inr_balance, _ = get_last_inr_balance(mode="TEST")

# Enforce fallback at session initialization
if not inr_balance or inr_balance <= 0:
    inr_balance = 10000.0 if not REAL_TRADING else 0.0   # Default only in TEST

# INR_WALLET = {"balance": get_last_inr_balance()}
INR_WALLET = {"balance": inr_balance}
# INR_WALLET =  {"balance": 10000.00}
# INR_WALLET = {"balance": get_razorpay_balance()}


def send_telegram_alert(message):
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": message}
            )
        except Exception as e:
            print("Telegram Error:", e)

# Background monitor thread for Render downtime or 500 hour usage
def start_background_monitor():
    MONITOR_URL = os.getenv("RENDER_APP_URL", "https://btc-autotrade-paytm.onrender.com")
    CHECK_INTERVAL = 600  # 10 minutes
    HOURS_LIMIT = 500
    max_checks = int((HOURS_LIMIT * 3600) / CHECK_INTERVAL)

    def monitor():
        up_count = 0
        while up_count < max_checks:
            try:
                r = requests.get(MONITOR_URL, timeout=10)
                if r.status_code == 200:
                    up_count += 1
                    if up_count == max_checks:
                        send_telegram("⚠️ Render usage reached 500 hours. Upgrade needed.")
                else:
                    raise Exception(f"App responded with status {r.status_code}")
            # except:
                # send_telegram("🚨 ALERT: Render app appears DOWN!")
            except Exception as e:
                send_telegram(f"🚨 ALERT: Render app appears DOWN! Error: {e}")
            time.sleep(CHECK_INTERVAL)

    t = threading.Thread(target=monitor, daemon=True)
    t.start()

# Call this once at the top of your Streamlit app after dotenv and env loading
start_background_monitor()

def get_current_inr_balance():
    conn = get_mysql_connection(); c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT balance_after FROM inr_wallet_transactions ORDER BY trade_time DESC LIMIT 1")
    row = c.fetchone(); conn.close()
    # return row['balance_after'] if row else 0.0
    return float(row['balance_after']) if row and row['balance_after'] is not None else 0.0
    # return row[0] if row else 0.0

# app = Flask(__name__)

# --- Streamlit Setup ---
st.set_page_config("BTC Autotrade Pro", layout="wide")

# --- Mode Selection ---
st.session_state.setdefault("REAL_TRADING", False)
REAL_TRADING = st.radio("Mode", ["Test", "Live"], index=1 if st.session_state.REAL_TRADING else 0, horizontal=True) == "Live"
st.session_state.REAL_TRADING = REAL_TRADING

if "inr_balance" not in st.session_state:
    st.session_state["inr_balance"] = get_current_inr_balance()

# File paths
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
PRICE_LOG = os.path.join(LOG_DIR, "price_log.csv")
TRADE_LOG = os.path.join(LOG_DIR, "trade_log.csv")
BTC_WALLET_NAME = "btc_autotrade_live"

# 🔐 Paytm Credentials

# TEST_MID = os.getenv("TEST_MID", "")
# TEST_KEY = os.getenv("TEST_KEY", "")  # Paytm staging default key
# TEST_WEBSITE = os.getenv("TEST_WEBSITE", "")
# TEST_CALLBACK = os.getenv("TEST_CALLBACK", "")
# TEST_BASE_URL = os.getenv("TEST_BASE_URL", "")

# LIVE_MID = os.getenv("LIVE_MID", "")
# LIVE_KEY = os.getenv("LIVE_KEY", "")
# LIVE_WEBSITE = os.getenv("LIVE_WEBSITE", "")
# LIVE_CALLBACK = os.getenv("LIVE_CALLBACK", "")
# LIVE_BASE_URL = os.getenv("LIVE_BASE_URL", "")

# PAYTM_CLIENT_ID = os.getenv("PAYTM_CLIENT_ID", "")
# PAYTM_CLIENT_SECRET = os.getenv("PAYTM_CLIENT_SECRET", "")
# OAUTH_URL = os.getenv("OAUTH_URL", "")
# PAYOUT_URL = os.getenv("PAYOUT_URL", "")

# if REAL_TRADING:
#     PAYTM_MID = LIVE_MID
#     PAYTM_MERCHANT_KEY = LIVE_KEY
#     PAYTM_WEBSITE = LIVE_WEBSITE
#     PAYTM_CALLBACK_URL = LIVE_CALLBACK
#     PAYTM_BASE_URL = LIVE_BASE_URL
# else:
#     PAYTM_MID = TEST_MID
#     PAYTM_MERCHANT_KEY = TEST_KEY
#     PAYTM_WEBSITE = TEST_WEBSITE
#     PAYTM_CALLBACK_URL = TEST_CALLBACK
#     PAYTM_BASE_URL = TEST_BASE_URL

# -----------------------------
# UroPay configuration
# -----------------------------
UROPAY_API_KEY = os.getenv("UROPAY_API_KEY", "")
UROPAY_API_SECRET = os.getenv("UROPAY_API_SECRET", "")
UROPAY_WEBHOOK_SECRET = os.getenv("UROPAY_WEBHOOK_SECRET", "")  # HMAC secret for webhook verification
UROPAY_API_BASE = os.getenv("UROPAY_API_BASE", "https://api.uropay.me")  # placeholder
REAL_TRADING = os.getenv("REAL_TRADING", "False").lower() in ("1", "true", "yes")

   
CUSTOMER_ID = os.getenv("CUSTOMER_ID", "")
# --- MySQL Connection ---


def save_bank_recipient(name, email, phone, ifsc, acc_number, contact_id, fund_account_id):
    with get_mysql_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO saved_recipients
                (name, email, phone, ifsc, account_number, contact_id, fund_account_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (name, email, phone, ifsc, acc_number, contact_id, fund_account_id))
        conn.commit()

def save_upi_recipient(name, email, phone, upi_id, contact_id, fund_account_id):
    with get_mysql_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO saved_upi_recipients
                (name, email, phone, upi_id, contact_id, fund_account_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (name, email, phone, upi_id, contact_id, fund_account_id))
        conn.commit()

def load_saved_recipients():
    with get_mysql_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM saved_recipients")
            return cur.fetchall()

def load_saved_upi_recipients():
    with get_mysql_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM saved_upi_recipients")
            return cur.fetchall()

def log_payout(name, method, fund_account_id, amount, status, payout_id):
    with get_mysql_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO payout_logs
                (recipient_name, method, fund_account_id, amount, status, razorpay_payout_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (name, method, fund_account_id, amount, status, payout_id))
        conn.commit()


def get_daily_wallet_summary():
    conn = get_mysql_connection(); c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("""
        SELECT DATE(trade_time) as day,
            SUM(CASE WHEN action='DEPOSIT' THEN amount ELSE 0 END) AS deposits,
            SUM(CASE WHEN action IN ('WITHDRAWAL') THEN amount ELSE 0 END) AS withdrawals,
            SUM(CASE WHEN action='DEPOSIT_FAILED' THEN 1 ELSE 0 END) AS failed_deposits
        FROM inr_wallet_transactions
        GROUP BY DATE(trade_time)
        ORDER BY day DESC
        LIMIT 7
    """)
    result = c.fetchall(); conn.close()
    return result

def check_balance_health():
    conn = get_mysql_connection(); c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    # Get latest 2 balances
    c.execute("""
        SELECT balance_after FROM inr_wallet_transactions
        WHERE action IN ('DEPOSIT', 'WITHDRAWAL')
        ORDER BY trade_time DESC LIMIT 2
    """)
    rows = c.fetchall(); conn.close()

    if len(rows) == 2:
        diff = rows[0]['balance_after'] - rows[1]['balance_after']
        if diff > 1000:  # You can adjust threshold
            st.warning(f"⚠️ Sudden balance drop detected: ₹{diff:.2f}")

def count_failed_refunds():
    conn = get_mysql_connection(); c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("""
        SELECT COUNT(*) as failures FROM inr_wallet_transactions
        WHERE status='FAILED' AND action='DEPOSIT_FAILED'
        AND trade_time >= CURDATE()
    """)
    count = c.fetchone()["failures"]; conn.close()
    if count > 0:
        st.error(f"❌ {count} failed deposits/refunds today!")
    
# --- Log Withdrawal ---
def log_withdrawal_old(amount):
    if not REAL_TRADING:
        test_bal = st.session_state.get("test_inr_balance", 5000.0)
        st.session_state["test_inr_balance"] = test_bal - amount
        return

    conn = get_mysql_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT balance_after FROM inr_wallet_transactions ORDER BY trade_time DESC LIMIT 1")
    row = cursor.fetchone()
    current_balance = row['balance_after'] if row else 0
    new_balance = current_balance - amount

    cursor.execute("""
        INSERT INTO inr_wallet_transactions
        (trade_time, action, amount, balance_after, mode, payment_id, status)
        VALUES (NOW(), %s, %s, %s, %s, %s, %s)
    """, ("WITHDRAWAL", -amount, new_balance, "LIVE", None, "COMPLETED"))
    conn.commit()
    conn.close()

def get_latest_inr_balance():
    if not REAL_TRADING:
        return st.session_state.get("test_inr_balance", 5000.0)
    conn = get_mysql_connection(); c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT balance_after FROM inr_wallet_transactions WHERE status='COMPLETED' ORDER BY trade_time DESC LIMIT 1")
    row = c.fetchone(); conn.close()
    # return float(row['balance_after']) if row else 0.0
    # return float(row[0]) if row else 0.0
    return float(row['balance_after']) if row and row['balance_after'] is not None else 0.0
    

def generate_qr_code_old(data):
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(data); qr.make(fit=True)
    img = qr.make_image(fill="black", back_color="white")
    buf = BytesIO(); img.save(buf, format="PNG")
    return buf.getvalue()

def credit_inr_wallet(amount, payment_id):
    conn = get_mysql_connection(); c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT COUNT(1) as cnt FROM inr_wallet_transactions WHERE payment_id=%s", (payment_id,))
    if c.fetchone()['cnt']>0: return conn.close()
    c.execute("SELECT balance_after FROM inr_wallet_transactions ORDER BY trade_time DESC LIMIT 1")
    # row = c.fetchone(); balance = row['balance_after'] if row else 0
    # row = c.fetchone(); balance = row[0] if row else 0
    row = c.fetchone(); balance = row['balance_after'] if row and row['balance_after'] is not None else 0
    new_balance = balance + amount
    c.execute("""
        INSERT INTO inr_wallet_transactions
        (trade_time,action,amount,balance_after,mode,payment_id,status)
        VALUES (NOW(),'DEPOSIT',%s,%s,'LIVE',%s,'COMPLETED')
    """, (amount, new_balance, payment_id))
    conn.commit(); conn.close()

def reverse_inr_wallet(amount, payment_id):
    conn = get_mysql_connection(); c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("""
        INSERT INTO inr_wallet_transactions
        (trade_time,action,amount,balance_after,mode,payment_id,status)
        VALUES (NOW(),'DEPOSIT_FAILED',0,0,'LIVE',%s,'FAILED')
    """, (payment_id,))
    conn.commit(); conn.close()


# --- QR Code Generator ---
# def generate_qr_code_15_11_2025(data: str):
def generate_qr_code(data: str) -> bytes:
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return buffer.getvalue()

def make_order_id(prefix="UROPAY"):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"

# -----------------------------
# Create UroPay payment link
# -----------------------------
def create_uropay_payment_link(amount_inr: float, customer_id: str = None, description: str = None):
    """
    Returns dict:
      { link, order_id, qr (png bytes), mode, raw_resp (if LIVE) }
    TEST mode returns a localhost simulate_deposit link that credits immediately when called.
    LIVE mode calls UroPay API (adjust endpoint/payload per UroPay docs).
    """
    amount = float(amount_inr)
    order_id = make_order_id()

    if not REAL_TRADING:
        # Simulated link for local dev (Streamlit default port 8501)
        simulation_url = f"http://localhost:5001/simulate_deposit?order_id={order_id}&amount={amount:.2f}"
        qr = generate_qr_code(simulation_url)
        return {"link": simulation_url, "order_id": order_id, "qr": qr, "mode": "TEST"}

    # LIVE mode: call UroPay API (placeholder endpoint/payload — update per UroPay docs)
    url = f"{UROPAY_API_BASE}/v1/payment_links"  # change if necessary
    payload = {
        "order_id": order_id,
        "amount": float(amount),
        "currency": "INR",
        "description": description or f"Deposit INR {amount:.2f}",
        "customer_id": customer_id or "",
    }
    headers = {
        "Content-Type": "application/json",
    }
    # If API key required, include it
    if UROPAY_API_KEY:
        headers["Authorization"] = f"Bearer {UROPAY_API_KEY}"

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        link = data.get("link") or data.get("url") or data.get("payment_url")
        if not link:
            raise Exception("UroPay returned no payment link: " + str(data))
        qr = generate_qr_code(link)
        return {"link": link, "order_id": order_id, "qr": qr, "mode": "LIVE", "raw_resp": data}
    except Exception as e:
        raise Exception(f"UroPay create link failed: {e}")

# -----------------------------
# Webhook verification
# -----------------------------
def verify_uropay_webhook(signature_header: str, payload_bytes: bytes) -> bool:
    """
    Default: HMAC-SHA256 using UROPAY_WEBHOOK_SECRET over the raw request body.
    signature_header may be "sha256=<hex>" or hex string.
    If UROPAY_WEBHOOK_SECRET is empty, verification fails (safe default).
    """
    if not UROPAY_WEBHOOK_SECRET:
        return False
    try:
        computed = hmac.new(UROPAY_WEBHOOK_SECRET.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        sig = signature_header or ""
        if sig.startswith("sha256="):
            sig = sig.split("=", 1)[1]
        return hmac.compare_digest(computed, sig)
    except Exception:
        return False
    
# If any of those functions are missing at import-time, define safe placeholders to avoid runtime errors.
def _safe_credit_inr_wallet(amount, payment_id):
    try:
        credit_inr_wallet(amount, payment_id)
    except NameError:
        # placeholder behavior: write to DB directly
        conn = get_mysql_connection()
        if conn:
            with conn.cursor() as cur:
                cur.execute("SELECT balance_after FROM inr_wallet_transactions ORDER BY trade_time DESC LIMIT 1")
                row = cur.fetchone()
                balance = float(row[0]) if row and row[0] is not None else 0.0
                new_balance = balance + amount
                cur.execute("""
                    INSERT INTO inr_wallet_transactions
                    (trade_time, action, amount, balance_after, trade_mode, payment_id, status)
                    VALUES (NOW(), 'DEPOSIT', %s, %s, 'LIVE', %s, 'COMPLETED')
                """, (amount, new_balance, payment_id))
                conn.commit()
                conn.close()

def _safe_log_inr_transaction(action, amount, balance, mode="LIVE"):
    try:
        log_inr_transaction(action, amount, balance, mode)
    except NameError:
        # fallback: insert minimally
        conn = get_mysql_connection()
        if conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO inr_wallet_transactions
                    (trade_time, action, amount, balance_after, trade_mode, payment_id, status)
                    VALUES (NOW(), %s, %s, %s, %s, %s, %s)
                """, (action, amount, balance, mode, None, "SUCCESS"))
                conn.commit()
                conn.close()

def _safe_get_current_inr_balance():
    try:
        return get_current_inr_balance()
    except NameError:
        conn = get_mysql_connection()
        if not conn:
            return 0.0
        cur = conn.cursor()
        cur.execute("SELECT balance_after FROM inr_wallet_transactions ORDER BY trade_time DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if row:
            try:
                return float(row[0])
            except Exception:
                return 0.0
        return 0.0

# --- Wallet Setup ---

def get_autotrade_active_from_db() -> bool:
    """
    Return True if the last auto-trade marker row indicates AUTO_TRADE_START.
    This checks the 'trade_type' marker for robustness.
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT trade_type
            FROM wallet_transactions
            WHERE trade_type IN ('AUTO_TRADE_START', 'AUTO_TRADE_STOP')
            ORDER BY trade_time DESC
            LIMIT 1
        """)
        row = cursor.fetchone()
        return bool(row and row.get('trade_type') == 'AUTO_TRADE_START')
    finally:
        conn.close()

def get_last_wallet_balance(mode: str | None = None):
    """
    Returns (balance_after: float, ts: float|None).
    - mode optional: derived from REAL_TRADING if not provided
    - Prefers real trade rows (BUY/SELL/MANUAL), skips autotrade marker rows
    - Falls back to any last non-marker successful row
    """
    if mode is None:
        mode = "LIVE" if REAL_TRADING else "TEST"

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Prefer real trades
        cursor.execute("""
            SELECT balance_after, EXTRACT(EPOCH FROM trade_time) AS ts
            FROM wallet_transactions
            WHERE status = 'SUCCESS'
              AND mode = %s
              AND trade_type IN ('AUTO_BUY','AUTO_SELL','MANUAL_BUY','MANUAL_SELL')
              AND COALESCE(is_autotrade_marker, FALSE) = FALSE
            ORDER BY trade_time DESC
            LIMIT 1
        """, (mode,))
        row = cursor.fetchone()
        if row:
            return float(row.get("balance_after") or 0.0), float(row.get("ts") or 0.0)

        # Fallback
        cursor.execute("""
            SELECT balance_after, EXTRACT(EPOCH FROM trade_time) AS ts
            FROM wallet_transactions
            WHERE status = 'SUCCESS'
              AND mode = %s
              AND COALESCE(is_autotrade_marker, FALSE) = FALSE
            ORDER BY trade_time DESC
            LIMIT 1
        """, (mode,))
        row = cursor.fetchone()
        if row:
            return float(row.get("balance_after") or 0.0), float(row.get("ts") or 0.0)

        return 0.0, None
    finally:
        conn.close()

def get_last_wallet_balance_bak_25_09_2025(mode: str):
    """
    Returns the last BTC balance and trade_time for the given mode.
    Only considers real BTC trade events (BUY/SELL).
    Always returns a tuple: (balance: float, trade_time: float or None)
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT balance_after, EXTRACT(EPOCH FROM trade_time) AS ts
            FROM wallet_transactions
            WHERE status = 'SUCCESS'
              AND mode = %s
              AND trade_type IN ('AUTO_BUY','AUTO_SELL','MANUAL_BUY','MANUAL_SELL')
            ORDER BY trade_time DESC
            LIMIT 1
        """, (mode,))
        row = cursor.fetchone()
        if row:
            balance = float(row.get("balance_after") or 0.0)
            ts = float(row.get("ts") or 0.0)
            return balance, ts
        else:
            return 0.0, None
    finally:
        conn.close()


def get_last_wallet_balance_old(mode: str):
    """
    Returns the last BTC balance and trade_time for the given mode.
    Always returns a tuple: (balance: float, trade_time: float or None)
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT balance_after, EXTRACT(EPOCH FROM trade_time) AS ts
            FROM wallet_transactions
            WHERE status = 'SUCCESS' AND mode = %s
            ORDER BY trade_time DESC
            LIMIT 1
        """, (mode,))
        row = cursor.fetchone()
        if row:
            balance = float(row.get("balance_after") or 0.0)
            ts = float(row.get("ts") or 0.0)
            return balance, ts
        else:
            return 0.0, None
    finally:
        conn.close()

# ---------------- Wallet Initialization ----------------
print("REAL_TRADING =", REAL_TRADING)

# Derive mode based on REAL_TRADING flag
mode = "LIVE" if REAL_TRADING else "TEST"

if REAL_TRADING:
    try:
        wallet = Wallet(BTC_WALLET_NAME)
    except:
        wallet = Wallet.create(BTC_WALLET_NAME)
    BALANCE_BTC = wallet.balance() / 1e8
    last_trade_time = None
else:
    print(f"Fetching last wallet balance from DB (mode={mode})")
    BALANCE_BTC, last_trade_time = get_last_wallet_balance(mode)
    print(f"Test Balance: {BALANCE_BTC}, Last Trade Time: {last_trade_time}")

BTC_WALLET = {"balance": BALANCE_BTC}

# Initialize auto-trade state
if 'AUTO_TRADING' not in st.session_state:
    st.session_state.AUTO_TRADING = {
        "active": get_autotrade_active_from_db(),
        "last_price": 0,
        "sell_streak": 0
    }
if 'AUTO_TRADE_STATE' not in st.session_state:
    st.session_state.AUTO_TRADE_STATE = {
        "entry_price": None
    }

# Initialize autotrade toggle state
if 'autotrade_toggle' not in st.session_state:
    st.session_state.autotrade_toggle = False

def log_inr_transaction(action, amount, balance, mode="TEST"):
    """
    Insert an INR wallet transaction with type safety
    (avoids tuple/float mismatches).
    """
    # --- sanitize values ---
    try:
        amount = float(amount) if amount is not None else 0.0
    except Exception:
        amount = 0.0

    try:
        balance = float(balance) if balance is not None else 0.0
    except Exception:
        balance = 0.0

    conn = get_mysql_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        INSERT INTO inr_wallet_transactions 
        (trade_time, action, amount, balance_after, trade_mode, status)
        VALUES (NOW(), %s, %s, %s, %s, %s)
    """, (
        str(action),      # enforce str
        float(amount),    # enforce float
        float(balance),   # enforce float
        str(mode),        # enforce str ("TEST" or "LIVE")
        "SUCCESS"
    ))

    conn.commit()
    conn.close()


def send_telegram(message):
    if ENABLE_NOTIFICATIONS:
        try:
            requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", params={"chat_id": CHAT_ID, "text": message})
        except Exception as e:
            st.error(f"Telegram failed: {e}")

def log_wallet_transaction(action, amount, balance, price_inr, trade_type="MANUAL"):
    """
    Insert a wallet transaction with strict type safety
    (avoids tuple + float issues).
    """
    # --- sanitize values (force floats where needed) ---
    try:
        amount = float(amount) if amount is not None else 0.0
    except Exception:
        amount = 0.0

    try:
        balance = float(balance) if balance is not None else 0.0
    except Exception:
        balance = 0.0

    try:
        price_inr = float(price_inr) if price_inr is not None else 0.0
    except Exception:
        price_inr = 0.0

    # INR value after trade = balance * price_inr
    try:
        inr_value = float(balance) * float(price_inr)
    except Exception:
        inr_value = 0.0

    conn = get_mysql_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        INSERT INTO wallet_transactions 
        (trade_time, action, amount, balance_after, inr_value, trade_type, autotrade_active, status)
        VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s)
    """, (
        str(action),   # enforce str
        float(amount), # enforce float
        float(balance),
        float(inr_value),
        str(trade_type),
        bool(st.session_state.get("AUTO_TRADING", {}).get("active", False)),
        "SUCCESS"
    ))

    conn.commit()
    conn.close()


def update_wallet_daily_summary(start=False, auto_end=False):
    conn = get_mysql_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    today = datetime.now().strftime("%Y-%m-%d")
    price = get_btc_price()
    inr_price = usd_to_inr(price) if price else 0
    
    if start:
        cursor.execute("""
            INSERT INTO wallet_history 
            (trade_date, start_balance, end_balance, current_inr_value, trade_count, auto_start_price, auto_end_price, auto_profit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            today,
            BTC_WALLET['balance'],
            BTC_WALLET['balance'],
            BTC_WALLET['balance'] * inr_price,
            0,
            inr_price,
            0,   # auto_end_price
            0    # auto_profit
        ))
    else:
        cursor.execute("SELECT COUNT(*) AS cnt FROM wallet_transactions WHERE DATE(trade_time) = CURRENT_DATE", (today,))
        count_row = cursor.fetchone()
    #     count = list(count_row.values())[0] if count_row else 0
        # count = count_row[0] if count_row else 0
        count = count_row['cnt'] if count_row else 0   # ✅ use dict key instead of [0]
        cursor.execute("""
            UPDATE wallet_history
            SET end_balance = %s, current_inr_value = %s, trade_count = %s
            WHERE trade_date = %s
        """, (BTC_WALLET['balance'], BTC_WALLET['balance'] * inr_price, count, today))
    
    if auto_end:
        cursor.execute("SELECT auto_start_price FROM wallet_history WHERE trade_date = %s", (today,))
        start_price_row = cursor.fetchone()
        # start_price = list(start_price_row.values())[0] if start_price_row else 0
        # start_price = start_price_row[0] if start_price_row else 0
        start_price = start_price_row['auto_start_price'] if start_price_row and start_price_row['auto_start_price'] is not None else 0
        profit = BTC_WALLET['balance'] * (inr_price - start_price)
        cursor.execute("""
            UPDATE wallet_history 
            SET auto_end_price=%s, auto_profit=%s 
            WHERE trade_date=%s
        """, (inr_price, profit, today))
    
    conn.commit()
    conn.close()

def save_trade_log(trade_type, btc_amount, btc_balance, price_inr, roi=0):
    """Appends a trade entry to a date-based CSV log file with ROI tracking"""

    # Get today’s date for filename
    today_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"trade_log_{today_str}.csv"
    
    file_exists = os.path.isfile(filename)

    with open(filename, mode='a', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=[
            "timestamp", "trade_type", "btc_amount", "btc_balance", "price_inr", "roi"
        ])

        if not file_exists:
            writer.writeheader()

        writer.writerow({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trade_type": trade_type,
            "btc_amount": round(btc_amount, 6),
            "btc_balance": round(btc_balance, 6),
            "price_inr": round(price_inr, 2),
            "roi": round(roi, 2)
        })

    df = pd.DataFrame([roi])
    df.to_csv(TRADE_LOG, mode='a', index=False, header=not os.path.exists(TRADE_LOG))

def is_autotrade_active_from_db():
    """Checks the latest wallet transaction to determine if auto-trade is active"""
    conn = get_mysql_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute("""
                SELECT trade_type
                FROM wallet_transactions
                WHERE trade_type IN ('AUTO_TRADE_START', 'AUTO_TRADE_STOP')
                ORDER BY trade_time DESC
                LIMIT 1
            """)
            row = cursor.fetchone()
            return row is not None and row['balance_after'] == 'AUTO_TRADE_START'
            # return (row is not None) and (row[0] == 'AUTO_TRADE_START')
            # return row is not None and row[0] == 'AUTO_TRADE_START'
    finally:
        conn.close()

def get_latest_auto_start_price():
    conn = get_mysql_connection()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT auto_start_price FROM wallet_history ORDER BY trade_date DESC LIMIT 1")
    result = c.fetchone()
    conn.close()
    return float(result['auto_start_price']) if result and result['auto_start_price'] is not None else 0.0

def update_wallet_history_profit(profit, trade_date=None):
    """Update profit column after SELL in wallet_history"""
    if not trade_date:
        trade_date = datetime.now().strftime("%Y-%m-%d")
    conn = get_mysql_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        UPDATE wallet_history
        SET auto_profit = COALESCE(auto_profit, 0) + %s
        WHERE trade_date = %s
    """, (profit, trade_date))
    conn.commit()
    conn.close()

def get_last_trade_time_from_logs():
    """
    Get the most recent AUTO_* transaction time from wallet or INR logs.
    """
    conn = get_mysql_connection()
    if not conn:
        return None
    cur = conn.cursor()

    cur.execute("""
        SELECT MAX(trade_time) 
        FROM (
            SELECT trade_time FROM wallet_transactions WHERE trade_type LIKE 'AUTO_%'
            UNION ALL
            SELECT trade_time FROM inr_wallet_transactions WHERE action LIKE 'AUTO_%'
        ) AS combined
    """)
    row = cur.fetchone()
    conn.close()

    if row and row[0]:
        return row[0]
    return None

def get_last_trade_time_from_db():
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT trade_time
            FROM wallet_transactions
            ORDER BY trade_time DESC
            LIMIT 1
        """)
        row = cursor.fetchone()
        if row and row[0]:
            trade_time = row[0]

            # 🔹 Normalize type
            if isinstance(trade_time, float):  # stored as UNIX timestamp
                return datetime.datetime.fromtimestamp(trade_time)
            elif isinstance(trade_time, (int,)):  # if accidentally integer
                return datetime.datetime.fromtimestamp(trade_time)
            elif isinstance(trade_time, str):  # stored as string
                try:
                    return datetime.datetime.fromisoformat(trade_time)
                except ValueError:
                    return None
            else:
                return trade_time  # already a datetime
        return None
    finally:
        conn.close()

def get_last_auto_trade():
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT trade_type, trade_time
            FROM wallet_transactions
            WHERE trade_type IN ('AUTO_BUY', 'AUTO_SELL')
            ORDER BY trade_time DESC
            LIMIT 1
        """)
        row = cursor.fetchone()
        return row if row else None
    finally:
        conn.close()

def start_autotrade():
    """Safe start for auto-trading (does not reset balances)."""
    try:
        # --- Load balances from DB (mode-aware) ---
        btc_balance, _ = get_last_wallet_balance(mode="LIVE" if REAL_TRADING else "TEST")
        inr_balance, _ = get_last_inr_balance(mode="LIVE" if REAL_TRADING else "TEST")

        if btc_balance is None:
            btc_balance = 0.0
        if inr_balance is None:
            inr_balance = 0.0

        # --- Save into session ---
        st.session_state["BTC_WALLET"] = {"balance": float(btc_balance or 0.0)}
        st.session_state["INR_WALLET"] = {"balance": float(inr_balance or 0.0)}

        # --- Initialize AUTO_TRADING state ---
        st.session_state.AUTO_TRADING = {
            "active": True,
            "entry_price": 0.0,
            "last_price": 0.0,
            "last_trade": None
        }
        st.session_state["autotrade_toggle"] = True
        update_autotrade_status_db(1)

        # --- Log marker in DB without affecting balances ---
        log_wallet_transaction(
            action="AUTO_START",
            amount=0,
            balance=st.session_state["BTC_WALLET"]["balance"],
            price_inr=0,
            trade_type="AUTO_TRADE_START"
        )
        log_inr_transaction(
            action="AUTO_START",
            amount=0,
            balance=st.session_state["INR_WALLET"]["balance"],
            mode="LIVE" if REAL_TRADING else "TEST"
        )

        # --- Notify ---
        st.success(f"🚀 Auto-Trade ACTIVATED | INR ₹{inr_balance:,.2f} | ₿{btc_balance:.6f}")
        send_telegram(f"🚀 Auto-Trade ACTIVATED | INR ₹{inr_balance:,.2f} | ₿{btc_balance:.6f}")

    except Exception as e:
        st.error(f"❌ Failed to start Auto-Trade: {e}")
        send_telegram(f"❌ Failed to start Auto-Trade: {e}")
    
def start_autotrade_bak_25_09_2025():
    try:
        # ✅ Load balances from DB (mode-aware)
        btc_balance, _ = get_last_wallet_balance(mode="LIVE" if REAL_TRADING else "TEST")
        inr_balance, _ = get_last_inr_balance(mode="LIVE" if REAL_TRADING else "TEST")

        btc_balance = float(btc_balance or 0.0)
        inr_balance = float(inr_balance or 0.0)

        # ✅ Save into session safely
        st.session_state["BTC_WALLET"] = {"balance": btc_balance}
        st.session_state["INR_WALLET"] = {"balance": inr_balance}

        # ✅ Init AUTO_TRADING state
        st.session_state.AUTO_TRADING = {
            "active": True,
            "entry_price": 0,
            "last_price": 0,
            "last_trade": None
        }
        st.session_state["autotrade_toggle"] = True

        # ✅ Log marker in DB
        log_wallet_transaction(
            trade_type="AUTO_TRADE_START",
            inr_change=0,
            btc_change=0,
            remarks="Auto-trade started"
        )

        msg = f"🚀 Auto-Trade ACTIVATED at ₹{inr_balance:,.2f} | ₿{btc_balance:.6f}"
        st.success(msg); st.toast(msg); send_telegram(msg)

    except Exception as e:
        stop_autotrade(f"❌ Failed to start Auto-Trade: {e}")

def stop_autotrade(message: str):
    """Centralized stop logic (prevents balance reset)."""
    st.session_state.AUTO_TRADING["active"] = False
    st.session_state["autotrade_toggle"] = False
    update_autotrade_status_db(0)

    try:
        btc_bal, _ = get_last_wallet_balance(mode="LIVE" if REAL_TRADING else "TEST")
        inr_bal, _ = get_last_inr_balance(mode="LIVE" if REAL_TRADING else "TEST")
    except Exception:
        btc_bal = st.session_state.get("BTC_WALLET", {}).get("balance", 0.0)
        inr_bal = st.session_state.get("INR_WALLET", {}).get("balance", 0.0)

    st.session_state.BTC_WALLET = {"balance": float(btc_bal or 0.0)}
    st.session_state.INR_WALLET = {"balance": float(inr_bal or 0.0)}

    log_wallet_transaction("AUTO_STOP", 0, st.session_state.BTC_WALLET["balance"], 0, "AUTO_TRADE_STOP")
    log_inr_transaction("AUTO_STOP", 0, st.session_state.INR_WALLET["balance"], "LIVE" if REAL_TRADING else "TEST")
    update_wallet_daily_summary(auto_end=True)

    st.warning(message)
    send_telegram(message)

def stop_autotrade_bak_25_09_2025(message: str):
    """Centralized stop logic (prevents balance reset)."""
    st.session_state.AUTO_TRADING["active"] = False
    st.session_state["autotrade_toggle"] = False
    update_autotrade_status_db(0)

    # Reload balances safely
    btc_bal, _ = get_last_wallet_balance(mode="LIVE" if REAL_TRADING else "TEST")
    inr_bal, _ = get_last_inr_balance(mode="LIVE" if REAL_TRADING else "TEST")

    st.session_state.BTC_WALLET = {"balance": float(btc_bal or 0.0)}
    st.session_state.INR_WALLET = {"balance": float(inr_bal or 0.0)}

    log_wallet_transaction("AUTO_STOP", 0, btc_bal, 0, "AUTO_TRADE_STOP")
    log_inr_transaction("AUTO_STOP", 0, inr_bal, "LIVE" if REAL_TRADING else "TEST")
    update_wallet_daily_summary(auto_end=True)

    st.warning(message)
    send_telegram(message)
        
# ------------------ AUTO TRADE FUNCTION ------------------
def check_auto_trading(price_inr: float):
    """
    Full Auto-Trading Logic:
    - Strict BUY→SELL sequence (no duplicate/cumulative trades).
    - Auto-BUY if INR>0 and last trade was SELL/None.
    - Auto-SELL with multiple conditions (ROI, force, fallback).
    - Force-SELL if INR wallet is empty but BTC>0.
    - Idle timeout (30m), trade cooldown (60s).
    - Wallet balances synced with DB before/after trade.
    - Auto-stop + notifications on error or idle.
    """
    try:
        # --- Ensure AUTO_TRADING state in session ---
        if "AUTO_TRADING" not in st.session_state:
            st.session_state.AUTO_TRADING = {
                "active": True,
                "entry_price": 0,
                "last_price": 0,
                "last_trade": None
            }

        # --- Always refresh balances from DB (mode-aware) ---
        btc_balance, _ = get_last_wallet_balance(mode="LIVE" if REAL_TRADING else "TEST")
        inr_balance, _ = get_last_inr_balance(mode="LIVE" if REAL_TRADING else "TEST")

        btc_balance = float(btc_balance or 0.0)
        inr_balance = float(inr_balance or 0.0)

        st.session_state.BTC_WALLET = {"balance": btc_balance}
        st.session_state.INR_WALLET = {"balance": inr_balance}

         # --- Active check ---
        db_active = get_autotrade_active_from_db()
        session_active = st.session_state.AUTO_TRADING.get("active", False)
        autotrade_active = bool(db_active) or bool(session_active)
        if not autotrade_active:
            return

        # 🚨 Stop autotrade immediately if both wallets are empty
        # if btc_balance == 0 and inr_balance == 0:
        #     stop_autotrade("⏹️ Auto-Trade stopped: Both INR and BTC wallets are empty.")
        #     return
        
        if autotrade_active and btc_balance == 0 and inr_balance == 0:
            stop_autotrade("⏹️ Auto-Trade stopped: Both INR and BTC wallets are empty.")
            return

        # --- Idle timeout check (30m) ---
        idle_timeout = 1800
        last_trade_time = get_last_trade_time_from_db()
        if last_trade_time:
            if isinstance(last_trade_time, (int, float)):
                last_trade_time = datetime.fromtimestamp(last_trade_time)
            elif isinstance(last_trade_time, str):
                try:
                    last_trade_time = datetime.fromisoformat(last_trade_time)
                except ValueError:
                    last_trade_time = None

        if last_trade_time and isinstance(last_trade_time, datetime):
            if (datetime.now() - last_trade_time).total_seconds() > idle_timeout:
                stop_autotrade("⏳ Auto-Trade stopped (idle timeout: 30m no trades).")
                return

        # --- Trade cooldown check (60s) ---
        last_trade = get_last_auto_trade()
        last_type = last_trade.get("trade_type") if last_trade else None
        last_ts = 0
        if last_trade and last_trade.get("trade_time"):
            trade_time_val = last_trade["trade_time"]
            if isinstance(trade_time_val, datetime):
                last_ts = int(trade_time_val.timestamp())
            elif isinstance(trade_time_val, (int, float)):
                last_ts = int(trade_time_val)
            elif isinstance(trade_time_val, str):
                try:
                    last_ts = int(datetime.fromisoformat(trade_time_val).timestamp())
                except ValueError:
                    last_ts = 0

        now_ts = int(time.time())
        if last_type in ("AUTO_BUY", "AUTO_SELL") and (now_ts - last_ts < 60):
            return  # prevent duplicate fast trades

        # --- Restore last_trade state ---
        if st.session_state.AUTO_TRADING.get("last_trade") is None:
            if last_type == "AUTO_BUY":
                st.session_state.AUTO_TRADING["last_trade"] = "BUY"
            elif last_type == "AUTO_SELL":
                st.session_state.AUTO_TRADING["last_trade"] = "SELL"

        last_trade_state = st.session_state.AUTO_TRADING.get("last_trade")

        # --- Strategy params ---
        min_roi = 0.01   # 1%
        threshold = 5    # INR
        entry_price = float(st.session_state.AUTO_TRADING.get("entry_price", 0) or 0.0)
        last_price = float(st.session_state.AUTO_TRADING.get("last_price", 0) or 0.0)
        price_diff = price_inr - last_price if last_price else 0

        # === AUTO-BUY ===
        if inr_balance >= 20 and (last_trade_state is None or last_trade_state == "SELL"):
            buy_inr = inr_balance * 0.5
            btc_bought = buy_inr / price_inr

            st.session_state.BTC_WALLET["balance"] = btc_balance + btc_bought
            st.session_state.INR_WALLET["balance"] = inr_balance - buy_inr
            st.session_state.AUTO_TRADING["entry_price"] = price_inr
            st.session_state.AUTO_TRADING["last_price"] = price_inr
            st.session_state.AUTO_TRADING["last_trade"] = "BUY"
            update_last_auto_trade_price_db(price_inr)

            msg = f"🟢 Auto-BUY ₹{buy_inr:.2f} → {btc_bought:.6f} BTC @ ₹{price_inr:.2f}"
            st.success(msg); st.toast(msg); send_telegram(msg)
            log_wallet_transaction("AUTO_BUY", btc_bought, st.session_state.BTC_WALLET["balance"], price_inr, "AUTO_BUY")
            log_inr_transaction("AUTO_BUY", -buy_inr, st.session_state.INR_WALLET["balance"], "LIVE" if REAL_TRADING else "TEST")
            save_trade_log("AUTO_BUY", btc_bought, st.session_state.BTC_WALLET["balance"], price_inr)
            return

        # === AUTO-SELL CONDITIONS ===
        roi = ((price_inr - entry_price) / entry_price) * 100 if entry_price > 0 else None
        cond1 = (roi is not None and roi >= min_roi and price_diff >= threshold)
        cond2 = (inr_balance <= 0 and btc_balance > 0)
        cond3 = (entry_price == 0 and btc_balance > 0)

        if btc_balance >= 0.0001 and (last_trade_state == "BUY") and (cond1 or cond2 or cond3):
            sell_btc = btc_balance
            inr_received = sell_btc * price_inr

            st.session_state.BTC_WALLET["balance"] = 0.0
            st.session_state.INR_WALLET["balance"] = inr_balance + inr_received
            st.session_state.AUTO_TRADING["entry_price"] = 0
            st.session_state.AUTO_TRADING["last_price"] = price_inr
            st.session_state.AUTO_TRADING["last_trade"] = "SELL"
            update_last_auto_trade_price_db(price_inr)

            roi_text = f"{roi:.4f}%" if roi is not None else "N/A"
            msg = f"🔴 Auto-SELL {sell_btc:.6f} BTC → ₹{inr_received:.2f} @ ₹{price_inr:.2f} | ROI {roi_text}"
            st.warning(msg); st.toast(msg); send_telegram(msg)
            log_wallet_transaction("AUTO_SELL", sell_btc, 0, price_inr, "AUTO_SELL")
            log_inr_transaction("AUTO_SELL", inr_received, st.session_state.INR_WALLET["balance"], "LIVE" if REAL_TRADING else "TEST")
            save_trade_log("AUTO_SELL", sell_btc, 0, price_inr, roi if roi else 0.0)
            return

        # ⚠️ Debugging skipped sells
        if btc_balance > 0 and (last_trade_state == "BUY") and not (cond1 or cond2 or cond3):
            reasons = []
            if not cond1: reasons.append("ROI not met")
            if not cond2: reasons.append("INR not empty")
            if not cond3: reasons.append("entry_price set")
            st.info(f"💡 Sell skipped: {', '.join(reasons)} | BTC={btc_balance:.6f}, INR={inr_balance:.2f}")

        # Fallback to set last_price if missing
        if last_price == 0:
            st.session_state.AUTO_TRADING["last_price"] = price_inr
            update_last_auto_trade_price_db(price_inr)

    except Exception as e:
        st.session_state.AUTO_TRADING["active"] = False
        st.session_state["autotrade_toggle"] = False
        update_autotrade_status_db(0)

        # use mode-aware balance fetch in exception
        btc_bal, _ = get_last_wallet_balance(mode="LIVE" if REAL_TRADING else "TEST")
        inr_bal, _ = get_last_inr_balance(mode="LIVE" if REAL_TRADING else "TEST")

        # btc_bal, _ = get_last_wallet_balance()
        # inr_bal, _ = get_last_inr_balance()
        log_wallet_transaction("AUTO_STOP", 0, btc_bal, 0, "AUTO_TRADE_STOP")
        log_inr_transaction("AUTO_STOP", 0, inr_bal, "LIVE" if REAL_TRADING else "TEST")
        update_wallet_daily_summary(auto_end=True)

        error_msg = f"❌ Auto-Trade stopped (technical error): {str(e)}"
        st.error(error_msg); send_telegram(error_msg)

def check_auto_trading_on_28_09_2025(price_inr: float):
    """
    Full Auto-Trading Logic:
    - Strict BUY→SELL sequence (no duplicate/cumulative trades).
    - Auto-BUY if INR>0 and last trade was SELL/None.
    - Auto-SELL only if ROI ≥ 1% (safe profit) OR INR empty with positive ROI.
    - Force-SELL if INR wallet is empty but BTC>0 and ROI positive.
    - Idle timeout (30m), trade cooldown (60s).
    - Wallet balances synced with DB before/after trade.
    - Auto-stop + notifications on error or idle.
    """
    try:
        # --- Ensure AUTO_TRADING state in session ---
        if "AUTO_TRADING" not in st.session_state:
            st.session_state.AUTO_TRADING = {
                "active": True,
                "entry_price": 0,
                "last_price": 0,
                "last_trade": None
            }

        # --- Always refresh balances from DB (mode-aware) ---
        btc_balance, _ = get_last_wallet_balance(mode="LIVE" if REAL_TRADING else "TEST")
        inr_balance, _ = get_last_inr_balance(mode="LIVE" if REAL_TRADING else "TEST")

        btc_balance = float(btc_balance or 0.0)
        inr_balance = float(inr_balance or 0.0)

        st.session_state.BTC_WALLET = {"balance": btc_balance}
        st.session_state.INR_WALLET = {"balance": inr_balance}

        # --- Active check ---
        db_active = get_autotrade_active_from_db()
        session_active = st.session_state.AUTO_TRADING.get("active", False)
        autotrade_active = bool(db_active) or bool(session_active)
        if not autotrade_active:
            return

        # 🚨 Stop autotrade immediately if both wallets are empty
        if autotrade_active and btc_balance == 0 and inr_balance == 0:
            stop_autotrade("⏹️ Auto-Trade stopped: Both INR and BTC wallets are empty.")
            return

        # --- Idle timeout check (30m) ---
        idle_timeout = 1800
        last_trade_time = get_last_trade_time_from_db()
        if last_trade_time:
            if isinstance(last_trade_time, (int, float)):
                last_trade_time = datetime.fromtimestamp(last_trade_time)
            elif isinstance(last_trade_time, str):
                try:
                    last_trade_time = datetime.fromisoformat(last_trade_time)
                except ValueError:
                    last_trade_time = None

        if last_trade_time and isinstance(last_trade_time, datetime):
            if (datetime.now() - last_trade_time).total_seconds() > idle_timeout:
                stop_autotrade("⏳ Auto-Trade stopped (idle timeout: 30m no trades).")
                return

        # --- Trade cooldown check (60s) ---
        last_trade = get_last_auto_trade()
        last_type = last_trade.get("trade_type") if last_trade else None
        last_ts = 0
        if last_trade and last_trade.get("trade_time"):
            trade_time_val = last_trade["trade_time"]
            if isinstance(trade_time_val, datetime):
                last_ts = int(trade_time_val.timestamp())
            elif isinstance(trade_time_val, (int, float)):
                last_ts = int(trade_time_val)
            elif isinstance(trade_time_val, str):
                try:
                    last_ts = int(datetime.fromisoformat(trade_time_val).timestamp())
                except ValueError:
                    last_ts = 0

        now_ts = int(time.time())
        if last_type in ("AUTO_BUY", "AUTO_SELL") and (now_ts - last_ts < 60):
            return  # prevent duplicate fast trades

        # --- Restore last_trade state ---
        if st.session_state.AUTO_TRADING.get("last_trade") is None:
            if last_type == "AUTO_BUY":
                st.session_state.AUTO_TRADING["last_trade"] = "BUY"
            elif last_type == "AUTO_SELL":
                st.session_state.AUTO_TRADING["last_trade"] = "SELL"

        last_trade_state = st.session_state.AUTO_TRADING.get("last_trade")

        # --- Strategy params ---
        min_roi = 1.0   # ✅ 1% ROI required
        threshold = 5   # INR
        entry_price = float(st.session_state.AUTO_TRADING.get("entry_price", 0) or 0.0)
        last_price = float(st.session_state.AUTO_TRADING.get("last_price", 0) or 0.0)
        price_diff = price_inr - last_price if last_price else 0

        # === AUTO-BUY ===
        if inr_balance >= 20 and (last_trade_state is None or last_trade_state == "SELL"):
            buy_inr = inr_balance * 0.5
            btc_bought = buy_inr / price_inr

            st.session_state.BTC_WALLET["balance"] = btc_balance + btc_bought
            st.session_state.INR_WALLET["balance"] = inr_balance - buy_inr
            st.session_state.AUTO_TRADING["entry_price"] = price_inr
            st.session_state.AUTO_TRADING["last_price"] = price_inr
            st.session_state.AUTO_TRADING["last_trade"] = "BUY"
            update_last_auto_trade_price_db(price_inr)

            msg = f"🟢 Auto-BUY ₹{buy_inr:.2f} → {btc_bought:.6f} BTC @ ₹{price_inr:.2f}"
            st.success(msg); st.toast(msg); send_telegram(msg)
            log_wallet_transaction("AUTO_BUY", btc_bought, st.session_state.BTC_WALLET["balance"], price_inr, "AUTO_BUY")
            log_inr_transaction("AUTO_BUY", -buy_inr, st.session_state.INR_WALLET["balance"], "LIVE" if REAL_TRADING else "TEST")
            save_trade_log("AUTO_BUY", btc_bought, st.session_state.BTC_WALLET["balance"], price_inr)
            return

        # === AUTO-SELL CONDITIONS ===
        roi = ((price_inr - entry_price) / entry_price) * 100 if entry_price > 0 else None
        cond1 = (roi is not None and roi >= min_roi and price_diff >= threshold)  # ✅ ROI SELL
        cond2 = (inr_balance <= 0 and btc_balance > 0)                            # ✅ FORCE SELL
        cond3 = (entry_price == 0 and btc_balance > 0)                            # ✅ Fallback SELL

        if btc_balance >= 0.0001 and (last_trade_state == "BUY") and (cond1 or cond2 or cond3):
            sell_btc = btc_balance
            inr_received = sell_btc * price_inr

            st.session_state.BTC_WALLET["balance"] = 0.0
            st.session_state.INR_WALLET["balance"] = inr_balance + inr_received
            st.session_state.AUTO_TRADING["entry_price"] = 0
            st.session_state.AUTO_TRADING["last_price"] = price_inr
            st.session_state.AUTO_TRADING["last_trade"] = "SELL"
            update_last_auto_trade_price_db(price_inr)

            # ✅ Safe ROI formatting
            roi_text = f"{roi:.4f}%" if roi is not None else "N/A"

            msg = f"🔴 Auto-SELL {sell_btc:.6f} BTC → ₹{inr_received:.2f} @ ₹{price_inr:.2f} | ROI {roi_text}"
            st.warning(msg); st.toast(msg); send_telegram(msg)

            log_wallet_transaction("AUTO_SELL", sell_btc, 0, price_inr, "AUTO_SELL")
            log_inr_transaction("AUTO_SELL", inr_received, st.session_state.INR_WALLET["balance"],
                                "LIVE" if REAL_TRADING else "TEST")
            save_trade_log("AUTO_SELL", sell_btc, 0, price_inr, roi if roi else 0.0)
            return

        # ⚠️ Debugging skipped sells
        if btc_balance > 0 and last_trade_state == "BUY" and not (cond1 or cond2 or cond3):
            reasons = []
            if not cond1: reasons.append("ROI < 1% or threshold not met")
            if not cond2: reasons.append("INR not empty or ROI not positive")
            if not cond3: reasons.append("entry_price set")
            st.info(f"💡 Sell skipped: {', '.join(reasons)} | BTC={btc_balance:.6f}, INR={inr_balance:.2f}, ROI={roi:.2f if roi else 'N/A'}")

        # Fallback to set last_price if missing
        if last_price == 0:
            st.session_state.AUTO_TRADING["last_price"] = price_inr
            update_last_auto_trade_price_db(price_inr)

    except Exception as e:
        st.session_state.AUTO_TRADING["active"] = False
        st.session_state["autotrade_toggle"] = False
        update_autotrade_status_db(0)

        # use mode-aware balance fetch in exception
        btc_bal, _ = get_last_wallet_balance(mode="LIVE" if REAL_TRADING else "TEST")
        inr_bal, _ = get_last_inr_balance(mode="LIVE" if REAL_TRADING else "TEST")

        log_wallet_transaction("AUTO_STOP", 0, btc_bal, 0, "AUTO_TRADE_STOP")
        log_inr_transaction("AUTO_STOP", 0, inr_bal, "LIVE" if REAL_TRADING else "TEST")
        update_wallet_daily_summary(auto_end=True)

        error_msg = f"❌ Auto-Trade stopped (technical error): {str(e)}"
        st.error(error_msg); send_telegram(error_msg)

def check_auto_trading_bak_27_09_2025(price_inr: float):
    """
    Full Auto-Trading Logic:
    - Strict BUY→SELL sequence (no duplicate/cumulative trades).
    - Auto-BUY if INR>0 and last trade was SELL/None.
    - Auto-SELL with multiple conditions (ROI, force, fallback).
    - Force-SELL if INR wallet is empty but BTC>0.
    - Idle timeout (30m), trade cooldown (60s).
    - Wallet balances synced with DB before/after trade.
    - Auto-stop + notifications on error or idle.
    """
    try:
        # --- Ensure AUTO_TRADING state in session ---
        if "AUTO_TRADING" not in st.session_state:
            st.session_state.AUTO_TRADING = {
                "active": True,
                "entry_price": 0,
                "last_price": 0,
                "last_trade": None
            }

        # --- Always refresh balances from DB (mode-aware) ---
        btc_balance, _ = get_last_wallet_balance(mode="LIVE" if REAL_TRADING else "TEST")
        inr_balance, _ = get_last_inr_balance(mode="LIVE" if REAL_TRADING else "TEST")

        btc_balance = float(btc_balance or 0.0)
        inr_balance = float(inr_balance or 0.0)

        st.session_state.BTC_WALLET = {"balance": btc_balance}
        st.session_state.INR_WALLET = {"balance": inr_balance}

         # --- Active check ---
        db_active = get_autotrade_active_from_db()
        session_active = st.session_state.AUTO_TRADING.get("active", False)
        autotrade_active = bool(db_active) or bool(session_active)
        if not autotrade_active:
            return

        # 🚨 Stop autotrade immediately if both wallets are empty
        # if btc_balance == 0 and inr_balance == 0:
        #     stop_autotrade("⏹️ Auto-Trade stopped: Both INR and BTC wallets are empty.")
        #     return
        
        if autotrade_active and btc_balance == 0 and inr_balance == 0:
            stop_autotrade("⏹️ Auto-Trade stopped: Both INR and BTC wallets are empty.")
            return

        # --- Idle timeout check (30m) ---
        idle_timeout = 1800
        last_trade_time = get_last_trade_time_from_db()
        if last_trade_time:
            if isinstance(last_trade_time, (int, float)):
                last_trade_time = datetime.fromtimestamp(last_trade_time)
            elif isinstance(last_trade_time, str):
                try:
                    last_trade_time = datetime.fromisoformat(last_trade_time)
                except ValueError:
                    last_trade_time = None

        if last_trade_time and isinstance(last_trade_time, datetime):
            if (datetime.now() - last_trade_time).total_seconds() > idle_timeout:
                stop_autotrade("⏳ Auto-Trade stopped (idle timeout: 30m no trades).")
                return

        # --- Trade cooldown check (60s) ---
        last_trade = get_last_auto_trade()
        last_type = last_trade.get("trade_type") if last_trade else None
        last_ts = 0
        if last_trade and last_trade.get("trade_time"):
            trade_time_val = last_trade["trade_time"]
            if isinstance(trade_time_val, datetime):
                last_ts = int(trade_time_val.timestamp())
            elif isinstance(trade_time_val, (int, float)):
                last_ts = int(trade_time_val)
            elif isinstance(trade_time_val, str):
                try:
                    last_ts = int(datetime.fromisoformat(trade_time_val).timestamp())
                except ValueError:
                    last_ts = 0

        now_ts = int(time.time())
        if last_type in ("AUTO_BUY", "AUTO_SELL") and (now_ts - last_ts < 60):
            return  # prevent duplicate fast trades

        # --- Restore last_trade state ---
        if st.session_state.AUTO_TRADING.get("last_trade") is None:
            if last_type == "AUTO_BUY":
                st.session_state.AUTO_TRADING["last_trade"] = "BUY"
            elif last_type == "AUTO_SELL":
                st.session_state.AUTO_TRADING["last_trade"] = "SELL"

        last_trade_state = st.session_state.AUTO_TRADING.get("last_trade")

        # --- Strategy params ---
        min_roi = 0.01   # 1%
        threshold = 5    # INR
        entry_price = float(st.session_state.AUTO_TRADING.get("entry_price", 0) or 0.0)
        last_price = float(st.session_state.AUTO_TRADING.get("last_price", 0) or 0.0)
        price_diff = price_inr - last_price if last_price else 0

        # === AUTO-BUY ===
        if inr_balance >= 20 and (last_trade_state is None or last_trade_state == "SELL"):
            buy_inr = inr_balance * 0.5
            btc_bought = buy_inr / price_inr

            st.session_state.BTC_WALLET["balance"] = btc_balance + btc_bought
            st.session_state.INR_WALLET["balance"] = inr_balance - buy_inr
            st.session_state.AUTO_TRADING["entry_price"] = price_inr
            st.session_state.AUTO_TRADING["last_price"] = price_inr
            st.session_state.AUTO_TRADING["last_trade"] = "BUY"
            update_last_auto_trade_price_db(price_inr)

            msg = f"🟢 Auto-BUY ₹{buy_inr:.2f} → {btc_bought:.6f} BTC @ ₹{price_inr:.2f}"
            st.success(msg); st.toast(msg); send_telegram(msg)
            log_wallet_transaction("AUTO_BUY", btc_bought, st.session_state.BTC_WALLET["balance"], price_inr, "AUTO_BUY")
            log_inr_transaction("AUTO_BUY", -buy_inr, st.session_state.INR_WALLET["balance"], "LIVE" if REAL_TRADING else "TEST")
            save_trade_log("AUTO_BUY", btc_bought, st.session_state.BTC_WALLET["balance"], price_inr)
            return

        # === AUTO-SELL CONDITIONS ===
        roi = ((price_inr - entry_price) / entry_price) * 100 if entry_price > 0 else None
        cond1 = (roi is not None and roi >= min_roi and price_diff >= threshold)
        cond2 = (inr_balance <= 0 and btc_balance > 0)
        cond3 = (entry_price == 0 and btc_balance > 0)

        if btc_balance >= 0.0001 and (last_trade_state == "BUY") and (cond1 or cond2 or cond3):
            sell_btc = btc_balance
            inr_received = sell_btc * price_inr

            st.session_state.BTC_WALLET["balance"] = 0.0
            st.session_state.INR_WALLET["balance"] = inr_balance + inr_received
            st.session_state.AUTO_TRADING["entry_price"] = 0
            st.session_state.AUTO_TRADING["last_price"] = price_inr
            st.session_state.AUTO_TRADING["last_trade"] = "SELL"
            update_last_auto_trade_price_db(price_inr)

            roi_text = f"{roi:.4f}%" if roi is not None else "N/A"
            msg = f"🔴 Auto-SELL {sell_btc:.6f} BTC → ₹{inr_received:.2f} @ ₹{price_inr:.2f} | ROI {roi_text}"
            st.warning(msg); st.toast(msg); send_telegram(msg)
            log_wallet_transaction("AUTO_SELL", sell_btc, 0, price_inr, "AUTO_SELL")
            log_inr_transaction("AUTO_SELL", inr_received, st.session_state.INR_WALLET["balance"], "LIVE" if REAL_TRADING else "TEST")
            save_trade_log("AUTO_SELL", sell_btc, 0, price_inr, roi if roi else 0.0)
            return

        # ⚠️ Debugging skipped sells
        if btc_balance > 0 and (last_trade_state == "BUY") and not (cond1 or cond2 or cond3):
            reasons = []
            if not cond1: reasons.append("ROI not met")
            if not cond2: reasons.append("INR not empty")
            if not cond3: reasons.append("entry_price set")
            st.info(f"💡 Sell skipped: {', '.join(reasons)} | BTC={btc_balance:.6f}, INR={inr_balance:.2f}")

        # Fallback to set last_price if missing
        if last_price == 0:
            st.session_state.AUTO_TRADING["last_price"] = price_inr
            update_last_auto_trade_price_db(price_inr)

    except Exception as e:
        st.session_state.AUTO_TRADING["active"] = False
        st.session_state["autotrade_toggle"] = False
        update_autotrade_status_db(0)

        # use mode-aware balance fetch in exception
        btc_bal, _ = get_last_wallet_balance(mode="LIVE" if REAL_TRADING else "TEST")
        inr_bal, _ = get_last_inr_balance(mode="LIVE" if REAL_TRADING else "TEST")

        # btc_bal, _ = get_last_wallet_balance()
        # inr_bal, _ = get_last_inr_balance()
        log_wallet_transaction("AUTO_STOP", 0, btc_bal, 0, "AUTO_TRADE_STOP")
        log_inr_transaction("AUTO_STOP", 0, inr_bal, "LIVE" if REAL_TRADING else "TEST")
        update_wallet_daily_summary(auto_end=True)

        error_msg = f"❌ Auto-Trade stopped (technical error): {str(e)}"
        st.error(error_msg); send_telegram(error_msg)

def get_last_auto_trade_price_from_db():
    """Gets the last stored auto-trade price marker"""
    conn = get_mysql_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cursor.execute("""
        SELECT last_price
        FROM wallet_transactions
        WHERE is_autotrade_marker IN (TRUE, 1)
        ORDER BY trade_time DESC
        LIMIT 1
    """)
    result = cursor.fetchone()
    conn.close()
    # return float(result[0]) if result and (result[0] is not None) else 0.0
    return float(result['balance_after']) if result and (result['balance_after'] is not None) else 0.0

def update_last_auto_trade_price_db(price_inr):
    """Insert a marker row with the latest auto-trade price."""
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute("""
            INSERT INTO wallet_transactions 
            (trade_time, action, amount, balance_after, inr_value, trade_type, autotrade_active, is_autotrade_marker, last_price)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            datetime.now(),         # trade_time
            "AUTO_META",            # action
            0,                      # amount
            0,                      # balance_after
            price_inr,             # inr_value
            "AUTO_TRADE",           # trade_type
            True,                      # autotrade_active
            True,                      # is_autotrade_marker
            price_inr              # last_price
        ))

        conn.commit()
        print(f"✅ Last price updated in DB: ₹{price_inr}")

    except Exception as e:
        st.error(f"❌ Failed to update last auto-trade price: {e}")
        print("❌ DB Error (last price update):", e)
        raise

    finally:
        if conn:
            conn.close()

def update_autotrade_status_db(status: int):
    """Insert a marker row indicating auto-trade status (active/inactive)."""
    conn = None
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute("""
            INSERT INTO wallet_transactions 
            (trade_time, action, amount, balance_after, inr_value, trade_type, autotrade_active, is_autotrade_marker, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            datetime.now(), 
            "AUTO_META",
            0,
            0,
            0,
            "AUTO_TRADE_START" if status else "AUTO_TRADE_STOP",  # ✅ better logging
            bool(status),   # ✅ matches BOOLEAN column
            True,
            "SUCCESS"   # ✅ explicitly set
        ))

        conn.commit()
        print(f"✅ Auto-trade status updated to: {'Active' if status else 'Inactive'}")

    except Exception as e:
        st.error(f"❌ Failed to update auto-trade status: {e}")
        print("❌ DB Error (autotrade status):", e)
        raise

    finally:
        if conn:
            conn.close()
            
def check_price_threshold(price):
    if price >= ALERT_THRESHOLD_UP:
        msg = f"🚀 BTC just crossed ${ALERT_THRESHOLD_UP:,}! Current: ${price:,.2f}"
        st.warning(msg)
        # send_telegram(msg)
    elif price <= ALERT_THRESHOLD_DOWN:
        msg = f"⚠️ BTC dropped below ${ALERT_THRESHOLD_DOWN:,}! Current: ${price:,.2f}"
        st.error(msg)
        # send_telegram(msg)

def check_auto_sell(price):
    if price < STOP_LOSS_THRESHOLD and BTC_WALLET['balance'] > 0:
        msg = f"🔥 STOP-LOSS TRIGGERED at ${price:,.2f}! Auto-Selling..."
        st.error(msg)
        send_telegram(msg)
        log_wallet_transaction("AUTO_SELL", BTC_WALLET['balance'], 0, price, trade_type="AUTO_SELL_STOP")
        BTC_WALLET['balance'] = 0
        update_wallet_daily_summary(start=False)

import time
import requests

def withdraw_inr(amount: float, mode: str = "TEST", max_retries: int = 3, retry_delay: int = 60):
    """
    Handles INR withdrawal via Paytm Payout API with retry.
    - On SUCCESS: balance reduced, transaction logged.
    - On FAILURE: balance unchanged, transaction logged with status=FAILED.
    - Retries up to max_retries if API/network error occurs.
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

        # --- Get latest balance ---
        cursor.execute("""
            SELECT balance_after
            FROM inr_wallet_transactions
            WHERE mode = %s
            ORDER BY trade_time DESC
            LIMIT 1
        """, (mode,))
        row = cursor.fetchone()
        current_balance = float(row[0]) if row else 0.0

        # --- Prevent overdraft ---
        if amount > current_balance:
            raise Exception("Insufficient balance")

        payout_success = False
        failure_reason = None

        # --- Retry loop for LIVE mode ---
        if mode == "LIVE":
            for attempt in range(1, max_retries + 1):
                try:
                    res = requests.post("https://securegw.paytm.in/payout-api", json={
                        "amount": amount,
                        "mode": "BANK",
                        "account": "xxxxxxx",
                        "ifsc": "xxxxxxx"
                    }, timeout=30)

                    data = res.json()
                    if data.get("status") == "SUCCESS":
                        payout_success = True
                        break  # exit retry loop
                    else:
                        failure_reason = data.get("statusMessage", "Unknown failure")
                        msg = f"⚠️ Withdraw attempt {attempt} failed: {failure_reason}"
                        st.warning(msg); send_telegram(msg)

                except Exception as e:
                    failure_reason = str(e)
                    msg = f"⚠️ Withdraw attempt {attempt} error: {failure_reason}"
                    st.warning(msg); send_telegram(msg)

                if attempt < max_retries:
                    time.sleep(retry_delay)

        else:
            # In TEST mode, always succeed
            payout_success = True

        # --- Update DB ---
        if payout_success:
            new_balance = current_balance - amount
            cursor.execute("""
                INSERT INTO inr_wallet_transactions
                (trade_type, amount, balance_after, status, mode, reference)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, ("WITHDRAW", -amount, new_balance, "SUCCESS", mode, "PAYTM_PAYOUT"))
            msg = f"✅ Withdraw successful: ₹{amount:.2f}, New Balance: ₹{new_balance:.2f}"
            st.success(msg); send_telegram(msg)
        else:
            # Log failure attempt (balance unchanged)
            cursor.execute("""
                INSERT INTO inr_wallet_transactions
                (trade_type, amount, balance_after, status, mode, reference)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, ("WITHDRAW", -amount, current_balance, "FAILED", mode, "PAYTM_PAYOUT_FAIL"))
            msg = f"❌ Withdraw failed after {max_retries} attempts: ₹{amount:.2f}, Reason: {failure_reason}"
            st.error(msg); send_telegram(msg)

        conn.commit()

    finally:
        conn.close()


def deduct_balance(amount, method, recipient_name, acc_no=None, ifsc=None, upi=None):
    con = get_mysql_connection()
    if not con:
        return

    try:
        with con.cursor() as cur:
            # 1. Get current balance
            cur.execute("SELECT inr_balance FROM user_wallets WHERE user_email=%s", ("testing@gmail.com",))
            row = cur.fetchone()

            if not row:
                st.error("⚠️ User wallet not found!")
                return

            current_balance = float(row[0])
            new_balance = current_balance - amount

            if new_balance < 0:
                st.error("❌ Insufficient funds!")
                return

            # 2. Update wallet balance
            cur.execute(
                "UPDATE user_wallets SET inr_balance=%s WHERE user_email=%s",
                (new_balance, "testing@gmail.com")
            )

            # 3. Log withdrawal
            cur.execute(
                """
                INSERT INTO inr_wallet_transactions 
                (trade_time, action, amount, balance_after, trade_mode, payment_id) 
                VALUES (NOW(), %s, %s, %s, %s, %s)
                """,
                (
                    f"WITHDRAW-{method}",
                    amount,
                    new_balance,
                    "TEST",
                    f"{recipient_name} | {method} | {acc_no or ifsc or upi or ''}"
                )
            )

        con.commit()

    except Exception as e:
        st.error(f"DB Error: {e}")
        con.rollback()
    finally:
        con.close()

# PAYTM API
def get_access_token():
    auth = base64.b64encode(f"{PAYTM_CLIENT_ID}:{PAYTM_CLIENT_SECRET}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    res = requests.post(OAUTH_URL, headers=headers, data={"grant_type": "client_credentials"})
    return res.json().get("access_token")

def send_paytm_payout(token, order_id, amount, name, method, acc_no=None, ifsc=None, upi=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "order_id": order_id,
        "amount": f"{amount:.2f}",
        "purpose": "SALARY",
        "mode": "IMPS" if method == "BANK" else "UPI"
    }
    if method == "BANK":
        payload["beneficiary_account"] = {
            "account_number": acc_no,
            "ifsc_code": ifsc,
            "name": name
        }
    else:
        payload["upi"] = {
            "vpa": upi,
            "name": name
        }
    res = requests.post(PAYOUT_URL, headers=headers, json=payload)
    return res.json()

# RECIPIENT STORAGE
def get_all_recipients():
    with get_mysql_connection() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM saved_recipients ORDER BY id DESC")
            return cur.fetchall()

def save_recipient_if_new(name, method, acc, ifsc, upi):
    with get_mysql_connection() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id FROM saved_recipients WHERE method=%s AND (account_number=%s OR upi_id=%s)", (method, acc, upi))
            if not cur.fetchone():
                cur.execute("""
                    INSERT INTO saved_recipients (name, method, account_number, ifsc, upi_id)
                    VALUES (%s, %s, %s, %s, %s)
                """, (name, method, acc, ifsc, upi))
        con.commit()

def log_payout(order_id, name, method, acc, ifsc, upi, amount, status, response):
    with get_mysql_connection() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO razorpay_payment_logs
                (order_id, customer_id, name, method, account_number, ifsc, upi_id, amount, status, response)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (order_id, CUSTOMER_ID, name, method, acc, ifsc, upi, amount, status, json.dumps(response)))
        con.commit()

# --- UI ---
st.title("📱📊 MM BTC Autotrade Pro BOT")
# price = get_btc_price()
# price_inr = usd_to_inr(price) if price else 0

price = cd_get_market_price("BTCUSDT")
price_inr = cd_get_market_price("BTCINR")
update_wallet_daily_summary(start=True)

st.metric("BTC/USDT", f"${price:,.2f}" if price else "N/A")
st.metric("BTC/INR", f"₹{price_inr:,.2f}" if price_inr else "N/A")

if price:
    check_price_threshold(price)
    check_auto_sell(price)
    check_auto_trading(price_inr)


# st.subheader("💳 Paytm Payment Gateway")
st.sidebar.title("⚙️ Paytm Settings")
# st.metric("Balance", f"₹{st.session_state['test_inr_balance']:.2f}")


deposit_amt = st.number_input("Deposit Amount ₹", 100, step=100, key="paytm_amt")
if st.button("🧾 Create Paytm Order"):
    order_id = f"ORDER{uuid.uuid4().hex[:8].upper()}"
    body = {
        "requestType": "Payment",
        "mid": PAYTM_MID,
        "websiteName": PAYTM_WEBSITE,
        "orderId": order_id,
        "callbackUrl": PAYTM_CALLBACK_URL,
        "txnAmount": {"value": str(deposit_amt), "currency": "INR"},
        "userInfo": {"custId": "CUST001"}
    }
    try:
        checksum = PaytmChecksum.generateSignature(json.dumps(body), PAYTM_MERCHANT_KEY)
    except Exception as e:
        st.error(f"❌ Paytm checksum generation failed: {e}")
        checksum = None

    headers = {"Content-Type": "application/json"}
    if checksum:
        headers["X-Verify-Signature"] = checksum

    base_url = PAYTM_BASE_URL if PAYTM_BASE_URL else "https://securegw-stage.paytm.in"
    initiate_url = f"{base_url}/theia/api/v1/initiateTransaction?mid={PAYTM_MID}&orderId={order_id}"

    try:
        response = requests.post(initiate_url, data=json.dumps(body), headers=headers, timeout=20)
        if response.status_code != 200:
            st.error(f"❌ Paytm initiate returned status {response.status_code}")
            st.write(response.text)
            res = {}
        else:
            res = response.json()
    except Exception as e:
        st.error(f"❌ Failed to call Paytm initiate API: {e}")
        res = {}

    txn_token = None
    if isinstance(res, dict):
        txn_token = res.get("body", {}).get("txnToken") or res.get("txnToken")

    if txn_token:
        pay_url = f"{base_url}/theia/api/v1/showPaymentPage?mid={PAYTM_MID}&orderId={order_id}&txnToken={txn_token}"
        try:
            qr_bytes = generate_qr_code(pay_url)
            st.image(Image.open(BytesIO(qr_bytes)), caption=f"Scan to Pay: {order_id}")
            st.markdown(f"**[Click to Pay via Paytm]({pay_url})**")
        except Exception as e:
            st.error(f"❌ Failed to render QR: {e}")
            st.markdown(f"**[Click to Pay via Paytm]({pay_url})**")
    else:
        st.error("❌ Failed to generate Paytm order. See response below.")
        st.write(res)

# Code changed on 28-08-2025 for withdrawal button login
st.subheader("🏧 Withdraw to Bank / UPI")

recipients = get_all_recipients()
recipient_names = [f"{r['name']} ({r['method']})" for r in recipients]
selected = st.selectbox("📋 Saved Recipient", ["-- New Recipient --"] + recipient_names)

if selected != "-- New Recipient --":
    sel = recipients[recipient_names.index(selected) - 1]
    method = sel['method']
    name = sel['name']
    acc_no = sel['account_number']
    ifsc = sel['ifsc']
    upi = sel['upi_id']
else:
    method = st.radio("Payout Method", ["BANK", "UPI"])
    name = st.text_input("Recipient Name")
    acc_no = st.text_input("Account Number") if method == "BANK" else ""
    ifsc = st.text_input("IFSC Code") if method == "BANK" else ""
    upi = st.text_input("UPI ID") if method == "UPI" else ""

payout_amt = st.number_input("Withdraw ₹", 100, step=100)

if st.button("🚀 Withdraw"):
    if method == "BANK" and (not acc_no or not ifsc):
        st.warning("❗ Please enter bank details.")
    elif method == "UPI" and not upi:
        st.warning("❗ Please enter UPI ID.")
    elif not name:
        st.warning("❗ Name is required.")
    else:
        save_recipient_if_new(name, method, acc_no, ifsc, upi)
        real_balance = get_current_inr_balance()

        if payout_amt > real_balance:
            st.error("❌ Insufficient balance")
        else:
            deduct_balance(payout_amt)
            st.session_state["inr_balance"] = get_current_inr_balance()
            send_telegram(f"✅ ₹{payout_amt:.2f} payout sent to {name} via {method}")
            st.success(f"✅ ₹{payout_amt:.2f} sent to {name}")


# --- Testing Mode ----
if not REAL_TRADING:
    st.subheader("🧪 Test Wallet Controls")

    col_test1, col_test2 = st.columns(2)
    with col_test1:
        st.metric("Test BTC Balance", f"{BTC_WALLET['balance']:.4f} BTC")
    with col_test2:
        st.metric("INR Value", f"₹{BTC_WALLET['balance'] * price_inr:,.2f}")

    # if last_trade_time:
    #     st.caption(f"📅 Last transaction: {last_trade_time.strftime('%Y-%m-%d %H:%M:%S')}")

    last_trade_time = get_last_trade_time_from_db()
    if last_trade_time:
        st.caption(f"📅 Last transaction: {last_trade_time.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        st.caption("📅 No transactions yet")

    if st.button("🔄 Reset Test Wallet to 0.005 BTC"):
        BTC_WALLET['balance'] = 0.005
        log_wallet_transaction("TEST_RESET", 0.005, BTC_WALLET['balance'], price_inr)
        update_wallet_daily_summary()
        st.success("✅ Test wallet reset to 0.005 BTC")
        
# --- Trading Panel ---
st.write("### 💱 Trading Panel")
trade_amount = st.number_input("BTC Amount to Trade", min_value=0.0001, max_value=1.0, value=0.001, step=0.001)
col1, col2, col3 = st.columns(3)

with col1:
    if st.button("💰 BUY BTC"):
        if INR_WALLET['balance'] >= trade_amount * price_inr:
            BTC_WALLET['balance'] += trade_amount
            INR_WALLET['balance'] -= trade_amount * price_inr
            log_inr_transaction("BUY", -trade_amount * price_inr, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
            st.success(f"Bought {trade_amount:.4f} BTC")
            log_wallet_transaction("BUY", trade_amount, BTC_WALLET['balance'], price_inr, trade_type="MANUAL_BUY")
            update_wallet_daily_summary(start=False)
        else:
            st.error("❌ Not enough INR balance")

with col2:
    if st.button("📤 SELL BTC"):
        if BTC_WALLET['balance'] >= trade_amount:
            BTC_WALLET['balance'] -= trade_amount
            sell_inr = trade_amount * price_inr
            INR_WALLET['balance'] += sell_inr
            log_inr_transaction("SELL", sell_inr, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
            st.success(f"Sold {trade_amount:.4f} BTC for ₹{sell_inr:,.2f}")
            log_wallet_transaction("SELL", trade_amount, BTC_WALLET['balance'], price_inr, trade_type="MANUAL_SELL")
            update_wallet_daily_summary(start=False)
        else:
            st.error("❌ Insufficient BTC balance")

with col3:
    if st.button("🔄 Reset Wallet"):
        BTC_WALLET['balance'] = 0.005
        log_wallet_transaction("RESET", 0, BTC_WALLET['balance'], price_inr, trade_type="MANUAL_RESET_BALANCE")
        update_wallet_daily_summary(start=False)

# --- Wallet Status ---
st.write("### 💼 Wallet Status")
wallet_col1, wallet_col2 = st.columns(2)

# --- BTC Wallet ---
btc_balance = st.session_state.get("BTC_WALLET", {}).get("balance", 0.0)
try:
    btc_balance = float(btc_balance)
except (TypeError, ValueError):
    btc_balance = 0.0

with wallet_col1:
    st.metric("BTC Balance", f"{btc_balance:.6f} BTC")

# --- INR Wallet ---
inr_balance = st.session_state.get("INR_WALLET", {}).get("balance", 0.0)

# Handle tuple case safely
if isinstance(inr_balance, tuple):
    inr_balance = inr_balance[0] if inr_balance else 0

try:
    inr_balance = float(inr_balance)
except (TypeError, ValueError):
    inr_balance = 0.0

with wallet_col2:
    st.metric("INR Wallet Balance", f"₹{inr_balance:,.2f}")
    st.metric("INR Value of BTC", f"₹{btc_balance * price_inr:,.2f}")

# --- Transaction History ---
with st.expander("📒 INR Wallet History"):
    conn = get_mysql_connection()
    df = pd.read_sql("SELECT * FROM inr_wallet_transactions ORDER BY trade_time DESC LIMIT 20", conn)
    st.dataframe(df)
    conn.close()

with st.expander("📋 View Transaction History"):
    conn = get_mysql_connection()
    df = pd.read_sql("SELECT * FROM wallet_transactions ORDER BY trade_time DESC LIMIT 20", conn)
    st.dataframe(df)
    conn.close()

with st.expander("📊 Wallet Daily Summary"):
    conn = get_mysql_connection()
    df = pd.read_sql("SELECT * FROM wallet_history ORDER BY trade_date DESC LIMIT 7", conn)
    st.dataframe(df)
    conn.close()

st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# ✅ Start background thread only once (per dyno boot, not per browser refresh)
# Restore state from DB on each refresh
db_active = get_autotrade_active_from_db()

if "AUTO_TRADING" not in st.session_state:
    st.session_state.AUTO_TRADING = {"active": db_active, "last_price": 0, "sell_streak": 0}
else:
    st.session_state.AUTO_TRADING["active"] = db_active

if "autotrade_toggle" not in st.session_state:
    st.session_state.autotrade_toggle = bool(db_active)
else:
    st.session_state.autotrade_toggle = bool(db_active)

# Idle warning toast if auto-stopped due to inactivity
last_trade_time = get_last_trade_time_from_logs()
if not db_active and last_trade_time:
    idle_minutes = (datetime.now() - last_trade_time).total_seconds() / 60
    if idle_minutes > 60:
        msg = f"⏰ Auto-Trade was auto-stopped after {idle_minutes:.0f} minutes of inactivity"
        st.warning(msg)
        st.toast(msg)

# --- Auto Trade Button ---
autotrade_active = get_autotrade_active_from_db()

if st.button(f"{'🚀 Start' if not autotrade_active else '🛑 Stop'} Auto-Trade"):
    if autotrade_active:
        # Stop trading
        update_wallet_daily_summary(auto_end=True)
        update_autotrade_status_db(0)
        msg = f"🛑 Auto-Trade STOPPED at ₹{price_inr:.2f}"
        log_wallet_transaction("AUTO_STOP", 0, BTC_WALLET['balance'], price_inr, "AUTO_TRADE_STOP")
        log_inr_transaction("AUTO_STOP", 0, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
    else:
        # Start trading
        update_autotrade_status_db(1)
        update_last_auto_trade_price_db(price_inr)  # initialize last price
        msg = f"🚀 Auto-Trade ACTIVATED at ₹{price_inr:.2f}"
        log_wallet_transaction("AUTO_START", 0, BTC_WALLET['balance'], price_inr, "AUTO_TRADE_START")
        log_inr_transaction("AUTO_START", 0, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")

    st.toast(msg)
    send_telegram(msg)

# --- BTC Wallet Address Display ---
if REAL_TRADING:
    st.subheader("📥 Deposit BTC")
    st.write("Send BTC to the address below to deposit into your wallet:")

    btc_address = wallet.get_key().address
    st.code(btc_address, language="text")
    st.button("📋 Copy Address", on_click=lambda: st.toast("Copied!", icon="📋"))

    # Generate QR Code
    qr = qrcode.make(btc_address)
    buf = BytesIO()
    qr.save(buf, format="PNG")
    st.image(Image.open(buf), caption="Scan to Deposit BTC")

    # --- BTC Withdrawal Section ---
    st.subheader("📤 Withdraw BTC")

    if BTC_WALLET['balance'] > 0:
        with st.form("btc_withdraw_form", clear_on_submit=False):
            withdraw_address = st.text_input("Destination BTC Address", placeholder="Enter recipient BTC address")
            withdraw_amount = st.number_input(
                "Amount (BTC)",
                min_value=0.0001,
                max_value=BTC_WALLET['balance'],
                step=0.0001,
                format="%.8f"
            )
            submitted = st.form_submit_button("Submit Withdrawal")

            if submitted:
                try:
                    tx = wallet.send_to(address=withdraw_address, amount=withdraw_amount, network='bitcoin')
                    st.success(f"✅ Withdrawal successful!\nTX ID: {tx.txid}")
                    BTC_WALLET['balance'] -= withdraw_amount
                    log_wallet_transaction("REAL_WITHDRAW", withdraw_amount, BTC_WALLET['balance'], price_inr, trade_type="REAL_WITHDRAW")
                except Exception as e:
                    st.error(f"❌ Withdrawal failed: {e}")
    else:
        st.warning("⚠️ Your BTC balance is 0.0 — Withdrawal not allowed.")

    # --- Sync INR Balance---
    st.subheader("🔄 Sync INR Balance")
    balance = sync_inr_wallet("LIVE")
    if balance:
        st.success(f"✅ Synced: ₹{balance:.2f}")

# Show current INR balance
# st.sidebar.metric("INR Balance", f"₹{st.session_state.INR_WALLET['balance']:.2f}")
    
    # Auto-refresh INR balance every 5 minutes
    if "last_inr_sync" not in st.session_state:
        st.session_state.last_inr_sync = 0

    import time
    if time.time() - st.session_state.last_inr_sync > 300:  # 300s = 5m
        balance = sync_inr_wallet("LIVE")
        if balance:
            st.session_state.last_inr_sync = time.time()

# # --- Daily Summary ---
# st.subheader("📊 INR Wallet - Daily Summary")
# summary = get_daily_wallet_summary()
# if summary:
#     df = pd.DataFrame(summary)
#     st.dataframe(df)
# else:
#     st.info("No wallet transactions yet.")

# # --- BTC/INR Candlestick Chart ---
# st.write("### 📊 Live BTC/INR Chart")

# # --- Date range input ---
# date_col1, date_col2 = st.columns(2)
# with date_col1:
#     start_date = st.date_input("From", value=datetime.today() - timedelta(days=3))
# with date_col2:
#     end_date = st.date_input("To", value=datetime.today())

# # --- Fetch logged data from wallet_history (PostgreSQL) ---
# def get_wallet_history(start_date, end_date, mode="LIVE"):
#     conn = get_mysql_connection()   # ✅ psycopg2 connection helper
#     try:
#         query = """
#             SELECT trade_date, auto_start_price, auto_end_price, current_inr_value
#             FROM wallet_history
#             WHERE trade_date BETWEEN %s AND %s
#               AND mode = %s
#             ORDER BY trade_date ASC
#         """
#         with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
#             cursor.execute(query, (start_date, end_date, mode))
#             rows = cursor.fetchall()
#             if not rows:
#                 return pd.DataFrame()
#             df = pd.DataFrame(rows)
#             df.rename(columns={
#                 "trade_date": "timestamp",
#                 "auto_start_price": "start_price",
#                 "auto_end_price": "end_price",
#                 "current_inr_value": "current_price"
#             }, inplace=True)
#             return df
#     finally:
#         conn.close()

# # --- Load data ---
# hist_df = get_wallet_history(start_date, end_date, mode="LIVE" if REAL_TRADING else "TEST")

# if not hist_df.empty:
#     # Convert timestamp → datetime
#     hist_df["timestamp"] = pd.to_datetime(hist_df["timestamp"])

#     # Construct OHLC from available columns
#     hist_df["open"] = hist_df["start_price"].fillna(hist_df["current_price"])
#     hist_df["close"] = hist_df["end_price"].fillna(hist_df["current_price"])
#     hist_df["high"] = hist_df[["start_price", "end_price", "current_price"]].max(axis=1)
#     hist_df["low"] = hist_df[["start_price", "end_price", "current_price"]].min(axis=1)

#     # Resample to hourly OHLC
#     ohlc_df = hist_df.resample("1H", on="timestamp").agg(
#         open=("open", "first"),
#         high=("high", "max"),
#         low=("low", "min"),
#         close=("close", "last")
#     ).dropna().reset_index()

#     if ohlc_df.empty:
#         st.info("⚠️ No aggregated data found for this period. Showing last 24h.")
#         hist_tail = hist_df.tail(24).copy()
#         ohlc_df = pd.DataFrame({
#             "timestamp": hist_tail["timestamp"],
#             "open": hist_tail["open"],
#             "high": hist_tail["high"],
#             "low": hist_tail["low"],
#             "close": hist_tail["close"]
#         })

#     # --- Plot candlestick ---
#     fig = go.Figure(go.Candlestick(
#         x=ohlc_df['timestamp'],
#         open=ohlc_df['open'],
#         high=ohlc_df['high'],
#         low=ohlc_df['low'],
#         close=ohlc_df['close'],
#         increasing_line_color='green',
#         decreasing_line_color='red'
#     ))

#     fig.update_layout(
#         xaxis_rangeslider_visible=True,
#         margin=dict(l=20, r=20, t=20, b=20),
#         height=400,
#         xaxis=dict(
#             rangeselector=dict(
#                 buttons=list([
#                     dict(count=12, label="12h", step="hour", stepmode="backward"),
#                     dict(count=1, label="24h", step="day", stepmode="backward"),
#                     dict(count=3, label="3d", step="day", stepmode="backward"),
#                     dict(step="all")
#                 ])
#             )
#         )
#     )

#     st.plotly_chart(fig, use_container_width=True)

# else:
#     st.warning("⚠️ No wallet history found. Please wait for logger to collect data.")

# --- Daily Summary ---
st.subheader("📊 INR Wallet - Daily Summary")
summary = get_daily_wallet_summary()
if summary:
    df = pd.DataFrame(summary)
    st.dataframe(df)
else:
    st.info("No wallet transactions yet.")

# --- BTC/INR Candlestick Chart ---
st.write("### 📊 Live BTC/INR Chart")

# --- Date range input ---
st.write("Select Date Range:")
date_col1, date_col2 = st.columns(2)
with date_col1:
    start_date = st.date_input("From", value=datetime.today() - timedelta(days=3))
with date_col2:
    end_date = st.date_input("To", value=datetime.today())

# --- Candle type switch ---
candle_type = st.radio("Candle Type", ["Hourly", "Daily"], horizontal=True)

# --- Fetch logged data from wallet_history (PostgreSQL) ---
def get_wallet_history(start_date, end_date, mode="LIVE"):
    conn = get_mysql_connection()   # ✅ psycopg2 connection helper
    try:
        query = """
            SELECT trade_date, auto_start_price, auto_end_price, current_inr_value
            FROM wallet_history
            WHERE trade_date BETWEEN %s AND %s
              AND mode = %s
            ORDER BY trade_date ASC
        """
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(query, (start_date, end_date, mode))
            rows = cursor.fetchall()
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            df.rename(columns={
                "trade_date": "timestamp",
                "auto_start_price": "start_price",
                "auto_end_price": "end_price",
                "current_inr_value": "current_price"
            }, inplace=True)
            return df
    finally:
        conn.close()

# --- Load data ---
hist_df = get_wallet_history(start_date, end_date, mode="LIVE" if REAL_TRADING else "TEST")

if not hist_df.empty:
    # Convert timestamp → datetime
    hist_df["timestamp"] = pd.to_datetime(hist_df["timestamp"], errors="coerce")
    hist_df.dropna(subset=["timestamp"], inplace=True)

    # Construct OHLC from available columns
    hist_df["open"] = hist_df["start_price"].fillna(hist_df["current_price"])
    hist_df["close"] = hist_df["end_price"].fillna(hist_df["current_price"])
    hist_df["high"] = hist_df[["start_price", "end_price", "current_price"]].max(axis=1)
    hist_df["low"] = hist_df[["start_price", "end_price", "current_price"]].min(axis=1)

    # --- Filter by selected date range ---
    filtered_df = hist_df[
        (hist_df["timestamp"].dt.date >= start_date) &
        (hist_df["timestamp"].dt.date <= end_date)
    ]

    # --- Resample based on candle type ---
    if not filtered_df.empty:
        if candle_type == "Hourly":
            ohlc_df = filtered_df.resample("1H", on="timestamp").agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last")
            ).dropna().reset_index()
        else:  # Daily
            ohlc_df = filtered_df.resample("1D", on="timestamp").agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last")
            ).dropna().reset_index()
    else:
        ohlc_df = pd.DataFrame()

    # --- Fallback if no candles ---
    if ohlc_df.empty:
        st.info("⚠️ No aggregated data found for this period. Showing last 24 records.")
        hist_tail = hist_df.tail(24).copy()
        ohlc_df = pd.DataFrame({
            "timestamp": hist_tail["timestamp"],
            "open": hist_tail["open"],
            "high": hist_tail["high"],
            "low": hist_tail["low"],
            "close": hist_tail["close"]
        })

    # --- Plot candlestick ---
    fig = go.Figure(go.Candlestick(
        x=ohlc_df['timestamp'],
        open=ohlc_df['open'],
        high=ohlc_df['high'],
        low=ohlc_df['low'],
        close=ohlc_df['close'],
        increasing_line_color='green',
        decreasing_line_color='red'
    ))

    fig.update_layout(
        xaxis_rangeslider_visible=True,
        margin=dict(l=20, r=20, t=20, b=20),
        height=400,
        xaxis=dict(
            rangeselector=dict(
                buttons=list([
                    dict(count=12, label="12h", step="hour", stepmode="backward"),
                    dict(count=1, label="24h", step="day", stepmode="backward"),
                    dict(count=3, label="3d", step="day", stepmode="backward"),
                    dict(step="all")
                ])
            )
        )
    )

    st.plotly_chart(fig, use_container_width=True)

else:
    st.warning("⚠️ No wallet history found. Please wait for logger to collect data.")

# --- rerun for auto-refresh ---
time.sleep(10)
st.rerun()
# st.experimental_rerun()