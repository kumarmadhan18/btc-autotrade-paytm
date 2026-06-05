# ============================================================
# autotrade_worker.py — Standalone Auto-Trade Worker
# Version: 1.0 | Python 3.11
#
# Runs independently of Streamlit UI.
# No page refresh needed. Bot keeps running 24/7 on Render.
#
# HOW IT WORKS:
#   - Reads autotrade ON/OFF status from DB every POLL_INTERVAL seconds
#   - When active: fetches live BTC/INR price → runs trade logic
#   - Telegram notifications for every BUY / SELL / error
#   - Safe: uses same DB trade lock as Streamlit to prevent double-orders
#
# RENDER SETUP:
#   Add a new Background Worker service on Render:
#   Start cmd: python autotrade_worker.py
#   Same env vars as your main Streamlit service
#
# REQUIRED ENV VARS (same as main bot):
#   APP_ENV, REAL_TRADING
#   COINDCX_API_KEY, COINDCX_API_SECRET
#   PG_HOST, PG_USER, PG_PASSWORD, PG_DB, PG_PORT
#   BOT_TOKEN, CHAT_ID
#   ENABLE_NOTIFICATIONS
# ============================================================

import os
import time
import json
import hmac
import hashlib
import uuid
import traceback
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────
# Config
# ─────────────────────────────────────────
APP_ENV      = os.getenv("APP_ENV", "local")
REAL_TRADING = os.getenv("REAL_TRADING", "false").lower() in ("1", "true", "yes")

API_KEY    = os.getenv("COINDCX_API_KEY", "")
API_SECRET = os.getenv("COINDCX_API_SECRET", "")
BASE_URL   = "https://api.coindcx.com"

BOT_TOKEN            = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID              = os.getenv("TELEGRAM_CHAT_ID", "")
ENABLE_NOTIFICATIONS = os.getenv("ENABLE_NOTIFICATIONS", "true").lower() == "true"

COINDCX_MIN_BTC_QTY = 0.0001

# How often the worker polls price and checks trade conditions (seconds)
POLL_INTERVAL = 30   # every 30s — fast enough, won't hammer the API

# Stop-loss defaults (same as main bot)
DEFAULT_STOP_LOSS_PCT     = 2.0
DEFAULT_TARGET_PCT        = 1.5   # 1.354% breakeven (taker+GST+TDS) + 0.15% net
DEFAULT_TRAILING_STOP_PCT = 1.5
DEFAULT_DAILY_LOSS_LIMIT  = 5.0


# ─────────────────────────────────────────
# Logging helper
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


def poll_telegram_stop_command() -> bool:
    """Returns True if user sent /stop to Telegram bot."""
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"offset": -5, "timeout": 2},
            timeout=5
        )
        for update in r.json().get("result", []):
            txt = update.get("message", {}).get("text", "").strip().lower()
            if txt in ("/stop", "stop"):
                return True
    except Exception:
        pass
    return False


# ─────────────────────────────────────────
# CoinDCX API Helpers
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
    resp = requests.post(f"{BASE_URL}{endpoint}", data=payload, headers=headers, timeout=15)
    resp.raise_for_status()
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


def _fetch_coindcx_balances() -> dict:
    """Returns dict like {"INR": 5000.0, "BTC": 0.00123}"""
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
            for item in resp.json()
            if item.get("currency")
        }
    except Exception as e:
        log(f"❌ CoinDCX balance fetch failed: {e}")
        return {}


def _poll_order_status(order_id: str, max_wait: int = 30) -> dict:
    data = {}
    for _ in range(max_wait):
        time.sleep(1)
        data = _coindcx_signed_request("/exchange/v1/orders/status", {"id": order_id})
        if data.get("status") in ("filled", "cancelled", "rejected"):
            return data
    send_telegram(
        f"⚠️ Order {order_id} still open after {max_wait}s. "
        f"Status: {data.get('status','unknown')}. Check exchange manually."
    )
    return data


