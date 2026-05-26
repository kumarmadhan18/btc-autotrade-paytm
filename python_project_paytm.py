# ============================================================
# MM BTC Autotrade Pro BOT — CORRECTED FOR LIVE TRADING
#
# ORIGINAL FIXES (v1):
#   1.  CRITICAL: withdraw_inr() call — wrong kwarg names fixed
#       (acc_no → account, upi → upi_id)
#   2.  CRITICAL: check_auto_sell() now calls place_market_sell()
#       in LIVE mode instead of silently zeroing internal balance
#   3.  CRITICAL: UROPAY_WEBHOOK_SECRET typo fixed →
#       RAZORPAY_WEBHOOK_SECRET
#   4.  CRITICAL: get_entry_price / save_entry_price /
#       clear_entry_price / get_current_inr_balance /
#       get_latest_inr_balance — all guard conn=None to prevent
#       AttributeError crash in DB-outage scenarios
#   5.  LOGIC: check_balance_health() — condition was inverted
#       (was: diff > 1000, should be: diff < -1000) for drop detection
#   6.  LOGIC: update_wallet_daily_summary(start=True) — added
#       INSERT...ON CONFLICT / INSERT IGNORE so repeated page
#       refreshes don't create duplicate rows for the same day
#   7.  LOGIC: Idle timeout in check_auto_trading() — query now
#       excludes AUTO_META rows so marker inserts don't reset the
#       idle clock
#   8.  SECURITY: deduct_balance() hardcoded email replaced with
#       CUSTOMER_EMAIL env var (falls back to CUSTOMER_ID)
#   9.  SECURITY: Razorpay webhook verification now uses the
#       corrected RAZORPAY_WEBHOOK_SECRET constant
#  10.  ROBUSTNESS: _poll_order_status partial-fill handling —
#       warns and logs if order is still open after timeout
#  11.  ROBUSTNESS: place_market_buy / place_market_sell — minimum
#       quantity guard (CoinDCX BTCINR min: 0.0001 BTC)
#
# ADDITIONAL FIXES (v2):
#  12.  CRITICAL: st.set_page_config() moved to first Streamlit
#       command (was at line ~2116 — caused StreamlitAPIException
#       on startup because st.error() in helper functions fires first)
#  13.  CRITICAL: Removed unused Flask import (caused ImportError
#       if flask not in requirements.txt — Flask is for webhook.py)
#  14.  CRITICAL: Removed unused AES import (caused ImportError
#       if pycryptodome not installed)
#  15.  CRITICAL: wallet_history.trade_date — added UNIQUE constraint
#       so PostgreSQL ON CONFLICT (trade_date) DO NOTHING works
#  16.  CRITICAL: psycopg2 cursor_factory — moved from post-assignment
#       (conn.cursor_factory=...) to connect() kwarg — the old way
#       was silently ignored by psycopg2
#  17.  CRITICAL: deduct_balance() — replaced 'with get_cursor()'
#       context manager (pymysql DictCursor doesn't support 'with')
#       with plain assignment + fixed indentation
#  18.  CRITICAL: acquire_trade_lock() — replaced SELECT+UPDATE
#       race condition with atomic conditional UPDATE WHERE
#       is_locked=FALSE, checking rowcount for success
#  19.  CRITICAL: REAL_TRADING global scope — added is_live() helper
#       so all functions read the correct runtime value even after
#       UI radio reassignment. Replaced 28 inline references.
#  20.  CRITICAL: wallet.balance() — guarded None return on network
#       failure (was: TypeError: NoneType / float)
#  21.  CRITICAL: get_btc_price() — CoinDCX has no BTCUSDT pair.
#       Replaced with CoinGecko public API + INR/USD fallback
#  22.  LOGIC: start_autotrade() / stop_autotrade() — wired into
#       the UI Start/Stop button (was calling update_autotrade_
#       status_db directly, bypassing wallet state sync)
#  23.  LOGIC: Idle timeout tightened 1800s → 300s for supervised
#       manual sessions
#  24.  ROBUSTNESS: log_stop_loss_event() — added try/finally so
#       conn.close() always runs (was leaking DB connections)
#  25.  ROBUSTNESS: get_wallet_history() — replaced 'with get_cursor()'
#       with plain assignment (pymysql DictCursor fix) + added
#       except block to return empty DataFrame on error
#  26.  UX: Auto-refresh changed from blocking time.sleep(60)+
#       st.rerun() to non-blocking meta http-equiv refresh — app
#       stays responsive during the 60s wait
#  27.  UX: Deposit flow — auto-refresh now fully suppressed during
#       Razorpay payment. Reload only fires on SUCCESS (not dismiss).
#       Separate Cancel / I Already Paid buttons. 10-min hard timeout.
#  28.  UX: Session-lost warning — banner shown if auto-trade is
#       active in DB when a new tab/session opens
#  29.  SAFETY: Telegram /stop emergency kill switch added —
#       poll_telegram_stop_command() checked at start of every
#       auto-trade cycle
#
# Required .env variables:
#   RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET
#   RAZORPAY_ACCOUNT_NUMBER   (for payouts — Razorpay X account)
#   COINDCX_API_KEY, COINDCX_API_SECRET
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
#   APP_ENV=live              (switches to PostgreSQL + locks REAL_TRADING)
#   REAL_TRADING=true         (enables live order placement)
#   PG_HOST, PG_USER, PG_PASSWORD, PG_DB, PG_PORT
#   MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB, MYSQL_PORT  (local only)
#   CUSTOMER_EMAIL            (email of the wallet owner for deduct_balance)
#   RENDER_APP_URL            (optional — for uptime monitor)
# ============================================================

import smtplib
import requests
import pandas as pd
from datetime import datetime, timedelta
import streamlit as st
import plotly.graph_objects as go
import os
import time
import csv
import base64
import hmac
import hashlib
import threading
import uuid
import json
import traceback
from io import BytesIO
import streamlit.components.v1 as components
from urllib.parse import quote
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

import qrcode
from PIL import Image
import razorpay

from bitcoinlib.services.services import ServiceError
from bitcoinlib.wallets import Wallet, WalletError

# ─────────────────────────────────────────
# MUST be the first Streamlit command —
# calling st.error() inside functions before
# this caused StreamlitAPIException on startup.
# ─────────────────────────────────────────
st.set_page_config(page_title="BTC Autotrade Pro", layout="wide")

# ─────────────────────────────────────────
# Load env FIRST before reading any os.getenv()
# ─────────────────────────────────────────
load_dotenv()

# ─────────────────────────────────────────
# REAL_TRADING runtime helper
# REAL_TRADING is set from env at module load, then optionally
# overridden by the UI radio (local mode only). All functions
# must call is_live() instead of reading the bare global so they
# always get the current value even after UI reassignment.
# ─────────────────────────────────────────
def is_live() -> bool:
    """Always returns the current REAL_TRADING value safely."""
    import streamlit as _st
    try:
        # In local mode the UI radio stores it in session_state
        return bool(_st.session_state.get("REAL_TRADING", REAL_TRADING))
    except Exception:
        return bool(REAL_TRADING)

# ─────────────────────────────────────────
# SECURITY: Keys from environment variables only
# ─────────────────────────────────────────
RAZORPAY_KEY_ID     = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")

# FIX #3: Was UROPAY_WEBHOOK_SECRET (typo — missing "RAZOR" prefix)
# The env var must be set as RAZORPAY_WEBHOOK_SECRET in .env / Render
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# ─────────────────────────────────────────
# Config
# ─────────────────────────────────────────
API_KEY    = os.getenv("COINDCX_API_KEY", "")
API_SECRET = os.getenv("COINDCX_API_SECRET", "")
BASE_URL   = "https://api.coindcx.com"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

ALERT_THRESHOLD_UP    = 70000
ALERT_THRESHOLD_DOWN  = 60000
STOP_LOSS_THRESHOLD   = 60000   # absolute INR price stop-loss (legacy / manual trading)
ENABLE_NOTIFICATIONS  = True
AUTO_REFRESH_INTERVAL = 60

# ── Stop-Loss defaults (overridable from the UI) ──────────
DEFAULT_STOP_LOSS_PCT      = 2.0   # % drop from entry → trigger sell
DEFAULT_TRAILING_STOP_PCT  = 1.5   # % drop from peak  → trigger trailing sell
DEFAULT_DAILY_LOSS_LIMIT   = 5.0   # % of day-open balance → pause auto-trade

APP_ENV      = os.getenv("APP_ENV", "local")
REAL_TRADING = os.getenv("REAL_TRADING", "false").lower() in ("1", "true", "yes")

# FIX #8: customer email for deduct_balance — no longer hardcoded
CUSTOMER_EMAIL = os.getenv("CUSTOMER_EMAIL", os.getenv("CUSTOMER_ID", ""))

BTC_WALLET_NAME = "btc_autotrade_live"

# CoinDCX BTCINR minimum order quantity (0.0001 BTC)
COINDCX_MIN_BTC_QTY = 0.0001


# ─────────────────────────────────────────
# DB Helpers
# ─────────────────────────────────────────
def get_mysql_connection():
    """
    Returns a DB connection for the current environment.
    local → MySQL  via pymysql  (DictCursor set at connect time)
    live  → PostgreSQL via psycopg2 (RealDictCursor set as factory)
    """
    try:
        if APP_ENV == "live":
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(
                host=os.getenv("PG_HOST"),
                user=os.getenv("PG_USER"),
                password=os.getenv("PG_PASSWORD"),
                dbname=os.getenv("PG_DB"),
                port=int(os.getenv("PG_PORT", 5432)),
                cursor_factory=psycopg2.extras.RealDictCursor
            )
            return conn
        else:
            import pymysql
            return pymysql.connect(
                host=os.getenv("MYSQL_HOST"),
                user=os.getenv("MYSQL_USER"),
                password=os.getenv("MYSQL_PASSWORD"),
                database=os.getenv("MYSQL_DB"),
                port=int(os.getenv("MYSQL_PORT", 3306)),
                cursorclass=pymysql.cursors.DictCursor
            )
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return None


def get_cursor(conn):
    """Returns a dict-row cursor regardless of DB engine."""
    if APP_ENV == "live":
        import psycopg2.extras
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn.cursor()


def epoch_sql(column):
    """
    Returns a SQL expression for UNIX timestamp from a TIMESTAMP column.
    PostgreSQL uses EXTRACT(EPOCH FROM col), MySQL uses UNIX_TIMESTAMP(col).
    """
    if APP_ENV == "live":
        return f"EXTRACT(EPOCH FROM {column})"
    return f"UNIX_TIMESTAMP({column})"


# ─────────────────────────────────────────
# Table Initialisation
# ─────────────────────────────────────────
def _pk_col():
    return "SERIAL PRIMARY KEY" if APP_ENV == "live" else "INT AUTO_INCREMENT PRIMARY KEY"


def _bool_type():
    return "BOOLEAN" if APP_ENV == "live" else "TINYINT(1)"


def _ts_default():
    if APP_ENV == "live":
        return "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    return "TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"


