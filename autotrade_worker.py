# ============================================================
# autotrade_worker.py — Standalone Auto-Trade Worker
# Version: 2.0 | Python 3.11
#
# Runs 24/7 independently of Streamlit UI on Render.
# No browser tab needed. Worker NEVER exits — only pauses.
#
# TELEGRAM COMMANDS:
#   /stop  → pauses trading (worker stays alive)
#   /start → resumes trading
#   /status → shows current status
#
# RENDER SETUP:
#   New Background Worker service:
#   Start cmd: python autotrade_worker.py
#   Same env vars as Streamlit service
#
# REQUIRED ENV VARS:
#   APP_ENV, REAL_TRADING
#   COINDCX_API_KEY, COINDCX_API_SECRET
#   MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB, MYSQL_PORT
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
#   ENABLE_NOTIFICATIONS
# ============================================================

import os
import math
import time
import json
import hmac
import hashlib
import uuid
import traceback
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────
# Render Port Binding + UptimeRobot Health Check
# ─────────────────────────────────────────
_worker_status = {"running": False, "last_cycle": "Never", "trades": 0}

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = "running" if _worker_status["running"] else "starting"
        body = (
            f"OK\n"
            f"status: {status}\n"
            f"last_cycle: {_worker_status['last_cycle']}\n"
            f"trades: {_worker_status['trades']}\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    def log_message(self, *args):
        pass  # suppress request logs


def _start_health_server():
    port = int(os.getenv("PORT", 10000))
    max_retries = 5
    for attempt in range(max_retries):
        try:
            server = HTTPServer(("0.0.0.0", port), _HealthHandler)
            # daemon=True so thread dies with main process (not the other way)
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            print(f"[health] ✅ HTTP server listening on port {port}", flush=True)
            return
        except OSError as e:
            print(f"[health] Attempt {attempt+1}/{max_retries} failed: {e}", flush=True)
            time.sleep(2)
    print("[health] ❌ Could not bind port — continuing without health server", flush=True)


# ─────────────────────────────────────────
# Config
# ─────────────────────────────────────────
APP_ENV      = os.getenv("APP_ENV", "local")
REAL_TRADING = os.getenv("REAL_TRADING", "false").lower() in ("1", "true", "yes")

API_KEY    = os.getenv("COINDCX_API_KEY", "")
API_SECRET = os.getenv("COINDCX_API_SECRET", "")
BASE_URL   = "https://api.coindcx.com"    # ticker, balances
SPOT_URL   = "https://apigw.coindcx.com"  # spot order placement (new URL)

BOT_TOKEN            = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID              = os.getenv("TELEGRAM_CHAT_ID", "")
ENABLE_NOTIFICATIONS = os.getenv("ENABLE_NOTIFICATIONS", "true").lower() == "true"

COINDCX_MIN_BTC_QTY   = 0.00001   # actual min from CoinDCX markets_details
POLL_INTERVAL         = 30         # seconds between each trade check
DEFAULT_TARGET_PCT    = 1.5        # 1.354% breakeven + 0.15% net profit
DEFAULT_STOP_LOSS_PCT = 2.0
DEFAULT_DAILY_LOSS_LIMIT = 5.0

# Telegram update offset — tracks last processed message
_tg_offset = 0

# Heartbeat tracking — sends status update on state change or every 2 hours
_last_tg_state = ""
_last_tg_time  = 0.0
HEARTBEAT_INTERVAL = 7200  # 2 hours


# ─────────────────────────────────────────
# Logging
# ─────────────────────────────────────────
def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ─────────────────────────────────────────
# DB Connection
# ─────────────────────────────────────────
def get_db():
    try:
        if APP_ENV == "live":
            import psycopg2
            import psycopg2.extras
            return psycopg2.connect(
                host=os.getenv("PG_HOST"),
                user=os.getenv("PG_USER"),
                password=os.getenv("PG_PASSWORD"),
                dbname=os.getenv("PG_DB"),
                port=int(os.getenv("PG_PORT", 5432)),
                cursor_factory=psycopg2.extras.RealDictCursor
            )
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
        log(f"❌ DB connection failed: {e}")
        return None


def get_cursor(conn):
    if APP_ENV == "live":
        import psycopg2.extras
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn.cursor()


def epoch_sql(col):
    return f"EXTRACT(EPOCH FROM {col})" if APP_ENV == "live" else f"UNIX_TIMESTAMP({col})"


# ─────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────
def send_telegram(msg: str):
    if not ENABLE_NOTIFICATIONS or not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=8
        )
    except Exception as e:
        log(f"⚠️ Telegram send failed: {e}")


