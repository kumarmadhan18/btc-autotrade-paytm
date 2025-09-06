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
from paytmchecksum import PaytmChecksum
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
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
        profit_inr DOUBLE PRECISION DEFAULT 0
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
        last_price DOUBLE PRECISION DEFAULT 0
    )
    """)

    conn.commit()
    cursor.close()
    conn.close()
    st.success("✅ PostgreSQL tables initialized successfully!")
    
init_mysql_tables()

def migrate_postgres_tables():
    conn = get_mysql_connection()
    if not conn:
        return
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # 🔹 inr_wallet_transactions
    cursor.execute("ALTER TABLE inr_wallet_transactions ALTER COLUMN status SET DEFAULT 'PENDING';")
    cursor.execute("ALTER TABLE inr_wallet_transactions ALTER COLUMN reversal_id SET DEFAULT '';")
    cursor.execute("ALTER TABLE inr_wallet_transactions ALTER COLUMN razorpay_order_id SET DEFAULT '';")

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
    # cursor.execute("ALTER TABLE wallet_transactions ALTER COLUMN autotrade_active TYPE INTEGER USING autotrade_active::integer, ALTER COLUMN autotrade_active SET DEFAULT 0;")
    # cursor.execute("ALTER TABLE wallet_transactions ALTER COLUMN is_autotrade_marker TYPE INT, ALTER COLUMN is_autotrade_marker SET DEFAULT 0, ALTER COLUMN is_autotrade_marker DROP NOT NULL;") 
    cursor.execute("ALTER TABLE wallet_transactions ALTER COLUMN is_autotrade_marker TYPE BOOLEAN USING (is_autotrade_marker::INTEGER <> 0);")
    # cursor.execute("TRUNCATE wallet_history;")
    # cursor.execute("TRUNCATE wallet_transactions;")
    # cursor.execute("TRUNCATE inr_wallet_transactions;")
    # cursor.execute("TRUNCATE user_wallets;")

    conn.commit()
    cursor.close()
    conn.close()
    st.success("✅ Migration completed! All tables updated with safe defaults.")

migrate_postgres_tables()

def get_btc_price():
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

def get_last_inr_balance_with_tuple_error():
    conn = get_mysql_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT balance_after FROM inr_wallet_transactions WHERE trade_mode = %s ORDER BY trade_time DESC LIMIT 1", 
                   ("LIVE" if REAL_TRADING else "TEST",))
    result = cursor.fetchone()
    conn.close()
    # return float(result['balance_after']) if result else 10000.0
    return float(result['balance_after']) if result and result['balance_after'] is not None else 10000.0
    # return float(result[0]) if result else 10000.0

def get_last_inr_balance():
    """
    Returns the last INR balance and trade_time.
    Always returns a tuple: (balance: float, trade_time: float or None)
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT balance_after, EXTRACT(EPOCH FROM trade_time) AS ts
            FROM inr_wallet_transactions
            WHERE status = 'SUCCESS'
            ORDER BY trade_time DESC
            LIMIT 1
        """)
        row = cursor.fetchone()
        if row:
            balance = float(row.get("balance_after") or 0.0)
            ts = float(row.get("ts") or 0.0)
            return balance, ts
        else:
            return 0.0, None
    finally:
        conn.close()

INR_WALLET = {"balance": get_last_inr_balance()}
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
                        send_telegram_alert("⚠️ Render usage reached 500 hours. Upgrade needed.")
                else:
                    raise Exception("App responded with error")
            except:
                send_telegram_alert("🚨 ALERT: Render app appears DOWN!")
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

TEST_MID = os.getenv("TEST_MID", "")
TEST_KEY = os.getenv("TEST_KEY", "")  # Paytm staging default key
TEST_WEBSITE = os.getenv("TEST_WEBSITE", "")
TEST_CALLBACK = os.getenv("TEST_CALLBACK", "")
TEST_BASE_URL = os.getenv("TEST_BASE_URL", "")

LIVE_MID = os.getenv("LIVE_MID", "")
LIVE_KEY = os.getenv("LIVE_KEY", "")
LIVE_WEBSITE = os.getenv("LIVE_WEBSITE", "")
LIVE_CALLBACK = os.getenv("LIVE_CALLBACK", "")
LIVE_BASE_URL = os.getenv("LIVE_BASE_URL", "")

PAYTM_CLIENT_ID = os.getenv("PAYTM_CLIENT_ID", "")
PAYTM_CLIENT_SECRET = os.getenv("PAYTM_CLIENT_SECRET", "")
OAUTH_URL = os.getenv("OAUTH_URL", "")
PAYOUT_URL = os.getenv("PAYOUT_URL", "")

if REAL_TRADING:
    PAYTM_MID = LIVE_MID
    PAYTM_MERCHANT_KEY = LIVE_KEY
    PAYTM_WEBSITE = LIVE_WEBSITE
    PAYTM_CALLBACK_URL = LIVE_CALLBACK
    PAYTM_BASE_URL = LIVE_BASE_URL
else:
    PAYTM_MID = TEST_MID
    PAYTM_MERCHANT_KEY = TEST_KEY
    PAYTM_WEBSITE = TEST_WEBSITE
    PAYTM_CALLBACK_URL = TEST_CALLBACK
    PAYTM_BASE_URL = TEST_BASE_URL
   
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
def generate_qr_code(data: str):
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return buffer.getvalue()

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
        
def get_last_wallet_balance_with_tuple_error():
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # ✅ Only pick rows where balance_after is not NULL and not zero
        cursor.execute("""
            SELECT balance_after, trade_time
            FROM wallet_transactions
            WHERE balance_after IS NOT NULL AND balance_after != 0
            ORDER BY trade_time DESC
            LIMIT 1
        """)
        result = cursor.fetchone()
        cursor.close()
        conn.close()

        if result:
            return float(result['balance_after']), result['trade_time']
        else:
            return 0.000, None

    except Exception as e:
        print(f"⚠️ Error fetching last wallet balance: {e}")
        return 0.000, None

# def get_last_wallet_balance():
#     """Return the last BTC wallet balance as a float (safe)."""
#     conn = get_mysql_connection()
#     try:
#         cursor = conn.cursor()
#         cursor.execute("""
#             SELECT balance_after 
#             FROM wallet_transactions
#             ORDER BY trade_time DESC
#             LIMIT 1
#         """)
#         row = cursor.fetchone()
#         if row:
#             # row may be tuple or dict depending on cursor type
#             if isinstance(row, tuple):
#                 return float(row[0] or 0)
#             if isinstance(row, dict):
#                 return float(row.get("balance_after", 0) or 0)
#         return 0.0
#     finally:
#         conn.close()

def get_last_wallet_balance():
    """
    Returns the last BTC balance and trade_time.
    Always returns a tuple: (balance: float, trade_time: float or None)
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT balance_after, EXTRACT(EPOCH FROM trade_time) AS ts
            FROM wallet_transactions
            WHERE status = 'SUCCESS'
            ORDER BY trade_time DESC
            LIMIT 1
        """)
        row = cursor.fetchone()
        if row:
            balance = float(row.get("balance_after") or 0.0)
            ts = float(row.get("ts") or 0.0)
            return balance, ts
        else:
            return 0.0, None
    finally:
        conn.close()

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

def log_inr_transaction_old(action, amount, balance_after, mode):
    try:
        conn = get_mysql_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute("""
                INSERT INTO inr_wallet_transactions 
                (trade_time, action, amount, balance_after, trade_mode)
                VALUES (NOW(), %s, %s, %s, %s)
            """, (action, amount, balance_after, mode))
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error logging INR transaction: {e}")