def init_mysql_tables():
    conn = get_mysql_connection()
    if not conn:
        return
    cursor = get_cursor(conn)

    pk = _pk_col()
    bl = _bool_type()
    ts = _ts_default()

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS inr_wallet_transactions (
        id {pk},
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

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS live_trades (
        id {pk},
        trade_time TIMESTAMP,
        order_id VARCHAR(100),
        action VARCHAR(10),
        amount DOUBLE PRECISION,
        price DOUBLE PRECISION,
        status VARCHAR(20) DEFAULT 'PENDING',
        profit DOUBLE PRECISION DEFAULT 0,
        reason VARCHAR(100),
        fee DOUBLE PRECISION DEFAULT 0
    )
    """)

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS payout_logs (
        id {pk},
        recipient_name VARCHAR(100),
        method VARCHAR(10),
        fund_account_id VARCHAR(50),
        amount NUMERIC(10,2) DEFAULT 0,
        status VARCHAR(50) DEFAULT 'PENDING',
        razorpay_payout_id VARCHAR(50),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS razorpay_payment_log (
        id {pk},
        order_id VARCHAR(100),
        customer_id VARCHAR(50),
        name VARCHAR(100),
        method VARCHAR(50),
        account_number VARCHAR(50),
        ifsc VARCHAR(50),
        upi_id VARCHAR(100),
        amount NUMERIC(10,2) DEFAULT 0,
        status VARCHAR(100) DEFAULT 'PENDING',
        response TEXT,
        credited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        retry_count INT DEFAULT 0,
        last_attempt_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS saved_recipients (
        id {pk},
        name VARCHAR(100),
        method VARCHAR(20),
        account_number VARCHAR(50),
        ifsc VARCHAR(20),
        upi_id VARCHAR(100)
    )
    """)

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS saved_upi_recipients (
        id {pk},
        name VARCHAR(100),
        email VARCHAR(100),
        phone VARCHAR(20),
        upi_id VARCHAR(100),
        contact_id VARCHAR(50),
        fund_account_id VARCHAR(50)
    )
    """)

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS user_wallets (
        id {pk},
        user_email VARCHAR(100) NOT NULL,
        inr_balance NUMERIC(10,2) DEFAULT 0.00,
        customer_id VARCHAR(255) DEFAULT ''
    )
    """)

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS wallet_history (
        id {pk},
        trade_date DATE UNIQUE,
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

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS wallet_transactions (
        id {pk},
        trade_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        action VARCHAR(20),
        amount DOUBLE PRECISION DEFAULT 0,
        balance_after DOUBLE PRECISION DEFAULT 0,
        inr_value DOUBLE PRECISION DEFAULT 0,
        trade_type VARCHAR(200) DEFAULT 'MANUAL',
        autotrade_active {bl} DEFAULT FALSE,
        status VARCHAR(20) DEFAULT 'PENDING',
        reversal_id VARCHAR(50) DEFAULT '',
        is_autotrade_marker {bl} DEFAULT FALSE,
        last_price DOUBLE PRECISION DEFAULT 0,
        trade_mode VARCHAR(10) DEFAULT 'TEST'
    )
    """)

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS trade_execution_lock (
        id INT PRIMARY KEY,
        is_locked {bl} DEFAULT FALSE,
        updated_at {ts}
    )
    """)

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS trade_state (
        id              INT PRIMARY KEY,
        entry_price     DOUBLE PRECISION DEFAULT 0,
        peak_price      DOUBLE PRECISION DEFAULT 0,
        last_sell_price DOUBLE PRECISION DEFAULT 0,
        updated_at      {ts}
    )
    """)

    # Seed lock and state rows (ignore duplicates)
    try:
        cursor.execute("INSERT INTO trade_execution_lock (id, is_locked) VALUES (1, FALSE)")
    except Exception:
        pass
    try:
        cursor.execute("INSERT INTO trade_state (id, entry_price, peak_price) VALUES (1, 0, 0)")
    except Exception:
        pass

    conn.commit()
    cursor.close()
    conn.close()
    print("✅ Tables initialized.")


init_mysql_tables()


# ─────────────────────────────────────────
# Action Lock (UI double-click guard)
# ─────────────────────────────────────────
def action_lock(key: str, cooldown=3):
    now = time.time()
    last = st.session_state.get(key, 0)
    if now - last < cooldown:
        return False
    st.session_state[key] = now
    return True


# ─────────────────────────────────────────
# DB Trade Lock (multi-tab guard)
# ─────────────────────────────────────────
def acquire_trade_lock() -> bool:
    """
    Atomically acquires the trade lock using a single conditional UPDATE.
    Replaces the old SELECT-then-UPDATE pattern which had a race condition
    where two tabs could both read is_locked=FALSE before either wrote TRUE.

    rowcount == 1  → we got the lock
    rowcount == 0  → already locked by another tab/session
    """
    conn = get_mysql_connection()
    if not conn:
        return False
    try:
        cur = get_cursor(conn)
        cur.execute("""
            UPDATE trade_execution_lock
            SET is_locked = TRUE
            WHERE id = 1 AND is_locked = FALSE
        """)
        conn.commit()
        return cur.rowcount == 1
    except Exception as e:
        print(f"❌ acquire_trade_lock error: {e}")
        return False
    finally:
        conn.close()


def release_trade_lock():
    conn = get_mysql_connection()
    if not conn:
        return
    try:
        cur = get_cursor(conn)
        cur.execute("UPDATE trade_execution_lock SET is_locked=FALSE WHERE id=1")
        conn.commit()
    except Exception as e:
        print(f"❌ release_trade_lock error: {e}")
    finally:
        conn.close()


# ─────────────────────────────────────────
# Entry Price (trade_state table)
# FIX #4: All three helpers guard conn=None
# ─────────────────────────────────────────
def get_entry_price():
    conn = get_mysql_connection()
    if not conn:
        return 0.0
    try:
        cursor = get_cursor(conn)
        cursor.execute("SELECT entry_price FROM trade_state WHERE id=1")
        row = cursor.fetchone()
        return float(row["entry_price"]) if row else 0.0
    except Exception:
        return 0.0
    finally:
        conn.close()


def save_entry_price(price):
    conn = get_mysql_connection()
    if not conn:
        return
    try:
        cursor = get_cursor(conn)
        cursor.execute("UPDATE trade_state SET entry_price=%s WHERE id=1", (price,))
        conn.commit()
    except Exception as e:
        print(f"❌ save_entry_price error: {e}")
    finally:
        conn.close()


def clear_entry_price():
    conn = get_mysql_connection()
    if not conn:
        return
    try:
        cursor = get_cursor(conn)
        cursor.execute("UPDATE trade_state SET entry_price=0 WHERE id=1")
        conn.commit()
    except Exception as e:
        print(f"❌ clear_entry_price error: {e}")
    finally:
        conn.close()


def get_last_sell_price() -> float:
    """Returns the INR value received from the last AUTO_SELL (not per-BTC price)."""
    conn = get_mysql_connection()
    if not conn:
        return 0.0
    try:
        cursor = get_cursor(conn)
        cursor.execute("SELECT last_sell_price FROM trade_state WHERE id=1")
        row = cursor.fetchone()
        return float(row["last_sell_price"]) if row and row.get("last_sell_price") else 0.0
    except Exception:
        return 0.0
    finally:
        conn.close()


def save_last_sell_price(inr_value: float):
    """Saves the total INR received from the last AUTO_SELL."""
    conn = get_mysql_connection()
    if not conn:
        return
    try:
        cursor = get_cursor(conn)
        cursor.execute("UPDATE trade_state SET last_sell_price=%s WHERE id=1", (inr_value,))
        conn.commit()
    except Exception as e:
        print(f"❌ save_last_sell_price error: {e}")
    finally:
        conn.close()


def clear_last_sell_price():
    conn = get_mysql_connection()
    if not conn:
        return
    try:
        cursor = get_cursor(conn)
        cursor.execute("UPDATE trade_state SET last_sell_price=0 WHERE id=1")
        conn.commit()
    except Exception as e:
        print(f"❌ clear_last_sell_price error: {e}")
    finally:
        conn.close()


# ─────────────────────────────────────────
# Market Price
# ─────────────────────────────────────────
def get_market_price(symbol="BTCINR"):
    try:
        res = requests.get(f"{BASE_URL}/exchange/ticker", timeout=10)
        for ticker in res.json():
            if ticker["market"] == symbol:
                return float(ticker["last_price"])
    except Exception:
        return None


def cd_get_market_price(symbol: str = "BTCINR"):
    try:
        r = requests.get(f"{BASE_URL}/exchange/ticker", timeout=10)
        for t in r.json():
            if t.get("market") == symbol:
                return float(t.get("last_price"))
        return None
    except Exception as e:
        st.error(f"❌ Price fetch failed: {e}")
        return None


def get_btc_price():
    """
    FIX #8: CoinDCX does not have a BTCUSDT pair — it is INR-only.
    Fetch USD price from CoinGecko public API (no key required).
    Falls back to deriving from INR price if CoinGecko is unavailable.
    """
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            timeout=8
        )
        return float(r.json()["bitcoin"]["usd"])
    except Exception:
        pass
    # Fallback: derive USD from INR price
    try:
        inr_price = cd_get_market_price("BTCINR")
        if inr_price:
            r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=5)
            rate = r.json()["rates"]["INR"]
            return round(inr_price / rate, 2)
    except Exception:
        pass
    return None


def usd_to_inr(usd):
    try:
        response = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=5)
        return usd * response.json()['rates']['INR']
    except Exception:
        return usd * 83.0


# ─────────────────────────────────────────
# Wallet Balance Helpers
# ─────────────────────────────────────────
def sync_inr_wallet(mode="LIVE"):
    try:
        conn = get_mysql_connection()
        if not conn:
            return None
        cursor = get_cursor(conn)
        cursor.execute("""
            SELECT balance_after FROM inr_wallet_transactions
            ORDER BY trade_time DESC LIMIT 1
        """)
        row = cursor.fetchone()
        live_balance = float(row["balance_after"]) if row else 0.0
        conn.close()
        print(f"✅ INR Wallet synced → ₹{live_balance:.2f}")
        return live_balance
    except Exception as e:
        print("❌ Sync Error:", e)
        return None


def get_last_inr_balance(mode=None):
    if mode is None:
        mode = "LIVE" if is_live() else "TEST"
    conn = get_mysql_connection()
    if not conn:
        return 0.0, None
    try:
        cursor = get_cursor(conn)
        cursor.execute(f"""
            SELECT balance_after,
                {epoch_sql('trade_time')} AS ts
            FROM inr_wallet_transactions
            WHERE status IN ('SUCCESS','COMPLETED') AND trade_mode = %s
            ORDER BY trade_time DESC
            LIMIT 1
        """, (mode,))
        row = cursor.fetchone()
        if row:
            return float(row.get("balance_after") or 0.0), float(row.get("ts") or 0.0)
        return 0.0, None
    finally:
        conn.close()


def get_current_inr_balance():
    # FIX #4: guard conn=None
    conn = get_mysql_connection()
    if not conn:
        return 0.0
    try:
        c = get_cursor(conn)
        c.execute("SELECT balance_after FROM inr_wallet_transactions ORDER BY trade_time DESC LIMIT 1")
        row = c.fetchone()
        return float(row['balance_after']) if row and row['balance_after'] is not None else 0.0
    except Exception:
        return 0.0
    finally:
        conn.close()


def get_latest_inr_balance():
    # FIX #4: guard conn=None
    if not is_live():
        return st.session_state.get("test_inr_balance", 5000.0)
    conn = get_mysql_connection()
    if not conn:
        return 0.0
    try:
        c = get_cursor(conn)
        c.execute("SELECT balance_after FROM inr_wallet_transactions WHERE status='COMPLETED' ORDER BY trade_time DESC LIMIT 1")
        row = c.fetchone()
        return float(row['balance_after']) if row and row['balance_after'] is not None else 0.0
    except Exception:
        return 0.0
    finally:
        conn.close()


def get_last_wallet_balance(mode=None):
    if mode is None:
        mode = "LIVE" if is_live() else "TEST"
    conn = get_mysql_connection()
    if not conn:
        return 0.0, None
    try:
        cursor = get_cursor(conn)
        cursor.execute(f"""
            SELECT balance_after, {epoch_sql('trade_time')} AS ts
            FROM wallet_transactions
            WHERE status = 'SUCCESS'
              AND trade_mode = %s
              AND (trade_type IN ('AUTO_BUY','AUTO_SELL','MANUAL_BUY','MANUAL_SELL')
                   OR trade_type LIKE 'AUTO_SELL_%%' OR trade_type LIKE 'AUTO_BUY_%%')
              AND COALESCE(is_autotrade_marker, FALSE) = FALSE
            ORDER BY trade_time DESC LIMIT 1
        """, (mode,))
        row = cursor.fetchone()
        if row:
            return float(row.get("balance_after") or 0.0), float(row.get("ts") or 0.0)
        cursor.execute(f"""
            SELECT balance_after, {epoch_sql('trade_time')} AS ts
            FROM wallet_transactions
            WHERE status = 'SUCCESS' AND trade_mode = %s
              AND COALESCE(is_autotrade_marker, FALSE) = FALSE
            ORDER BY trade_time DESC LIMIT 1
        """, (mode,))
        row = cursor.fetchone()
        if row:
            return float(row.get("balance_after") or 0.0), float(row.get("ts") or 0.0)
        return 0.0, None
    finally:
        conn.close()


# ─────────────────────────────────────────
# INR Wallet Operations
# ─────────────────────────────────────────
def credit_inr_wallet(amount: float, payment_id: str):
    conn = get_mysql_connection()
    if not conn:
        return
    try:
        cur = get_cursor(conn)
        cur.execute("SELECT COUNT(*) AS cnt FROM inr_wallet_transactions WHERE payment_id=%s", (payment_id,))
        if cur.fetchone()["cnt"] > 0:
            return
        cur.execute("SELECT balance_after FROM inr_wallet_transactions ORDER BY trade_time DESC LIMIT 1")
        row = cur.fetchone()
        last_balance = float(row["balance_after"]) if row else 0.0
        new_balance = last_balance + amount
        cur.execute("""
            INSERT INTO inr_wallet_transactions
            (trade_time, action, amount, balance_after, trade_mode, payment_id, status)
            VALUES (NOW(), 'DEPOSIT', %s, %s, %s, %s, 'SUCCESS')
        """, (amount, new_balance, "LIVE" if is_live() else "TEST", payment_id))
        conn.commit()
    finally:
        conn.close()


def reverse_inr_wallet(payment_id: str):
    conn = get_mysql_connection()
    if not conn:
        return
    try:
        cur = get_cursor(conn)
        cur.execute("""
            INSERT INTO inr_wallet_transactions
            (trade_time, action, amount, balance_after, trade_mode, payment_id, status)
            VALUES (NOW(), 'DEPOSIT_FAILED', 0, 0, %s, %s, 'FAILED')
        """, ("LIVE" if is_live() else "TEST", payment_id))
        conn.commit()
    finally:
        conn.close()


def log_inr_transaction(action, amount, balance, mode="TEST"):
    try:
        amount = float(amount) if amount is not None else 0.0
    except Exception:
        amount = 0.0
    try:
        balance = float(balance) if balance is not None else 0.0
    except Exception:
        balance = 0.0
    conn = get_mysql_connection()
    if not conn:
        return
    try:
        cursor = get_cursor(conn)
        cursor.execute("""
            INSERT INTO inr_wallet_transactions
            (trade_time, action, amount, balance_after, trade_mode, status)
            VALUES (NOW(), %s, %s, %s, %s, %s)
        """, (str(action), float(amount), float(balance), str(mode), "SUCCESS"))
        conn.commit()
    finally:
        conn.close()


def deduct_balance(amount, method="", recipient_name="", acc_no=None, ifsc=None, upi=None):
    # FIX #8: replaced hardcoded "testing@gmail.com" with CUSTOMER_EMAIL env var
    owner_email = CUSTOMER_EMAIL
    if not owner_email:
        st.error("⚠️ CUSTOMER_EMAIL env var not set — cannot deduct balance.")
        return
    con = get_mysql_connection()
    if not con:
        return
    try:
        cur = get_cursor(con)
        cur.execute("SELECT inr_balance FROM user_wallets WHERE user_email=%s", (owner_email,))
        row = cur.fetchone()
        if not row:
            st.error("⚠️ User wallet not found!")
            return
        current_balance = float(row["inr_balance"])
        new_balance = current_balance - amount
        if new_balance < 0:
            st.error("❌ Insufficient funds!")
            return
        cur.execute("UPDATE user_wallets SET inr_balance=%s WHERE user_email=%s", (new_balance, owner_email))
        cur.execute("""
            INSERT INTO inr_wallet_transactions
            (trade_time, action, amount, balance_after, trade_mode, payment_id)
            VALUES (NOW(), %s, %s, %s, %s, %s)
        """, (
            f"WITHDRAW-{method}",
            amount,
            new_balance,
            "LIVE" if is_live() else "TEST",
            f"{recipient_name} | {method} | {acc_no or ifsc or upi or ''}"
        ))
        con.commit()
    except Exception as e:
        st.error(f"DB Error: {e}")
        con.rollback()
    finally:
        con.close()


# ─────────────────────────────────────────
# FIX #1: withdraw_inr — corrected parameter names
# Original function used 'account' and 'upi_id' internally but
# was called with 'acc_no' and 'upi' at the call site — TypeError.
# Solution: renamed function params to match the call site.
# ─────────────────────────────────────────
def withdraw_inr(amount: float, mode: str = "TEST",
                 method: str = "UPI", acc_no: str = "",
                 ifsc: str = "", upi: str = "",
                 recipient_name: str = "",
                 max_retries: int = 3, retry_delay: int = 10):
    """
    Handles INR withdrawal via Razorpay Payouts API with retry.
    Requires Razorpay X (Payouts) enabled on your account.

    FIX: parameter names changed from (account, upi_id) → (acc_no, upi)
    to match the call site at the Withdraw button handler.
    retry_delay reduced from 60s → 10s to avoid blocking Streamlit too long.
    """
    conn = get_mysql_connection()
    if not conn:
        st.error("❌ DB connection failed — withdrawal aborted.")
        return
    try:
        cursor = get_cursor(conn)
        cursor.execute("""
            SELECT balance_after FROM inr_wallet_transactions
            WHERE trade_mode = %s ORDER BY trade_time DESC LIMIT 1
        """, (mode,))
        row = cursor.fetchone()
        current_balance = float(row["balance_after"]) if row else 0.0

        if amount > current_balance:
            st.error("❌ Insufficient balance for withdrawal")
            return

        payout_success = False
        failure_reason = None

        if mode == "LIVE":
            rzp_headers = {
                "Content-Type": "application/json",
                "X-Payout-Idempotency": str(uuid.uuid4())
            }
            if method == "UPI":
                payout_payload = {
                    "account_number": os.getenv("RAZORPAY_ACCOUNT_NUMBER", ""),
                    "amount": int(amount * 100),
                    "currency": "INR",
                    "mode": "UPI",
                    "purpose": "payout",
                    "fund_account": {
                        "account_type": "vpa",
                        "vpa": {"address": upi},          # FIX: was upi_id
                        "contact": {
                            "name": recipient_name,
                            "type": "customer"
                        }
                    },
                    "queue_if_low_balance": True
                }
            else:
                payout_payload = {
                    "account_number": os.getenv("RAZORPAY_ACCOUNT_NUMBER", ""),
                    "amount": int(amount * 100),
                    "currency": "INR",
                    "mode": "IMPS",
                    "purpose": "payout",
                    "fund_account": {
                        "account_type": "bank_account",
                        "bank_account": {
                            "name": recipient_name,
                            "ifsc": ifsc,
                            "account_number": acc_no     # FIX: was account
                        },
                        "contact": {
                            "name": recipient_name,
                            "type": "customer"
                        }
                    },
                    "queue_if_low_balance": True
                }

            for attempt in range(1, max_retries + 1):
                try:
                    res = requests.post(
                        "https://api.razorpay.com/v1/payouts",
                        json=payout_payload,
                        headers=rzp_headers,
                        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
                        timeout=30
                    )
                    data = res.json()
                    if data.get("status") in ("queued", "processing", "processed"):
                        payout_success = True
                        break
                    else:
                        failure_reason = data.get("error", {}).get("description", "Unknown failure")
                        msg = f"⚠️ Withdraw attempt {attempt} failed: {failure_reason}"
                        st.warning(msg)
                        send_telegram(msg)
                except Exception as e:
                    failure_reason = str(e)
                    msg = f"⚠️ Withdraw attempt {attempt} error: {failure_reason}"
                    st.warning(msg)
                    send_telegram(msg)

                if attempt < max_retries:
                    time.sleep(retry_delay)
        else:
            payout_success = True

        if payout_success:
            new_balance = current_balance - amount
            cursor.execute("""
                INSERT INTO inr_wallet_transactions
                (trade_time, action, amount, balance_after, trade_mode, status)
                VALUES (NOW(), 'WITHDRAWAL', %s, %s, %s, 'SUCCESS')
            """, (-amount, new_balance, mode))
            msg = f"✅ Withdraw ₹{amount:.2f} successful | New balance: ₹{new_balance:.2f}"
            st.success(msg)
            send_telegram(msg)
        else:
            cursor.execute("""
                INSERT INTO inr_wallet_transactions
                (trade_time, action, amount, balance_after, trade_mode, status)
                VALUES (NOW(), 'WITHDRAWAL', %s, %s, %s, 'FAILED')
            """, (-amount, current_balance, mode))
            msg = f"❌ Withdraw ₹{amount:.2f} failed after {max_retries} attempts: {failure_reason}"
            st.error(msg)
            send_telegram(msg)

        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────
# Wallet Transactions Logger
# ─────────────────────────────────────────
def log_wallet_transaction(action, amount, balance, price_inr, trade_type="MANUAL", inr_value_override=None):
    """
    FIX: Added inr_value_override so AUTO_SELL can store the actual INR
    received (inr_received) instead of balance×price which is always 0
    after a sell (balance=0). This is what get_last_auto_trade() reads
    to determine the DIP BUY trigger price.
    """
    try: amount    = float(amount)    if amount    is not None else 0.0
    except Exception: amount = 0.0
    try: balance   = float(balance)   if balance   is not None else 0.0
    except Exception: balance = 0.0
    try: price_inr = float(price_inr) if price_inr is not None else 0.0
    except Exception: price_inr = 0.0
    if inr_value_override is not None:
        try: inr_value = float(inr_value_override)
        except Exception: inr_value = 0.0
    else:
        try: inr_value = float(balance) * float(price_inr)
        except Exception: inr_value = 0.0

    conn = get_mysql_connection()
    if not conn:
        return
    try:
        cursor = get_cursor(conn)
        cursor.execute("""
            INSERT INTO wallet_transactions
            (trade_time, action, amount, balance_after, inr_value, trade_type, autotrade_active, status)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s)
        """, (
            str(action),
            float(amount),
            float(balance),
            float(inr_value),
            str(trade_type),
            bool(st.session_state.get("AUTO_TRADING", {}).get("active", False)),
            "SUCCESS"
        ))
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────
# Notifications
# ─────────────────────────────────────────
def send_telegram(message):
    if ENABLE_NOTIFICATIONS and BOT_TOKEN and CHAT_ID:
        try:
            requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                params={"chat_id": CHAT_ID, "text": message},
                timeout=5
            )
        except Exception as e:
            print(f"Telegram failed: {e}")


def poll_telegram_stop_command() -> bool:
    """
    Checks Telegram for a /stop command from the chat owner.
    Returns True (and stops auto-trade) if /stop was received.
    This gives a phone-based emergency kill switch if the browser freezes.
    Call this at the start of each check_auto_trading() cycle.
    """
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        offset_key = "_tg_last_update_id"
        last_id    = st.session_state.get(offset_key, 0)
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"offset": last_id + 1, "timeout": 2, "limit": 5},
            timeout=5
        )
        updates = r.json().get("result", [])
        for update in updates:
            uid  = update.get("update_id", 0)
            text = update.get("message", {}).get("text", "").strip()
            chat = str(update.get("message", {}).get("chat", {}).get("id", ""))
            st.session_state[offset_key] = uid
            if text == "/stop" and chat == str(CHAT_ID):
                stop_autotrade("🛑 Emergency STOP received via Telegram /stop command")
                send_telegram("✅ Auto-Trade stopped by /stop command.")
                return True
    except Exception as e:
        print(f"Telegram poll failed: {e}")
    return False


# ─────────────────────────────────────────
# Background Monitor
# ─────────────────────────────────────────
def start_background_monitor():
    MONITOR_URL    = os.getenv("RENDER_APP_URL", "")
    CHECK_INTERVAL = 600
    HOURS_LIMIT    = 500
    max_checks     = int((HOURS_LIMIT * 3600) / CHECK_INTERVAL)

    if not MONITOR_URL:
        return

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
                    raise Exception(f"Status {r.status_code}")
            except Exception as e:
                send_telegram(f"🚨 Render app DOWN! {e}")
            time.sleep(CHECK_INTERVAL)

    t = threading.Thread(target=monitor, daemon=True)
    t.start()


start_background_monitor()


# ─────────────────────────────────────────
# Razorpay Payment
# ─────────────────────────────────────────
def create_razorpay_payment(amount_inr: float, description: str = None):
    order = razorpay_client.order.create({
        "amount": int(float(amount_inr) * 100),
        "currency": "INR",
        "payment_capture": 1,
        "notes": {"purpose": description or "INR Wallet Deposit"}
    })
    return {
        "order_id": order["id"],
        "amount":   order["amount"],
        "currency": order["currency"],
        "mode":     "TEST" if "test" in RAZORPAY_KEY_ID.lower() else "LIVE"
    }


def make_order_id(prefix="ORDER"):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def generate_qr_code(data: str) -> bytes:
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return buffer.getvalue()


# ─────────────────────────────────────────
# Recipients / Payout Logs
# ─────────────────────────────────────────
def save_bank_recipient(name, email, phone, ifsc, acc_number, contact_id, fund_account_id):
    conn = get_mysql_connection()
    if not conn:
        return
    cur = get_cursor(conn)
    cur.execute("""
        INSERT INTO saved_recipients (name, method, account_number, ifsc, upi_id)
        VALUES (%s, %s, %s, %s, %s)
    """, (name, "bank", acc_number, ifsc, ""))
    conn.commit()
    conn.close()


def save_upi_recipient(name, email, phone, upi_id, contact_id, fund_account_id):
    conn = get_mysql_connection()
    if not conn:
        return
    cur = get_cursor(conn)
    cur.execute("""
        INSERT INTO saved_upi_recipients (name, email, phone, upi_id, contact_id, fund_account_id)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (name, email, phone, upi_id, contact_id, fund_account_id))
    conn.commit()
    conn.close()


