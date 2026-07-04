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

# ── DCA Stage Config ──────────────────────────────────────────────────────────
# BUY stages  → triggered when price drops X% below last sell price
#               each stage spends Y% of total INR balance
BUY_STAGES  = [(3.0, 0.10), (6.0, 0.25), (9.0, 0.50)]  # (dip_pct, inr_fraction)
INR_RESERVE = 0.15   # always keep 15% of INR in reserve

# SELL stages → triggered when price rises X% above avg buy price
#               each stage sells Y% of total BTC held
SELL_STAGES = [(3.0, 0.25), (6.0, 0.35), (9.0, 0.40)]  # (rise_pct, btc_fraction)

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


def place_buy_order(inr_to_spend: float, spot_price: float) -> dict:
    """
    BUY with a specific INR amount (not all-in).
    inr_to_spend is already calculated by the DCA stage logic.
    """
    if inr_to_spend <= 0:
        raise ValueError(f"INR to spend ₹{inr_to_spend:.2f} — nothing to buy with.")

    usable_inr  = inr_to_spend * 0.98   # 2% buffer for price movement
    limit_price = int(spot_price)
    btc_qty     = math.floor((usable_inr / limit_price) / 0.00001) * 0.00001
    btc_qty     = round(btc_qty, 6)

    if btc_qty <= COINDCX_MIN_BTC_QTY:
        raise ValueError(
            f"BTC qty {btc_qty:.6f} at/below minimum {COINDCX_MIN_BTC_QTY}. "
            f"INR ₹{inr_to_spend:.2f} is too low at current price ₹{spot_price:,.2f}."
        )

    notional = btc_qty * spot_price
    if notional < 100:
        raise ValueError(f"Order notional ₹{notional:.2f} below CoinDCX minimum ₹100.")

    log(f"📤 BUY {btc_qty:.6f} BTC @ ₹{spot_price:,.2f} (spending ₹{inr_to_spend:.2f})")
    result = _execute_order("buy", btc_qty, spot_price)
    return result


def place_sell_order(btc_qty_to_sell: float, spot_price: float) -> dict:
    """
    SELL a specific BTC quantity (not all-in).
    btc_qty_to_sell is already calculated by the DCA stage logic.
    """
    if btc_qty_to_sell <= 0:
        raise ValueError(f"BTC qty {btc_qty_to_sell:.8f} — nothing to sell.")

    btc_qty = math.floor(btc_qty_to_sell / 0.000001) * 0.000001
    btc_qty = round(btc_qty, 6)

    if btc_qty <= COINDCX_MIN_BTC_QTY:
        raise ValueError(
            f"BTC qty {btc_qty:.6f} at/below minimum {COINDCX_MIN_BTC_QTY}. "
            f"Cannot sell yet — holding."
        )

    notional = btc_qty * spot_price
    if notional < 100:
        raise ValueError(f"Order notional ₹{notional:.2f} below CoinDCX minimum ₹100.")

    log(f"📤 SELL {btc_qty:.6f} BTC @ ₹{spot_price:,.2f}")
    result = _execute_order("sell", btc_qty, spot_price)
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


# ─────────────────────────────────────────
# DCA State Helpers
# buy_stage  : 0=none, 1=first 10%, 2=+25%, 3=+50%
# sell_stage : 0=none, 1=first 25%, 2=+35%, 3=+40%
# avg_buy_price      : weighted average buy price across all stages
# last_sell_price_btc: per-BTC price of last sell (used as dip reference)
# ─────────────────────────────────────────
def get_dca_state() -> dict:
    conn = get_db()
    if not conn:
        return {"buy_stage": 0, "sell_stage": 0, "avg_buy_price": 0.0, "last_sell_price_btc": 0.0}
    try:
        cur = get_cursor(conn)
        cur.execute("""
            SELECT dca_buy_stage, dca_sell_stage, avg_buy_price, last_sell_price_btc
            FROM trade_state WHERE id=1
        """)
        row = cur.fetchone()
        if not row:
            return {"buy_stage": 0, "sell_stage": 0, "avg_buy_price": 0.0, "last_sell_price_btc": 0.0}
        return {
            "buy_stage":           int(row.get("dca_buy_stage", 0) or 0),
            "sell_stage":          int(row.get("dca_sell_stage", 0) or 0),
            "avg_buy_price":       float(row.get("avg_buy_price", 0) or 0),
            "last_sell_price_btc": float(row.get("last_sell_price_btc", 0) or 0),
        }
    finally:
        conn.close()