# def log_inr_transaction(action, amount, balance, mode="TEST"):
#     conn = get_mysql_connection()
#     cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
#     cursor.execute("""
#         INSERT INTO inr_wallet_transactions 
#         (trade_time, action, amount, balance_after, trade_mode, status)
#         VALUES (NOW(), %s, %s, %s, %s, %s)
#     """, (
#         action,
#         amount,
#         balance,
#         mode,
#         "SUCCESS"   # ✅ explicitly set
#     ))
#     conn.commit()
#     conn.close()

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

def log_wallet_transaction_old(action, amount, balance, price_inr, trade_type="MANUAL"):
    conn = get_mysql_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        INSERT INTO wallet_transactions 
        (trade_time, action, amount, balance_after, inr_value, trade_type, autotrade_active)
        VALUES (NOW(), %s, %s, %s, %s, %s, %s)
    """, (action, amount, balance, balance * price_inr, trade_type, bool(st.session_state.AUTO_TRADING["active"]) if "AUTO_TRADING" in st.session_state else False))
    conn.commit()
    conn.close()

def log_wallet_transaction_25_08_2025(action, amount, balance, price_inr, trade_type="MANUAL"):
    conn = get_mysql_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        INSERT INTO wallet_transactions
        (trade_time, action, amount, balance_after, inr_value, trade_type, autotrade_active)
        VALUES (NOW(), %s, %s, %s, %s, %s, %s)
    """, (
        action,
        amount,
        balance,
        balance * price_inr,
        trade_type,
        bool(st.session_state.get("AUTO_TRADING", {}).get("active", False))
    ))

    conn.commit()
    conn.close()

# def log_wallet_transaction(action, amount, balance, price_inr, trade_type="MANUAL"):
#     conn = get_mysql_connection()
#     cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
#     cursor.execute("""
#         INSERT INTO wallet_transactions 
#         (trade_time, action, amount, balance_after, inr_value, trade_type, autotrade_active, status)
#         VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s)
#     """, (
#         action,
#         amount,
#         balance,
#         balance * price_inr,
#         trade_type,
#         bool(st.session_state.get("AUTO_TRADING", {}).get("active", False)),
#         "SUCCESS"   # ✅ explicitly set
#     ))
#     conn.commit()
#     conn.close()

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

def is_autotrade_active_from_db_old():
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
    finally:
        conn.close()

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

# ------------------ AUTO TRADE FUNCTION ------------------