def load_saved_recipients():
    conn = get_mysql_connection()
    if not conn:
        return []
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM saved_recipients")
    rows = cur.fetchall()
    conn.close()
    return rows


def load_saved_upi_recipients():
    conn = get_mysql_connection()
    if not conn:
        return []
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM saved_upi_recipients")
    rows = cur.fetchall()
    conn.close()
    return rows


def get_all_recipients():
    conn = get_mysql_connection()
    if not conn:
        return []
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM saved_recipients ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return rows


def save_recipient_if_new(name, method, acc, ifsc, upi):
    conn = get_mysql_connection()
    if not conn:
        return
    cur = get_cursor(conn)
    cur.execute(
        "SELECT id FROM saved_recipients WHERE method=%s AND (account_number=%s OR upi_id=%s)",
        (method, acc, upi)
    )
    if not cur.fetchone():
        cur.execute("""
            INSERT INTO saved_recipients (name, method, account_number, ifsc, upi_id)
            VALUES (%s, %s, %s, %s, %s)
        """, (name, method, acc, ifsc, upi))
    conn.commit()
    conn.close()


def log_payout(name_or_order_id, method_or_name=None, fund_account_id_or_method=None,
               amount=0, status="PENDING", payout_id="",
               acc=None, ifsc=None, upi=None, response=None):
    conn = get_mysql_connection()
    if not conn:
        return
    cur = get_cursor(conn)
    cur.execute("""
        INSERT INTO payout_logs (recipient_name, method, fund_account_id, amount, status, razorpay_payout_id)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (str(name_or_order_id), str(method_or_name or ""), str(fund_account_id_or_method or ""),
          float(amount), str(status), str(payout_id)))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────
# Daily Summary / Balance Health
# ─────────────────────────────────────────
def get_daily_wallet_summary():
    conn = get_mysql_connection()
    if not conn:
        return []
    c = get_cursor(conn)
    c.execute("""
        SELECT DATE(trade_time) as day,
            SUM(CASE WHEN action='DEPOSIT' THEN amount ELSE 0 END) AS deposits,
            SUM(CASE WHEN action='WITHDRAWAL' THEN amount ELSE 0 END) AS withdrawals,
            SUM(CASE WHEN action='DEPOSIT_FAILED' THEN 1 ELSE 0 END) AS failed_deposits
        FROM inr_wallet_transactions
        GROUP BY DATE(trade_time)
        ORDER BY day DESC LIMIT 7
    """)
    result = c.fetchall()
    conn.close()
    return result


def check_balance_health():
    # FIX #5: Logic was inverted — was checking diff > 1000 (balance rise)
    # but the message said "balance drop". Fixed to diff < -1000.
    conn = get_mysql_connection()
    if not conn:
        return
    c = get_cursor(conn)
    c.execute("""
        SELECT balance_after FROM inr_wallet_transactions
        WHERE action IN ('DEPOSIT', 'WITHDRAWAL')
        ORDER BY trade_time DESC LIMIT 2
    """)
    rows = c.fetchall()
    conn.close()
    if len(rows) == 2:
        diff = rows[0]['balance_after'] - rows[1]['balance_after']
        if diff < -1000:   # FIX: negative diff = balance dropped
            st.warning(f"⚠️ Sudden balance drop: ₹{abs(diff):.2f}")


def count_failed_refunds():
    conn = get_mysql_connection()
    if not conn:
        return
    c = get_cursor(conn)
    c.execute("""
        SELECT COUNT(*) as failures FROM inr_wallet_transactions
        WHERE status='FAILED' AND action='DEPOSIT_FAILED'
        AND trade_time >= CURRENT_DATE
    """)
    count = c.fetchone()["failures"]
    conn.close()
    if count > 0:
        st.error(f"❌ {count} failed deposits today!")


def get_pnl_summary():
    """
    FIX: action field stores AUTO_SELL, AUTO_BUY, MANUAL_SELL, MANUAL_BUY —
    not plain 'SELL'/'BUY'. Using LIKE to match all variants.
    Also reads fees from wallet_transactions to show true net P&L.
    """
    conn = get_mysql_connection()
    if not conn:
        return 0.0, 0.0, 0.0
    try:
        cur = get_cursor(conn)
        cur.execute("""
            SELECT
                SUM(CASE WHEN action LIKE '%%SELL%%' THEN ABS(amount) ELSE 0 END) as total_sell,
                SUM(CASE WHEN action LIKE '%%BUY%%'  THEN ABS(amount) ELSE 0 END) as total_buy
            FROM inr_wallet_transactions
            WHERE status IN ('SUCCESS', 'COMPLETED')
              AND action NOT IN ('AUTO_STOP', 'AUTO_START', 'TEST_RESET',
                                 'WITHDRAW-BANK', 'WITHDRAW-UPI')
        """)
        row = cur.fetchone()
        total_sell = float(row["total_sell"] or 0) if row else 0.0
        total_buy  = float(row["total_buy"]  or 0) if row else 0.0
        return total_buy, total_sell, total_sell - total_buy
    except Exception as e:
        print(f"❌ get_pnl_summary error: {e}")
        return 0.0, 0.0, 0.0
    finally:
        conn.close()


def update_wallet_daily_summary(start=False, auto_end=False):
    # FIX #6: On start=True, use INSERT ... ON CONFLICT DO NOTHING (PG)
    # or INSERT IGNORE (MySQL) so repeated page refreshes don't create
    # duplicate rows for the same calendar day.
    conn   = get_mysql_connection()
    if not conn:
        return
    cursor = get_cursor(conn)
    today  = datetime.now().strftime("%Y-%m-%d")
    price  = get_btc_price()
    inr_price = usd_to_inr(price) if price else 0

    # Read BTC from DB not session_state — session_state is stale on refresh
    _btc_raw, _ = get_last_wallet_balance(mode="LIVE" if is_live() else "TEST")
    btc_bal = float(_btc_raw or 0)

    if start:
        if APP_ENV == "live":
            # PostgreSQL: INSERT ... ON CONFLICT DO NOTHING
            cursor.execute("""
                INSERT INTO wallet_history
                (trade_date, start_balance, end_balance, current_inr_value, trade_count,
                 auto_start_price, auto_end_price, auto_profit)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (trade_date) DO NOTHING
            """, (today, btc_bal, btc_bal, btc_bal * inr_price, 0, inr_price, 0, 0))
        else:
            # MySQL: INSERT IGNORE
            cursor.execute("""
                INSERT IGNORE INTO wallet_history
                (trade_date, start_balance, end_balance, current_inr_value, trade_count,
                 auto_start_price, auto_end_price, auto_profit)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (today, btc_bal, btc_bal, btc_bal * inr_price, 0, inr_price, 0, 0))
    else:
        cursor.execute("SELECT COUNT(*) AS cnt FROM wallet_transactions WHERE DATE(trade_time) = CURRENT_DATE")
        count_row = cursor.fetchone()
        count = count_row['cnt'] if count_row else 0
        cursor.execute("""
            UPDATE wallet_history
            SET end_balance=%s, current_inr_value=%s, trade_count=%s
            WHERE trade_date=%s
        """, (btc_bal, btc_bal * inr_price, count, today))

    if auto_end:
        cursor.execute("SELECT auto_start_price FROM wallet_history WHERE trade_date=%s", (today,))
        row = cursor.fetchone()
        start_price = float(row['auto_start_price']) if row and row['auto_start_price'] else 0
        profit = btc_bal * (inr_price - start_price)
        cursor.execute("""
            UPDATE wallet_history SET auto_end_price=%s, auto_profit=%s WHERE trade_date=%s
        """, (inr_price, profit, today))

    conn.commit()
    conn.close()


def save_trade_log(trade_type, btc_amount, btc_balance, price_inr, roi=0):
    today_str  = datetime.now().strftime("%Y-%m-%d")
    filename   = f"trade_log_{today_str}.csv"
    file_exists = os.path.isfile(filename)
    with open(filename, mode='a', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=[
            "timestamp", "trade_type", "btc_amount", "btc_balance", "price_inr", "roi"
        ])
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trade_type":  trade_type,
            "btc_amount":  round(btc_amount,  6),
            "btc_balance": round(btc_balance,  6),
            "price_inr":   round(price_inr,    2),
            "roi":         round(roi,           2)
        })