# ─────────────────────────────────────────
# Order Placement
# ─────────────────────────────────────────
def place_market_buy(buy_inr: float) -> dict:
    if not REAL_TRADING:
        spot = get_market_price() or 1.0
        btc  = buy_inr / spot
        fee  = btc * 0.001
        return {"status": "filled", "filled_qty": round(btc - fee, 8),
                "avg_price": spot, "fee": round(fee, 8),
                "order_id": f"TEST_{uuid.uuid4().hex[:8]}"}

    spot = get_market_price()
    if not spot:
        raise RuntimeError("Cannot fetch price — aborting BUY.")

    btc_qty = round(buy_inr / spot, 6)
    if btc_qty < COINDCX_MIN_BTC_QTY:
        raise ValueError(
            f"BTC qty {btc_qty:.6f} below minimum {COINDCX_MIN_BTC_QTY}. "
            f"Need at least ₹{COINDCX_MIN_BTC_QTY * spot:.2f}."
        )

    resp     = _coindcx_signed_request("/exchange/v1/orders/create", {
        "side": "buy", "order_type": "market_order",
        "market": "BTCINR", "total_quantity": btc_qty
    })
    order_id = resp.get("id") or resp.get("orders", [{}])[0].get("id", "")
    _log_live_trade(order_id, "BUY", btc_qty, spot, "PENDING")

    final      = _poll_order_status(order_id)
    filled_qty = float(final.get("total_quantity", btc_qty))
    avg_price  = float(final.get("avg_price") or spot)
    fee_btc    = float(final.get("fee_amount", 0))
    _update_live_trade(order_id, final.get("status", "filled"), avg_price, filled_qty, fee_btc)

    return {"status": final.get("status", "filled"),
            "filled_qty": round(filled_qty - fee_btc, 8),
            "avg_price": avg_price, "fee": round(fee_btc, 8), "order_id": order_id}


def place_market_sell(btc_qty: float) -> dict:
    if not REAL_TRADING:
        spot    = get_market_price() or 1.0
        fee_inr = btc_qty * spot * 0.001
        return {"status": "filled", "filled_qty": round(btc_qty, 8),
                "avg_price": spot, "fee": round(fee_inr, 2),
                "order_id": f"TEST_{uuid.uuid4().hex[:8]}"}

    spot = get_market_price()
    if not spot:
        raise RuntimeError("Cannot fetch price — aborting SELL.")

    qty = round(btc_qty, 6)
    if qty < COINDCX_MIN_BTC_QTY:
        raise ValueError(f"BTC qty {qty:.6f} below minimum {COINDCX_MIN_BTC_QTY}.")

    resp     = _coindcx_signed_request("/exchange/v1/orders/create", {
        "side": "sell", "order_type": "market_order",
        "market": "BTCINR", "total_quantity": qty
    })
    order_id = resp.get("id") or resp.get("orders", [{}])[0].get("id", "")
    _log_live_trade(order_id, "SELL", qty, spot, "PENDING")

    final      = _poll_order_status(order_id)
    filled_qty = float(final.get("total_quantity", qty))
    avg_price  = float(final.get("avg_price") or spot)
    fee_inr    = float(final.get("fee_amount", 0))
    _update_live_trade(order_id, final.get("status", "filled"), avg_price, filled_qty, fee_inr)

    return {"status": final.get("status", "filled"),
            "filled_qty": round(filled_qty, 8),
            "avg_price": avg_price, "fee": round(fee_inr, 2), "order_id": order_id}


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
    except Exception as e:
        log(f"❌ acquire_trade_lock: {e}")
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
        return float(row["entry_price"]) if row else 0.0
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


def get_last_sell_price() -> float:
    conn = get_db()
    if not conn:
        return 0.0
    try:
        cur = get_cursor(conn)
        cur.execute("SELECT last_sell_price FROM trade_state WHERE id=1")
        row = cur.fetchone()
        return float(row["last_sell_price"]) if row and row.get("last_sell_price") else 0.0
    finally:
        conn.close()


def save_last_sell_price(price: float):
    conn = get_db()
    if not conn:
        return
    try:
        cur = get_cursor(conn)
        cur.execute("UPDATE trade_state SET last_sell_price=%s WHERE id=1", (price,))
        conn.commit()
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
        r = dict(row)
        raw = r.get("trade_type", "")
        if raw.startswith("AUTO_SELL"):
            r["trade_type"] = "AUTO_SELL"
        elif raw.startswith("AUTO_BUY"):
            r["trade_type"] = "AUTO_BUY"
        return r
    finally:
        conn.close()