def check_auto_trading_old_with_updated(price_inr):
    """Profit-only auto-trading with auto-disable on errors (DB integrated)"""
    try:
        if not st.session_state.AUTO_TRADING["active"]:
            st.info("🔒 Auto-trade is inactive. No action taken.")
            return

        # --- Fetch last price from DB if not set in session ---
        if st.session_state.AUTO_TRADING["last_price"] == 0:
            # db_last_price = get_last_auto_trade_price_from_db()
            db_last_price = get_latest_auto_start_price()
            if db_last_price > 0:
                st.session_state.AUTO_TRADING["last_price"] = db_last_price
                st.info(f"📌 Restored last trade price ₹{db_last_price:.2f} from DB")
            else:
                st.session_state.AUTO_TRADING["last_price"] = price_inr
                update_last_auto_trade_price_db(price_inr)
                st.info("📌 Initializing last price for auto-trading.")
                return  # Avoid trading immediately after initialization

        # --- Force initial BUY if BTC is 0 and INR is available ---
        if (
            BTC_WALLET['balance'] == 0 and
            INR_WALLET['balance'] >= 20 and
            st.session_state.AUTO_TRADE_STATE.get("last_price", 0) == 0
        ):
            buy_amount_inr = INR_WALLET['balance'] * 0.5
            btc_bought = buy_amount_inr / price_inr
            BTC_WALLET['balance'] += btc_bought
            INR_WALLET['balance'] -= buy_amount_inr

            st.session_state.AUTO_TRADING["last_price"] = price_inr
            st.session_state.AUTO_TRADING["sell_streak"] = 0
            st.session_state.AUTO_TRADE_STATE["last_price"] = price_inr

            update_last_auto_trade_price_db(price_inr)

            msg = f"🟢 Initial Auto-BUY ₹{buy_amount_inr:.2f} → {btc_bought:.6f} BTC at ₹{price_inr:.2f}"
            st.success(msg)
            st.toast(msg)
            send_telegram(msg)

            log_wallet_transaction("AUTO_BUY", btc_bought, BTC_WALLET['balance'], price_inr, "AUTO_INITIAL_BUY")
            log_inr_transaction("AUTO_BUY", -buy_amount_inr, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
            save_trade_log("AUTO_BUY", btc_bought, BTC_WALLET['balance'], price_inr)

            return  # Skip further trading logic for now

        threshold = 5
        min_roi = 0.01
        price_diff = price_inr - st.session_state.AUTO_TRADING["last_price"]

        # --- BUY ---
        if price_diff <= -threshold:
            if st.session_state.AUTO_TRADE_STATE.get("last_price", 0) == 0:
                buy_amount_inr = INR_WALLET['balance'] * 0.5
                if buy_amount_inr >= 20:
                    btc_bought = buy_amount_inr / price_inr
                    BTC_WALLET['balance'] += btc_bought
                    INR_WALLET['balance'] -= buy_amount_inr

                    st.session_state.AUTO_TRADING["last_price"] = price_inr
                    st.session_state.AUTO_TRADING["sell_streak"] = 0
                    st.session_state.AUTO_TRADE_STATE["last_price"] = price_inr

                    update_last_auto_trade_price_db(price_inr)

                    msg = f"🟢 Auto-BUY ₹{buy_amount_inr:.2f} → {btc_bought:.6f} BTC at ₹{price_inr:.2f}"
                    st.success(msg)
                    st.toast(msg)
                    send_telegram(msg)

                    log_wallet_transaction("AUTO_BUY", btc_bought, BTC_WALLET['balance'], price_inr, "AUTO_BUY")
                    log_inr_transaction("AUTO_BUY", -buy_amount_inr, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
                    save_trade_log("AUTO_BUY", btc_bought, BTC_WALLET['balance'], price_inr)

        # --- SELL ---
        elif price_diff >= threshold:
            sell_btc = BTC_WALLET['balance'] * 1
            entry_price = st.session_state.AUTO_TRADE_STATE.get("entry_price", 0)

            if sell_btc >= 0.0001 and entry_price > 0:
                roi = ((price_inr - entry_price) / entry_price) * 100

                if roi >= min_roi:
                    BTC_WALLET['balance'] -= sell_btc
                    inr_received = sell_btc * price_inr
                    INR_WALLET['balance'] += inr_received

                    st.session_state.AUTO_TRADING["last_price"] = price_inr
                    st.session_state.AUTO_TRADING["sell_streak"] = 0
                    st.session_state.AUTO_TRADE_STATE["last_price"] = 0

                    update_last_auto_trade_price_db(price_inr)

                    msg = f"🔴 Auto-SELL {sell_btc:.6f} BTC → ₹{inr_received:.2f} at ₹{price_inr:.2f} | ROI: {roi:.2f}%"
                    st.warning(msg)
                    st.toast(msg)
                    send_telegram(msg)

                    log_wallet_transaction("AUTO_SELL", sell_btc, BTC_WALLET['balance'], price_inr, "AUTO_SELL")
                    log_inr_transaction("AUTO_SELL", inr_received, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
                    save_trade_log("AUTO_SELL", sell_btc, BTC_WALLET['balance'], price_inr, roi)
                else:
                    st.info(f"⚠️ Auto-SELL skipped: ROI {roi:.2f}% < {min_roi}%")

        # --- Auto-disable after 3 failed sells ---
        if st.session_state.AUTO_TRADING["sell_streak"] >= 3:
            st.session_state.AUTO_TRADING["active"] = False
            st.session_state["autotrade_toggle"] = False
            update_wallet_daily_summary(auto_end=True)
            update_autotrade_status_db(0)
            msg = "🛑 Auto-Trade auto-disabled after 3 losing trades"
            st.warning(msg)
            send_telegram(msg)

    except Exception as e:
        st.session_state.AUTO_TRADING["active"] = False
        st.session_state["autotrade_toggle"] = False
        update_wallet_daily_summary(auto_end=True)
        update_autotrade_status_db(0)
        error_msg = f"❌ Auto-Trade stopped due to error: {str(e)}"
        st.error(error_msg)
        send_telegram(error_msg)


def get_last_trade_time_from_db_with_float_error():
    """
    Return the last AUTO trade time as an integer epoch (seconds),
    or None if no AUTO trades exist.
    Works for both Postgres and MySQL DB drivers that return datetime objects.
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT trade_time
            FROM wallet_transactions
            WHERE trade_type LIKE 'AUTO%'
            ORDER BY trade_time DESC
            LIMIT 1
        """)
        row = cursor.fetchone()
        cursor.close()

        if not row:
            return None

        # If using RealDictCursor the row is a dict
        trade_time = row.get("trade_time") if isinstance(row, dict) else row[0]

        if trade_time is None:
            return None

        # If the driver already returns a datetime, convert to epoch
        if isinstance(trade_time, datetime):
            return int(trade_time.timestamp())

        # Otherwise parse string timestamps (fallback)
        ts_str = str(trade_time)
        try:
            # try ISO first
            dt = datetime.fromisoformat(ts_str)
        except Exception:
            # fallback common format (adjust if your DB uses another format)
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")

        return int(dt.timestamp())

    except Exception as e:
        # optional: log or re-raise
        print("⚠️ get_last_trade_time_from_db error:", e)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass

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
        
def start_auto_trading():
    """Mark auto-trade as active without resetting balances"""
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

        # ✅ Get last known balances
        btc_balance, _ = get_last_wallet_balance()
        inr_balance, _ = get_last_inr_balance()

        # ✅ Default to 0 if None
        btc_balance = float(btc_balance if btc_balance is not None else 0.0)
        inr_balance = float(inr_balance if inr_balance is not None else 0.0)

        # 🔹 Insert AUTO_TRADE_START marker (btc wallet)
        cursor.execute("""
            INSERT INTO wallet_transactions 
            (trade_time, action, amount, balance_after, inr_value, trade_type, autotrade_active, status)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s)
        """, (
            "START",
            0.0,
            btc_balance,
            btc_balance,  # kept for compatibility
            "AUTO_TRADE_START",
            True,
            "SUCCESS"
        ))

        # 🔹 Insert AUTO_TRADE_START marker (inr wallet)
        cursor.execute("""
            INSERT INTO inr_wallet_transactions
            (trade_time, action, amount, balance_after, trade_mode, status)
            VALUES (NOW(), %s, %s, %s, %s, %s)
        """, (
            "START",
            0.0,
            inr_balance,
            "LIVE" if REAL_TRADING else "TEST",
            "SUCCESS"
        ))

        conn.commit()

        # ✅ Show balances when autotrade starts
        msg = f"🚀 Auto-Trade ACTIVATED with BTC: {btc_balance:.6f}, INR: ₹{inr_balance:,.2f}"
        st.caption(msg)
        send_telegram(msg)

        # ⚠️ Warning + Telegram if both balances are 0
        if btc_balance == 0.0 and inr_balance == 0.0:
            warn_msg = "⚠️ Auto-Trade started with both BTC and INR balances at 0. No trades will execute until funds are available."
            st.warning(warn_msg)
            send_telegram(warn_msg)

    finally:
        conn.close()

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
        # --- Ensure wallets exist in session ---
        if "BTC_WALLET" not in st.session_state:
            btc_bal, _ = get_last_wallet_balance()
            st.session_state.BTC_WALLET = {"balance": float(btc_bal or 0)}
        if "INR_WALLET" not in st.session_state:
            inr_bal, _ = get_last_inr_balance()
            st.session_state.INR_WALLET = {"balance": float(inr_bal or 0)}

        btc_balance = float(st.session_state.BTC_WALLET.get("balance", 0.0) or 0.0)
        inr_balance = float(st.session_state.INR_WALLET.get("balance", 0.0) or 0.0)

        # 🚨 Stop autotrade immediately if both wallets are empty
        if btc_balance == 0 and inr_balance == 0:
            st.session_state.AUTO_TRADING["active"] = False
            st.session_state["autotrade_toggle"] = False
            update_autotrade_status_db(0)

            log_wallet_transaction("AUTO_STOP", 0, 0, 0, "AUTO_TRADE_STOP")
            log_inr_transaction("AUTO_STOP", 0, 0, "LIVE" if REAL_TRADING else "TEST")
            update_wallet_daily_summary(auto_end=True)

            msg = "⏹️ Auto-Trade stopped: Both INR and BTC wallets are empty."
            st.warning(msg)
            send_telegram(msg)
            return

        # --- Ensure AUTO_TRADING state ---
        if "AUTO_TRADING" not in st.session_state:
            st.session_state.AUTO_TRADING = {"active": True, "entry_price": 0, "last_price": 0, "last_trade": None}

        # --- Active check ---
        db_active = get_autotrade_active_from_db()
        session_active = st.session_state.AUTO_TRADING.get("active", False)
        autotrade_active = bool(db_active) or bool(session_active)
        if not autotrade_active:
            return

        # --- Idle timeout check ---
        idle_timeout = 1800  # 30 min
        last_trade_time = get_last_trade_time_from_db()
        if last_trade_time and (time.time() - last_trade_time) > idle_timeout:
            st.session_state.AUTO_TRADING["active"] = False
            st.session_state["autotrade_toggle"] = False
            update_autotrade_status_db(0)

            btc_bal, _ = get_last_wallet_balance()
            inr_bal, _ = get_last_inr_balance()

            st.session_state.BTC_WALLET["balance"] = btc_bal
            st.session_state.INR_WALLET["balance"] = inr_bal
            log_wallet_transaction("AUTO_STOP", 0, btc_bal, 0, "AUTO_TRADE_STOP")
            log_inr_transaction("AUTO_STOP", 0, inr_bal, "LIVE" if REAL_TRADING else "TEST")
            update_wallet_daily_summary(auto_end=True)

            msg = "⏳ Auto-Trade stopped (idle timeout: 30m no trades)."
            st.warning(msg); send_telegram(msg)
            return

        # --- Trade cooldown check ---
        last_trade = get_last_auto_trade()
        last_type = last_trade.get("trade_type") if last_trade else None
        last_ts = int(last_trade["trade_time"].timestamp()) if last_trade and last_trade.get("trade_time") else 0
        now_ts = int(time.time())
        trade_cooldown = 60
        if last_type in ("AUTO_BUY", "AUTO_SELL") and (now_ts - last_ts < trade_cooldown):
            return  # prevent duplicate fast trades

        # --- Restore last_trade state into session ---
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
        cond1 = (roi is not None and roi >= min_roi and price_diff >= threshold)  # ROI SELL
        cond2 = (inr_balance <= 0 and btc_balance > 0)                            # FORCE SELL (INR empty)
        cond3 = (entry_price == 0 and btc_balance > 0)                            # fallback SELL

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

        btc_bal, _ = get_last_wallet_balance()
        inr_bal, _ = get_last_inr_balance()
        log_wallet_transaction("AUTO_STOP", 0, btc_bal, 0, "AUTO_TRADE_STOP")
        log_inr_transaction("AUTO_STOP", 0, inr_bal, "LIVE" if REAL_TRADING else "TEST")
        update_wallet_daily_summary(auto_end=True)

        error_msg = f"❌ Auto-Trade stopped (technical error): {str(e)}"
        st.error(error_msg); send_telegram(error_msg)


def check_auto_trading_on_04_09_2025(price_inr):
    """
    Final Stable Auto-Trading Logic:
    - Initial BUY only if BTC balance == 0
    - Single BUY → HOLD → SELL cycle
    - ROI + threshold based SELL
    - Idle timeout (30m)
    - Duplicate prevention via DB + cooldown
    - Full DB logging for persistence
    """
    try:
        if not st.session_state.AUTO_TRADING.get("active", False):
            return

        if not price_inr or price_inr <= 0:
            st.info("⚠️ Skipping auto-trade: invalid price.")
            return

        # --- Idle timeout ---
        idle_timeout = 1800  # 30 min
        last_trade_time = get_last_trade_time_from_db()
        if last_trade_time and (time.time() - last_trade_time) > idle_timeout:
            st.session_state.AUTO_TRADING["active"] = False
            st.session_state["autotrade_toggle"] = False
            update_autotrade_status_db(0)

            # STOP marker → is_autotrade_maker = FALSE
            btc_bal = BTC_WALLET.get('balance', 0) or 0.0
            inr_bal = INR_WALLET.get('balance', 0) or 0.0
            log_wallet_transaction("AUTO_STOP", 0, btc_bal, 0, "AUTO_TRADE_STOP")
            log_inr_transaction("AUTO_STOP", 0, inr_bal, "LIVE" if REAL_TRADING else "TEST")

            update_wallet_daily_summary(auto_end=True)
            send_telegram("⏳ Auto-Trade stopped (idle timeout)")
            return

        # --- Trade history (for duplicate prevention) ---
        last_trade = get_last_auto_trade()
        now_ts = int(time.time())
        last_type = last_trade["trade_type"] if last_trade else None
        last_ts = int(last_trade["trade_time"].timestamp()) if last_trade else 0
        trade_cooldown = 120  # 2 minutes

        # --- Config ---
        threshold = 5      # INR price trigger
        min_roi = 0.01     # % minimum profit
        last_price = st.session_state.AUTO_TRADING.get("last_price", 0)
        entry_price = st.session_state.AUTO_TRADING.get("entry_price", 0)
        price_diff = price_inr - last_price if last_price else 0

        # === INITIAL AUTO-BUY (only if BTC = 0) ===
        if BTC_WALLET['balance'] == 0 and INR_WALLET['balance'] >= 20:
            if last_type == "AUTO_INITIAL_BUY" and (now_ts - last_ts < trade_cooldown):
                return  # skip duplicate

            buy_amount_inr = INR_WALLET['balance'] * 0.5
            btc_bought = buy_amount_inr / price_inr

            BTC_WALLET['balance'] = btc_bought
            INR_WALLET['balance'] -= buy_amount_inr

            st.session_state.AUTO_TRADING["entry_price"] = price_inr
            st.session_state.AUTO_TRADING["last_price"] = price_inr

            update_last_auto_trade_price_db(price_inr)

            msg = f"🟢 Initial Auto-BUY {btc_bought:.6f} BTC @ ₹{price_inr:.2f}"
            st.success(msg); st.toast(msg); send_telegram(msg)

            # START marker → is_autotrade_maker = TRUE
            log_wallet_transaction("AUTO_START", 0, BTC_WALLET['balance'], price_inr, "AUTO_TRADE_START")
            log_inr_transaction("AUTO_START", 0, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")

            # BUY marker → is_autotrade_maker = TRUE
            log_wallet_transaction("AUTO_BUY", btc_bought, BTC_WALLET['balance'], price_inr, "AUTO_INITIAL_BUY")
            log_inr_transaction("AUTO_BUY", -buy_amount_inr, INR_WALLET['balance'],
                                "LIVE" if REAL_TRADING else "TEST")
            save_trade_log("AUTO_BUY", btc_bought, BTC_WALLET['balance'], price_inr)
            return

        # === AUTO-SELL (only if BTC > 0 and entry_price set) ===
        if BTC_WALLET['balance'] > 0 and entry_price > 0:
            roi = ((price_inr - entry_price) / entry_price) * 100
            if roi >= min_roi and price_diff >= threshold:
                if last_type == "AUTO_SELL" and (now_ts - last_ts < trade_cooldown):
                    return  # skip duplicate

                sell_btc = BTC_WALLET['balance']
                if sell_btc <= 0:
                    return  # nothing to sell

                inr_received = sell_btc * price_inr

                # ✅ Reset balances first
                BTC_WALLET['balance'] = 0
                INR_WALLET['balance'] += inr_received

                # ✅ Reset entry for next cycle
                st.session_state.AUTO_TRADING["entry_price"] = 0
                st.session_state.AUTO_TRADING["last_price"] = price_inr

                update_last_auto_trade_price_db(price_inr)

                msg = f"🔴 Auto-SELL {sell_btc:.6f} BTC → ₹{inr_received:.2f} @ ₹{price_inr:.2f} | ROI {roi:.4f}%"
                st.warning(msg); st.toast(msg); send_telegram(msg)

                # ✅ Logs now reflect updated balances
                log_wallet_transaction("AUTO_SELL", sell_btc, BTC_WALLET['balance'], price_inr, "AUTO_SELL")
                log_inr_transaction("AUTO_SELL", inr_received, INR_WALLET['balance'],
                                    "LIVE" if REAL_TRADING else "TEST")
                save_trade_log("AUTO_SELL", sell_btc, BTC_WALLET['balance'], price_inr, roi)
                return

        # === Update last price if not set ===
        if last_price == 0:
            st.session_state.AUTO_TRADING["last_price"] = price_inr
            update_last_auto_trade_price_db(price_inr)

    except Exception as e:
        st.session_state.AUTO_TRADING["active"] = False
        st.session_state["autotrade_toggle"] = False
        update_autotrade_status_db(0)

        # STOP marker → is_autotrade_maker = FALSE
        btc_bal = BTC_WALLET.get('balance', 0) or 0.0
        inr_bal = INR_WALLET.get('balance', 0) or 0.0
        log_wallet_transaction("AUTO_STOP", 0, btc_bal, 0, "AUTO_TRADE_STOP")
        log_inr_transaction("AUTO_STOP", 0, inr_bal, "LIVE" if REAL_TRADING else "TEST")

        update_wallet_daily_summary(auto_end=True)
        error_msg = f"❌ Auto-Trade stopped: {str(e)}"
        st.error(error_msg); send_telegram(error_msg)

def check_auto_trading_old(price_inr):
    """Profit-only auto-trading with auto-disable on errors (DB integrated)"""
    try:
        if not st.session_state.AUTO_TRADING["active"]:
            st.info("🔒 Auto-trade is inactive. No action taken.")
            return

        # --- Fetch last price from DB if not set in session ---
        if st.session_state.AUTO_TRADING["last_price"] == 0:
            # db_last_price = get_last_auto_trade_price_from_db()
            db_last_price = get_latest_auto_start_price()
            if db_last_price > 0:
                st.session_state.AUTO_TRADING["last_price"] = db_last_price
                st.info(f"📌 Restored last trade price ₹{db_last_price:.2f} from DB")
            else:
                st.session_state.AUTO_TRADING["last_price"] = price_inr
                update_last_auto_trade_price_db(price_inr)
                st.info("📌 Initializing last price for auto-trading.")
                return  # Avoid trading immediately after initialization

        # --- Force initial BUY if BTC is 0 and INR is available ---
        if (
            BTC_WALLET['balance'] == 0 and
            INR_WALLET['balance'] >= 20 and
            # st.session_state.AUTO_TRADE_STATE.get("last_price", 0) == 0
            get_latest_auto_start_price() == 0   # ✅ check DB, not just session
        ):
            buy_amount_inr = INR_WALLET['balance'] * 0.5
            btc_bought = buy_amount_inr / price_inr
            BTC_WALLET['balance'] += btc_bought
            INR_WALLET['balance'] -= buy_amount_inr

            st.session_state.AUTO_TRADING["last_price"] = price_inr
            st.session_state.AUTO_TRADING["sell_streak"] = 0
            st.session_state.AUTO_TRADE_STATE["last_price"] = price_inr
            st.session_state.AUTO_TRADE_STATE["entry_price"] = price_inr

            update_last_auto_trade_price_db(price_inr)
            update_autotrade_status_db(1)   # ✅ mark auto-trade active

            msg = f"🟢 Initial Auto-BUY ₹{buy_amount_inr:.2f} → {btc_bought:.6f} BTC at ₹{price_inr:.2f}"
            st.success(msg)
            st.toast(msg)
            send_telegram(msg)

            # # --- Log AUTO_TRADE_START marker ---
            # log_wallet_transaction("AUTO_START", 0, BTC_WALLET['balance'], price_inr, "AUTO_TRADE_START")
            # log_inr_transaction("AUTO_START", 0, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
            # update_autotrade_status_db(1)   # ✅ mark as active in DB

            log_wallet_transaction("AUTO_BUY", btc_bought, BTC_WALLET['balance'], price_inr, "AUTO_INITIAL_BUY")
            log_inr_transaction("AUTO_BUY", -buy_amount_inr, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
            save_trade_log("AUTO_BUY", btc_bought, BTC_WALLET['balance'], price_inr)

            return  # Skip further trading logic for now

        threshold = 5
        min_roi = 0.01
        price_diff = price_inr - st.session_state.AUTO_TRADING["last_price"]

        # --- BUY ---
        if price_diff <= -threshold:
            if st.session_state.AUTO_TRADE_STATE.get("last_price", 0) == 0:
                buy_amount_inr = INR_WALLET['balance'] * 0.5
                if buy_amount_inr >= 20:
                    btc_bought = buy_amount_inr / price_inr
                    BTC_WALLET['balance'] += btc_bought
                    INR_WALLET['balance'] -= buy_amount_inr

                    st.session_state.AUTO_TRADING["last_price"] = price_inr
                    st.session_state.AUTO_TRADING["sell_streak"] = 0
                    st.session_state.AUTO_TRADE_STATE["last_price"] = price_inr

                    update_last_auto_trade_price_db(price_inr)

                    msg = f"🟢 Auto-BUY ₹{buy_amount_inr:.2f} → {btc_bought:.6f} BTC at ₹{price_inr:.2f}"
                    st.success(msg)
                    st.toast(msg)
                    send_telegram(msg)

                    log_wallet_transaction("AUTO_BUY", btc_bought, BTC_WALLET['balance'], price_inr, "AUTO_BUY")
                    log_inr_transaction("AUTO_BUY", -buy_amount_inr, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
                    save_trade_log("AUTO_BUY", btc_bought, BTC_WALLET['balance'], price_inr)

        # --- SELL ---
        elif price_diff >= threshold:
            sell_btc = BTC_WALLET['balance'] * 1
            entry_price = st.session_state.AUTO_TRADE_STATE.get("entry_price", 0)

            if sell_btc >= 0.0001 and entry_price > 0:
                roi = ((price_inr - entry_price) / entry_price) * 100

                if roi >= min_roi:
                    BTC_WALLET['balance'] -= sell_btc
                    inr_received = sell_btc * price_inr
                    INR_WALLET['balance'] += inr_received

                    st.session_state.AUTO_TRADING["last_price"] = price_inr
                    st.session_state.AUTO_TRADING["sell_streak"] = 0
                    st.session_state.AUTO_TRADE_STATE["last_price"] = 0
                    st.session_state.AUTO_TRADE_STATE["entry_price"] = 0

                    update_last_auto_trade_price_db(price_inr)

                    msg = f"🔴 Auto-SELL {sell_btc:.6f} BTC → ₹{inr_received:.2f} at ₹{price_inr:.2f} | ROI: {roi:.2f}%"
                    st.warning(msg)
                    st.toast(msg)
                    send_telegram(msg)

                    log_wallet_transaction("AUTO_SELL", sell_btc, BTC_WALLET['balance'], price_inr, "AUTO_SELL")
                    log_inr_transaction("AUTO_SELL", inr_received, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
                    save_trade_log("AUTO_SELL", sell_btc, BTC_WALLET['balance'], price_inr, roi)
                else:
                    st.session_state.AUTO_TRADING["sell_streak"] += 1
                    st.info(f"⚠️ Auto-SELL skipped: ROI {roi:.2f}% < {min_roi}%")

        # --- Auto-disable after 3 failed sells ---
        if st.session_state.AUTO_TRADING["sell_streak"] >= 3:
            st.session_state.AUTO_TRADING["active"] = False
            st.session_state["autotrade_toggle"] = False
            update_wallet_daily_summary(auto_end=True)
            update_autotrade_status_db(0)

            msg = "🛑 Auto-Trade auto-disabled after 3 losing trades"
            st.warning(msg)
            send_telegram(msg)

            # ✅ Log STOP marker transaction explicitly
            log_wallet_transaction("AUTO_STOP", 0, BTC_WALLET['balance'], price_inr, "AUTO_TRADE_STOP")
            log_inr_transaction("AUTO_STOP", 0, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")

    except Exception as e:
        st.session_state.AUTO_TRADING["active"] = False
        st.session_state["autotrade_toggle"] = False
        update_wallet_daily_summary(auto_end=True)
        update_autotrade_status_db(0)
        error_msg = f"❌ Auto-Trade stopped due to error: {str(e)}"
        st.error(error_msg)
        send_telegram(error_msg)

    # Auto-STOP logic if needed can go here...
    # is_autotrade_marker changed into 1 to Ture
def get_last_auto_trade_price_from_db_old():
    conn = get_mysql_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT last_price FROM wallet_transactions 
        WHERE is_autotrade_marker = TRUE
        ORDER BY trade_time DESC LIMIT 1
    """)
    result = cursor.fetchone()
    conn.close()
    return float(result['balance_after']) if result and result['balance_after'] is not None else 0.0

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

def update_autotrade_status_db_old(status: int):
    """Insert a marker row indicating auto-trade status (active/inactive)."""
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute("""
            INSERT INTO wallet_transactions 
            (trade_time, action, amount, balance_after, inr_value, trade_type, autotrade_active, is_autotrade_marker)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            datetime.now(),         # trade_time
            "AUTO_META",            # action
            0,                      # amount
            0,                      # balance_after
            0,                      # inr_value
            "AUTO_TRADE",           # trade_type
            status,                 # autotrade_active
            1                       # is_autotrade_marker
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


def update_autotrade_status_db_new(active: int, last_price: float = 0.0):
    """
    Insert a marker row indicating auto-trade start/stop.
    Also ensures today's wallet_history has auto_start_price set when starting.
    """
    conn = None
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        trade_type = "AUTO_TRADE_START" if active else "AUTO_TRADE_STOP"
        cursor.execute("""
            INSERT INTO wallet_transactions 
            (trade_time, action, amount, balance_after, inr_value, trade_type, autotrade_active, is_autotrade_marker, status, last_price)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            trade_type, 0, 0, 0, trade_type, bool(active), True, "SUCCESS", last_price
        ))

        # If starting, ensure wallet_history row for today and set auto_start_price if not present
        if active:
            today = datetime.now().date()
            cursor.execute("SELECT id, auto_start_price FROM wallet_history WHERE trade_date=%s", (today,))
            row = cursor.fetchone()
            if not row:
                cursor.execute("""
                    INSERT INTO wallet_history (trade_date, start_balance, end_balance, current_inr_value, trade_count, auto_start_price)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (today, 0, 0, 0, 0, last_price))
            else:
                if not row.get("auto_start_price"):
                    cursor.execute("UPDATE wallet_history SET auto_start_price=%s WHERE trade_date=%s", (last_price, today))

        conn.commit()
    except Exception as e:
        print("update_autotrade_status_db error:", e)
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

def deduct_balance_old(amount):
    with get_mysql_connection() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute (
                 "INSERT INTO user_wallets (user_email, inr_balance, customer_id) VALUES (%s, %s, %s)", ('testing@gmail.com', amount, CUSTOMER_ID)
                 )
        con.commit()

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

# def background_autotrade_loop():
#     """Runs continuously in the background, checking DB flag & auto-trading."""
#     while True:
#         try:
#             if get_autotrade_active_from_db():  # ✅ DB decides
#                 price_inr = cd_get_market_price("BTCINR")
#                 if price_inr:
#                     check_auto_trading(price_inr)
#                 else:
#                     # API failed → still run idle monitoring
#                     check_auto_trading(0)   # ✅ safe call for idle check
#             else:
#                 time.sleep(5)  # short sleep if inactive
#         except Exception as e:
#             print("⚠️ Background auto-trade error:", str(e))
#         time.sleep(AUTO_REFRESH_INTERVAL)  # e.g., 15 sec loop

AUTO_REFRESH_INTERVAL = 15  # seconds

# def background_autotrade_loop():
#     """Runs continuously in the background, checking DB flag & auto-trading + idle timeout."""
#     while True:
#         try:
#             if get_autotrade_active_from_db():  # ✅ DB decides

#                 # --- Idle Monitoring ---
#                 last_trade_time = get_last_trade_time_from_logs()
#                 if last_trade_time:
#                     idle_minutes = (datetime.now() - last_trade_time).total_seconds() / 60
#                     if idle_minutes > 60:
#                         update_wallet_daily_summary(auto_end=True)
#                         update_autotrade_status_db(0)

#                         msg = f"⏰ Auto-Trade auto-stopped after {idle_minutes:.0f} minutes of inactivity (background)"
#                         print(msg)
#                         send_telegram(msg)

#                         log_wallet_transaction("AUTO_IDLE_STOP", 0, BTC_WALLET['balance'], 0, "AUTO_IDLE_STOP")
#                         log_inr_transaction("AUTO_IDLE_STOP", 0, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
#                         continue

#                 # --- Run trade logic ---
#                 price_inr = cd_get_market_price("BTCINR")
#                 if price_inr:
#                     check_auto_trading(price_inr)

#             else:
#                 time.sleep(5)

#         except Exception as e:
#             print("⚠️ Background auto-trade error:", str(e))

#         time.sleep(AUTO_REFRESH_INTERVAL)  # e.g., 15 sec loop


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


# ✅ Withdrawal Simulation
# st.subheader("🏧 Wallet Withdraw")
# payout_amt = st.number_input("Payout ₹", 100, step=100, key="paytm_withdraw")
# if st.button("🚀 Withdraw"):
#     if payout_amt <= st.session_state["inr_balance"]:
#         st.session_state["inr_balance"] -= payout_amt
#         if REAL_TRADING:
#             st.success(f"✅ ₹{payout_amt:.2f} withdrawal logged (Production mode - handle actual payout)")
#             recipients = get_all_recipients()
#             recipient_names = [f"{r['name']} ({r['method']})" for r in recipients]
#             selected = st.selectbox("Saved Recipient", ["-- New Recipient --"] + recipient_names)

#             if selected != "-- New Recipient --":
#                 sel = recipients[recipient_names.index(selected) - 1]
#                 method = sel['method']
#                 name = sel['name']
#                 acc_no = sel['account_number']
#                 ifsc = sel['ifsc']
#                 upi = sel['upi_id']
#             else:
#                 method = st.radio("Send via", ["BANK", "UPI"])
#                 name = st.text_input("Recipient Name")
#                 acc_no = st.text_input("Account Number") if method == "BANK" else ""
#                 ifsc = st.text_input("IFSC Code") if method == "BANK" else ""
#                 upi = st.text_input("UPI ID") if method == "UPI" else ""

#             amount = st.number_input("Amount ₹", 100.0, step=100.0)

#             if st.button("🚀 Pay Now"):
#                 if amount > get_inr_balance():
#                     st.error("❌ Insufficient balance")
#                 elif not name or (method == "BANK" and (not acc_no or not ifsc)) or (method == "UPI" and not upi):
#                     st.warning("⚠️ Fill all fields")
#                 else:
#                     with st.spinner("Processing..."):
#                         order_id = f"WD{uuid.uuid4().hex[:8].upper()}"
#                         token = get_access_token()
#                         res = send_paytm_payout(token, order_id, amount, name, method, acc_no, ifsc, upi)
#                         status = res.get("status", "FAILED")

#                         log_payout(order_id, name, method, acc_no, ifsc, upi, amount, status, res)
#                         save_recipient_if_new(name, method, acc_no, ifsc, upi)

#                         if status == "SUCCESS":
#                             deduct_balance(amount)
#                             st.success(f"✅ ₹{amount:.2f} sent to {name}")
#                         else:
#                             st.error("❌ Payout failed")
#                         st.json(res)
#         else:
#             st.success(f"✅ ₹{payout_amt:.2f} withdrawn (Simulated - TEST)")
#     else:
#         st.error("❌ Not enough balance")


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

# CODE CHANGES ENDED FOR WITHDRAWAL BUTTON LOGIC -----

# if st.button("🚀 Withdraw"):
#     if method == "BANK" and (not acc_no or not ifsc):
#         st.warning("❗ Please enter bank details.")
#     elif method == "UPI" and not upi:
#         st.warning("❗ Please enter UPI ID.")
#     elif not name:
#         st.warning("❗ Name is required.")
#     else:
#         save_recipient_if_new(name, method, acc_no, ifsc, upi)
#         real_balance = get_current_inr_balance()

#         if payout_amt > real_balance:
#             st.error("❌ Insufficient balance")
#         else:
#             # ✅ Deduct + Log with full details
#             deduct_balance(payout_amt, method, name, acc_no, ifsc, upi)

#             # ✅ Refresh session balance
#             st.session_state["inr_balance"] = get_current_inr_balance()

#             # ✅ Notify
#             send_telegram(f"✅ ₹{payout_amt:.2f} payout sent to {name} via {method}")
#             st.success(f"✅ ₹{payout_amt:.2f} sent to {name} via {method}")

# # --- Show Current INR Wallet (for simulation) ---
st.info(f"💼 Current INR Wallet Balance: ₹{st.session_state['inr_balance']:.2f}")

# --- INR Wallet Actions ---
# st.write("### 🏦 INR Wallet Actions")

# inr_deposit = st.number_input("Deposit INR", min_value=0, step=100, key="inr_deposit")
# inr_withdraw = st.number_input("Withdraw INR", min_value=0, step=100, key="inr_withdraw")

# if st.button("Process INR Wallet Transaction"):
#     if inr_deposit > 0:
#         INR_WALLET['balance'] += inr_deposit
#         log_inr_transaction("DEPOSIT", inr_deposit, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
#         st.success(f"✅ Deposited ₹{inr_deposit}")
#     elif inr_withdraw > 0:
#         if INR_WALLET['balance'] >= inr_withdraw:
#             INR_WALLET['balance'] -= inr_withdraw
#             log_inr_transaction("WITHDRAW", -inr_withdraw, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
#             st.success(f"✅ Withdrew ₹{inr_withdraw}")
#         else:
#             st.error("❌ Insufficient INR Balance")



# st.write("### 🏦 INR Wallet Actions")

# inr_deposit = st.number_input("Deposit INR", min_value=0, step=100, key="inr_deposit")
# inr_withdraw = st.number_input("Withdraw INR", min_value=0, step=100, key="inr_withdraw")

# if st.button("Process INR Wallet Transaction"):
#     current_balance = get_razorpay_balance()
#     trade_mode = "LIVE" if REAL_TRADING else "TEST"

#     if inr_deposit > 0:
#         if current_balance >= inr_deposit:
#             INR_WALLET['balance'] += inr_deposit
#             log_inr_transaction("DEPOSIT", inr_deposit, INR_WALLET['balance'], trade_mode)
#             st.success(f"✅ Deposited ₹{inr_deposit}")
#         else:
#             st.error("❌ Insufficient Razorpay balance for deposit")

#     elif inr_withdraw > 0:
#         if INR_WALLET['balance'] >= inr_withdraw:
#             INR_WALLET['balance'] -= inr_withdraw
#             log_inr_transaction("WITHDRAW", -inr_withdraw, INR_WALLET['balance'], trade_mode)
#             st.success(f"✅ Withdrew ₹{inr_withdraw}")
#         else:
#             st.error("❌ Insufficient INR Wallet balance")


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
with wallet_col1:
    st.metric("BTC Balance", f"{BTC_WALLET['balance']:.4f} BTC")
with wallet_col2:
    st.metric("INR Value", f"₹{BTC_WALLET['balance'] * price_inr:,.2f}")

    balance = INR_WALLET.get("balance", 0)

    # If it's a tuple, pick first element
    if isinstance(balance, tuple):
        balance = balance[0] if balance else 0

    try:
        balance = float(balance)
    except (TypeError, ValueError):
        balance = 0

    st.metric("INR Wallet Balance", f"₹{balance:,.2f}")
    # st.metric("INR Wallet Balance", f"₹{INR_WALLET['balance']:,.2f}")

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

# ------------------ START BACKGROUND THREAD ------------------

# if "autotrade_thread_started" not in st.session_state:
#     st.session_state.autotrade_thread_started = True
#     t = threading.Thread(target=background_autotrade_loop, daemon=True)
#     t.start()

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


# --- Auto Trade Button old on 29-08-2025---
# if st.button(f"{'🚀 Start' if not st.session_state.autotrade_toggle else '🛑 Stop'} Auto-Trade"):
#     st.session_state.autotrade_toggle = not st.session_state.autotrade_toggle
#     st.session_state.AUTO_TRADING["active"] = st.session_state.autotrade_toggle
    
#     if st.session_state.autotrade_toggle:
#         # Initialize auto-trade
#         st.session_state.AUTO_TRADING.update({
#             "last_price": price_inr,
#             "sell_streak": 0
#         })
#         st.session_state.AUTO_TRADE_STATE["entry_price"] = price_inr
#         st.session_state.last_trade_time = datetime.now()
#         msg = f"🚀 Auto-Trade ACTIVATED at ₹{price_inr:.2f}"

#         # ✅ Persist to DB
#         log_wallet_transaction("AUTO_START", 0, BTC_WALLET['balance'], price_inr, "AUTO_TRADE_START")
#         log_inr_transaction("AUTO_START", 0, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
#         update_autotrade_status_db(1)  # DB flag → thread picks up
#     else:
#         update_wallet_daily_summary(auto_end=True)
#         msg = f"🛑 Auto-Trade STOPPED at ₹{price_inr:.2f}"

#         log_wallet_transaction("AUTO_STOP", 0, BTC_WALLET['balance'], price_inr, "AUTO_TRADE_STOP")
#         log_inr_transaction("AUTO_STOP", 0, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
#         update_autotrade_status_db(0)  # DB flag → thread stops
    
#     st.toast(msg)
#     send_telegram(msg)
#     log_wallet_transaction("AUTO_TRADE_TOGGLE", 0, BTC_WALLET['balance'], price_inr, 
#                          "AUTO_TRADE_START" if st.session_state.autotrade_toggle else "AUTO_TRADE_STOP")


# # --- Auto Trade Button OLD ---
# if st.button(f"{'🚀 Start' if not st.session_state.autotrade_toggle else '🛑 Stop'} Auto-Trade"):
#     st.session_state.autotrade_toggle = not st.session_state.autotrade_toggle
#     st.session_state.AUTO_TRADING["active"] = st.session_state.autotrade_toggle
    
#     if st.session_state.autotrade_toggle:
#         # Initialize auto-trade
#         st.session_state.AUTO_TRADING.update({
#             "last_price": price_inr,
#             "sell_streak": 0
#         })
#         st.session_state.AUTO_TRADE_STATE["entry_price"] = price_inr
#         msg = f"🚀 Auto-Trade ACTIVATED at ₹{price_inr:.2f}"

#         # ✅ Log clean START marker
#         log_wallet_transaction("AUTO_START", 0, BTC_WALLET['balance'], price_inr, "AUTO_TRADE_START")
#         log_inr_transaction("AUTO_START", 0, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
#         update_autotrade_status_db(1)
#     else:
#         update_wallet_daily_summary(auto_end=True)
#         msg = f"🛑 Auto-Trade STOPPED at ₹{price_inr:.2f}"

#         # ✅ Log clean STOP marker
#         log_wallet_transaction("AUTO_STOP", 0, BTC_WALLET['balance'], price_inr, "AUTO_TRADE_STOP")
#         log_inr_transaction("AUTO_STOP", 0, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
#         update_autotrade_status_db(0)
    
#     st.toast(msg)
#     send_telegram(msg)
#     log_wallet_transaction("AUTO_TRADE_TOGGLE", 0, BTC_WALLET['balance'], price_inr, 
#                          "AUTO_TRADE_START" if st.session_state.autotrade_toggle else "AUTO_TRADE_STOP")
#  OLD BUTTON DESIGN FINISH #####
# if st.button(f"{'🚀 Start' if not st.session_state.autotrade_toggle else '🛑 Stop'} Auto-Trade"):
#     # Flip toggle
#     st.session_state.autotrade_toggle = not st.session_state.autotrade_toggle
#     st.session_state.AUTO_TRADING["active"] = st.session_state.autotrade_toggle
    
#     if st.session_state.autotrade_toggle:
#         # --- START ---
#         if not get_autotrade_active_from_db():   # ✅ only insert if not already active
#             st.session_state.AUTO_TRADING.update({
#                 "last_price": price_inr,
#                 "sell_streak": 0
#             })
#             st.session_state.AUTO_TRADE_STATE["entry_price"] = price_inr
#             msg = f"🚀 Auto-Trade ACTIVATED at ₹{price_inr:.2f}"
            
#             # ✅ Log clean START marker
#             log_wallet_transaction("AUTO_START", 0, BTC_WALLET['balance'], price_inr, "AUTO_TRADE_START")
#             log_inr_transaction("AUTO_START", 0, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
#             update_autotrade_status_db(1)
#         else:
#             msg = "⚠️ Auto-Trade is already active — ignoring duplicate START."

#     else:
#         # --- STOP ---
#         if get_autotrade_active_from_db():   # ✅ only insert if currently active
#             update_wallet_daily_summary(auto_end=True)
#             msg = f"🛑 Auto-Trade STOPPED at ₹{price_inr:.2f}"
            
#             # ✅ Log clean STOP marker
#             log_wallet_transaction("AUTO_STOP", 0, BTC_WALLET['balance'], price_inr, "AUTO_TRADE_STOP")
#             log_inr_transaction("AUTO_STOP", 0, INR_WALLET['balance'], "LIVE" if REAL_TRADING else "TEST")
#             update_autotrade_status_db(0)
#         else:
#             msg = "⚠️ Auto-Trade is already stopped — ignoring duplicate STOP."

#     st.toast(msg)
#     send_telegram(msg)


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

# --- Daily Summary ---
st.subheader("📊 INR Wallet - Daily Summary")
summary = get_daily_wallet_summary()
if summary:
    df = pd.DataFrame(summary)
    # df["net"] = df["deposits"] - df["withdrawals"]
    st.dataframe(df)
else:
    st.info("No wallet transactions yet.")

# --- Hourly Candlestick Chart ---
# st.write("### 📊 Live BTC Chart")

# @st.cache_data(ttl=300)

# def get_historical_klines(market="BTCINR", interval="1h", limit=168):
#     """
#     Fetch historical OHLCV data from CoinDCX
#     interval: "1m", "5m", "15m", "1h", "1d"
#     limit: number of candles
#     """
#     url = f"{BASE_URL}/exchange/v1/market_data/ohlc"
#     payload = {
#         "pair": market,
#         "interval": interval,
#         "limit": limit
#     }
#     try:
#         res = requests.post(url, json=payload)
#         data = res.json()
#         # Format into list of [time, open, high, low, close, volume]
#         ohlcv = []
#         for item in data[market]:
#             ohlcv.append([
#                 datetime.fromtimestamp(item["time"]/1000),  # convert ms → datetime
#                 float(item["open"]),
#                 float(item["high"]),
#                 float(item["low"]),
#                 float(item["close"]),
#                 float(item["volume"])
#             ])
#         return ohlcv
#     except Exception as e:
#         print("Error fetching OHLCV:", e)
#         return []

# def get_hourly_klines():
#     try:
#         # data = client.get_historical_klines(
#         #     "BTCUSDT",
#         #     Client.KLINE_INTERVAL_1HOUR,
#         #     "7 day ago UTC"
#         # )
#         data = get_historical_klines("BTCINR", interval="1h", limit=168)  # last 7 days
#         df = pd.DataFrame(data, columns=[
#             "timestamp", "open", "high", "low", "close", "volume",
#             "close_time", "quote_asset_volume", "number_of_trades",
#             "taker_buy_base_volume", "taker_buy_quote_volume", "ignore"
#         ])
#         df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
#         df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
#         return df[["timestamp", "open", "high", "low", "close"]]
#     except Exception as e:
#         st.error(f"Error fetching data: {e}")
#         return pd.DataFrame()

# # Date selection
# st.write("Select Date Range:")
# date_col1, date_col2 = st.columns(2)
# with date_col1:
#     start_date = st.date_input("From", value=datetime.today() - timedelta(days=3))
# with date_col2:
#     end_date = st.date_input("To", value=datetime.today())

# hist_df = get_hourly_klines()
# if not hist_df.empty:
#     filtered_df = hist_df[
#         (hist_df['timestamp'].dt.date >= start_date) &
#         (hist_df['timestamp'].dt.date <= end_date)
#     ]
    
#     if filtered_df.empty:
#         filtered_df = hist_df.tail(24)  # Show last 24 hours if empty
    
#     fig = go.Figure(go.Candlestick(
#         x=filtered_df['timestamp'],
#         open=filtered_df['open'],
#         high=filtered_df['high'],
#         low=filtered_df['low'],
#         close=filtered_df['close'],
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

st.write("### 📊 Live BTC Chart")

@st.cache_data(ttl=300)
def get_historical_klines(market="BTCINR", interval="1h", limit=168):
    """
    Fetch historical OHLCV data from CoinDCX
    interval: "1m", "5m", "15m", "1h", "1d"
    limit: number of candles
    """
    url = f"{BASE_URL}/exchange/v1/market_data/ohlc"
    payload = {
        "pair": market.upper(),
        "interval": interval,
        "limit": limit
    }
    try:
        res = requests.post(url, json=payload)
        data = res.json()

        if market.upper() not in data:
            return None   # ❌ not found → caller decides fallback

        ohlcv = []
        for item in data[market.upper()]:
            ohlcv.append([
                datetime.fromtimestamp(item["time"] / 1000),  # ms → datetime
                float(item["open"]),
                float(item["high"]),
                float(item["low"]),
                float(item["close"]),
                float(item["volume"])
            ])
        return ohlcv
    except Exception as e:
        st.error(f"Error fetching OHLCV: {e}")
        return None


def get_hourly_klines():
    try:
        # ✅ Try BTCINR first
        data = get_historical_klines("BTCINR", interval="1h", limit=168)

        # ✅ If not available, fallback to BTCUSDT
        if not data:
            st.warning("BTCINR not available, falling back to BTCUSDT")
            data = get_historical_klines("BTCUSDT", interval="1h", limit=168)

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=[
            "timestamp", "open", "high", "low", "close", "volume"
        ])
        df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
        return df
    except Exception as e:
        st.error(f"Error fetching data: {e}")
        return pd.DataFrame()


# @st.cache_data(ttl=300)
# def get_historical_klines(market="BTCINR", interval="1h", limit=168):
#     """
#     Fetch historical OHLCV data from CoinDCX
#     interval: "1m", "5m", "15m", "1h", "1d"
#     limit: number of candles
#     """
#     url = f"{BASE_URL}/exchange/v1/market_data/ohlc"
#     payload = {
#         "pair": market,
#         "interval": interval,
#         "limit": limit
#     }
#     try:
#         res = requests.post(url, json=payload)
#         data = res.json()
#         ohlcv = []
#         for item in data[market]:
#             ohlcv.append([
#                 datetime.fromtimestamp(item["time"] / 1000),  # ms → datetime
#                 float(item["open"]),
#                 float(item["high"]),
#                 float(item["low"]),
#                 float(item["close"]),
#                 float(item["volume"])
#             ])
#         return ohlcv
#     except Exception as e:
#         st.error(f"Error fetching OHLCV: {e}")
#         return []

# def get_hourly_klines():
#     try:
#         data = get_historical_klines("BTCINR", interval="1h", limit=168)  # last 7 days
#         if not data:
#             return pd.DataFrame()

#         df = pd.DataFrame(data, columns=[
#             "timestamp", "open", "high", "low", "close", "volume"
#         ])
#         # Already converted to datetime above, no unit="ms" needed
#         df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
#         return df
#     except Exception as e:
#         st.error(f"Error fetching data: {e}")
#         return pd.DataFrame()

# Date selection
st.write("Select Date Range:")
date_col1, date_col2 = st.columns(2)
with date_col1:
    start_date = st.date_input("From", value=datetime.today() - timedelta(days=3))
with date_col2:
    end_date = st.date_input("To", value=datetime.today())

hist_df = get_hourly_klines()
if not hist_df.empty:
    filtered_df = hist_df[
        (hist_df['timestamp'].dt.date >= start_date) &
        (hist_df['timestamp'].dt.date <= end_date)
    ]
    
    if filtered_df.empty:
        filtered_df = hist_df.tail(24)  # fallback to last 24h
    
    fig = go.Figure(go.Candlestick(
        x=filtered_df['timestamp'],
        open=filtered_df['open'],
        high=filtered_df['high'],
        low=filtered_df['low'],
        close=filtered_df['close'],
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
    st.warning("No data available for chart.")

# --- rerun for auto-refresh ---
time.sleep(10)
st.rerun()
# st.experimental_rerun()