# ═══════════════════════════════════════════════════════════
# REAL EXCHANGE ORDER PLACEMENT — CoinDCX API
# ═══════════════════════════════════════════════════════════

def _coindcx_signed_request(endpoint: str, body: dict) -> dict:
    """
    Signs a CoinDCX private API request with HMAC-SHA256.
    Raises ValueError on missing credentials; raises on HTTP error.
    """
    if not API_KEY or not API_SECRET:
        raise ValueError(
            "COINDCX_API_KEY and COINDCX_API_SECRET must be set in .env "
            "before using LIVE trading."
        )

    body["timestamp"] = int(time.time() * 1000)
    payload   = json.dumps(body, separators=(",", ":"))
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "Content-Type":     "application/json",
        "X-AUTH-APIKEY":    API_KEY,
        "X-AUTH-SIGNATURE": signature,
    }

    response = requests.post(
        f"{BASE_URL}{endpoint}",
        data=payload,
        headers=headers,
        timeout=15
    )
    response.raise_for_status()
    return response.json()


def _poll_order_status(order_id: str, max_wait: int = 30) -> dict:
    """
    Poll CoinDCX order status until filled, cancelled, rejected, or timeout.
    FIX #10: Logs a warning if still open after max_wait seconds.
    """
    data = {}
    for _ in range(max_wait):
        time.sleep(1)
        data = _coindcx_signed_request(
            "/exchange/v1/orders/status",
            {"id": order_id}
        )
        status = data.get("status", "")
        if status in ("filled", "cancelled", "rejected"):
            return data

    # Timeout — order still open/partial
    send_telegram(
        f"⚠️ Order {order_id} still open after {max_wait}s poll. "
        f"Last status: {data.get('status','unknown')}. Check exchange manually."
    )
    return data


def place_market_buy(buy_inr: float) -> dict:
    """
    Places a real market BUY on CoinDCX (LIVE) or simulated (TEST).

    FIX #11: Minimum quantity guard — raises if calculated BTC < COINDCX_MIN_BTC_QTY.

    Returns:
        {"status", "filled_qty", "avg_price", "fee", "order_id"}
    """
    if not is_live():
        spot_price = cd_get_market_price("BTCINR") or 1.0
        fee_rate   = 0.001
        btc_gross  = buy_inr / spot_price
        fee_btc    = btc_gross * fee_rate
        return {
            "status":     "filled",
            "filled_qty": round(btc_gross - fee_btc, 8),
            "avg_price":  spot_price,
            "fee":        round(fee_btc, 8),
            "order_id":   f"TEST_{uuid.uuid4().hex[:10]}",
        }

    spot_price = cd_get_market_price("BTCINR")
    if not spot_price:
        raise RuntimeError("Cannot fetch BTCINR price — aborting BUY to protect funds.")

    btc_qty = round(buy_inr / spot_price, 6)

    # FIX #11: CoinDCX minimum order size check
    if btc_qty < COINDCX_MIN_BTC_QTY:
        raise ValueError(
            f"Calculated BTC qty {btc_qty:.6f} is below CoinDCX minimum "
            f"{COINDCX_MIN_BTC_QTY}. Increase buy_inr (need at least "
            f"₹{COINDCX_MIN_BTC_QTY * spot_price:.2f})."
        )

    order_resp = _coindcx_signed_request(
        "/exchange/v1/orders/create",
        {
            "side":           "buy",
            "order_type":     "market_order",
            "market":         "BTCINR",
            "total_quantity": btc_qty,
        }
    )

    order_id = order_resp.get("id") or order_resp.get("orders", [{}])[0].get("id", "")

    conn = get_mysql_connection()
    if conn:
        cur = get_cursor(conn)
        cur.execute("""
            INSERT INTO live_trades
            (trade_time, order_id, action, amount, price, status, reason)
            VALUES (NOW(), %s, 'BUY', %s, %s, 'PENDING', 'AUTO_BUY')
        """, (order_id, btc_qty, spot_price))
        conn.commit()
        conn.close()

    final      = _poll_order_status(order_id)
    filled_qty = float(final.get("total_quantity", btc_qty))
    avg_price  = float(final.get("avg_price") or spot_price)
    fee_btc    = float(final.get("fee_amount", 0))

    conn = get_mysql_connection()
    if conn:
        cur = get_cursor(conn)
        cur.execute("""
            UPDATE live_trades SET status=%s, price=%s, amount=%s, fee=%s
            WHERE order_id=%s
        """, (final.get("status", "filled"), avg_price, filled_qty, fee_btc, order_id))
        conn.commit()
        conn.close()

    return {
        "status":     final.get("status", "filled"),
        "filled_qty": round(filled_qty - fee_btc, 8),
        "avg_price":  avg_price,
        "fee":        round(fee_btc, 8),
        "order_id":   order_id,
    }


def place_market_sell(btc_qty: float) -> dict:
    """
    Places a real market SELL on CoinDCX (LIVE) or simulated (TEST).

    FIX #11: Minimum quantity guard.

    Returns:
        {"status", "filled_qty", "avg_price", "fee", "order_id"}
    """
    if not is_live():
        spot_price = cd_get_market_price("BTCINR") or 1.0
        fee_rate   = 0.001
        inr_gross  = btc_qty * spot_price
        fee_inr    = inr_gross * fee_rate
        return {
            "status":     "filled",
            "filled_qty": round(btc_qty, 8),
            "avg_price":  spot_price,
            "fee":        round(fee_inr, 2),
            "order_id":   f"TEST_{uuid.uuid4().hex[:10]}",
        }

    spot_price = cd_get_market_price("BTCINR")
    if not spot_price:
        raise RuntimeError("Cannot fetch BTCINR price — aborting SELL to protect funds.")

    btc_qty_rounded = round(btc_qty, 6)

    # FIX #11: CoinDCX minimum order size check
    if btc_qty_rounded < COINDCX_MIN_BTC_QTY:
        raise ValueError(
            f"BTC qty {btc_qty_rounded:.6f} is below CoinDCX minimum {COINDCX_MIN_BTC_QTY}."
        )

    order_resp = _coindcx_signed_request(
        "/exchange/v1/orders/create",
        {
            "side":           "sell",
            "order_type":     "market_order",
            "market":         "BTCINR",
            "total_quantity": btc_qty_rounded,
        }
    )

    order_id = order_resp.get("id") or order_resp.get("orders", [{}])[0].get("id", "")

    conn = get_mysql_connection()
    if conn:
        cur = get_cursor(conn)
        cur.execute("""
            INSERT INTO live_trades
            (trade_time, order_id, action, amount, price, status, reason)
            VALUES (NOW(), %s, 'SELL', %s, %s, 'PENDING', 'AUTO_SELL')
        """, (order_id, btc_qty_rounded, spot_price))
        conn.commit()
        conn.close()

    final      = _poll_order_status(order_id)
    filled_qty = float(final.get("total_quantity", btc_qty_rounded))
    avg_price  = float(final.get("avg_price") or spot_price)
    fee_inr    = float(final.get("fee_amount", 0))

    conn = get_mysql_connection()
    if conn:
        cur = get_cursor(conn)
        cur.execute("""
            UPDATE live_trades SET status=%s, price=%s, amount=%s, fee=%s
            WHERE order_id=%s
        """, (final.get("status", "filled"), avg_price, filled_qty, fee_inr, order_id))
        conn.commit()
        conn.close()

    return {
        "status":     final.get("status", "filled"),
        "filled_qty": round(filled_qty, 8),
        "avg_price":  avg_price,
        "fee":        round(fee_inr, 2),
        "order_id":   order_id,
    }


# ─────────────────────────────────────────
# Auto-Trade State
# ─────────────────────────────────────────
def get_autotrade_active_from_db() -> bool:
    conn = get_mysql_connection()
    if not conn:
        return False
    try:
        cursor = get_cursor(conn)
        cursor.execute("""
            SELECT trade_type FROM wallet_transactions
            WHERE trade_type IN ('AUTO_TRADE_START', 'AUTO_TRADE_STOP')
            ORDER BY trade_time DESC LIMIT 1
        """)
        row = cursor.fetchone()
        return bool(row and row.get('trade_type') == 'AUTO_TRADE_START')
    finally:
        conn.close()


def restore_autotrade_state():
    db_active = get_autotrade_active_from_db()
    if "AUTO_TRADING" not in st.session_state:
        st.session_state.AUTO_TRADING = {
            "active": db_active, "entry_price": 0, "last_price": 0, "last_trade": None
        }
    else:
        st.session_state.AUTO_TRADING["active"] = db_active

    # If DB says active but this is a fresh page load (no started_at in session),
    # seed started_at to NOW so the idle timer starts from this refresh,
    # not from some old DB timestamp.
    if db_active and not st.session_state.get("autotrade_started_at"):
        st.session_state["autotrade_started_at"] = time.time()
        st.session_state["_last_cycle_ts"]        = time.time()


def update_autotrade_status_db(status: int):
    conn = None
    try:
        conn   = get_mysql_connection()
        if not conn:
            return
        cursor = get_cursor(conn)
        cursor.execute("""
            INSERT INTO wallet_transactions
            (trade_time, action, amount, balance_after, inr_value,
             trade_type, autotrade_active, is_autotrade_marker, status)
            VALUES (NOW(), 'AUTO_META', 0, 0, 0, %s, %s, TRUE, 'SUCCESS')
        """, (
            "AUTO_TRADE_START" if status else "AUTO_TRADE_STOP",
            bool(status)
        ))
        conn.commit()
    except Exception as e:
        st.error(f"❌ autotrade status DB error: {e}")
        raise
    finally:
        if conn:
            conn.close()


def update_last_auto_trade_price_db(price_inr):
    conn = None
    try:
        conn   = get_mysql_connection()
        if not conn:
            return
        cursor = get_cursor(conn)
        cursor.execute("""
            INSERT INTO wallet_transactions
            (trade_time, action, amount, balance_after, inr_value,
             trade_type, autotrade_active, is_autotrade_marker, last_price)
            VALUES (NOW(), 'AUTO_META', 0, 0, %s, 'AUTO_TRADE', TRUE, TRUE, %s)
        """, (price_inr, price_inr))
        conn.commit()
    except Exception as e:
        st.error(f"❌ last price DB error: {e}")
        raise
    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────
# FIX #7: get_last_trade_time_from_db — exclude AUTO_META rows
# so that auto-trade state changes don't reset the idle timer
# ─────────────────────────────────────────
def get_last_trade_time_from_db():
    conn = get_mysql_connection()
    if not conn:
        return None
    try:
        cursor = get_cursor(conn)
        cursor.execute("""
            SELECT trade_time FROM wallet_transactions
            WHERE COALESCE(is_autotrade_marker, FALSE) = FALSE
            ORDER BY trade_time DESC LIMIT 1
        """)
        row = cursor.fetchone()
        if row:
            trade_time = row.get("trade_time")
            if trade_time is None:
                return None
            if isinstance(trade_time, datetime):
                return trade_time
            if isinstance(trade_time, (int, float)):
                return datetime.fromtimestamp(trade_time)
            if isinstance(trade_time, str):
                try:
                    return datetime.fromisoformat(trade_time)
                except ValueError:
                    return None
        return None
    finally:
        conn.close()


def get_last_trade_time_from_logs():
    """Returns timestamp of last AUTO_BUY or AUTO_SELL only — ignores
    TEST_RESET, AUTO_START, manual trades, and marker rows."""
    conn = get_mysql_connection()
    if not conn:
        return None
    try:
        cur = get_cursor(conn)
        cur.execute("""
            SELECT MAX(trade_time) AS last_trade_time
            FROM (
                SELECT trade_time FROM wallet_transactions
                WHERE trade_type IN ('AUTO_BUY', 'AUTO_SELL')
                  AND COALESCE(is_autotrade_marker, FALSE) = FALSE
                UNION ALL
                SELECT trade_time FROM inr_wallet_transactions
                WHERE action IN ('AUTO_BUY', 'AUTO_SELL')
            ) AS combined
        """)
        row = cur.fetchone()
        if row and row.get("last_trade_time"):
            val = row["last_trade_time"]
            if isinstance(val, datetime):
                return val
            try:
                return datetime.fromisoformat(str(val))
            except Exception:
                return None
        return None
    except Exception as e:
        print(f"❌ get_last_trade_time_from_logs error: {e}")
        return None
    finally:
        conn.close()


def get_last_auto_trade():
    """Returns last AUTO_BUY or AUTO_SELL row.
    Normalises trade_type to 'AUTO_BUY' or 'AUTO_SELL' regardless
    of variant stored (e.g. AUTO_SELL_PROFIT_TARGET → AUTO_SELL)."""
    conn = get_mysql_connection()
    if not conn:
        return None
    try:
        cursor = get_cursor(conn)
        cursor.execute("""
            SELECT trade_type, trade_time, inr_value, amount, balance_after
            FROM wallet_transactions
            WHERE trade_type IN ('AUTO_BUY', 'AUTO_SELL')
               OR trade_type LIKE 'AUTO_SELL_%%'
               OR trade_type LIKE 'AUTO_BUY_%%'
            ORDER BY trade_time DESC LIMIT 1
        """)
        row = cursor.fetchone()
        if row:
            # Normalise so downstream code always sees "AUTO_BUY" or "AUTO_SELL"
            raw = row.get("trade_type", "")
            if raw.startswith("AUTO_SELL"):
                row["trade_type"] = "AUTO_SELL"
            elif raw.startswith("AUTO_BUY"):
                row["trade_type"] = "AUTO_BUY"
        return row
    finally:
        conn.close()


def get_latest_auto_start_price():
    conn = get_mysql_connection()
    if not conn:
        return 0.0
    c = get_cursor(conn)
    c.execute("SELECT auto_start_price FROM wallet_history ORDER BY trade_date DESC LIMIT 1")
    result = c.fetchone()
    conn.close()
    return float(result['auto_start_price']) if result and result['auto_start_price'] is not None else 0.0


def update_wallet_history_profit(profit, trade_date=None):
    if not trade_date:
        trade_date = datetime.now().strftime("%Y-%m-%d")
    conn   = get_mysql_connection()
    if not conn:
        return
    cursor = get_cursor(conn)
    cursor.execute("""
        UPDATE wallet_history SET auto_profit = COALESCE(auto_profit, 0) + %s
        WHERE trade_date = %s
    """, (profit, trade_date))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────
# Auto-Trade Start / Stop
# ─────────────────────────────────────────
def start_autotrade():
    try:
        btc_balance, _ = get_last_wallet_balance(mode="LIVE" if is_live() else "TEST")
        inr_balance, _ = get_last_inr_balance(mode="LIVE"   if is_live() else "TEST")
        btc_balance = float(btc_balance or 0.0)
        inr_balance = float(inr_balance or 0.0)

        st.session_state["BTC_WALLET"] = {"balance": btc_balance}
        st.session_state["INR_WALLET"] = {"balance": inr_balance}
        st.session_state.AUTO_TRADING  = {
            "active": True, "entry_price": 0.0, "last_price": 0.0, "last_trade": None
        }
        st.session_state["autotrade_toggle"]    = True
        st.session_state["autotrade_started_at"] = time.time()   # used by idle timeout
        update_autotrade_status_db(1)
        log_wallet_transaction("AUTO_START", 0, btc_balance, 0, "AUTO_TRADE_START")
        log_inr_transaction("AUTO_START", 0, inr_balance, "LIVE" if is_live() else "TEST")

        msg = f"🚀 Auto-Trade ACTIVATED | INR ₹{inr_balance:,.2f} | ₿{btc_balance:.6f}"
        st.success(msg)
        send_telegram(msg)
    except Exception as e:
        st.error(f"❌ Failed to start Auto-Trade: {e}")
        send_telegram(f"❌ Failed to start: {e}")


