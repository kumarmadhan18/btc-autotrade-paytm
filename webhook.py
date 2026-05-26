import os
import hmac
import hashlib
import json
import logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

# ── Logging setup ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

APP_ENV      = os.getenv("APP_ENV", "live")
REAL_TRADING = os.getenv("REAL_TRADING", "false").lower() in ("1", "true", "yes")

app = Flask(__name__)


# ─────────────────────────────────────────
# DB CONNECTION
# FIX #1: psycopg2 cursor_factory passed to connect() correctly
# FIX #2: psycopg2 row returns tuple not dict — use RealDictCursor
# ─────────────────────────────────────────
def get_db_connection():
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
                cursor_factory=psycopg2.extras.RealDictCursor  # FIX #1+2
            )
            return conn
        else:
            import pymysql
            return pymysql.connect(
                host=os.getenv("MYSQL_HOST"),
                user=os.getenv("MYSQL_USER"),
                password=os.getenv("MYSQL_PASSWORD"),
                database=os.getenv("MYSQL_DB"),
                cursorclass=pymysql.cursors.DictCursor
            )
    except Exception as e:
        log.error(f"❌ DB connection failed: {e}")
        return None


# ─────────────────────────────────────────
# Razorpay Signature Verification
# FIX #3: hmac.new() → hmac.new() is correct BUT was missing
#         the raw bytes check — payload must stay as bytes
# ─────────────────────────────────────────
def verify_razorpay_webhook(signature: str, payload: bytes) -> bool:
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret:
        log.warning("⚠️ RAZORPAY_WEBHOOK_SECRET not set — rejecting all webhooks")
        return False
    try:
        computed = hmac.new(
            secret.encode("utf-8"),
            payload,           # must be raw bytes — do NOT decode before passing
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(computed, signature)
    except Exception as e:
        log.error(f"❌ Signature verification error: {e}")
        return False


# ─────────────────────────────────────────
# Webhook Logger
# FIX #4: CREATE TABLE IF NOT EXISTS runs on every request — moved to
#         startup. Also added try/finally so conn always closes.
# ─────────────────────────────────────────
def ensure_webhook_log_table():
    """Called once at startup to create the table if needed."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        if APP_ENV == "live":
            cur.execute("""
                CREATE TABLE IF NOT EXISTS webhook_logs (
                    id SERIAL PRIMARY KEY,
                    received_at TIMESTAMP DEFAULT NOW(),
                    payload TEXT,
                    status VARCHAR(50)
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS webhook_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    payload TEXT,
                    status VARCHAR(50)
                )
            """)
        conn.commit()
    except Exception as e:
        log.error(f"❌ ensure_webhook_log_table error: {e}")
    finally:
        conn.close()


def log_webhook(payload, status: str):
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO webhook_logs (received_at, payload, status) VALUES (NOW(), %s, %s)",
            (str(payload)[:5000], status)   # truncate huge payloads
        )
        conn.commit()
    except Exception as e:
        log.error(f"❌ log_webhook error: {e}")
    finally:
        conn.close()


# ─────────────────────────────────────────
# Razorpay Webhook Endpoint
# FIX #5: URL changed from /webhook/razorpay → /razorpay/webhook
#         to match what Razorpay dashboard expects and what
#         btc_autotrade_render.py references
# FIX #6: conn = get_db_connection() was not guarded for None —
#         if DB is down, code crashes with AttributeError
# FIX #7: row["balance_after"] fails on psycopg2 plain cursor
#         (returns tuple) — fixed by RealDictCursor in get_db_connection()
# ─────────────────────────────────────────
@app.route("/razorpay/webhook", methods=["POST"])
def razorpay_webhook():
    raw       = request.get_data()           # raw bytes — must stay bytes for HMAC
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not verify_razorpay_webhook(signature, raw):
        log.warning(f"⚠️ Invalid Razorpay signature from {request.remote_addr}")
        log_webhook(raw[:500], "INVALID_SIGNATURE")
        return jsonify({"error": "Invalid signature"}), 403

    try:
        data = json.loads(raw)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    event = data.get("event", "")
    log.info(f"📩 Razorpay webhook received: {event}")

    if event != "payment.captured":
        log_webhook(data, f"IGNORED_{event}")
        return jsonify({"message": f"Event {event} ignored"}), 200

    # ── Extract payment details ───────────────────────────────
    try:
        payment    = data["payload"]["payment"]["entity"]
        payment_id = payment["id"]
        order_id   = payment.get("order_id", "")
        amount     = float(payment["amount"]) / 100    # paise → INR
    except (KeyError, TypeError, ValueError) as e:
        log.error(f"❌ Malformed payment payload: {e}")
        log_webhook(data, "MALFORMED_PAYLOAD")
        return jsonify({"error": "Malformed payload"}), 400

    # ── DB operations ─────────────────────────────────────────
    conn = get_db_connection()
    if not conn:
        log.error("❌ DB unavailable — webhook cannot process payment")
        return jsonify({"error": "DB unavailable"}), 503   # 503 → Razorpay will retry

    try:
        cur = conn.cursor()

        # ── Idempotency check ─────────────────────────────────
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM inr_wallet_transactions WHERE payment_id = %s",
            (payment_id,)
        )
        row = cur.fetchone()
        cnt = row["cnt"] if isinstance(row, dict) else row[0]
        if cnt > 0:
            log.info(f"ℹ️ Payment {payment_id} already processed — skipping")
            return jsonify({"message": "Already processed"}), 200

        # ── Get current INR balance ───────────────────────────
        cur.execute("""
            SELECT balance_after
            FROM inr_wallet_transactions
            WHERE status IN ('SUCCESS', 'COMPLETED')
            ORDER BY trade_time DESC
            LIMIT 1
        """)
        row          = cur.fetchone()
        last_balance = float(row["balance_after"]) if row else 0.0
        new_balance  = last_balance + amount

        # ── Credit wallet ─────────────────────────────────────
        cur.execute("""
            INSERT INTO inr_wallet_transactions
            (trade_time, action, amount, balance_after, trade_mode, payment_id, status)
            VALUES (NOW(), 'DEPOSIT', %s, %s, %s, %s, 'COMPLETED')
        """, (
            amount,
            new_balance,
            "LIVE" if REAL_TRADING else "TEST",
            payment_id
        ))
        conn.commit()

        log.info(
            f"✅ Wallet credited | payment={payment_id} | "
            f"order={order_id} | amount=₹{amount:.2f} | "
            f"new_balance=₹{new_balance:.2f}"
        )

        # ── Send Telegram notification ────────────────────────
        _notify_deposit(amount, new_balance, payment_id)

        log_webhook(data, "SUCCESS")
        return jsonify({
            "message": "Wallet credited",
            "amount":  amount,
            "balance": new_balance
        }), 200

    except Exception as e:
        log.error(f"❌ Webhook processing error: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        log_webhook(data, f"ERROR_{str(e)[:40]}")
        return jsonify({"error": "Internal error"}), 500

    finally:
        conn.close()


# ─────────────────────────────────────────
# Telegram deposit notification
# ─────────────────────────────────────────
def _notify_deposit(amount: float, new_balance: float, payment_id: str):
    """Send Telegram message when deposit is credited."""
    bot_token = os.getenv("BOT_TOKEN", "")
    chat_id   = os.getenv("CHAT_ID", "")
    if not bot_token or not chat_id:
        return
    try:
        import requests as req
        msg = (
            f"💰 Deposit Received!\n"
            f"  Amount: ₹{amount:,.2f}\n"
            f"  New Balance: ₹{new_balance:,.2f}\n"
            f"  Payment ID: {payment_id}"
        )
        req.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            params={"chat_id": chat_id, "text": msg},
            timeout=5
        )
    except Exception as e:
        log.warning(f"⚠️ Telegram notify failed: {e}")


# ─────────────────────────────────────────
# Health check endpoint
# ─────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    """Uptime Robot / Render health check endpoint."""
    return jsonify({
        "status":  "ok",
        "env":     APP_ENV,
        "trading": "LIVE" if REAL_TRADING else "TEST"
    }), 200


# ─────────────────────────────────────────
# Startup
# ─────────────────────────────────────────
with app.app_context():
    ensure_webhook_log_table()
    log.info(f"✅ Webhook server starting | APP_ENV={APP_ENV} | REAL_TRADING={REAL_TRADING}")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