def poll_telegram_commands() -> str | None:
    """
    Polls Telegram for commands.
    Returns: "stop", "start", "status", or None.
    Worker NEVER exits on /stop — only pauses trading.
    """
    global _tg_offset
    if not BOT_TOKEN or not CHAT_ID:
        return None
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"offset": _tg_offset, "timeout": 2, "limit": 10},
            timeout=5
        )
        updates = r.json().get("result", [])
        command = None
        for update in updates:
            _tg_offset = update["update_id"] + 1
            txt = update.get("message", {}).get("text", "").strip().lower()
            if txt in ("/stop", "stop"):
                command = "stop"
            elif txt in ("/start", "start"):
                command = "start"
            elif txt in ("/status", "status"):
                command = "status"
        return command
    except Exception:
        return None


# ─────────────────────────────────────────
# CoinDCX API
# ─────────────────────────────────────────
def _coindcx_signed_request(endpoint: str, body: dict) -> dict:
    if not API_KEY or not API_SECRET:
        raise ValueError("COINDCX_API_KEY and COINDCX_API_SECRET must be set.")
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
    resp = requests.post(f"{SPOT_URL}{endpoint}", data=payload, headers=headers, timeout=15)
    if not resp.ok:
        try:
            err = resp.json()
        except Exception:
            err = resp.text
        raise requests.exceptions.HTTPError(
            f"{resp.status_code} {resp.reason} | CoinDCX: {err} | body: {payload}",
            response=resp
        )
    return resp.json()


def get_market_price(symbol="BTCINR") -> float | None:
    try:
        r = requests.get(f"{BASE_URL}/exchange/ticker", timeout=10)
        for t in r.json():
            if t.get("market") == symbol:
                return float(t.get("last_price"))
    except Exception as e:
        log(f"⚠️ Price fetch failed: {e}")
    return None


def _fetch_coindcx_balances(retries: int = 3) -> dict | None:
    """
    Returns dict of balances on success, or None if ALL retries failed.
    None != {} — None means "API call failed, don't trust this",
    {} or balances with 0 means "API succeeded, balance really is 0".
    """
    last_err = None
    for attempt in range(retries):
        try:
            timestamp_ms = str(int(time.time() * 1000))
            body = json.dumps({"timestamp": timestamp_ms}, separators=(",", ":"))
            signature = hmac.new(
                API_SECRET.encode("utf-8"),
                body.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
            headers = {
                "Content-Type":     "application/json",
                "X-AUTH-APIKEY":    API_KEY,
                "X-AUTH-SIGNATURE": signature,
            }
            resp = requests.post(
                f"{BASE_URL}/exchange/v1/users/balances",
                data=body, headers=headers, timeout=10
            )
            resp.raise_for_status()
            return {
                item.get("currency", "").upper(): float(item.get("balance", 0.0))
                for item in resp.json() if item.get("currency")
            }
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2)  # brief pause before retry

    log(f"❌ Balance fetch failed after {retries} attempts: {last_err}")
    return None


def _cancel_order(order_id: str):
    try:
        _coindcx_signed_request("/exchange/v1/orders/cancel", {"id": str(order_id)})
        log(f"🚫 Order {order_id} cancelled.")
    except Exception as e:
        log(f"⚠️ Cancel failed for {order_id}: {e}")


def _poll_order_status(order_id: str, max_wait: int = 120) -> dict:
    """
    Poll until terminal state. Limit orders at market price fill in seconds.
    partially_filled → cancel remainder, return filled portion as success.
    still open after max_wait → cancel entirely.
    """
    data   = {}
    status = ""
    for i in range(max_wait):
        time.sleep(1)
        try:
            data   = _coindcx_signed_request("/exchange/v1/orders/status", {"id": str(order_id)})
            status = data.get("status", "")
            if status in ("filled", "cancelled", "rejected", "closed"):
                return data
            if i > 0 and i % 30 == 0:
                log(f"⏳ Order {order_id} still {status} after {i}s...")
        except Exception as e:
            log(f"⚠️ Status poll error {order_id}: {e}")

    # Timeout handling
    if status == "partially_filled":
        filled_qty = float(data.get("total_quantity", 0)) - float(data.get("remaining_quantity", 0))
        log(f"⚠️ Order {order_id} partially filled ({filled_qty:.6f} BTC) — cancelling remainder.")
        send_telegram(f"⚠️ Order {order_id} partially filled ({filled_qty:.6f} BTC). Cancelling remainder.")
        _cancel_order(order_id)
        data["_partial_filled_qty"] = filled_qty
        data["status"]              = "partially_filled"
        return data
    else:
        log(f"⚠️ Order {order_id} still {status} after {max_wait}s — cancelling.")
        send_telegram(f"⚠️ Order {order_id} still {status} after {max_wait}s — cancelling to protect funds.")
        _cancel_order(order_id)
        data["status"] = "cancelled"
        return data