def get_current_inr_balance() -> float:
    if REAL_TRADING:
        balances = _fetch_coindcx_balances()
        return float(balances.get("INR", 0.0))
    conn = get_db()
    if not conn:
        return 0.0
    try:
        cur = get_cursor(conn)
        cur.execute("SELECT balance_after FROM inr_wallet_transactions ORDER BY trade_time DESC LIMIT 1")
        row = cur.fetchone()
        return float(row["balance_after"]) if row and row["balance_after"] is not None else 0.0
    finally:
        conn.close()


def get_current_btc_balance() -> float:
    if REAL_TRADING:
        balances = _fetch_coindcx_balances()
        return float(balances.get("BTC", 0.0))
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
        return float(row["balance_after"]) if row and row["balance_after"] is not None else 0.0
    finally:
        conn.close()


def log_wallet_transaction(action, amount, balance, price_inr, trade_type, inr_value_override=None):
    conn = get_db()
    if not conn:
        return
    mode = "LIVE" if REAL_TRADING else "TEST"
    inr_val = inr_value_override if inr_value_override is not None else price_inr
    try:
        cur = get_cursor(conn)
        cur.execute("""
            INSERT INTO wallet_transactions
            (trade_time, action, amount, balance_after, inr_value, trade_type,
             autotrade_active, status, trade_mode)
            VALUES (NOW(), %s, %s, %s, %s, %s, TRUE, 'SUCCESS', %s)
        """, (str(action), float(amount), float(balance), float(inr_val), str(trade_type), mode))
        conn.commit()
    finally:
        conn.close()


def log_inr_transaction(action, amount, balance, mode=None):
    if mode is None:
        mode = "LIVE" if REAL_TRADING else "TEST"
    conn = get_db()
    if not conn:
        return
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


def save_trade_log(trade_type, btc_amount, btc_balance, price_inr, roi=0):
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


def _log_live_trade(order_id, action, amount, price, status):
    conn = get_db()
    if not conn:
        return
    try:
        cur = get_cursor(conn)
        cur.execute("""
            INSERT INTO live_trades
            (trade_time, order_id, action, amount, price, status, reason)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s)
        """, (order_id, action, float(amount), float(price), status, f"AUTO_{action}"))
        conn.commit()
    finally:
        conn.close()


def _update_live_trade(order_id, status, price, amount, fee):
    conn = get_db()
    if not conn:
        return
    try:
        cur = get_cursor(conn)
        cur.execute("""
            UPDATE live_trades SET status=%s, price=%s, amount=%s, fee=%s
            WHERE order_id=%s
        """, (status, float(price), float(amount), float(fee), order_id))
        conn.commit()
    finally:
        conn.close()


