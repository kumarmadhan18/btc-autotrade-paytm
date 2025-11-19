# webhook.py
# Minimal Flask webhook app for UroPay notifications (PostgreSQL)
import os
import hmac
import hashlib
import traceback
from datetime import datetime

from flask import Flask, request, jsonify
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras

load_dotenv()

app = Flask(__name__)

# Config
UROPAY_WEBHOOK_SECRET = os.getenv("UROPAY_WEBHOOK_SECRET", "")
REAL_TRADING = os.getenv("REAL_TRADING", "False").lower() in ("1", "true", "yes")

def get_mysql_connection():
    try:
        return psycopg2.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            dbname=os.getenv("DB_NAME"),
            port=int(os.getenv("DB_PORT", 5432))
        )
    except Exception as e:
        print("Postgres connection failed:", e)
        return None

def log_webhook_request(payload, status):
    """
    Save webhook payload in webhook_logs (create table if not exists).
    """
    try:
        conn = get_mysql_connection()
        if not conn:
            print("No DB connection to log webhook.")
            return
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS webhook_logs (
                id SERIAL PRIMARY KEY,
                received_at TIMESTAMP,
                payload TEXT,
                status VARCHAR(100)
            )
        """)
        cur.execute("INSERT INTO webhook_logs (received_at, payload, status) VALUES (NOW(), %s, %s)", (str(payload), status))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("Failed to log webhook:", e)

def verify_uropay_webhook(signature_header: str, payload_bytes: bytes) -> bool:
    """
    HMAC-SHA256 verification using UROPAY_WEBHOOK_SECRET.
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

@app.route("/webhook/uropay", methods=["POST"])
def uropay_webhook():
    try:
        raw = request.get_data()
        sig_header = request.headers.get("X-Uropay-Signature", "")
        ok = verify_uropay_webhook(sig_header, raw)
        if not ok:
            # Allow insecure test bypass if set and not REAL_TRADING
            if os.getenv("UROPAY_ALLOW_INSECURE_TEST", "0") == "1" and not REAL_TRADING:
                pass
            else:
                log_webhook_request(request.get_data(as_text=True), "INVALID_SIGNATURE")
                return jsonify({"error": "Invalid signature"}), 403

        payload = request.get_json(force=True)
        order_id = payload.get("order_id") or payload.get("reference") or payload.get("merchant_order_id")
        amount = float(payload.get("amount", 0))
        status = (payload.get("status") or "").upper()
        payment_id = payload.get("payment_id") or payload.get("txn_id") or payload.get("upi_reference") or order_id

        if status in ("SUCCESS", "SUCCESSFUL", "PAID", "COMPLETED"):
            # Credit wallet (insert row into inr_wallet_transactions)
            conn = get_mysql_connection()
            if not conn:
                return jsonify({"error": "DB connection failed"}), 500
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            # fetch last balance for LIVE mode account
            cur.execute("SELECT balance_after FROM inr_wallet_transactions WHERE trade_mode = %s ORDER BY trade_time DESC LIMIT 1", ("LIVE",))
            row = cur.fetchone()
            last_balance = float(row['balance_after']) if row and row['balance_after'] is not None else 0.0
            new_balance = last_balance + amount

            cur.execute("""
                INSERT INTO inr_wallet_transactions
                (trade_time, action, amount, balance_after, trade_mode, payment_id, status)
                VALUES (NOW(), %s, %s, %s, %s, %s, %s)
            """, ("DEPOSIT", amount, new_balance, "LIVE", payment_id, "COMPLETED"))
            conn.commit()
            cur.close()
            conn.close()

            log_webhook_request(payload, "TXN_SUCCESS")
            return jsonify({"ok": True}), 200
        else:
            # log failed deposit
            conn = get_mysql_connection()
            if conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO inr_wallet_transactions
                    (trade_time, action, amount, balance_after, trade_mode, payment_id, status)
                    VALUES (NOW(), %s, %s, %s, %s, %s, %s)
                """, ("DEPOSIT_FAILED", 0, 0, "LIVE", payment_id, "FAILED"))
                conn.commit()
                cur.close()
                conn.close()
            log_webhook_request(payload, "TXN_FAILED")
            return jsonify({"ok": True, "status": status}), 200

    except Exception as e:
        traceback.print_exc()
        log_webhook_request(str(e), "EXCEPTION")
        return jsonify({"error": str(e)}), 500

@app.route("/simulate_deposit", methods=["GET", "POST"])
def simulate_deposit():
    """
    Simple simulation endpoint for local dev (TEST mode).
    GET params: order_id, amount
    This mirrors the behavior above and inserts a DEPOSIT row with trade_mode 'TEST'.
    """
    if REAL_TRADING:
        return jsonify({"error": "Simulation disabled in LIVE mode"}), 403
    try:
        order_id = request.args.get("order_id") or request.form.get("order_id") or f"SIM_{uuid.uuid4().hex[:10]}"
        amount = float(request.args.get("amount") or request.form.get("amount") or 0)
        payment_id = f"SIM_{uuid.uuid4().hex[:10]}"

        conn = get_mysql_connection()
        if not conn:
            return jsonify({"error": "DB connection failed"}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT balance_after FROM inr_wallet_transactions WHERE trade_mode = %s ORDER BY trade_time DESC LIMIT 1", ("TEST",))
        row = cur.fetchone()
        last_balance = float(row['balance_after']) if row and row['balance_after'] is not None else 0.0
        new_balance = last_balance + amount

        cur.execute("""
            INSERT INTO inr_wallet_transactions
            (trade_time, action, amount, balance_after, trade_mode, payment_id, status)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s)
        """, ("DEPOSIT", amount, new_balance, "TEST", payment_id, "COMPLETED"))
        conn.commit()
        cur.close()
        conn.close()
        log_webhook_request({"order_id": order_id, "amount": amount}, "SIM_SUCCESS")
        return jsonify({"ok": True, "order_id": order_id, "amount": amount, "payment_id": payment_id}), 200
    except Exception as e:
        traceback.print_exc()
        log_webhook_request(str(e), "SIM_ERROR")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    # Run on port 5001 (or any port you prefer); use HTTPS in production
    app.run(host="0.0.0.0", port=int(os.getenv("WEBHOOK_PORT", 5001)), debug=True)