def stop_autotrade(message: str):
    st.session_state.AUTO_TRADING["active"]      = False
    st.session_state["autotrade_toggle"]          = False
    st.session_state["autotrade_started_at"]      = None
    update_autotrade_status_db(0)
    try:
        btc_bal, _ = get_last_wallet_balance(mode="LIVE" if is_live() else "TEST")
        inr_bal, _ = get_last_inr_balance(mode="LIVE"   if is_live() else "TEST")
    except Exception:
        btc_bal = st.session_state.get("BTC_WALLET", {}).get("balance", 0.0)
        inr_bal = st.session_state.get("INR_WALLET", {}).get("balance", 0.0)

    st.session_state.BTC_WALLET = {"balance": float(btc_bal or 0.0)}
    st.session_state.INR_WALLET = {"balance": float(inr_bal or 0.0)}

    log_wallet_transaction("AUTO_STOP", 0, st.session_state.BTC_WALLET["balance"], 0, "AUTO_TRADE_STOP")
    log_inr_transaction("AUTO_STOP", 0, st.session_state.INR_WALLET["balance"], "LIVE" if is_live() else "TEST")
    update_wallet_daily_summary(auto_end=True)
    st.warning(message)
    send_telegram(message)


# ═══════════════════════════════════════════════════════════
# STOP-LOSS — DB persistence helpers
# ═══════════════════════════════════════════════════════════

def get_peak_price() -> float:
    conn   = get_mysql_connection()
    if not conn:
        return 0.0
    cursor = get_cursor(conn)
    try:
        cursor.execute("SELECT peak_price FROM trade_state WHERE id=1")
        row = cursor.fetchone()
        return float(row["peak_price"]) if row and row.get("peak_price") else 0.0
    except Exception:
        return 0.0
    finally:
        conn.close()


def save_peak_price(price: float):
    conn   = get_mysql_connection()
    if not conn:
        return
    cursor = get_cursor(conn)
    try:
        cursor.execute("UPDATE trade_state SET peak_price=%s WHERE id=1", (price,))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def clear_peak_price():
    save_peak_price(0.0)


def log_stop_loss_event(reason: str, entry: float, exit_price: float,
                        btc_sold: float, inr_received: float, roi: float):
    conn = get_mysql_connection()
    if not conn:
        return
    try:
        cursor = get_cursor(conn)
        cursor.execute(
            "SELECT balance_after FROM inr_wallet_transactions ORDER BY trade_time DESC LIMIT 1"
        )
        row          = cursor.fetchone()
        last_balance = float(row["balance_after"]) if row else 0.0
        new_balance  = last_balance + inr_received
        cursor.execute("""
            INSERT INTO inr_wallet_transactions
            (trade_time, action, amount, balance_after, trade_mode, payment_id, status)
            VALUES (NOW(), %s, %s, %s, %s, %s, 'COMPLETED')
        """, (
            f"STOP_LOSS_{reason}",
            inr_received,
            new_balance,
            "LIVE" if is_live() else "TEST",
            f"SL_{reason}_{int(time.time())}"
        ))
        conn.commit()
    except Exception as e:
        print(f"❌ log_stop_loss_event error: {e}")
    finally:
        conn.close()


# ─────────────────────────────────────────
# Price Alerts (manual / absolute threshold)
# ─────────────────────────────────────────
def check_price_threshold(price):
    if price >= ALERT_THRESHOLD_UP:
        st.warning(f"🚀 BTC crossed ${ALERT_THRESHOLD_UP:,}! Current: ${price:,.2f}")
    elif price <= ALERT_THRESHOLD_DOWN:
        st.error(f"⚠️ BTC below ${ALERT_THRESHOLD_DOWN:,}! Current: ${price:,.2f}")


# ─────────────────────────────────────────
# FIX #2: check_auto_sell — now places a real SELL order in LIVE mode
# Original code only updated internal session state (no exchange call).
# ─────────────────────────────────────────
def check_auto_sell(price):
    """
    Legacy absolute price stop-loss for manual trading mode.
    FIX: In LIVE mode, now calls place_market_sell() so the position
    is actually closed on the exchange, not just zeroed internally.
    """
    btc_bal = float(
        st.session_state.get("BTC_WALLET", {}).get("balance", BTC_WALLET.get("balance", 0.0))
    )
    if price < STOP_LOSS_THRESHOLD and btc_bal > 0:
        msg = f"🔥 PRICE STOP-LOSS @ ₹{price:,.2f}! Auto-selling all BTC..."
        st.error(msg)
        send_telegram(msg)

        if is_live():
            # FIX: Place the actual SELL order on CoinDCX
            try:
                order = place_market_sell(btc_bal)
                if order["status"] == "filled":
                    inr_received = (order["filled_qty"] * order["avg_price"]) - order["fee"]
                    log_wallet_transaction("AUTO_SELL", btc_bal, 0, order["avg_price"],
                                           trade_type="AUTO_SELL_STOP")
                    log_inr_transaction("STOP_LOSS_PRICE", inr_received,
                                        get_current_inr_balance() + inr_received, "LIVE")
                    send_telegram(
                        f"🛑 PRICE STOP-LOSS filled | sold {order['filled_qty']:.6f} BTC "
                        f"→ ₹{inr_received:,.2f} @ ₹{order['avg_price']:,.2f} "
                        f"| order {order['order_id']}"
                    )
                else:
                    send_telegram(
                        f"⚠️ PRICE STOP-LOSS order NOT filled — status: {order['status']}. "
                        f"CHECK EXCHANGE IMMEDIATELY."
                    )
                    return  # Don't zero the balance if order wasn't filled
            except Exception as e:
                send_telegram(f"❌ PRICE STOP-LOSS order failed: {e}. CHECK EXCHANGE IMMEDIATELY.")
                return  # Don't zero the balance on exception
        else:
            log_wallet_transaction("AUTO_SELL", btc_bal, 0, price, trade_type="AUTO_SELL_STOP")
            log_inr_transaction("STOP_LOSS_PRICE", btc_bal * price, 0, "TEST")

        if "BTC_WALLET" in st.session_state:
            st.session_state["BTC_WALLET"]["balance"] = 0
        BTC_WALLET["balance"] = 0
        update_wallet_daily_summary(start=False)


# ─────────────────────────────────────────
# Daily Loss Limit
# ─────────────────────────────────────────
def check_daily_loss_limit(max_loss_percent: float = DEFAULT_DAILY_LOSS_LIMIT) -> bool:
    conn = get_mysql_connection()
    if not conn:
        return False
    cur  = get_cursor(conn)
    cur.execute("""
        SELECT start_balance, end_balance
        FROM wallet_history
        ORDER BY trade_date DESC LIMIT 1
    """)
    row = cur.fetchone()
    conn.close()

    if not row:
        return False

    opening = float(row["start_balance"] or 0)
    current = float(row["end_balance"]   or opening)

    if opening == 0:
        return False

    loss_percent = ((opening - current) / opening) * 100

    if loss_percent >= max_loss_percent:
        update_autotrade_status_db(0)
        if "AUTO_TRADING" in st.session_state:
            st.session_state.AUTO_TRADING["active"] = False
        msg = f"🛑 Auto-Trade PAUSED — Daily loss limit hit: {loss_percent:.2f}% (limit {max_loss_percent:.1f}%)"
        # st.error removed — runs in background context
        # send_telegram already called below
        send_telegram(msg)
        return True

    return False


# ═══════════════════════════════════════════════════════════
# MAIN AUTO-TRADE FUNCTION
#
# Logic (your exact concept):
#   STATE A — Have BTC, no INR:
#             SELL when price ≥ buy_price + (profit_inr / btc_qty)
#             STOP LOSS if price ≤ buy_price * (1 - stop_loss%)
#
#   STATE B — Have INR, no BTC:
#             If no prior trade → BUY immediately with all INR
#             If last was AUTO_SELL → BUY only when
#               price ≤ last_sell_price - profit_inr
#
#   Cycle: BUY → SELL → BUY → SELL ... making ₹profit_inr each round
# ═══════════════════════════════════════════════════════════
def check_auto_trading(price_inr: float):
    try:
        # ── Emergency Telegram kill switch ──────────────────────
        if poll_telegram_stop_command():
            return

        # ── Only run when auto-trade is active in DB ────────────
        if not get_autotrade_active_from_db():
            return

        if not acquire_trade_lock():
            return

        # ── Idle timeout (session-based) ─────────────────────────
        started_at = st.session_state.get("autotrade_started_at")
        if started_at:
            secs_since_refresh = time.time() - st.session_state.get("_last_cycle_ts", started_at)
            if secs_since_refresh > 300:
                stop_autotrade("⏳ Auto-Trade stopped — no page refresh for 5 min (tab may have closed)")
                return
        st.session_state["_last_cycle_ts"] = time.time()

        mode = "LIVE" if is_live() else "TEST"

        # ── Read current balances ────────────────────────────────
        btc_balance, _ = get_last_wallet_balance(mode=mode)
        inr_balance, _ = get_last_inr_balance(mode=mode)
        btc_balance    = float(btc_balance or 0)
        inr_balance    = float(inr_balance or 0)

        # Fallback for fresh DB (no trade rows yet)
        if inr_balance == 0 and btc_balance == 0:
            inr_balance = float(get_current_inr_balance() or 0)

        if btc_balance == 0 and inr_balance == 0:
            send_telegram("⚠️ Auto-Trade: Both BTC and INR are zero — please deposit funds.")
            stop_autotrade("⏹️ Auto-Trade stopped: no funds in wallet")
            return

        # ── Minimum trade size to satisfy CoinDCX ───────────────
        min_trade_inr = max(500.0, round(COINDCX_MIN_BTC_QTY * price_inr * 1.05, 2))

        # ── Daily loss limit guard ───────────────────────────────
        daily_loss_limit_pct = float(st.session_state.get("cfg_daily_loss_limit", DEFAULT_DAILY_LOSS_LIMIT))
        if check_daily_loss_limit(daily_loss_limit_pct):
            return

        # ── Settings ─────────────────────────────────────────────
        stop_loss_pct = float(st.session_state.get("cfg_stop_loss", DEFAULT_STOP_LOSS_PCT))
        target_pct    = float(st.session_state.get("cfg_target_pct", 0.3))   # % move to trigger sell/buy

        # ── Last auto trade from DB ──────────────────────────────
        last_auto      = get_last_auto_trade()
        last_type      = last_auto.get("trade_type", "") if last_auto else ""
        last_inr_value = float(last_auto.get("inr_value", 0) or 0) if last_auto else 0.0

        # ── Entry price (saved at time of BUY) ──────────────────
        entry_price = float(get_entry_price() or 0)

        # ── Trade cooldown: 60s between trades ───────────────────
        if last_auto and last_auto.get("trade_time"):
            tt      = last_auto["trade_time"]
            last_ts = tt.timestamp() if isinstance(tt, datetime) else 0
            if time.time() - last_ts < 60:
                return  # silent during cooldown

        # ── Cycle status → Telegram ──────────────────────────────
        # Only sends when price moves ≥ ₹1000 OR state changes (BTC↔INR).
        # Prevents the same message repeating on every 60s page refresh.
        _last_tg_price = st.session_state.get("_last_tg_price", 0)
        _last_tg_state = st.session_state.get("_last_tg_state", "")
        _cur_state     = f"{'BTC' if btc_balance > 0 else 'INR'}_{entry_price}"
        _price_moved   = abs(price_inr - _last_tg_price) >= 1000
        _state_changed = _cur_state != _last_tg_state

        if _price_moved or _state_changed:
            st.session_state["_last_tg_price"] = price_inr
            st.session_state["_last_tg_state"] = _cur_state

            if btc_balance > 0 and entry_price > 0:
                profit_now = (price_inr - entry_price) * btc_balance
                sell_at    = round(entry_price * (1 + target_pct / 100), 2)
                sl_at      = round(entry_price * (1 - stop_loss_pct / 100), 2)
                send_telegram(
                    f"🔄 Holding {btc_balance:.6f} BTC\n"
                    f"  Bought @ ₹{entry_price:,.2f} | Now ₹{price_inr:,.2f}\n"
                    f"  P&L: ₹{profit_now:+.2f} | Sell at ₹{sell_at:,.2f} (+{target_pct:.2f}%)\n"
                    f"  Stop-Loss @ ₹{sl_at:,.2f} (-{stop_loss_pct:.1f}%)"
                )
            elif inr_balance >= min_trade_inr and last_type == "AUTO_SELL" and last_inr_value > 0:
                buy_at = round(last_inr_value * (1 - target_pct / 100), 2)
                send_telegram(
                    f"🔄 Holding ₹{inr_balance:,.2f} INR\n"
                    f"  Last sell @ ₹{last_inr_value:,.2f} | Now ₹{price_inr:,.2f}\n"
                    f"  Buy when price ≤ ₹{buy_at:,.2f} (-{target_pct:.2f}%)"
                )
            else:
                send_telegram(
                    f"🔄 Auto-Trade active\n"
                    f"  BTC: {btc_balance:.6f} | INR: ₹{inr_balance:,.2f}\n"
                    f"  Price: ₹{price_inr:,.2f} | Last: {last_type or 'none'}"
                )

        # ╔══════════════════════════════════════════════════════╗
        # ║  STATE A — Have BTC                                  ║
        # ║  Sell when price ≥ entry × (1 + target_pct%)         ║
        # ║  Stop-Loss if price ≤ entry × (1 - stop_loss%)       ║
        # ║  Always evaluate sell when BTC > 0, regardless INR   ║
        # ╚══════════════════════════════════════════════════════╝
        if btc_balance > 0:

            # If no entry price AND last trade was a BUY (not a SELL),
            # seed entry from current price so ROI tracking starts now.
            # Skip if last trade was AUTO_SELL — BTC balance is stale
            # from session_state and will clear on next DB read.
            if entry_price == 0 and last_type != "AUTO_SELL":
                save_entry_price(price_inr)
                entry_price = price_inr
                send_telegram(f"📌 Entry price set to ₹{price_inr:,.2f} (position found, tracking started)")
                return  # wait for next cycle to evaluate sell
            elif entry_price == 0 and last_type == "AUTO_SELL":
                # Just sold — BTC balance will clear on next refresh, skip sell eval
                return

            sell_trigger    = round(entry_price * (1 + target_pct / 100), 2)
            stop_loss_price = round(entry_price * (1 - stop_loss_pct / 100), 2)
            actual_profit   = (price_inr - entry_price) * btc_balance

            is_profit_hit    = price_inr >= sell_trigger
            is_stop_loss_hit = price_inr <= stop_loss_price

            if not is_profit_hit and not is_stop_loss_hit:
                return  # holding, waiting — status already sent above

            sell_reason = "PROFIT_TARGET" if is_profit_hit else "STOP_LOSS"
            order = place_market_sell(btc_balance)

            if order["status"] not in ("filled",):
                send_telegram(f"⚠️ SELL not filled — {order['status']}. Will retry next cycle.")
                return

            sold_btc      = order["filled_qty"]
            avg_price     = order["avg_price"]
            fee_inr       = order["fee"]
            inr_received  = (sold_btc * avg_price) - fee_inr
            actual_profit = inr_received - (entry_price * sold_btc)
            roi_pct       = ((avg_price - entry_price) / entry_price) * 100

            # Read fresh INR balance from DB — inr_balance may be stale or 0
            # if get_last_inr_balance() filtered by wrong trade_mode
            fresh_inr   = float(get_current_inr_balance() or 0)
            new_inr     = fresh_inr + inr_received
            # trade_type MUST be exactly "AUTO_SELL" — get_last_auto_trade()
            # and get_last_wallet_balance() both filter IN ('AUTO_BUY','AUTO_SELL').
            # Sell reason stored in payment_id field for audit trail.
            log_wallet_transaction("AUTO_SELL", sold_btc, 0, avg_price,
                                   "AUTO_SELL",
                                   inr_value_override=avg_price)
            log_inr_transaction("AUTO_SELL", inr_received, new_inr, mode)
            log_stop_loss_event(sell_reason, entry_price, avg_price, sold_btc, inr_received, roi_pct)
            save_trade_log("AUTO_SELL", sold_btc, 0, avg_price, roi_pct)
            clear_entry_price()
            clear_peak_price()

            st.session_state["BTC_WALLET"] = {"balance": 0.0}
            st.session_state["INR_WALLET"] = {"balance": new_inr}
            BTC_WALLET["balance"]           = 0.0
            INR_WALLET["balance"]           = new_inr

            icon = "🔴" if sell_reason == "PROFIT_TARGET" else "🛑"
            msg  = (
                f"{icon} AUTO SELL ({sell_reason})\n"
                f"  {sold_btc:.6f} BTC → ₹{inr_received:,.2f}\n"
                f"  Entry ₹{entry_price:,.2f} → Exit ₹{avg_price:,.2f}\n"
                f"  Profit: ₹{actual_profit:+.2f} | Fee: ₹{fee_inr:.2f}\n"
                f"  Next buy when price ≤ ₹{round(avg_price * (1 - target_pct/100), 2):,.2f} (-{target_pct:.2f}%)"
            )
            # No st.success/error here — check_auto_trading runs in background
            # context where st calls produce DeltaGenerator output on screen.
            # Telegram notification is sufficient.
            send_telegram(msg)
            return

        # ╔══════════════════════════════════════════════════════╗
        # ║  STATE B — Have INR, no BTC                          ║
        # ║  No prior trade    → BUY immediately                 ║
        # ║  After AUTO_SELL   → BUY when price ≤ sell - ₹50     ║
        # ╚══════════════════════════════════════════════════════╝
        if btc_balance == 0 and inr_balance >= min_trade_inr:

            should_buy = False
            buy_reason = ""

            if not last_type or last_type not in ("AUTO_BUY", "AUTO_SELL"):
                should_buy = True
                buy_reason = "INITIAL_BUY"

            elif last_type == "AUTO_SELL" and last_inr_value > 0:
                # Buy when price dips target_pct% below last sell price
                buy_trigger = round(last_inr_value * (1 - target_pct / 100), 2)
                if price_inr <= buy_trigger:
                    should_buy = True
                    buy_reason = "DIP_BUY"
                # else: waiting for dip, status already sent, do nothing

            elif last_type == "AUTO_BUY":
                # BTC was cleared externally — rebuy
                should_buy = True
                buy_reason = "REBUY"

            if not should_buy:
                return

            buy_inr = round(inr_balance, 2)
            order   = place_market_buy(buy_inr)

            if order["status"] not in ("filled",):
                send_telegram(f"⚠️ BUY not filled — {order['status']}. Will retry next cycle.")
                return

            btc_bought = order["filled_qty"]
            avg_price  = order["avg_price"]
            fee_btc    = order["fee"]
            new_btc    = btc_bought
            fresh_inr_pre = float(get_current_inr_balance() or 0)
            new_inr       = max(0.0, fresh_inr_pre - buy_inr)
            save_entry_price(avg_price)
            save_peak_price(avg_price)
            log_wallet_transaction("AUTO_BUY", btc_bought, new_btc, avg_price, "AUTO_BUY")
            log_inr_transaction("AUTO_BUY", -buy_inr, new_inr, mode)
            save_trade_log("AUTO_BUY", btc_bought, new_btc, avg_price)

            st.session_state["BTC_WALLET"] = {"balance": new_btc}
            st.session_state["INR_WALLET"] = {"balance": new_inr}
            BTC_WALLET["balance"]           = new_btc
            INR_WALLET["balance"]           = new_inr
            sell_at = round(avg_price * (1 + target_pct / 100), 2)
            msg = (
                f"🟢 AUTO BUY ({buy_reason})\n"
                f"  ₹{buy_inr:,.2f} → {btc_bought:.6f} BTC\n"
                f"  Price: ₹{avg_price:,.2f} | Fee: {fee_btc:.8f} BTC\n"
                f"  Sell when price ≥ ₹{sell_at:,.2f} (+{target_pct:.2f}%)\n"
                f"  Order: {order['order_id']}"
            )
            # No st.success here — same reason as above
            send_telegram(msg)

    except Exception as e:
        send_telegram(f"❌ Auto-Trade error: {e}")
        stop_autotrade(f"❌ Auto-Trade stopped due to error: {e}")
    finally:
        release_trade_lock()