# ─────────────────────────────────────────
# Order Placement
# ─────────────────────────────────────────
def _execute_order(side: str, qty: float, spot_price: float) -> dict:
    """Core order execution — shared by BUY and SELL."""
    if not REAL_TRADING:
        fee = qty * spot_price * 0.001
        return {
            "status":     "filled",
            "filled_qty": round(qty - (fee / spot_price if side == "buy" else 0), 8),
            "avg_price":  spot_price,
            "fee":        round(fee, 8),
            "order_id":   f"TEST_{uuid.uuid4().hex[:8]}"
        }

    limit_price = int(spot_price)
    resp        = _coindcx_signed_request(
        "/exchange/v1/orders/create",
        {
            "side":           side,
            "order_type":     "limit_order",
            "market":         "BTCINR",
            "total_quantity": round(qty, 6),
            "price_per_unit": limit_price,
        }
    )
    orders_list = resp if isinstance(resp, list) else resp.get("orders", [resp])
    order_id    = str(orders_list[0].get("id", "")) if orders_list else ""

    final  = _poll_order_status(order_id)
    status = final.get("status", "filled")

    if "_partial_filled_qty" in final:
        filled_qty = float(final["_partial_filled_qty"])
    else:
        total_qty     = float(final.get("total_quantity", qty))
        remaining_qty = float(final.get("remaining_quantity", 0))
        filled_qty    = total_qty - remaining_qty if remaining_qty > 0 else total_qty

    avg_price = float(final.get("avg_price") or spot_price)
    fee       = float(final.get("fee_amount", 0))

    # Treat partial fills as success
    effective_status = "filled" if status in ("filled", "partially_filled") and filled_qty > 0 else status

    return {
        "status":     effective_status,
        "filled_qty": round(filled_qty, 8),
        "avg_price":  avg_price,
        "fee":        round(fee, 8),
        "order_id":   order_id
    }


def place_buy_order(inr_balance: float, spot_price: float) -> dict:
    """
    BUY: uses the live INR balance already fetched this cycle by run_trade_cycle
    (passed in as inr_balance) — does NOT re-fetch, avoiding a second API call
    that could transiently fail and produce a misleading "no funds" error.
    """
    if inr_balance <= 0:
        raise ValueError(f"INR balance ₹{inr_balance:.2f} — nothing to buy with.")

    usable_inr  = inr_balance * 0.98
    limit_price = int(spot_price)
    btc_qty     = math.floor((usable_inr / limit_price) / 0.00001) * 0.00001
    btc_qty     = round(btc_qty, 6)

    if btc_qty <= COINDCX_MIN_BTC_QTY:
        raise ValueError(
            f"BTC qty {btc_qty:.6f} at/below minimum {COINDCX_MIN_BTC_QTY}. "
            f"INR balance ₹{inr_balance:.2f} is too low."
        )

    notional = btc_qty * spot_price
    if notional < 100:
        raise ValueError(f"Order notional ₹{notional:.2f} below CoinDCX minimum ₹100.")

    log(f"📤 BUY {btc_qty:.6f} BTC @ ₹{spot_price:,.2f} (usable ₹{usable_inr:.2f})")
    result = _execute_order("buy", btc_qty, spot_price)

    if result["status"] == "filled":
        save_entry_price(result["avg_price"])
    return result


def place_sell_order(btc_balance: float, spot_price: float) -> dict:
    """
    SELL: uses the live BTC balance already fetched this cycle by run_trade_cycle
    (passed in as btc_balance) — does NOT re-fetch, avoiding a second API call
    that could transiently fail and produce a misleading "no funds" error.
    """
    if btc_balance <= 0:
        raise ValueError(f"BTC balance {btc_balance:.8f} — nothing to sell.")

    btc_qty = math.floor(btc_balance / 0.000001) * 0.000001
    btc_qty = round(btc_qty, 6)

    # Use <= (not <) — CoinDCX rejects orders exactly AT the minimum boundary.
    # Raising ValueError here (not HTTPError) lets run_trade_cycle skip cleanly
    # instead of counting it as a worker error.
    if btc_qty <= COINDCX_MIN_BTC_QTY:
        raise ValueError(
            f"BTC balance {btc_balance:.6f} at/below minimum {COINDCX_MIN_BTC_QTY}. "
            f"Cannot sell yet — holding."
        )

    notional = btc_qty * spot_price
    if notional < 100:
        raise ValueError(f"Order notional ₹{notional:.2f} below CoinDCX minimum ₹100.")

    log(f"📤 SELL {btc_qty:.6f} BTC @ ₹{spot_price:,.2f} (live balance)")
    result = _execute_order("sell", btc_qty, spot_price)

    if result["status"] == "filled":
        clear_entry_price()
    return result