def save_dca_state(buy_stage=None, sell_stage=None, avg_buy_price=None, last_sell_price_btc=None):
    conn = get_db()
    if not conn:
        return
    try:
        cur = get_cursor(conn)
        fields, vals = [], []
        if buy_stage is not None:
            fields.append("dca_buy_stage=%s");      vals.append(int(buy_stage))
        if sell_stage is not None:
            fields.append("dca_sell_stage=%s");     vals.append(int(sell_stage))
        if avg_buy_price is not None:
            fields.append("avg_buy_price=%s");      vals.append(float(avg_buy_price))
        if last_sell_price_btc is not None:
            fields.append("last_sell_price_btc=%s"); vals.append(float(last_sell_price_btc))
        if not fields:
            return
        vals.append(1)
        cur.execute(f"UPDATE trade_state SET {', '.join(fields)} WHERE id=1", vals)
        conn.commit()
    except Exception as e:
        log(f"❌ save_dca_state error: {e}")
    finally:
        conn.close()


def reset_dca_state():
    """Full reset after all 3 sell stages complete."""
    save_dca_state(buy_stage=0, sell_stage=0, avg_buy_price=0.0)


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


def _send_heartbeat(btc_balance, inr_balance, avg_buy, price_inr,
                     next_rise_pct, min_trade_inr, last_type, last_inr_value,
                     sell_stage=0, buy_stage=0, sell_trigger=0, last_sell_px=0):
    """
    Sends a Telegram status update when:
      1. Trade state changes (stage change / entry price changes) — immediately
      2. Every HEARTBEAT_INTERVAL seconds (2h) as a "still alive" ping
    """
    global _last_tg_state, _last_tg_time

    cur_state      = f"{'BTC' if btc_balance > 0 else 'INR'}_{buy_stage}_{sell_stage}_{round(avg_buy)}"
    state_changed  = cur_state != _last_tg_state
    heartbeat_due  = (time.time() - _last_tg_time) >= HEARTBEAT_INTERVAL

    if not (state_changed or heartbeat_due):
        return

    _last_tg_state = cur_state
    _last_tg_time  = time.time()
    tag = "🔔 State Update" if state_changed else "⏰ 2h Heartbeat"

    if btc_balance >= COINDCX_MIN_BTC_QTY and avg_buy > 0:
        profit_now = (price_inr - avg_buy) * btc_balance
        send_telegram(
            f"{tag}\n"
            f"🔄 Holding {btc_balance:.6f} BTC | Buy stage {buy_stage}/3\n"
            f"  Avg buy ₹{avg_buy:,.2f} | Now ₹{price_inr:,.2f}\n"
            f"  P&L: ₹{profit_now:+.2f}\n"
            f"  Next sell S{sell_stage+1} @ ₹{sell_trigger:,.2f} (+{next_rise_pct:.1f}%)\n"
            f"  Sell stage: {sell_stage}/3 done | No stop-loss — holding"
        )
    elif inr_balance >= min_trade_inr and last_sell_px > 0:
        next_buy_stage = buy_stage + 1
        if next_buy_stage <= len(BUY_STAGES):
            next_dip, next_pct = BUY_STAGES[next_buy_stage - 1]
            buy_at = round(last_sell_px * (1 - next_dip / 100), 2)
            send_telegram(
                f"{tag}\n"
                f"🔄 Holding ₹{inr_balance:,.2f} INR | Sell stage {sell_stage}/3\n"
                f"  Last sell ₹{last_sell_px:,.2f} | Now ₹{price_inr:,.2f}\n"
                f"  Next DCA B{next_buy_stage} @ ₹{buy_at:,.2f} (-{next_dip}%) → "
                f"{int(next_pct*100)}% INR\n"
                f"  Buy stage: {buy_stage}/3 done"
            )
    else:
        send_telegram(
            f"{tag}\n"
            f"🔄 Auto-Trade active\n"
            f"  BTC: {btc_balance:.6f} | INR: ₹{inr_balance:,.2f}\n"
            f"  Price: ₹{price_inr:,.2f} | B-stage {buy_stage} | S-stage {sell_stage}"
        )