def stop_autotrade_in_db(reason: str):
    """Marks autotrade as stopped in DB — Streamlit UI will reflect this on next refresh."""
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
            VALUES (NOW(), 'AUTO_STOP', 0, 0, 0, 'AUTO_TRADE_STOP', FALSE, 'SUCCESS', %s)
        """, (mode,))
        conn.commit()
        log(f"🛑 Auto-Trade stopped in DB: {reason}")
        send_telegram(f"🛑 Auto-Trade STOPPED by worker\nReason: {reason}")
    finally:
        conn.close()


def check_daily_loss_limit(pct: float = DEFAULT_DAILY_LOSS_LIMIT) -> bool:
    conn = get_db()
    if not conn:
        return False
    try:
        cur = get_cursor(conn)
        cur.execute("""
            SELECT start_balance, end_balance FROM wallet_history
            ORDER BY trade_date DESC LIMIT 1
        """)
        row = cur.fetchone()
        if not row:
            return False
        opening = float(row["start_balance"] or 0)
        current = float(row["end_balance"] or opening)
        if opening == 0:
            return False
        loss_pct = ((opening - current) / opening) * 100
        if loss_pct >= pct:
            stop_autotrade_in_db(f"Daily loss limit hit: {loss_pct:.2f}% (limit {pct:.1f}%)")
            return True
        return False
    finally:
        conn.close()


# ─────────────────────────────────────────
# Core Trade Cycle
# ─────────────────────────────────────────
def run_trade_cycle(price_inr: float):
    """
    One full evaluation of trade conditions.
    Same logic as check_auto_trading() in main bot,
    but runs in a clean loop without Streamlit dependencies.
    """
    mode = "LIVE" if REAL_TRADING else "TEST"

    # ── Balances from CoinDCX API (LIVE) or DB (TEST) ──
    inr_balance = get_current_inr_balance()
    btc_balance = get_current_btc_balance()

    log(f"💰 INR: ₹{inr_balance:,.2f} | BTC: {btc_balance:.6f} | Price: ₹{price_inr:,.2f}")

    if btc_balance == 0 and inr_balance == 0:
        stop_autotrade_in_db("Both balances are zero — no funds to trade.")
        return

    min_trade_inr = max(500.0, round(COINDCX_MIN_BTC_QTY * price_inr * 1.065, 2))

    # ── Daily loss guard ──
    if check_daily_loss_limit(DEFAULT_DAILY_LOSS_LIMIT):
        return

    # ── Settings ──
    target_pct    = DEFAULT_TARGET_PCT  # 1.5% min after all Indian charges
    stop_loss_pct = DEFAULT_STOP_LOSS_PCT

    # ── Last auto trade ──
    last_auto      = get_last_auto_trade()
    last_type      = last_auto.get("trade_type", "") if last_auto else ""
    last_inr_value = float(last_auto.get("inr_value", 0) or 0) if last_auto else 0.0

    # ── Trade cooldown: 60s between trades ──
    if last_auto and last_auto.get("ts"):
        if time.time() - float(last_auto["ts"]) < 60:
            return

    # ── Entry price ──
    entry_price = get_entry_price()

    # ╔════════════════════════════════════╗
    # ║  STATE A — Have BTC → SELL logic  ║
    # ╚════════════════════════════════════╝
    if btc_balance >= COINDCX_MIN_BTC_QTY:

        if entry_price == 0 and last_type != "AUTO_SELL":
            save_entry_price(price_inr)
            log(f"📌 Entry price set to ₹{price_inr:,.2f}")
            send_telegram(f"📌 Entry price set to ₹{price_inr:,.2f} — tracking started.")
            return

        if entry_price == 0:
            return  # just sold, wait for next cycle

        sell_trigger    = round(entry_price * (1 + target_pct / 100), 2)
        stop_loss_price = round(entry_price * (1 - stop_loss_pct / 100), 2)
        profit_now      = (price_inr - entry_price) * btc_balance

        log(f"📊 Holding BTC | Entry ₹{entry_price:,.2f} | "
            f"Sell @ ₹{sell_trigger:,.2f} | SL @ ₹{stop_loss_price:,.2f} | "
            f"P&L ₹{profit_now:+.2f}")

        if price_inr < sell_trigger and price_inr > stop_loss_price:
            return  # holding, not yet

        sell_reason = "PROFIT_TARGET" if price_inr >= sell_trigger else "STOP_LOSS"
        log(f"🔔 {sell_reason} triggered! Placing SELL order...")

        order = place_market_sell(btc_balance)

        if order["status"] != "filled":
            send_telegram(f"⚠️ SELL not filled — {order['status']}. Retrying next cycle.")
            return

        sold_btc      = order["filled_qty"]
        avg_price     = order["avg_price"]
        fee_inr       = order["fee"]
        inr_received  = (sold_btc * avg_price) - fee_inr
        actual_profit = inr_received - (entry_price * sold_btc)
        roi_pct       = ((avg_price - entry_price) / entry_price) * 100

        fresh_inr = get_current_inr_balance()
        new_inr   = fresh_inr + inr_received if not REAL_TRADING else fresh_inr

        log_wallet_transaction("AUTO_SELL", sold_btc, 0, avg_price, "AUTO_SELL",
                               inr_value_override=avg_price)
        log_inr_transaction("AUTO_SELL", inr_received, new_inr, mode)
        save_trade_log("AUTO_SELL", sold_btc, 0, avg_price, roi_pct)
        clear_entry_price()
        save_last_sell_price(avg_price)

        icon = "🟢" if sell_reason == "PROFIT_TARGET" else "🛑"
        msg  = (
            f"{icon} AUTO SELL ({sell_reason})\n"
            f"  {sold_btc:.6f} BTC → ₹{inr_received:,.2f}\n"
            f"  Entry ₹{entry_price:,.2f} → Exit ₹{avg_price:,.2f}\n"
            f"  Profit: ₹{actual_profit:+.2f} | Fee: ₹{fee_inr:.2f} | ROI: {roi_pct:+.2f}%\n"
            f"  Next buy when ≤ ₹{round(avg_price * (1 - target_pct/100), 2):,.2f}"
        )
        log(msg)
        send_telegram(msg)
        return

    # ╔═════════════════════════════════════╗
    # ║  STATE B — Have INR → BUY logic    ║
    # ╚═════════════════════════════════════╝
    if btc_balance < COINDCX_MIN_BTC_QTY and inr_balance >= min_trade_inr:

        should_buy = False
        buy_reason = ""

        if not last_type or last_type not in ("AUTO_BUY", "AUTO_SELL"):
            should_buy = True
            buy_reason = "INITIAL_BUY"

        elif last_type == "AUTO_SELL" and last_inr_value > 0:
            buy_trigger = round(last_inr_value * (1 - target_pct / 100), 2)
            log(f"⏳ Waiting for dip — buy when ≤ ₹{buy_trigger:,.2f} (now ₹{price_inr:,.2f})")
            if price_inr <= buy_trigger:
                should_buy = True
                buy_reason = "DIP_BUY"

        elif last_type == "AUTO_BUY":
            should_buy = True
            buy_reason = "REBUY"

        if not should_buy:
            return

        log(f"🔔 {buy_reason} — Placing BUY with ₹{inr_balance:,.2f}...")

        order = place_market_buy(inr_balance)

        if order["status"] != "filled":
            send_telegram(f"⚠️ BUY not filled — {order['status']}. Retrying next cycle.")
            return

        btc_bought = order["filled_qty"]
        avg_price  = order["avg_price"]
        fee_btc    = order["fee"]
        new_btc    = btc_bought
        new_inr    = max(0.0, get_current_inr_balance() - inr_balance) if not REAL_TRADING else 0.0

        save_entry_price(avg_price)
        log_wallet_transaction("AUTO_BUY", btc_bought, new_btc, avg_price, "AUTO_BUY")
        log_inr_transaction("AUTO_BUY", -inr_balance, new_inr, mode)
        save_trade_log("AUTO_BUY", btc_bought, new_btc, avg_price)

        sell_at = round(avg_price * (1 + target_pct / 100), 2)
        msg = (
            f"🟢 AUTO BUY ({buy_reason})\n"
            f"  ₹{inr_balance:,.2f} → {btc_bought:.6f} BTC\n"
            f"  Price: ₹{avg_price:,.2f} | Fee: {fee_btc:.8f} BTC\n"
            f"  Sell when ≥ ₹{sell_at:,.2f} (+{target_pct:.2f}%)\n"
            f"  Order ID: {order['order_id']}"
        )
        log(msg)
        send_telegram(msg)


# ─────────────────────────────────────────
# Main Worker Loop
# ─────────────────────────────────────────
def main():
    log("🚀 autotrade_worker.py started")
    send_telegram("🤖 Auto-Trade Worker started — monitoring market every 30s.")

    consecutive_errors = 0

    while True:
        try:
            # ── Telegram kill switch ──
            if poll_telegram_stop_command():
                stop_autotrade_in_db("Telegram /stop command received.")
                log("🛑 Stopped via Telegram /stop command.")
                break

            # ── Check if autotrade is enabled in DB ──
            if not get_autotrade_active():
                log("⏸ Auto-Trade is OFF — waiting...")
                time.sleep(POLL_INTERVAL)
                consecutive_errors = 0
                continue

            # ── Fetch current price ──
            price = get_market_price("BTCINR")
            if not price:
                log("⚠️ Could not fetch price, skipping cycle.")
                time.sleep(POLL_INTERVAL)
                continue

            # ── Acquire trade lock ──
            if not acquire_trade_lock():
                log("🔒 Trade lock held by another process, skipping cycle.")
                time.sleep(POLL_INTERVAL)
                continue

            try:
                run_trade_cycle(price)
            finally:
                release_trade_lock()

            consecutive_errors = 0

        except Exception as e:
            consecutive_errors += 1
            err_msg = f"❌ Worker error (#{consecutive_errors}): {e}\n{traceback.format_exc()}"
            log(err_msg)

            # After 5 consecutive errors, stop the bot to protect funds
            if consecutive_errors >= 5:
                stop_autotrade_in_db(f"Worker stopped after 5 consecutive errors. Last: {e}")
                log("🛑 Too many errors — worker stopping autotrade to protect funds.")
                send_telegram(
                    f"🚨 Worker stopped Auto-Trade after 5 errors.\n"
                    f"Last error: {e}\nPlease check Render logs."
                )
                consecutive_errors = 0

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