# ─────────────────────────────────────────
# DB Helpers
# ─────────────────────────────────────────
def get_autotrade_active() -> bool:
    conn = get_db()
    if not conn:
        return False
    try:
        cur = get_cursor(conn)
        cur.execute("""
            SELECT trade_type FROM wallet_transactions
            WHERE trade_type IN ('AUTO_TRADE_START','AUTO_TRADE_STOP')
            ORDER BY trade_time DESC LIMIT 1
        """)
        row = cur.fetchone()
        return bool(row and row.get("trade_type") == "AUTO_TRADE_START")
    finally:
        conn.close()


def set_autotrade_active(active: bool, reason: str = ""):
    conn = get_db()
    if not conn:
        return
    mode       = "LIVE" if REAL_TRADING else "TEST"
    trade_type = "AUTO_TRADE_START" if active else "AUTO_TRADE_STOP"
    try:
        cur = get_cursor(conn)
        cur.execute("""
            INSERT INTO wallet_transactions
            (trade_time, action, amount, balance_after, inr_value,
             trade_type, autotrade_active, status, trade_mode)
            VALUES (NOW(), %s, 0, 0, 0, %s, %s, 'SUCCESS', %s)
        """, (trade_type, trade_type, active, mode))
        conn.commit()
        log(f"{'▶️' if active else '⏸'} Auto-Trade {'started' if active else 'paused'} in DB. {reason}")
    finally:
        conn.close()


def acquire_trade_lock() -> bool:
    conn = get_db()
    if not conn:
        return False
    try:
        cur = get_cursor(conn)
        cur.execute("""
            UPDATE trade_execution_lock
            SET is_locked = TRUE WHERE id = 1 AND is_locked = FALSE
        """)
        conn.commit()
        return cur.rowcount == 1
    except Exception:
        return False
    finally:
        conn.close()


def release_trade_lock():
    conn = get_db()
    if not conn:
        return
    try:
        cur = get_cursor(conn)
        cur.execute("UPDATE trade_execution_lock SET is_locked=FALSE WHERE id=1")
        conn.commit()
    finally:
        conn.close()


def get_entry_price() -> float:
    conn = get_db()
    if not conn:
        return 0.0
    try:
        cur = get_cursor(conn)
        cur.execute("SELECT entry_price FROM trade_state WHERE id=1")
        row = cur.fetchone()
        return float(row["entry_price"]) if row and row.get("entry_price") else 0.0
    finally:
        conn.close()


def save_entry_price(price: float):
    conn = get_db()
    if not conn:
        return
    try:
        cur = get_cursor(conn)
        cur.execute("UPDATE trade_state SET entry_price=%s WHERE id=1", (price,))
        conn.commit()
    finally:
        conn.close()


def clear_entry_price():
    conn = get_db()
    if not conn:
        return
    try:
        cur = get_cursor(conn)
        cur.execute("UPDATE trade_state SET entry_price=0 WHERE id=1")
        conn.commit()
    finally:
        conn.close()


def get_last_auto_buy_price() -> float:
    """
    Recover entry_price from last successful BUY after restart.
    Checks both 'BUY' (written by Streamlit bot's _log_live_trade)
    and 'AUTO_BUY' (written by this worker's save_trade_log) —
    the shared live_trades table can contain rows from either process.
    """
    conn = get_db()
    if not conn:
        return 0.0
    try:
        cur = get_cursor(conn)
        cur.execute("""
            SELECT price FROM live_trades
            WHERE action IN ('BUY', 'AUTO_BUY') AND status IN ('filled','partially_filled')
            ORDER BY trade_time DESC LIMIT 1
        """)
        row = cur.fetchone()
        return float(row["price"]) if row and row.get("price") else 0.0
    finally:
        conn.close()