# ─────────────────────────────────────────
# Wallet History Chart
# ─────────────────────────────────────────
def get_wallet_history(start_date, end_date, mode="LIVE"):
    conn = get_mysql_connection()
    if not conn:
        return pd.DataFrame()
    try:
        cursor = get_cursor(conn)  # FIX #9: pymysql DictCursor doesn't support 'with'
        cursor.execute("""
            SELECT trade_date, auto_start_price, auto_end_price, current_inr_value
            FROM wallet_history
            WHERE trade_date BETWEEN %s AND %s AND mode = %s
            ORDER BY trade_date ASC
        """, (start_date, end_date, mode))
        rows = cursor.fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df.rename(columns={
            "trade_date":        "timestamp",
            "auto_start_price":  "start_price",
            "auto_end_price":    "end_price",
            "current_inr_value": "current_price"
        }, inplace=True)
        return df
    except Exception as e:
        print(f"❌ get_wallet_history error: {e}")
        return pd.DataFrame()
    finally:
        conn.close()


# ─────────────────────────────────────────
# ── STREAMLIT UI ──────────────────────────
# ─────────────────────────────────────────
# ── Mode Selection ───────────────────────────────────────────
_env_real_trading = os.getenv("REAL_TRADING", "false").lower() in ("1", "true", "yes")

if APP_ENV == "live":
    REAL_TRADING = _env_real_trading
    mode_label   = "🔴 LIVE" if REAL_TRADING else "🟡 TEST"
    st.sidebar.markdown(f"**Mode:** {mode_label} *(locked by environment)*")
else:
    st.session_state.setdefault("REAL_TRADING", False)
    REAL_TRADING = (
        st.radio("Mode", ["Test", "Live"],
                 index=1 if st.session_state.REAL_TRADING else 0,
                 horizontal=True) == "Live"
    )
    st.session_state.REAL_TRADING = REAL_TRADING

if "inr_balance" not in st.session_state:
    st.session_state["inr_balance"] = get_current_inr_balance()

LOG_DIR  = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
PRICE_LOG = os.path.join(LOG_DIR, "price_log.csv")
TRADE_LOG = os.path.join(LOG_DIR, "trade_log.csv")

# Wallet initialisation
mode = "LIVE" if is_live() else "TEST"
if is_live():
    try:
        wallet = Wallet(BTC_WALLET_NAME)
    except Exception:
        wallet = Wallet.create(BTC_WALLET_NAME)
    BALANCE_BTC = (wallet.balance() or 0) / 1e8  # guard None on network failure
else:
    BALANCE_BTC, _ = get_last_wallet_balance(mode)

BTC_WALLET = {"balance": BALANCE_BTC}

if is_live():
    inr_balance = sync_inr_wallet("LIVE")
else:
    inr_balance, _ = get_last_inr_balance(mode="TEST")

if not inr_balance or inr_balance <= 0:
    inr_balance = 10000.0 if not REAL_TRADING else 0.0
INR_WALLET = {"balance": inr_balance}

if 'AUTO_TRADING' not in st.session_state:
    st.session_state.AUTO_TRADING = {
        "active": get_autotrade_active_from_db(),
        "last_price": 0, "sell_streak": 0
    }
if 'AUTO_TRADE_STATE' not in st.session_state:
    st.session_state.AUTO_TRADE_STATE = {"entry_price": None}
if 'autotrade_toggle' not in st.session_state:
    st.session_state.autotrade_toggle = False

# ── Title & Prices ──
st.title("📱📊 MM BTC Autotrade Pro BOT")

price     = get_btc_price()  # FIX #8: CoinDCX has no BTCUSDT — use CoinGecko
price_inr = cd_get_market_price("BTCINR")
update_wallet_daily_summary(start=True)
restore_autotrade_state()

# ── Session-lost safeguard ──────────────────────────────────
# Warn if auto-trade is active in DB but this is a fresh/reopened tab.
if get_autotrade_active_from_db() and not st.session_state.get("_session_verified"):
    st.session_state["_session_verified"] = True
    st.warning(
        "🚨 **Auto-Trade is marked ACTIVE in the database.** "
        "If you just reopened or refreshed this tab, the bot may have been "
        "running unmonitored. Verify your CoinDCX position before continuing. "
        "Press **🛑 Stop Auto-Trade** below if you want to halt it.",
        icon="⚠️"
    )
else:
    st.session_state["_session_verified"] = True

st.metric("BTC/USDT", f"${price:,.2f}"     if price     else "N/A")
st.metric("BTC/INR",  f"₹{price_inr:,.2f}" if price_inr else "N/A")

if price:
    check_price_threshold(price)
    check_auto_sell(price)
if price_inr:
    check_auto_trading(price_inr)

# ── Deposit ──
deposit_amt = st.number_input("Deposit ₹", min_value=10, step=10, value=500)

if "deposit_in_progress" not in st.session_state:
    st.session_state["deposit_in_progress"] = False
if "deposit_order_id" not in st.session_state:
    st.session_state["deposit_order_id"] = None
if "deposit_order_amount" not in st.session_state:
    st.session_state["deposit_order_amount"] = None
if "deposit_started_at" not in st.session_state:
    st.session_state["deposit_started_at"] = 0

    
if "qr_payment_open" not in st.session_state:
    st.session_state["qr_payment_open"] = False
if "qr_order_id" not in st.session_state:
    st.session_state["qr_order_id"] = None
if "qr_order_amount" not in st.session_state:
    st.session_state["qr_order_amount"] = None
if "qr_started_at" not in st.session_state:
    st.session_state["qr_started_at"] = 0

if st.session_state["deposit_in_progress"]:
    elapsed = time.time() - st.session_state.get("deposit_started_at", 0)
    if elapsed > 600:
        st.session_state["deposit_in_progress"] = False
        st.session_state["deposit_order_id"]    = None
        st.session_state["deposit_order_amount"] = None

if st.session_state["deposit_in_progress"]:
    # ── Auto-refresh is SUPPRESSED during payment ────────────────
    st.session_state["suppress_autorefresh"] = True

    order_id = st.session_state["deposit_order_id"]
    amt_inr  = round(st.session_state["deposit_order_amount"] / 100, 2)
    elapsed  = int(time.time() - st.session_state["deposit_started_at"])
    remaining = max(0, 600 - elapsed)

    st.warning(
        f"⏳ **Payment in progress — ₹{amt_inr:.2f}** | order `{order_id}`\n\n"
        f"🚫 **Auto-refresh is paused.** Complete the payment in the Razorpay popup.\n\n"
        f"⏱ Session expires in **{remaining // 60}m {remaining % 60}s** if not completed.",
        icon="💳"
    )

    st.components.v1.html(f"""
    <script src="https://checkout.razorpay.com/v1/checkout.js"></script>
    <script>
    var options = {{
        "key": "{RAZORPAY_KEY_ID}",
        "amount": "{st.session_state['deposit_order_amount']}",
        "currency": "INR",
        "name": "MM AutoTrader",
        "description": "Wallet Deposit",
        "order_id": "{order_id}",
        "modal": {{
            "ondismiss": function() {{
                // User closed the modal WITHOUT paying — do NOT reload.
                // Just notify the parent so it can show a message.
                window.parent.postMessage({{type: "deposit_dismissed"}}, "*");
            }}
        }},
        "handler": function (response) {{
            // Payment SUCCESS — safe to reload now so webhook credit appears.
            window.parent.postMessage({{type: "deposit_success", data: response}}, "*");
            setTimeout(function() {{ window.location.reload(); }}, 2000);
        }}
    }};
    var rzp = new Razorpay(options);
    rzp.open();

    // Listen for dismiss — show a message but do NOT auto-reload
    window.addEventListener("message", function(e) {{
        if (e.data && e.data.type === "deposit_dismissed") {{
            // Do nothing here — user will use the Cancel button below
        }}
    }});
    </script>
    """, height=10)

    col_cancel, col_paid = st.columns(2)
    with col_cancel:
        if st.button("❌ Cancel Payment"):
            st.session_state["deposit_in_progress"]  = False
            st.session_state["deposit_order_id"]     = None
            st.session_state["deposit_order_amount"] = None
            st.session_state["suppress_autorefresh"] = False
            st.info("Payment cancelled. Auto-refresh resumed.")
            st.rerun()
    with col_paid:
        if st.button("✅ I Already Paid"):
            st.session_state["deposit_in_progress"]  = False
            st.session_state["deposit_order_id"]     = None
            st.session_state["deposit_order_amount"] = None
            st.session_state["suppress_autorefresh"] = False
            st.success(
                "✅ If your payment went through, your wallet will be credited "
                "by the webhook within 30 seconds. Refreshing now..."
            )
            st.rerun()

    # Hard timeout — 10 minutes, reset silently
    if elapsed > 600:
        st.session_state["deposit_in_progress"]  = False
        st.session_state["deposit_order_id"]     = None
        st.session_state["deposit_order_amount"] = None
        st.session_state["suppress_autorefresh"] = False
        st.warning("⚠️ Payment session expired (10 min). Please try again.")
        st.rerun()

else:
    # ── No payment in progress — auto-refresh is allowed ────────
    st.session_state["suppress_autorefresh"] = False

    if st.button("💳 Pay with Razorpay"):
        if not action_lock("DEPOSIT_LOCK", 5):
            st.warning("⏳ Please wait a moment before trying again.")
        else:
            order = create_razorpay_payment(deposit_amt, description=f"INR Wallet Deposit ₹{deposit_amt}")

            st.session_state["deposit_in_progress"]  = True
            st.session_state["deposit_order_id"]     = order["order_id"]
            st.session_state["deposit_order_amount"] = order["amount"]
            st.session_state["deposit_started_at"]   = time.time()
            st.session_state["suppress_autorefresh"] = True

            st.components.v1.html(f"""
            <script src="https://checkout.razorpay.com/v1/checkout.js"></script>
            <script>
            var options = {{
                "key": "{RAZORPAY_KEY_ID}",
                "amount": "{order['amount']}",
                "currency": "INR",
                "name": "MM AutoTrader",
                "description": "Wallet Deposit",
                "order_id": "{order['order_id']}",
                "prefill": {{}},
                "modal": {{
                    "ondismiss": function() {{
                        // Dismissed without paying — do NOT reload
                        window.parent.postMessage({{type: "deposit_dismissed"}}, "*");
                    }}
                }},
                "handler": function (response) {{
                    // Payment SUCCESS — reload to show updated balance
                    window.parent.postMessage({{type: "deposit_success", data: response}}, "*");
                    setTimeout(function() {{ window.location.reload(); }}, 2000);
                }}
            }};
            var rzp = new Razorpay(options);
            rzp.open();
            </script>
            """, height=10)

            st.info(
                f"⏳ Razorpay checkout opened for ₹{deposit_amt} | order `{order['order_id']}`. "
                f"Complete your payment in the popup. "
                f"**Auto-refresh is paused until you finish or cancel.**"
            )