def run_trade_cycle(price_inr: float):
    mode = "LIVE" if REAL_TRADING else "TEST"

    # Single combined balance fetch — distinguishes API failure from real zero
    inr_balance, btc_balance, fetch_ok = get_live_balances()

    if not fetch_ok:
        log("⚠️ Balance fetch failed after retries — skipping this cycle (NOT pausing).")
        return

    log(f"💰 INR ₹{inr_balance:,.2f} | BTC {btc_balance:.6f} | Price ₹{price_inr:,.2f}")

    if btc_balance == 0 and inr_balance == 0:
        set_autotrade_active(False, "Both balances confirmed zero (fetch succeeded).")
        send_telegram("⚠️ Auto-Trade paused — INR and BTC are both zero. Deposit funds and /start to resume.")
        return

    min_trade_inr = max(500.0, round(COINDCX_MIN_BTC_QTY * price_inr * 1.065, 2))

    # ── DCA state ─────────────────────────────────────────────────────────────
    dca          = get_dca_state()
    buy_stage    = dca["buy_stage"]
    sell_stage   = dca["sell_stage"]
    avg_buy      = dca["avg_buy_price"]
    last_sell_px = dca["last_sell_price_btc"]

    # ── Last trade (for cooldown) ──────────────────────────────────────────────
    last_auto      = get_last_auto_trade()
    last_type      = last_auto.get("trade_type", "") if last_auto else ""
    last_inr_value = float(last_auto.get("inr_value", 0) or 0) if last_auto else 0.0

    # 60s cooldown between trades
    if last_auto and last_auto.get("ts"):
        if time.time() - float(last_auto["ts"]) < 60:
            return

    # ── Restore entry_price / avg_buy from DB after restart ───────────────────
    entry_price = get_entry_price()
    if entry_price == 0 and btc_balance >= COINDCX_MIN_BTC_QTY:
        last_buy_price = get_last_auto_buy_price()
        if last_buy_price > 0:
            save_entry_price(last_buy_price)
            entry_price = last_buy_price
            log(f"📌 Entry price restored from last BUY: ₹{entry_price:,.2f}")
    if avg_buy == 0 and entry_price > 0:
        avg_buy = entry_price
        save_dca_state(avg_buy_price=avg_buy)

    # ══════════════════════════════════════════════════════════════════════════
    # STATE A — Holding BTC → STAGED SELL
    #
    #   Stage 1 = initial sell: fires immediately at current price (no rise wait)
    #             sells 25% of BTC right after buy completes.
    #   Stage 2: price rises 3% above Stage 1 sell price → sell 35% of BTC
    #   Stage 3: price rises 6% above Stage 1 sell price → sell 40% of BTC
    #   NO stop-loss — hold remaining BTC until each stage target is hit.
    # ══════════════════════════════════════════════════════════════════════════
    if btc_balance >= COINDCX_MIN_BTC_QTY:

        if avg_buy == 0:
            avg_buy = price_inr
            save_dca_state(avg_buy_price=avg_buy)
            save_entry_price(avg_buy)
            log(f"📌 avg_buy seeded to current price ₹{avg_buy:,.2f}")
            return

        next_sell = sell_stage + 1
        if next_sell > len(SELL_STAGES):
            log("⚠️ All sell stages complete but still holding BTC — waiting for DCA reset.")
            return

        next_rise_pct, next_btc_pct = SELL_STAGES[next_sell - 1]

        # Stage 1: sell immediately at current price — no rise wait
        # Stages 2 & 3: wait for price to rise from Stage 1 sell price
        if next_sell == 1:
            sell_trigger = price_inr   # fire immediately
            sell_label   = "INITIAL"
        else:
            # last_sell_px is set to Stage 1's avg_price after Stage 1 fills
            if last_sell_px == 0:
                last_sell_px = avg_buy   # fallback: use avg buy as reference
            sell_trigger = round(last_sell_px * (1 + next_rise_pct / 100), 2)
            sell_label   = f"+{next_rise_pct}%"

        profit_now = (price_inr - avg_buy) * btc_balance
        log(f"📊 Avg buy ₹{avg_buy:,.2f} | S{next_sell} [{sell_label}] target "
            f"₹{sell_trigger:,.2f} | P&L ₹{profit_now:+.2f} | Now ₹{price_inr:,.2f} "
            f"| Sell stage {sell_stage}/3")

        _send_heartbeat(btc_balance, inr_balance, avg_buy, price_inr,
                        next_rise_pct, min_trade_inr, last_type, last_inr_value,
                        sell_stage, buy_stage, sell_trigger, last_sell_px)

        if next_sell > 1 and price_inr < sell_trigger:
            return  # not at target yet — hold

        # ── Execute sell stage ────────────────────────────────────────────────
        sell_qty = round(btc_balance * next_btc_pct, 6)
        if sell_qty < COINDCX_MIN_BTC_QTY:
            sell_qty = min(btc_balance, COINDCX_MIN_BTC_QTY * 2)

        log(f"🔔 SELL Stage {next_sell}/3 [{sell_label}] — "
            f"selling {sell_qty:.6f} BTC ({int(next_btc_pct*100)}% of holdings)...")

        try:
            order = place_sell_order(sell_qty, price_inr)
        except ValueError as e:
            log(f"⚠️ SELL S{next_sell} skipped: {e}")
            send_telegram(f"⚠️ SELL S{next_sell} skipped: {e}")
            return

        if order["status"] != "filled":
            send_telegram(f"⚠️ SELL S{next_sell} not filled — {order['status']}. Retrying next cycle.")
            return

        avg_price    = order["avg_price"]
        sold_btc     = order["filled_qty"]
        fee          = order["fee"]
        inr_received = (sold_btc * avg_price) - fee
        roi_pct      = ((avg_price - avg_buy) / avg_buy) * 100
        actual_profit= inr_received - (avg_buy * sold_btc)
        new_btc      = max(0.0, btc_balance - sold_btc)

        _new_inr, _, _ok = get_live_balances()
        new_inr = (_new_inr if _ok else (inr_balance + inr_received))

        log_wallet_transaction("AUTO_SELL", sold_btc, new_btc, avg_price,
                               f"AUTO_SELL_S{next_sell}")
        log_inr_transaction("AUTO_SELL", inr_received, new_inr)
        save_trade_log(f"AUTO_SELL_S{next_sell}", sold_btc, avg_price, roi_pct)
        _worker_status["trades"] += 1

        new_sell_stage = next_sell
        # After Stage 1, save its price as the rise reference for Stages 2 & 3
        new_last_sell_px = avg_price if next_sell == 1 else last_sell_px
        save_dca_state(sell_stage=new_sell_stage, last_sell_price_btc=new_last_sell_px)

        if new_sell_stage >= len(SELL_STAGES):
            clear_entry_price()
            reset_dca_state()
            save_dca_state(last_sell_price_btc=avg_price)   # keep for next buy dip ref
            completion = "✅ All 3 sell stages complete — watching for next buy dip"
        else:
            clear_entry_price()
            remaining_pct = sum(p for _, p in SELL_STAGES[new_sell_stage:])
            next_s        = SELL_STAGES[new_sell_stage]
            next_trigger  = round(avg_price * (1 + next_s[0] / 100), 2)
            completion    = (f"Next S{new_sell_stage+1} @ ₹{next_trigger:,.2f} "
                             f"(+{next_s[0]}% from S1) | {int(remaining_pct*100)}% BTC remaining")

        send_telegram(
            f"🟢 *AUTO SELL — Stage {next_sell}/3 [{sell_label}]*\n"
            f"  {sold_btc:.6f} BTC → ₹{inr_received:,.2f}\n"
            f"  Avg buy ₹{avg_buy:,.2f} → Exit ₹{avg_price:,.2f}\n"
            f"  Profit: ₹{actual_profit:+.2f} | ROI: {roi_pct:+.2f}%\n"
            f"  {completion}"
        )
        return

    # ══════════════════════════════════════════════════════════════════════════
    # STATE B — Holding INR → STAGED DCA BUY
    #
    #   INITIAL BUY (no prior trade):
    #     Stage 1: buy 10% of INR immediately at current price (no dip wait)
    #     Keep 15% reserve for further DCA if price drops.
    #
    #   DCA BUY (after a sell):
    #     Stage 1: price drops 3% from last sell → spend 10% of total INR
    #     Stage 2: price drops 6% from last sell → spend 25% of total INR
    #     Stage 3: price drops 9% from last sell → spend 50% of total INR
    #     Reserve: always keep 15% in INR
    #
    #   avg_buy_price is recalculated as weighted average after each DCA buy.
    # ══════════════════════════════════════════════════════════════════════════
    if btc_balance < COINDCX_MIN_BTC_QTY and inr_balance >= min_trade_inr:

        # ── Determine next buy stage ──────────────────────────────────────────
        # buy_stage=0 means no buy has happened yet → next_buy=1 (Stage 1)
        # Stage 1 fires immediately at current price (no dip wait) — this IS
        # the initial buy. Stages 2 and 3 wait for dips from the Stage 1 price.
        next_buy = buy_stage + 1

        if next_buy > len(BUY_STAGES):
            log(f"⏳ All {len(BUY_STAGES)} DCA buy stages done. Waiting for price recovery.")
            _send_heartbeat(btc_balance, inr_balance, avg_buy, price_inr,
                            0, min_trade_inr, last_type, last_inr_value,
                            sell_stage, buy_stage, 0, last_sell_px)
            return

        next_dip_pct, next_inr_pct = BUY_STAGES[next_buy - 1]

        # ── Stage 1 (initial buy): fire immediately at current market price ──
        # ── Stages 2 & 3: wait for price to dip from Stage 1 buy price ───────
        if next_buy == 1:
            # No dip required — buy right now
            buy_trigger = price_inr
            label = "INITIAL"
        else:
            # Use Stage 1 buy price as the dip reference (stored in last_sell_px
            # after Stage 1, or fall back to last_inr_value from wallet_transactions)
            if last_sell_px == 0:
                last_sell_px = last_inr_value
                if last_sell_px > 0:
                    save_dca_state(last_sell_price_btc=last_sell_px)
            if last_sell_px == 0:
                log("⚠️ No Stage 1 reference price yet — waiting.")
                return
            buy_trigger = round(last_sell_px * (1 - next_dip_pct / 100), 2)
            label = f"DCA -{next_dip_pct}%"

        _send_heartbeat(btc_balance, inr_balance, avg_buy, price_inr,
                        0, min_trade_inr, last_type, last_inr_value,
                        sell_stage, buy_stage, buy_trigger, last_sell_px)

        if next_buy > 1 and price_inr > buy_trigger:
            log(f"⏳ DCA B{next_buy} trigger ₹{buy_trigger:,.2f} (-{next_dip_pct}% from "
                f"₹{last_sell_px:,.2f}) | Now ₹{price_inr:,.2f} | Buy stage {buy_stage}/3")
            return  # price not low enough yet

        # ── Calculate INR to spend ────────────────────────────────────────────
        deployable = inr_balance * (1 - INR_RESERVE)
        buy_inr    = round(deployable * next_inr_pct, 2)

        if buy_inr < min_trade_inr:
            buy_inr = min_trade_inr
        max_allowed = max(0.0, inr_balance - (inr_balance * INR_RESERVE))
        buy_inr     = min(buy_inr, round(max_allowed, 2))

        if buy_inr < min_trade_inr:
            log(f"⚠️ B{next_buy} skipped — deployable ₹{buy_inr:.2f} < minimum ₹{min_trade_inr:.2f}")
            send_telegram(
                f"⚠️ DCA B{next_buy} skipped — only ₹{buy_inr:.2f} deployable "
                f"(need ₹{min_trade_inr:.2f}). Reserve: ₹{inr_balance * INR_RESERVE:.2f}"
            )
            return

        log(f"🔔 BUY Stage {next_buy}/3 [{label}] — spending ₹{buy_inr:.2f} "
            f"({int(next_inr_pct*100)}% of deployable)...")

        try:
            order = place_buy_order(buy_inr, price_inr)
        except ValueError as e:
            log(f"⚠️ BUY B{next_buy} skipped: {e}")
            send_telegram(f"⚠️ BUY B{next_buy} skipped: {e}")
            return

        if order["status"] != "filled":
            send_telegram(f"⚠️ BUY B{next_buy} not filled — {order['status']}. Retrying.")
            return

        btc_bought = order["filled_qty"]
        avg_price  = order["avg_price"]
        new_inr    = max(0.0, inr_balance - buy_inr)

        # Recalculate blended average buy price across all stages
        prev_btc = btc_balance  # BTC already held from earlier stages
        new_btc  = prev_btc + btc_bought
        if new_btc > 0:
            prev_val    = prev_btc * (avg_buy if avg_buy > 0 else avg_price)
            new_avg_buy = (prev_val + (btc_bought * avg_price)) / new_btc
        else:
            new_avg_buy = avg_price
        new_avg_buy = round(new_avg_buy, 2)

        save_entry_price(avg_price)
        # After Stage 1, store buy price as the dip reference for Stages 2 & 3
        new_last_sell_px = avg_price if next_buy == 1 else last_sell_px
        save_dca_state(
            buy_stage=next_buy,
            sell_stage=0,               # reset sell stage — new avg resets targets
            avg_buy_price=new_avg_buy,
            last_sell_price_btc=new_last_sell_px
        )
        log_wallet_transaction("AUTO_BUY", btc_bought, new_btc, avg_price,
                               f"AUTO_BUY_S{next_buy}")
        log_inr_transaction("AUTO_BUY", -buy_inr, new_inr)
        save_trade_log(f"AUTO_BUY_S{next_buy}", btc_bought, avg_price)
        _worker_status["trades"] += 1

        # Build next stage hint
        next_hint = ""
        if next_buy < len(BUY_STAGES):
            nb_dip, nb_pct = BUY_STAGES[next_buy]
            nb_trigger = round(avg_price * (1 - nb_dip / 100), 2)
            next_hint = (f"\n  DCA B{next_buy+1} @ ₹{nb_trigger:,.2f} "
                         f"(-{nb_dip}% from Stage 1) → {int(nb_pct*100)}% INR")

        send_telegram(
            f"🟢 *AUTO BUY — Stage {next_buy}/3 [{label}]*\n"
            f"  ₹{buy_inr:,.2f} → {btc_bought:.6f} BTC @ ₹{avg_price:,.2f}\n"
            f"  Total BTC: {new_btc:.6f} | Avg buy: ₹{new_avg_buy:,.2f}\n"
            f"  Reserve: ₹{new_inr:,.2f} | Sell S1 @ ₹{round(new_avg_buy*1.03,2):,.2f} (+3%)"
            f"{next_hint}"
        )