def get_last_auto_trade() -> dict | None:
    conn = get_db()
    if not conn:
        return None
    try:
        cur = get_cursor(conn)
        cur.execute(f"""
            SELECT trade_type, inr_value, {epoch_sql('trade_time')} AS ts
            FROM wallet_transactions
            WHERE trade_type IN ('AUTO_BUY','AUTO_SELL')
               OR trade_type LIKE 'AUTO_BUY_%'
               OR trade_type LIKE 'AUTO_SELL_%'
            ORDER BY trade_time DESC LIMIT 1
        """)
        row = cur.fetchone()
        if not row:
            return None
        r   = dict(row)
        raw = r.get("trade_type", "")
        r["trade_type"] = "AUTO_SELL" if raw.startswith("AUTO_SELL") else "AUTO_BUY"
        return r
    finally:
        conn.close()


def get_current_inr_balance() -> float:
    if REAL_TRADING:
        balances = _fetch_coindcx_balances()
        return float((balances or {}).get("INR", 0.0))
    conn = get_db()
    if not conn:
        return 0.0
    try:
        cur = get_cursor(conn)
        cur.execute("SELECT balance_after FROM inr_wallet_transactions ORDER BY trade_time DESC LIMIT 1")
        row = cur.fetchone()
        return float(row["balance_after"]) if row and row["balance_after"] else 0.0
    finally:
        conn.close()


def get_current_btc_balance() -> float:
    if REAL_TRADING:
        balances = _fetch_coindcx_balances()
        return float((balances or {}).get("BTC", 0.0))
    conn = get_db()
    if not conn:
        return 0.0
    try:
        cur = get_cursor(conn)
        cur.execute("""
            SELECT balance_after FROM wallet_transactions
            WHERE trade_type IN ('AUTO_BUY','AUTO_SELL')
            ORDER BY trade_time DESC LIMIT 1
        """)
        row = cur.fetchone()
        return float(row["balance_after"]) if row and row["balance_after"] else 0.0
    finally:
        conn.close()


def get_live_balances() -> tuple[float, float, bool]:
    """
    Returns (inr_balance, btc_balance, fetch_succeeded).
    Single combined API call — used by run_trade_cycle to avoid
    making two separate calls (and two chances for transient failure).
    fetch_succeeded=False means the API call failed — caller should
    SKIP the cycle, never treat as "balance is zero".
    """
    if not REAL_TRADING:
        return get_current_inr_balance(), get_current_btc_balance(), True

    balances = _fetch_coindcx_balances()
    if balances is None:
        return 0.0, 0.0, False
    return float(balances.get("INR", 0.0)), float(balances.get("BTC", 0.0)), True


def log_wallet_transaction(action, amount, balance, price_inr, trade_type):
    conn = get_db()
    if not conn:
        return
    mode = "LIVE" if REAL_TRADING else "TEST"
    try:
        cur = get_cursor(conn)
        cur.execute("""
            INSERT INTO wallet_transactions
            (trade_time, action, amount, balance_after, inr_value,
             trade_type, autotrade_active, status, trade_mode)
            VALUES (NOW(), %s, %s, %s, %s, %s, TRUE, 'SUCCESS', %s)
        """, (str(action), float(amount), float(balance), float(price_inr), str(trade_type), mode))
        conn.commit()
    finally:
        conn.close()


def log_inr_transaction(action, amount, balance):
    conn = get_db()
    if not conn:
        return
    mode = "LIVE" if REAL_TRADING else "TEST"
    try:
        cur = get_cursor(conn)
        cur.execute("""
            INSERT INTO inr_wallet_transactions
            (trade_time, action, amount, balance_after, trade_mode, status)
            VALUES (NOW(), %s, %s, %s, %s, 'SUCCESS')
        """, (str(action), float(amount), float(balance), mode))
        conn.commit()
    finally:
        conn.close()


def save_trade_log(trade_type, btc_amount, price_inr, roi=0):
    conn = get_db()
    if not conn:
        return
    try:
        cur = get_cursor(conn)
        cur.execute("""
            INSERT INTO live_trades
            (trade_time, order_id, action, amount, price, status, profit)
            VALUES (NOW(), %s, %s, %s, %s, 'filled', %s)
        """, (f"AUTO_{uuid.uuid4().hex[:8]}", trade_type,
              float(btc_amount), float(price_inr), float(roi)))
        conn.commit()
    finally:
        conn.close()


