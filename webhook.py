
from flask import Flask, request, jsonify
from paytmchecksum import PaytmChecksum
import pymysql
import psycopg2
import streamlit as st
import os
from datetime import datetime
from dotenv import load_dotenv
from Crypto.Cipher import AES

IV = "@@@@&&&&####$$$$"

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


def generate_signature(params, merchant_key):
    """
    Generate checksum signature for Paytm request.
    """
    params_string = get_string_by_sorted_keys(params)
    salt = generate_random_string(4)
    final_string = params_string + "|" + salt
    hasher = hashlib.sha256(final_string.encode())
    hash_string = hasher.hexdigest() + salt

    return encrypt(hash_string, merchant_key)

def verify_signature(params, merchant_key, paytm_checksum):
    """
    Verify checksum signature returned by Paytm.
    """
    if "CHECKSUMHASH" in params:
        params.pop("CHECKSUMHASH")

    params_string = get_string_by_sorted_keys(params)
    paytm_hash = decrypt(paytm_checksum, merchant_key)
    salt = paytm_hash[-4:]
    final_string = params_string + "|" + salt
    hasher = hashlib.sha256(final_string.encode()).hexdigest()

    return hasher + salt == paytm_hash

def encrypt(input_string, key):
    key = key.encode("utf-8")
    iv = IV.encode("utf-8")
    input_string = pad(input_string)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(input_string.encode("utf-8"))
    encrypted = base64.b64encode(encrypted).decode("utf-8")
    return encrypted

def decrypt(encrypted, key):
    key = key.encode("utf-8")
    iv = IV.encode("utf-8")
    encrypted = base64.b64decode(encrypted)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(encrypted).decode("utf-8")
    return unpad(decrypted)

def pad(data):
    block_size = 16
    padding = block_size - len(data) % block_size
    return data + chr(padding) * padding

def unpad(data):
    return data[:-ord(data[-1])]

def generate_random_string(length):
    return "".join(random.choice(string.ascii_uppercase + string.digits + string.ascii_lowercase) for _ in range(length))

def get_string_by_sorted_keys(params):
    return "|".join([str(params[key]) for key in sorted(params.keys())])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