# ─────────────────────────────────────────
# Main Worker Loop
# ─────────────────────────────────────────
def main():
    log("🚀 autotrade_worker.py v2.0 started")
    send_telegram(
        "🤖 *Auto-Trade Worker v2.1 started — DCA Strategy*\n"
        "Buy stages:  B1 -3% (10%) | B2 -6% (25%) | B3 -9% (50%) | Reserve 15%\n"
        "Sell stages: S1 +3% (25%) | S2 +6% (35%) | S3 +9% (40%)\n"
        "Commands:\n"
        "  /stop → pause trading\n"
        "  /start → resume trading\n"
        "  /status → check status + DCA stages"
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
                dca           = get_dca_state()
                status_icon   = "▶️ ACTIVE" if active else "⏸ PAUSED"
                entry_line    = f"₹{entry:,.2f}" if entry else "Not set"
                avg_line      = f"₹{dca['avg_buy_price']:,.2f}" if dca['avg_buy_price'] else "Not set"
                balance_note  = "" if bal_ok else "\n  ⚠️ Balance fetch failed — showing 0"
                send_telegram(
                    f"📊 *Worker Status*\n"
                    f"  Trading: {status_icon}\n"
                    f"  BTC Price: ₹{price:,.2f}\n"
                    f"  INR Balance: ₹{inr_bal:,.2f}\n"
                    f"  BTC Balance: {btc_bal:.6f}\n"
                    f"  Entry Price: {entry_line}\n"
                    f"  Avg Buy Price: {avg_line}\n"
                    f"  Buy stage: {dca['buy_stage']}/3 | Sell stage: {dca['sell_stage']}/3\n"
                    f"  Last sell ref: ₹{dca['last_sell_price_btc']:,.2f}\n"
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