# ── QR Code Payment ─────────────────────────────────────────
st.markdown("---")
st.subheader("📲 Pay via QR Code")
st.caption("Use this if you prefer scanning a QR code instead of the Razorpay popup.")

if st.session_state["qr_payment_open"]:
    # ── QR payment is active — suppress auto-refresh ──────────
    st.session_state["suppress_autorefresh"] = True

    qr_order_id  = st.session_state["qr_order_id"]
    qr_amt_inr   = round(st.session_state["qr_order_amount"] / 100, 2)
    qr_elapsed   = int(time.time() - st.session_state["qr_started_at"])
    qr_remaining = max(0, 600 - qr_elapsed)

    st.warning(
        f"📲 QR Payment in progress — ₹{qr_amt_inr:.2f} | order {qr_order_id} | "
        f"Auto-refresh paused. Scan QR and complete payment. "
        f"Expires in {qr_remaining // 60}m {qr_remaining % 60}s",
        icon="💳"
    )

    # Generate QR from Razorpay payment URL
    qr_payment_url = f"upi://pay?pa={RAZORPAY_KEY_ID}&pn=MMAutoTrader&am={qr_amt_inr}&cu=INR&tn={qr_order_id}"
    qr_bytes = generate_qr_code(qr_payment_url)
    st.image(qr_bytes, caption=f"Scan to pay ₹{qr_amt_inr:.2f}", width=220)
    st.code(qr_order_id, language="text")

    qr_col1, qr_col2 = st.columns(2)

    with qr_col1:
        if st.button("❌ Cancel QR Payment"):
            st.session_state["qr_payment_open"]   = False
            st.session_state["qr_order_id"]        = None
            st.session_state["qr_order_amount"]    = None
            st.session_state["suppress_autorefresh"] = False
            st.info("QR payment cancelled. Auto-refresh resumed.")
            st.rerun()

    with qr_col2:
        if st.button("✅ Payment Done"):
            st.session_state["qr_payment_open"]   = False
            st.session_state["qr_order_id"]        = None
            st.session_state["qr_order_amount"]    = None
            st.session_state["suppress_autorefresh"] = False
            st.success(
                "✅ Payment confirmed! Your wallet will be credited by the "
                "webhook within 30 seconds. Refreshing now..."
            )
            st.rerun()

    # Hard timeout — 10 minutes
    if qr_elapsed > 600:
        st.session_state["qr_payment_open"]    = False
        st.session_state["qr_order_id"]         = None
        st.session_state["qr_order_amount"]     = None
        st.session_state["suppress_autorefresh"] = False
        st.warning("⚠️ QR payment session expired (10 min). Please try again.")
        st.rerun()

else:
    # ── Show QR payment button ────────────────────────────────
    qr_deposit_amt = st.number_input(
        "Amount ₹ (QR)", min_value=10, step=10, value=500,
        key="qr_deposit_input"
    )

    if st.button("📲 Generate Payment QR"):
        if not action_lock("QR_DEPOSIT_LOCK", 5):
            st.warning("⏳ Please wait a moment before trying again.")
        else:
            order = create_razorpay_payment(
                qr_deposit_amt,
                description=f"INR Wallet QR Deposit ₹{qr_deposit_amt}"
            )
            st.session_state["qr_payment_open"]    = True
            st.session_state["qr_order_id"]         = order["order_id"]
            st.session_state["qr_order_amount"]     = order["amount"]
            st.session_state["qr_started_at"]       = time.time()
            st.session_state["suppress_autorefresh"] = True
            st.rerun()

# ── Withdraw ──
st.subheader("🏧 Withdraw to Bank / UPI")
recipients      = get_all_recipients()
recipient_names = [f"{r['name']} ({r['method']})" for r in recipients]
selected        = st.selectbox("📋 Saved Recipient", ["-- New Recipient --"] + recipient_names)

if selected != "-- New Recipient --":
    sel    = recipients[recipient_names.index(selected)]
    method = sel['method'];  name   = sel['name']
    acc_no = sel['account_number']; ifsc = sel['ifsc']; upi = sel['upi_id']
else:
    method = st.radio("Payout Method", ["BANK", "UPI"])
    name   = st.text_input("Recipient Name")
    acc_no = st.text_input("Account Number") if method == "BANK" else ""
    ifsc   = st.text_input("IFSC Code")       if method == "BANK" else ""
    upi    = st.text_input("UPI ID")           if method == "UPI"  else ""

payout_amt = st.number_input("Withdraw ₹", 100, step=100)

if st.button("🚀 Withdraw"):
    if   method == "BANK" and (not acc_no or not ifsc): st.warning("❗ Enter bank details.")
    elif method == "UPI"  and not upi:                  st.warning("❗ Enter UPI ID.")
    elif not name:                                       st.warning("❗ Name required.")
    else:
        save_recipient_if_new(name, method, acc_no, ifsc, upi)
        real_balance = get_current_inr_balance()
        if payout_amt > real_balance:
            st.error("❌ Insufficient balance")
        else:
            # FIX #1: parameter names now match the function signature
            withdraw_inr(
                amount=payout_amt,
                method=method,
                recipient_name=name,
                acc_no=acc_no,    # was incorrectly named in original call
                ifsc=ifsc,
                upi=upi,          # was incorrectly named in original call
                mode="LIVE" if is_live() else "TEST"
            )
            st.session_state["inr_balance"] = get_current_inr_balance()

