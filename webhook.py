
from flask import Flask, request, jsonify
from paytmchecksum import PaytmChecksum
import pymysql
import psycopg2
import streamlit as st
import os
from datetime import datetime
from dotenv import load_dotenv

app = Flask(__name__)

# Load from .env
load_dotenv()
PAYTM_MERCHANT_KEY = os.getenv("PAYTM_MERCHANT_KEY")

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

def get_mysql_connection_old():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DB", "btc_autotrade"),
        cursorclass=pymysql.cursors.DictCursor
    )

def log_webhook_request(data, status):
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS webhook_logs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                received_at DATETIME,
                payload TEXT,
                status VARCHAR(50)
            )
        """)
        cursor.execute("""
            INSERT INTO webhook_logs (received_at, payload, status)
            VALUES (%s, %s, %s)
        """, (datetime.now(), str(data), status))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Failed to log webhook:", e)

@app.route("/paytm-webhook", methods=["POST"])
def paytm_webhook():
    data = request.form.to_dict()
    paytm_checksum = data.pop("CHECKSUMHASH", None)
    is_valid = PaytmChecksum.verifySignature(data, PAYTM_MERCHANT_KEY, paytm_checksum)

    txn_status = data.get("STATUS")
    txn_id = data.get("TXNID")
    order_id = data.get("ORDERID")
    amount = float(data.get("TXNAMOUNT", 0.0))

    if not is_valid:
        log_webhook_request(data, "INVALID_CHECKSUM")
        return jsonify({"status": "invalid checksum"}), 400

    conn = get_mysql_connection()
    c = conn.cursor()

    if txn_status == "TXN_SUCCESS":
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT balance_after FROM inr_wallet_transactions ORDER BY trade_time DESC LIMIT 1")
        row = c.fetchone()
        balance = float(row['balance_after']) if row and row['balance_after'] is not None else 0.0
        # balance = row['balance_after'] if row else 0
        new_balance = balance + amount
        c.execute("""
            INSERT INTO inr_wallet_transactions
            (trade_time, action, amount, balance_after, trade_mode, payment_id, status)
            VALUES (NOW(),'DEPOSIT',%s,%s,'LIVE',%s,'COMPLETED')
        """, (amount, new_balance, txn_id))
        log_webhook_request(data, "TXN_SUCCESS")
    else:
        c.execute("""
            INSERT INTO inr_wallet_transactions
            (trade_time, action, amount, balance_after, trade_mode, payment_id, status)
            VALUES (NOW(),'DEPOSIT_FAILED',0,0,'LIVE',%s,'FAILED')
        """, (txn_id,))
        log_webhook_request(data, "TXN_FAILED")

    conn.commit()
    conn.close()

    return jsonify({"status": txn_status})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