def _send_heartbeat(btc_balance, inr_balance, entry_price, price_inr,
                     target_pct, min_trade_inr, last_type, last_inr_value):
    """
    Sends a Telegram status update when:
      1. Trade state changes (BTC↔INR / entry price changes) — immediately
      2. Every HEARTBEAT_INTERVAL seconds (2h) as a "still alive" ping
    Mirrors the Streamlit heartbeat behaviour for the worker process.
    """
    global _last_tg_state, _last_tg_time

    cur_state      = f"{'BTC' if btc_balance > 0 else 'INR'}_{entry_price}"
    state_changed  = cur_state != _last_tg_state
    heartbeat_due  = (time.time() - _last_tg_time) >= HEARTBEAT_INTERVAL

    if not (state_changed or heartbeat_due):
        return

    _last_tg_state = cur_state
    _last_tg_time  = time.time()
    tag = "🔔 State Update" if state_changed else "⏰ 2h Heartbeat"

    if btc_balance >= COINDCX_MIN_BTC_QTY and entry_price > 0:
        profit_now = (price_inr - entry_price) * btc_balance
        sell_at    = round(entry_price * (1 + target_pct / 100), 2)
        send_telegram(
            f"{tag}\n"
            f"🔄 Holding {btc_balance:.6f} BTC\n"
            f"  Bought @ ₹{entry_price:,.2f} | Now ₹{price_inr:,.2f}\n"
            f"  P&L: ₹{profit_now:+.2f} | Sell at ₹{sell_at:,.2f} (+{target_pct:.2f}%)\n"
            f"  No stop-loss — holding until profit target"
        )
    elif inr_balance >= min_trade_inr and last_type == "AUTO_SELL" and last_inr_value > 0:
        buy_at = round(last_inr_value * (1 - target_pct / 100), 2)
        send_telegram(
            f"{tag}\n"
            f"🔄 Holding ₹{inr_balance:,.2f} INR\n"
            f"  Last sell @ ₹{last_inr_value:,.2f} | Now ₹{price_inr:,.2f}\n"
            f"  Buy when price ≤ ₹{buy_at:,.2f} (-{target_pct:.2f}%)"
        )
    else:
        send_telegram(
            f"{tag}\n"
            f"🔄 Auto-Trade active\n"
            f"  BTC: {btc_balance:.6f} | INR: ₹{inr_balance:,.2f}\n"
            f"  Price: ₹{price_inr:,.2f} | Last: {last_type or 'none'}"
        )