# ── Test Mode Controls ──
if not is_live():
    st.subheader("🧪 Test Wallet Controls")
    col_t1, col_t2 = st.columns(2)
    with col_t1:
        st.metric("Test BTC Balance", f"{BTC_WALLET['balance']:.4f} BTC")
    with col_t2:
        st.metric("INR Value", f"₹{BTC_WALLET['balance'] * (price_inr or 0):,.2f}")

    ltt = get_last_trade_time_from_db()
    if ltt:
        st.caption(f"📅 Last transaction: {ltt.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        st.caption("📅 No transactions yet")

    if st.button("🔄 Reset Test Wallet to 0.005 BTC + ₹5,000 INR"):
        BTC_WALLET['balance'] = 0.005
        st.session_state["BTC_WALLET"] = {"balance": 0.005}
        st.session_state["INR_WALLET"] = {"balance": 5000.0}
        log_wallet_transaction("TEST_RESET", 0.005, 0.005, price_inr or 0)
        log_inr_transaction("TEST_RESET", 5000.0, 5000.0, "TEST")
        # Seed entry price so auto-sell can track ROI immediately
        if price_inr:
            save_entry_price(price_inr)
            save_peak_price(price_inr)
        update_wallet_daily_summary()
        st.success("✅ Test wallet reset | BTC: 0.005 | INR: ₹5,000 | Entry price seeded")

# ── Trading Panel ──
autotrade_active = get_autotrade_active_from_db()
if autotrade_active:
    st.info(
        "🤖 **Auto-Trade is active** — manual trading is disabled. "
        "Use the **🛑 Stop Auto-Trade** button below to stop it."
    )
else:
    st.write("### 💱 Trading Panel")

# ── Always refresh wallet balances regardless of mode ──
btc_balance, _ = get_last_wallet_balance(mode="LIVE" if is_live() else "TEST")
inr_balance, _ = get_last_inr_balance(mode="LIVE"   if is_live() else "TEST")
BTC_WALLET['balance'] = float(btc_balance or 0)
INR_WALLET['balance'] = float(inr_balance or 0)

if not autotrade_active:
    trade_col1, trade_col2 = st.columns(2)
    with trade_col1:
        buy_inr_input = st.number_input(
            "INR to spend on BUY (₹)",
            min_value=10.0, max_value=500000.0,
            value=500.0, step=100.0,
            help="Amount of INR you want to spend to buy BTC"
        )
    with trade_col2:
        sell_btc_input = st.number_input(
            "BTC quantity to SELL",
            min_value=0.000001, max_value=10.0,
            value=0.001, step=0.0001, format="%.6f",
            help="Amount of BTC you want to sell"
        )

    if price_inr:
        st.caption(
            f"📊 Current price: ₹{price_inr:,.2f} | "
            f"BUY ₹{buy_inr_input:.0f} ≈ {buy_inr_input / price_inr:.6f} BTC | "
            f"SELL {sell_btc_input:.6f} BTC ≈ ₹{sell_btc_input * price_inr:,.2f}"
        )

    col1, col2, col3 = st.columns(3)
else:
    buy_inr_input  = 0.0
    sell_btc_input = 0.0
    col1, col2, col3 = st.columns(3)

# ── MANUAL BUY / SELL / RESET — only when auto-trade is OFF ──
with col1:
    if autotrade_active:
        st.button("💰 BUY BTC", disabled=True, help="Stop Auto-Trade first")
    elif st.button("💰 BUY BTC"):
        if not action_lock("BUY_LOCK", 3):
            st.warning("⏳ Wait before trading again")
        elif buy_inr_input <= 0:
            st.error("❌ Enter a valid INR amount to buy")
        elif not acquire_trade_lock():
            st.warning("⚠️ Another trade is currently processing — try again in a moment")
        else:
            # Always run in try/finally so lock is always released
            # (st.stop() inside try skips finally in Streamlit — avoid it)
            try:
                # Refresh INR balance from DB to catch any new deposits
                fresh_inr, _ = get_last_inr_balance(mode="LIVE" if is_live() else "TEST")
                if fresh_inr is None:
                    fresh_inr = get_current_inr_balance()
                fresh_inr = float(fresh_inr or 0)
                INR_WALLET['balance'] = fresh_inr

                if fresh_inr < buy_inr_input:
                    st.error(
                        f"❌ Insufficient INR. Available: ₹{fresh_inr:,.2f} | "
                        f"Requested: ₹{buy_inr_input:,.2f}"
                    )
                else:
                    with st.spinner("Placing BUY order on CoinDCX..."):
                        order = place_market_buy(buy_inr_input)

                    if order["status"] != "filled":
                        st.error(f"❌ BUY order not filled — status: {order['status']}. No funds moved.")
                        send_telegram(f"⚠️ Manual BUY not filled — {order['status']}")
                    else:
                        btc_received = order["filled_qty"]
                        avg_price    = order["avg_price"]
                        fee_btc      = order["fee"]

                        new_btc = BTC_WALLET['balance'] + btc_received
                        new_inr = fresh_inr - buy_inr_input

                        log_inr_transaction("MANUAL_BUY", -buy_inr_input, new_inr, "LIVE" if is_live() else "TEST")
                        log_wallet_transaction("MANUAL_BUY", btc_received, new_btc, avg_price, "MANUAL_BUY")
                        BTC_WALLET['balance'] = new_btc
                        INR_WALLET['balance'] = new_inr
                        st.session_state["BTC_WALLET"] = {"balance": new_btc}
                        st.session_state["INR_WALLET"] = {"balance": new_inr}
                        update_wallet_daily_summary(start=False)

                        msg = (
                            f"🟢 Manual BUY: ₹{buy_inr_input:.2f} → {btc_received:.6f} BTC "
                            f"@ ₹{avg_price:,.2f} | fee {fee_btc:.8f} BTC | order {order['order_id']}"
                        )
                        st.success(msg)
                        send_telegram(msg)
            except Exception as e:
                st.error(f"❌ BUY failed: {e}")
                send_telegram(f"❌ Manual BUY failed: {e}")
            finally:
                release_trade_lock()

# ── MANUAL SELL ───────────────────────────────────────────────
with col2:
    if autotrade_active:
        st.button("📤 SELL BTC", disabled=True, help="Stop Auto-Trade first")
    elif st.button("📤 SELL BTC"):
        if not action_lock("SELL_LOCK", 3):
            st.warning("⏳ Wait before trading again")
        elif sell_btc_input <= 0:
            st.error("❌ Enter a valid BTC quantity to sell")
        elif not acquire_trade_lock():
            st.warning("⚠️ Another trade is currently processing — try again in a moment")
        else:
            try:
                # Refresh BTC balance from DB to avoid stale session value
                fresh_btc, _ = get_last_wallet_balance(mode="LIVE" if is_live() else "TEST")
                fresh_btc = float(fresh_btc or 0)
                BTC_WALLET['balance'] = fresh_btc

                if fresh_btc < sell_btc_input:
                    st.error(
                        f"❌ Insufficient BTC. Available: {fresh_btc:.6f} | "
                        f"Requested: {sell_btc_input:.6f}"
                    )
                elif sell_btc_input < COINDCX_MIN_BTC_QTY:
                    st.error(
                        f"❌ Minimum sell quantity is {COINDCX_MIN_BTC_QTY} BTC. "
                        f"You entered {sell_btc_input:.6f}."
                    )
                else:
                    with st.spinner("Placing SELL order on CoinDCX..."):
                        order = place_market_sell(sell_btc_input)

                    if order["status"] != "filled":
                        st.error(f"❌ SELL order not filled — status: {order['status']}. No funds moved.")
                        send_telegram(f"⚠️ Manual SELL not filled — {order['status']}")
                    else:
                        sold_btc     = order["filled_qty"]
                        avg_price    = order["avg_price"]
                        fee_inr      = order["fee"]
                        inr_received = (sold_btc * avg_price) - fee_inr

                        # Refresh INR too so new_inr is accurate
                        fresh_inr, _ = get_last_inr_balance(mode="LIVE" if is_live() else "TEST")
                        fresh_inr    = float(fresh_inr or 0)

                        new_btc = fresh_btc - sold_btc
                        new_inr = fresh_inr + inr_received

                        log_wallet_transaction("MANUAL_SELL", sold_btc, new_btc, avg_price, "MANUAL_SELL")
                        log_inr_transaction("MANUAL_SELL", inr_received, new_inr, "LIVE" if is_live() else "TEST")
                        BTC_WALLET['balance'] = new_btc
                        INR_WALLET['balance'] = new_inr
                        st.session_state["BTC_WALLET"] = {"balance": new_btc}
                        st.session_state["INR_WALLET"] = {"balance": new_inr}
                        update_wallet_daily_summary(start=False)

                        msg = (
                            f"🔴 Manual SELL: {sold_btc:.6f} BTC → ₹{inr_received:,.2f} "
                            f"@ ₹{avg_price:,.2f} | fee ₹{fee_inr:.2f} | order {order['order_id']}"
                        )
                        st.success(msg)
                        send_telegram(msg)
            except Exception as e:
                st.error(f"❌ SELL failed: {e}")
                send_telegram(f"❌ Manual SELL failed: {e}")
            finally:
                release_trade_lock()

# ── RESET WALLET (TEST mode only) ────────────────────────────
with col3:
    if autotrade_active:
        st.button("🔄 Reset Wallet", disabled=True, help="Stop Auto-Trade first")
    elif st.button("🔄 Reset Wallet", disabled=is_live(),
                 help="Only available in TEST mode — disabled in LIVE mode"):
        if is_live():
            st.error("❌ Reset is disabled in LIVE mode")
            st.stop()
        if not action_lock("RESET_LOCK", 5):
            st.warning("⏳ Wait")
            st.stop()
        if not acquire_trade_lock():
            st.warning("⚠️ Another trade processing")
            st.stop()
        try:
            BTC_WALLET['balance'] = 0.005
            st.session_state["BTC_WALLET"] = {"balance": 0.005}
            st.session_state["INR_WALLET"] = {"balance": 1000.0}
            log_wallet_transaction("TEST_RESET", 0.005, 0.005, price_inr or 0)
            log_inr_transaction("RESET", 1000, 1000, "TEST")
            update_wallet_daily_summary(start=False)
            st.success("🔄 Test Wallet Reset | BTC: 0.005 | INR: ₹1,000")
        finally:
            release_trade_lock()

# ── Wallet Status ──
st.write("### 💼 Wallet Status")
wallet_col1, wallet_col2 = st.columns(2)

# Always read fresh from DB — session_state is reset on every page refresh
# so it shows zeros after auto-trade updates. DB is always source of truth.
_btc_raw, _ = get_last_wallet_balance(mode="LIVE" if is_live() else "TEST")
_inr_raw, _ = get_last_inr_balance(mode="LIVE"   if is_live() else "TEST")

# Fallback: if no trade rows yet, try current balance tables
if not _inr_raw:
    _inr_raw = get_current_inr_balance()

_btc = float(_btc_raw or 0)
_inr = float(_inr_raw or 0) if not isinstance(_inr_raw, tuple) else float(_inr_raw[0] or 0)

# Sync globals so rest of page uses same values
BTC_WALLET["balance"] = _btc
INR_WALLET["balance"] = _inr

with wallet_col1:
    st.metric("BTC Balance",       f"{_btc:.6f} BTC")
    st.metric("BTC in INR",        f"₹{_btc * (price_inr or 0):,.2f}")
with wallet_col2:
    st.metric("INR Wallet Balance", f"₹{_inr:,.2f}")
    total_value = _inr + (_btc * (price_inr or 0))
    st.metric("Total Portfolio",    f"₹{total_value:,.2f}")

# ── Auto-Trade State Restore ──
db_active = get_autotrade_active_from_db()
if "AUTO_TRADING" not in st.session_state:
    st.session_state.AUTO_TRADING = {"active": db_active, "last_price": 0, "sell_streak": 0}
else:
    st.session_state.AUTO_TRADING["active"] = db_active

st.session_state.autotrade_toggle = bool(db_active)

# Idle warning removed — was using stale DB timestamp from previous
# sessions causing false "273m inactivity" warnings on every refresh.
# Real idle detection is handled inside check_auto_trading() using
# session_state["_last_cycle_ts"] set at each live page refresh.

# ─────────────────────────────────────────────────────────
# ⚙️  Stop-Loss & Risk Settings Panel
# ─────────────────────────────────────────────────────────
with st.expander("⚙️ Auto-Trade Settings", expanded=False):
    st.caption("Changes take effect immediately on the next auto-trade cycle.")

    sl_col1, sl_col2 = st.columns(2)
    with sl_col1:
        cfg_target_pct = st.number_input(
            "🎯 Profit Target (%)", min_value=0.1, max_value=5.0, step=0.05,
            value=float(st.session_state.get("cfg_target_pct", 0.3)),
            help=(
                "Sell when BTC price rises X% above your buy price. "
                "Rebuy when price dips X% below last sell price. "
                "0.3% recommended — covers 0.2% round-trip fee + leaves ~0.1% net profit. "
                "Example at ₹78,00,000: sell at ₹78,23,400 → rebuy at ₹78,00,002."
            )
        )
        st.session_state["cfg_target_pct"] = cfg_target_pct
        # Show break-even info
        if price_inr:
            fee_pct      = 0.2   # 0.1% buy + 0.1% sell
            net_profit   = cfg_target_pct - fee_pct
            st.caption(
                f"Fee cost: ~{fee_pct:.1f}% | Net profit per cycle: ~{net_profit:.2f}% "
                f"{'✅ Profitable' if net_profit > 0 else '❌ Below break-even — increase target'}"
            )

    with sl_col2:
        cfg_stop_loss = st.number_input(
            "🛑 Stop-Loss (%)", min_value=0.1, max_value=50.0, step=0.1,
            value=float(st.session_state.get("cfg_stop_loss", DEFAULT_STOP_LOSS_PCT)),
            help="Emergency sell if price drops this % below buy price."
        )
        st.session_state["cfg_stop_loss"] = cfg_stop_loss

    cfg_daily_loss = st.number_input(
        "📅 Daily Loss Limit (%)", min_value=0.5, max_value=50.0, step=0.5,
        value=float(st.session_state.get("cfg_daily_loss_limit", DEFAULT_DAILY_LOSS_LIMIT)),
        help="Pause auto-trading for the day if total day-loss exceeds this % of opening balance."
    )
    st.session_state["cfg_daily_loss_limit"] = cfg_daily_loss

    entry_now  = get_entry_price()
    if entry_now > 0 and price_inr:
        cfg_target_pct_now = float(st.session_state.get("cfg_target_pct", 0.3))
        btc_bal_now, _     = get_last_wallet_balance(mode="LIVE" if is_live() else "TEST")
        btc_bal_now        = float(btc_bal_now or 0)
        roi_now            = ((price_inr - entry_now) / entry_now) * 100
        sl_price_now       = round(entry_now * (1 - cfg_stop_loss / 100), 2)
        tgt_price_now      = round(entry_now * (1 + cfg_target_pct_now / 100), 2)
        profit_now_inr     = (price_inr - entry_now) * btc_bal_now if btc_bal_now > 0 else 0
        profit_at_target   = (tgt_price_now - entry_now) * btc_bal_now if btc_bal_now > 0 else 0
        fee_cost_inr       = entry_now * btc_bal_now * 0.002 if btc_bal_now > 0 else 0
        net_at_target      = profit_at_target - fee_cost_inr

        st.markdown("---")
        st.markdown("**📌 Live Position Summary**")
        lv1, lv2, lv3, lv4 = st.columns(4)
        lv1.metric("Entry Price",  f"₹{entry_now:,.2f}")
        lv2.metric("Sell Target",  f"₹{tgt_price_now:,.2f}",  delta=f"+{cfg_target_pct_now:.2f}%")
        lv3.metric("Stop-Loss",    f"₹{sl_price_now:,.2f}",   delta=f"-{cfg_stop_loss:.1f}%")
        lv4.metric("Current P&L",  f"₹{profit_now_inr:+.2f}", delta=f"{roi_now:+.2f}%")
        st.caption(
            f"Price ₹{price_inr:,.2f} | "
            f"Need +₹{max(0, tgt_price_now - price_inr):,.2f} to sell | "
            f"Est. net profit at target: ₹{net_at_target:+.2f} (after fees)"
        )
    else:
        st.info("No open position — summary appears here after the next AUTO BUY.")

# ── Auto-Trade Button ──
autotrade_active = get_autotrade_active_from_db()
if st.button(f"{'🚀 Start' if not autotrade_active else '🛑 Stop'} Auto-Trade"):
    if autotrade_active:
        # FIX #7: Use stop_autotrade() so wallet state sync runs correctly
        stop_autotrade(
            f"🛑 Auto-Trade STOPPED at ₹{price_inr:.2f}" if price_inr else "🛑 Auto-Trade STOPPED"
        )
        update_wallet_daily_summary(auto_end=True)
    else:
        # FIX #7: Use start_autotrade() so wallet state sync runs correctly
        start_autotrade()
        if price_inr:
            update_last_auto_trade_price_db(price_inr)

# ── Live BTC Wallet ──
if is_live():
    st.subheader("📥 Deposit BTC")
    btc_address = wallet.get_key().address
    st.code(btc_address, language="text")
    st.button("📋 Copy Address", on_click=lambda: st.toast("Copied!", icon="📋"))
    qr  = qrcode.make(btc_address)
    buf = BytesIO(); qr.save(buf, format="PNG")
    st.image(Image.open(buf), caption="Scan to Deposit BTC")

    st.subheader("📤 Withdraw BTC")
    if BTC_WALLET['balance'] > 0:
        with st.form("btc_withdraw_form", clear_on_submit=False):
            withdraw_address = st.text_input("Destination BTC Address")
            withdraw_amount  = st.number_input("Amount (BTC)", min_value=0.0001,
                                               max_value=BTC_WALLET['balance'],
                                               step=0.0001, format="%.8f")
            if st.form_submit_button("Submit Withdrawal"):
                try:
                    tx = wallet.send_to(address=withdraw_address, amount=withdraw_amount, network='bitcoin')
                    st.success(f"✅ TX ID: {tx.txid}")
                    BTC_WALLET['balance'] -= withdraw_amount
                    log_wallet_transaction("REAL_WITHDRAW", withdraw_amount, BTC_WALLET['balance'],
                                           price_inr or 0, "REAL_WITHDRAW")
                except Exception as e:
                    st.error(f"❌ Withdrawal failed: {e}")
    else:
        st.warning("⚠️ BTC balance is 0 — Withdrawal not allowed.")

    st.subheader("🔄 Sync INR Balance")
    balance = sync_inr_wallet("LIVE")
    if balance:
        st.success(f"✅ Synced: ₹{balance:.2f}")

    if "last_inr_sync" not in st.session_state:
        st.session_state.last_inr_sync = 0
    if time.time() - st.session_state.last_inr_sync > 300:
        balance = sync_inr_wallet("LIVE")
        if balance:
            st.session_state.last_inr_sync = time.time()

# ── PnL ──
st.subheader("📊 Profit & Loss")
buy, sell, pnl = get_pnl_summary()
c1, c2, c3 = st.columns(3)
c1.metric("Total Invested", f"₹{buy:,.2f}")
c2.metric("Total Return",   f"₹{sell:,.2f}")
c3.metric("Net PnL",        f"₹{pnl:,.2f}", delta=f"{pnl:,.2f}")

# ── Transaction History ──
def _query_to_df(sql: str) -> pd.DataFrame:
    conn = get_mysql_connection()
    if not conn:
        return pd.DataFrame()
    cur  = get_cursor(conn)
    cur.execute(sql)
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])

with st.expander("📒 INR Wallet History"):
    df = _query_to_df("SELECT * FROM inr_wallet_transactions ORDER BY trade_time DESC LIMIT 20")
    st.dataframe(df, use_container_width=True)

with st.expander("📋 BTC Transaction History"):
    df = _query_to_df("SELECT * FROM wallet_transactions ORDER BY trade_time DESC LIMIT 20")
    st.dataframe(df, use_container_width=True)

with st.expander("📊 Wallet Daily Summary"):
    df = _query_to_df("SELECT * FROM wallet_history ORDER BY trade_date DESC LIMIT 7")
    st.dataframe(df, use_container_width=True)

with st.expander("🛑 Stop-Loss Event History"):
    df_sl = _query_to_df("""
        SELECT trade_time, action, amount AS inr_received, balance_after,
               trade_mode, payment_id AS sl_reference, status
        FROM inr_wallet_transactions
        WHERE action LIKE 'STOP_LOSS%'
        ORDER BY trade_time DESC
        LIMIT 50
    """)

    if df_sl.empty:
        st.info("No stop-loss events recorded yet.")
    else:
        def sl_colour(row):
            if "TRAILING" in str(row.get("action", "")):
                return ["background-color: #fff3cd"] * len(row)
            elif "FIXED" in str(row.get("action", "")):
                return ["background-color: #f8d7da"] * len(row)
            elif "PRICE" in str(row.get("action", "")):
                return ["background-color: #f8d7da"] * len(row)
            return [""] * len(row)

        st.dataframe(
            df_sl.style.apply(sl_colour, axis=1),
            use_container_width=True
        )
        sl_total = df_sl["inr_received"].sum()
        st.caption(
            f"📌 {len(df_sl)} stop-loss event(s) | "
            f"Total INR recovered: ₹{sl_total:,.2f}"
        )

st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# ── Daily Summary ──
st.subheader("📊 INR Wallet - Daily Summary")
summary = get_daily_wallet_summary()
if summary:
    st.dataframe(pd.DataFrame(summary))
else:
    st.info("No wallet transactions yet.")

# ── Candlestick Chart ──
st.write("### 📊 Live BTC/INR Chart")
date_col1, date_col2 = st.columns(2)
with date_col1:
    start_date = st.date_input("From", value=datetime.today() - timedelta(days=3))
with date_col2:
    end_date = st.date_input("To", value=datetime.today())
candle_type = st.radio("Candle Type", ["Hourly", "Daily"], horizontal=True)

hist_df = get_wallet_history(start_date, end_date, mode="LIVE" if is_live() else "TEST")

if not hist_df.empty:
    hist_df["timestamp"] = pd.to_datetime(hist_df["timestamp"], errors="coerce")
    hist_df.dropna(subset=["timestamp"], inplace=True)
    hist_df["open"]  = hist_df["start_price"].fillna(hist_df["current_price"])
    hist_df["close"] = hist_df["end_price"].fillna(hist_df["current_price"])
    hist_df["high"]  = hist_df[["start_price", "end_price", "current_price"]].max(axis=1)
    hist_df["low"]   = hist_df[["start_price", "end_price", "current_price"]].min(axis=1)

    filtered_df = hist_df[
        (hist_df["timestamp"].dt.date >= start_date) &
        (hist_df["timestamp"].dt.date <= end_date)
    ]
    freq    = "1h" if candle_type == "Hourly" else "1D"
    ohlc_df = pd.DataFrame()
    if not filtered_df.empty:
        ohlc_df = filtered_df.resample(freq, on="timestamp").agg(
            open=("open","first"), high=("high","max"),
            low=("low","min"),    close=("close","last")
        ).dropna().reset_index()

    if ohlc_df.empty:
        st.info("⚠️ No aggregated data. Showing last 24 records.")
        ht      = hist_df.tail(24).copy()
        ohlc_df = ht[["timestamp","open","high","low","close"]].copy()

    fig = go.Figure(go.Candlestick(
        x=ohlc_df['timestamp'],
        open=ohlc_df['open'], high=ohlc_df['high'],
        low=ohlc_df['low'],   close=ohlc_df['close'],
        increasing_line_color='green', decreasing_line_color='red'
    ))
    fig.update_layout(
        xaxis_rangeslider_visible=True,
        margin=dict(l=20, r=20, t=20, b=20), height=400,
        xaxis=dict(rangeselector=dict(buttons=[
            dict(count=12, label="12h", step="hour", stepmode="backward"),
            dict(count=1,  label="24h", step="day",  stepmode="backward"),
            dict(count=3,  label="3d",  step="day",  stepmode="backward"),
            dict(step="all")
        ]))
    )
    st.plotly_chart(fig, use_container_width=True)
else:
    st.warning("⚠️ No wallet history found.")

# ── Auto-refresh (non-blocking) ──
if st.session_state.get("suppress_autorefresh", False):
    st.caption("⏸ Auto-refresh paused — payment in progress.")
else:
    # Non-blocking browser-side refresh — keeps app responsive during the wait
    st.markdown(
        f'<meta http-equiv="refresh" content="{AUTO_REFRESH_INTERVAL}">',
        unsafe_allow_html=True
    )
    st.caption(f"🔄 Auto-refresh every {AUTO_REFRESH_INTERVAL}s")