def run_trade_cycle(price_inr: float):
    mode = "LIVE" if REAL_TRADING else "TEST"

    # Single combined balance fetch — distinguishes API failure from real zero
    inr_balance, btc_balance, fetch_ok = get_live_balances()

    if not fetch_ok:
        log("⚠️ Balance fetch failed after retries — skipping this cycle (NOT pausing).")
        return  # transient API issue — just skip, try again next cycle

    log(f"💰 INR ₹{inr_balance:,.2f} | BTC {btc_balance:.6f} | Price ₹{price_inr:,.2f}")

    if btc_balance == 0 and inr_balance == 0:
        set_autotrade_active(False, "Both balances confirmed zero (fetch succeeded).")
        send_telegram("⚠️ Auto-Trade paused — INR and BTC balances are both ₹0 / 0 BTC (confirmed via API). Deposit funds and send /start to resume.")
        return

    min_trade_inr = max(500.0, round(COINDCX_MIN_BTC_QTY * price_inr * 1.065, 2))
    target_pct    = DEFAULT_TARGET_PCT  # sell only when profit target is reached

    last_auto      = get_last_auto_trade()
    last_type      = last_auto.get("trade_type", "") if last_auto else ""
    last_inr_value = float(last_auto.get("inr_value", 0) or 0) if last_auto else 0.0

    # 60s cooldown between trades
    if last_auto and last_auto.get("ts"):
        if time.time() - float(last_auto["ts"]) < 60:
            return

    # ── Restore entry_price from DB — never resets on start/stop ──
    entry_price = get_entry_price()
    if entry_price == 0 and btc_balance >= COINDCX_MIN_BTC_QTY:
        last_buy_price = get_last_auto_buy_price()
        if last_buy_price > 0:
            save_entry_price(last_buy_price)
            entry_price = last_buy_price
            log(f"📌 Entry price restored from last BUY: ₹{entry_price:,.2f}")

    # ══════════════════════════════════════
    # STATE A — Holding BTC → SELL logic
    # ══════════════════════════════════════
    if btc_balance >= COINDCX_MIN_BTC_QTY:

        if entry_price == 0:
            save_entry_price(price_inr)
            entry_price = price_inr
            log(f"📌 Entry price set to current ₹{price_inr:,.2f}")
            send_telegram(f"📌 Entry price set to ₹{price_inr:,.2f} — watching for sell target.")
            return

        sell_trigger = round(entry_price * (1 + target_pct / 100), 2)
        profit_now   = (price_inr - entry_price) * btc_balance

        log(f"📊 Entry ₹{entry_price:,.2f} | Sell target ₹{sell_trigger:,.2f} | P&L ₹{profit_now:+.2f} | Now ₹{price_inr:,.2f}")

        # NO stop loss — only sell when profit target is reached
        # Bot will HOLD BTC indefinitely until target is hit (may take days)
        if price_inr < sell_trigger:
            _send_heartbeat(btc_balance, inr_balance, entry_price, price_inr,
                            target_pct, min_trade_inr, last_type, last_inr_value)
            return  # waiting for profit target — holding BTC

        log(f"🔔 PROFIT TARGET reached — placing SELL of full live BTC balance...")

        try:
            order = place_sell_order(btc_balance, price_inr)
        except ValueError as e:
            log(f"⚠️ SELL skipped: {e}")
            send_telegram(f"⚠️ SELL skipped: {e}")
            return

        if order["status"] != "filled":
            send_telegram(f"⚠️ SELL not filled — {order['status']}. Will retry next cycle.")
            return

        avg_price    = order["avg_price"]
        sold_btc     = order["filled_qty"]
        fee          = order["fee"]
        inr_received = (sold_btc * avg_price) - fee
        profit       = inr_received - (entry_price * sold_btc)
        roi_pct      = ((avg_price - entry_price) / entry_price) * 100

        # Fetch updated balance for logging — fall back to estimate if API hiccups
        _new_inr, _new_btc, _new_ok = get_live_balances()
        new_inr = _new_inr if _new_ok else (inr_balance + inr_received)

        log_wallet_transaction("AUTO_SELL", sold_btc, 0, avg_price, "AUTO_SELL")
        log_inr_transaction("AUTO_SELL", inr_received, new_inr)
        save_trade_log("AUTO_SELL", sold_btc, avg_price, roi_pct)
        _worker_status["trades"] += 1

        send_telegram(
            f"🟢 *AUTO SELL — PROFIT TARGET*\n"
            f"  {sold_btc:.6f} BTC → ₹{inr_received:,.2f}\n"
            f"  Entry ₹{entry_price:,.2f} → Exit ₹{avg_price:,.2f}\n"
            f"  Profit: ₹{profit:+.2f} | ROI: {roi_pct:+.2f}%\n"
            f"  Next buy when ≤ ₹{round(avg_price*(1-target_pct/100),2):,.2f}"
        )
        return

    # ══════════════════════════════════════
    # STATE B — Holding INR → BUY logic
    # ══════════════════════════════════════
    if btc_balance < COINDCX_MIN_BTC_QTY and inr_balance >= min_trade_inr:

        should_buy = False
        buy_reason = ""

        if not last_type or last_type not in ("AUTO_BUY", "AUTO_SELL"):
            should_buy = True
            buy_reason = "INITIAL_BUY"
        elif last_type == "AUTO_SELL" and last_inr_value > 0:
            buy_trigger = round(last_inr_value * (1 - target_pct / 100), 2)
            log(f"⏳ Waiting for dip ≤ ₹{buy_trigger:,.2f} (now ₹{price_inr:,.2f})")
            if price_inr <= buy_trigger:
                should_buy = True
                buy_reason = "DIP_BUY"
        elif last_type == "AUTO_BUY":
            should_buy = True
            buy_reason = "REBUY"

        if not should_buy:
            _send_heartbeat(btc_balance, inr_balance, entry_price, price_inr,
                            target_pct, min_trade_inr, last_type, last_inr_value)
            return

        log(f"🔔 {buy_reason} — placing BUY with ₹{inr_balance:,.2f}...")

        try:
            order = place_buy_order(inr_balance, price_inr)
        except ValueError as e:
            log(f"⚠️ BUY skipped: {e}")
            send_telegram(f"⚠️ BUY skipped: {e}")
            return

        if order["status"] != "filled":
            send_telegram(f"⚠️ BUY not filled — {order['status']}. Will retry next cycle.")
            return

        avg_price = order["avg_price"]
        btc_got   = order["filled_qty"]
        fee       = order["fee"]
        cost      = (avg_price * btc_got) + fee  # fee is INR-denominated, charged on top

        # Fetch updated balance for logging — fall back to estimate if API hiccups
        _new_inr, _new_btc, _new_ok = get_live_balances()
        new_inr = _new_inr if _new_ok else max(0.0, inr_balance - cost)

        log_wallet_transaction("AUTO_BUY", btc_got, btc_got, avg_price, "AUTO_BUY")
        log_inr_transaction("AUTO_BUY", -inr_balance, new_inr)
        save_trade_log("AUTO_BUY", btc_got, avg_price)
        _worker_status["trades"] += 1

        send_telegram(
            f"🟢 *AUTO BUY — {buy_reason}*\n"
            f"  ₹{inr_balance:,.2f} → {btc_got:.6f} BTC\n"
            f"  Price: ₹{avg_price:,.2f} | Fee: {fee:.8f} BTC\n"
            f"  Sell target: ₹{round(avg_price*(1+target_pct/100),2):,.2f} (+{target_pct}%)\n"
            f"  Order ID: {order['order_id']}"
        )


# ─────────────────────────────────────────
# Main Worker Loop
# ─────────────────────────────────────────
def main():
    log("🚀 autotrade_worker.py v2.0 started")
    send_telegram(
        "🤖 *Auto-Trade Worker v2.0 started*\n"
        "Commands:\n"
        "  /stop → pause trading\n"
        "  /start → resume trading\n"
        "  /status → check status"
    )

    consecutive_errors = 0

    while True:   # ← NEVER exits — only pauses on /stop
        try:
            # ── Telegram commands ──────────────────
            cmd = poll_telegram_commands()

            if cmd == "stop":
                set_autotrade_active(False, "Telegram /stop")
                send_telegram("⏸ Auto-Trade *PAUSED* via Telegram.\nSend /start to resume.")
                log("⏸ Paused via Telegram /stop")

            elif cmd == "start":
                set_autotrade_active(True, "Telegram /start")
                send_telegram("▶️ Auto-Trade *RESUMED* via Telegram.")
                log("▶️ Resumed via Telegram /start")

            elif cmd == "status":
                active        = get_autotrade_active()
                price         = get_market_price("BTCINR") or 0
                inr_bal, btc_bal, bal_ok = get_live_balances()
                entry         = get_entry_price()
                status_icon   = "▶️ ACTIVE" if active else "⏸ PAUSED"
                entry_line    = f"₹{entry:,.2f}" if entry else "Not set"
                balance_note  = "" if bal_ok else "\n  ⚠️ Balance fetch failed — showing 0"
                send_telegram(
                    f"📊 *Worker Status*\n"
                    f"  Trading: {status_icon}\n"
                    f"  BTC Price: ₹{price:,.2f}\n"
                    f"  INR Balance: ₹{inr_bal:,.2f}\n"
                    f"  BTC Balance: {btc_bal:.6f}\n"
                    f"  Entry Price: {entry_line}\n"
                    f"  Trades this run: {_worker_status['trades']}"
                    f"{balance_note}"
                )

            # ── Check if trading is active ──────────
            if not get_autotrade_active():
                log("⏸ Auto-Trade is OFF — waiting...")
                _worker_status["last_cycle"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                time.sleep(POLL_INTERVAL)
                consecutive_errors = 0
                continue

            # ── Fetch price ─────────────────────────
            price = get_market_price("BTCINR")
            if not price:
                log("⚠️ Could not fetch price, skipping.")
                time.sleep(POLL_INTERVAL)
                continue

            # ── Trade lock ──────────────────────────
            if not acquire_trade_lock():
                log("🔒 Trade lock held, skipping.")
                time.sleep(POLL_INTERVAL)
                continue

            try:
                run_trade_cycle(price)
                _worker_status["last_cycle"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            finally:
                release_trade_lock()

            consecutive_errors = 0

        except Exception as e:
            consecutive_errors += 1
            log(f"❌ Worker error #{consecutive_errors}: {e}\n{traceback.format_exc()}")

            if consecutive_errors >= 5:
                set_autotrade_active(False, f"5 consecutive errors: {e}")
                send_telegram(
                    f"🚨 *Auto-Trade paused after 5 errors*\n"
                    f"Last error: `{e}`\n"
                    f"Check Render logs. Send /start to resume."
                )
                consecutive_errors = 0

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    # Start health server FIRST — Render needs port binding immediately
    _start_health_server()

    # Small delay to ensure port is bound before Render scans
    time.sleep(2)

    # Update status so UptimeRobot health endpoint shows correct state
    _worker_status["running"]    = True
    _worker_status["last_cycle"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    log("🚀 autotrade_worker.py v2.0 ready")

    # Keep restarting main() if it ever crashes
    # Health server stays alive regardless via daemon thread
    restart_count = 0
    while True:
        try:
            _worker_status["running"] = True
            main()
        except Exception as e:
            restart_count += 1
            _worker_status["running"] = False
            log(f"💥 main() crashed (restart #{restart_count}): {e}\n{traceback.format_exc()}")
            send_telegram(
                f"🔄 Worker restarting (#{restart_count})\n"
                f"Error: `{e}`\n"
                f"Resuming in 10s..."
            )
            log("🔄 Restarting main() in 10s...")
            time.sleep(10)
