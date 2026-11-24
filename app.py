import streamlit as st
import sqlite3
import pandas as pd
import math
from datetime import datetime, timedelta
import os
import requests
import altair as alt
from utils import (
    render_backup_sync_ui,
    render_category_analytics,
    render_export_and_reports,
    render_member_ledger,
    get_known_members,
)
from utils.fees import FeeStore, render_fees_page

# Database configuration
DB_PATH = 'ekonomi.db'

# Supabase configuration (optional fallback to SQLite)
try:
    SUPABASE_URL = st.secrets.get("SUPABASE_URL")
    SUPABASE_KEY = st.secrets.get("SUPABASE_KEY")
except Exception:
    SUPABASE_URL = None
    SUPABASE_KEY = None

if not SUPABASE_URL:
    SUPABASE_URL = os.environ.get("SUPABASE_URL")
if not SUPABASE_KEY:
    SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_KEY)

def is_using_supabase():
    # If user selected offline fallback or env vars are missing
    if st.session_state.get("force_offline_mode", False):
        return False
    return USE_SUPABASE

def invalidate_cache():
    st.cache_data.clear()

def test_supabase_connection():
    if not (SUPABASE_URL and SUPABASE_KEY):
        return False, 0, "Supabase bağlantı bilgileri eksik."
    import time
    t0 = time.time()
    try:
        c = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        c._get("ory_interest_rate_logs", params={"select": "id", "limit": 1})
        latency = round((time.time() - t0) * 1000)
        return True, latency, f"Çevrimiçi ({latency} ms)"
    except requests.exceptions.Timeout:
        return False, 0, "Zaman aşımı (Timeout: 6s). Sunucu uykuda olabilir."
    except requests.exceptions.ConnectionError:
        return False, 0, "Bağlantı kurulamadı (DNS/Sunucu kapalı). Supabase projeniz duraklatılmış (Paused) olabilir."
    except Exception as e:
        return False, 0, f"Bağlantı Hatası: {str(e)}"

@st.cache_resource
def _get_http_session():
    return requests.Session()

class SupabaseClient:
    def __init__(self, url, key):
        self.url = url.rstrip('/')
        self.key = key
        self.session = _get_http_session()
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
        
    def _get(self, table, params=None):
        r = self.session.get(f"{self.url}/rest/v1/{table}", headers=self.headers, params=params, timeout=6)
        r.raise_for_status()
        return r.json()
        
    def _insert(self, table, data):
        r = self.session.post(f"{self.url}/rest/v1/{table}", headers=self.headers, json=data, timeout=6)
        r.raise_for_status()
        return r.json()
        
    def _update(self, table, data, id_val):
        r = self.session.patch(f"{self.url}/rest/v1/{table}?id=eq.{id_val}", headers=self.headers, json=data, timeout=6)
        r.raise_for_status()
        return r.json()
        
    def _delete(self, table, id_val):
        r = self.session.delete(f"{self.url}/rest/v1/{table}?id=eq.{id_val}", headers=self.headers, timeout=6)
        r.raise_for_status()
        return r.json()

# --- DATABASE OPERATIONS ---

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    _init_key = f"db_init_done_{is_using_supabase()}"
    if st.session_state.get(_init_key):
        return
    if is_using_supabase():
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        try:
            logs = client._get("ory_interest_rate_logs", params={"select": "id"})
            if not logs:
                client._insert("ory_interest_rate_logs", {"date": "2026-01-01", "rate": 0.41})
        except Exception:
            # Let the user run the SQL schema in their Supabase console first
            pass
    else:
        conn = get_connection()
        cursor = conn.cursor()
        
        # 1. Categories Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            emoji TEXT NOT NULL,
            color TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('Gelir', 'Gider'))
        )
        """)
        
        # 2. Transactions Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            category_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            description TEXT,
            time_range TEXT NOT NULL DEFAULT '05:00 - 18:15',
            debt_id INTEGER,
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE,
            FOREIGN KEY (debt_id) REFERENCES debts(id) ON DELETE CASCADE
        )
        """)
        
        # 3. Debts Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK(type IN ('Alınacak', 'Verilecek')),
            amount REAL NOT NULL,
            name TEXT NOT NULL,
            due_date TEXT,
            status TEXT NOT NULL CHECK(status IN ('Bekliyor', 'Ödendi')),
            category_id INTEGER,
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL
        )
        """)
        
        # Migration safety for existing databases
        cursor.execute("PRAGMA table_info(transactions)")
        cols_tx = [row['name'] for row in cursor.fetchall()]
        if 'time_range' not in cols_tx:
            cursor.execute("ALTER TABLE transactions ADD COLUMN time_range TEXT NOT NULL DEFAULT '05:00 - 18:15'")
        if 'debt_id' not in cols_tx:
            cursor.execute("ALTER TABLE transactions ADD COLUMN debt_id INTEGER REFERENCES debts(id) ON DELETE CASCADE")
            
        cursor.execute("PRAGMA table_info(debts)")
        cols_debt = [row['name'] for row in cursor.fetchall()]
        if 'category_id' not in cols_debt:
            cursor.execute("ALTER TABLE debts ADD COLUMN category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL")
        
        # 4. Interest Rate Logs Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS interest_rate_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            rate REAL NOT NULL
        )
        """)
        
        # Seed default interest rate of 41% if database is newly created
        cursor.execute("SELECT COUNT(*) as count FROM interest_rate_logs")
        if cursor.fetchone()['count'] == 0:
            cursor.execute("INSERT INTO interest_rate_logs (date, rate) VALUES (?, ?)", ('2026-01-01', 0.41))
            
        conn.commit()
        conn.close()
    st.session_state[_init_key] = True

# Category CRUD
def add_category(name, emoji, color, type_val):
    if is_using_supabase():
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        client._insert("ory_categories", {"name": name, "emoji": emoji, "color": color, "type": type_val})
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO categories (name, emoji, color, type) VALUES (?, ?, ?, ?)",
            (name, emoji, color, type_val)
        )
        conn.commit()
        conn.close()
    invalidate_cache()

def delete_category(category_id):
    if is_using_supabase():
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        client._delete("ory_categories", category_id)
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        conn.commit()
        conn.close()
    invalidate_cache()

def update_category(category_id, name, emoji, color, type_val):
    if is_using_supabase():
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        client._update("ory_categories", {"name": name, "emoji": emoji, "color": color, "type": type_val}, category_id)
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE categories
            SET name = ?, emoji = ?, color = ?, type = ?
            WHERE id = ?
        """, (name, emoji, color, type_val, category_id))
        conn.commit()
        conn.close()
    invalidate_cache()

@st.cache_data(ttl=60, show_spinner=False)
def _cached_get_categories(using_supabase: bool):
    if using_supabase:
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        return client._get("ory_categories", params={"select": "*", "order": "name.asc"})
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, emoji, color, type FROM categories ORDER BY name ASC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

def get_categories():
    return _cached_get_categories(is_using_supabase())

# Transaction CRUD
def add_transaction(date_str, category_id, amount, description, time_range='05:00 - 18:15'):
    if is_using_supabase():
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        client._insert("ory_transactions", {
            "date": date_str,
            "category_id": category_id,
            "amount": amount,
            "description": description,
            "time_range": time_range
        })
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO transactions (date, category_id, amount, description, time_range) VALUES (?, ?, ?, ?, ?)",
            (date_str, category_id, amount, description, time_range)
        )
        conn.commit()
        conn.close()
    invalidate_cache()

def delete_transaction(transaction_id):
    if is_using_supabase():
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        client._delete("ory_transactions", transaction_id)
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
        conn.commit()
        conn.close()
    invalidate_cache()

def update_transaction(transaction_id, date_str, category_id, amount, description, time_range):
    if is_using_supabase():
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        client._update("ory_transactions", {
            "date": date_str,
            "category_id": category_id,
            "amount": amount,
            "description": description,
            "time_range": time_range
        }, transaction_id)
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE transactions
            SET date = ?, category_id = ?, amount = ?, description = ?, time_range = ?
            WHERE id = ?
        """, (date_str, category_id, amount, description, time_range, transaction_id))
        conn.commit()
        conn.close()
    invalidate_cache()

@st.cache_data(ttl=60, show_spinner=False)
def _cached_get_transactions(using_supabase: bool, limit: int = None):
    if using_supabase:
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        params = {
            "select": "id,date,category_id,amount,description,time_range,categories:ory_categories(name,emoji,color,type)",
            "order": "date.desc,id.desc"
        }
        if limit:
            params["limit"] = limit
        data = client._get("ory_transactions", params=params)
        res = []
        for row in data:
            item = {
                "id": row["id"],
                "date": row["date"],
                "category_id": row["category_id"],
                "amount": row["amount"],
                "description": row["description"],
                "time_range": row["time_range"],
            }
            cat = row.get("categories")
            if cat:
                item["category_name"] = cat["name"]
                item["emoji"] = cat["emoji"]
                item["color"] = cat["color"]
                item["type"] = cat["type"]
            else:
                item["category_name"] = None
                item["emoji"] = None
                item["color"] = None
                item["type"] = None
            res.append(item)
        return res
    else:
        conn = get_connection()
        cursor = conn.cursor()
        query = """
            SELECT t.id, t.date, t.category_id, t.amount, t.description, t.time_range, c.name as category_name, c.emoji, c.color, c.type
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            ORDER BY t.date DESC, t.id DESC
        """
        if limit:
            query += f" LIMIT {limit}"
        cursor.execute(query)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

def get_transactions(limit=None):
    return _cached_get_transactions(is_using_supabase(), limit)

# Debt CRUD
def add_debt(type_val, amount, name, due_date, status, category_id=None):
    if is_using_supabase():
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        client._insert("ory_debts", {
            "type": type_val,
            "amount": amount,
            "name": name,
            "due_date": due_date,
            "status": status,
            "category_id": category_id
        })
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO debts (type, amount, name, due_date, status, category_id) VALUES (?, ?, ?, ?, ?, ?)",
            (type_val, amount, name, due_date, status, category_id)
        )
        conn.commit()
        conn.close()
    invalidate_cache()

def delete_debt(debt_id):
    if is_using_supabase():
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        client._delete("ory_debts", debt_id)
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM debts WHERE id = ?", (debt_id,))
        conn.commit()
        conn.close()
    invalidate_cache()

def update_debt_status(debt_id, status):
    if is_using_supabase():
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        client._update("ory_debts", {"status": status}, debt_id)
        if status == 'Ödendi':
            debts = client._get("ory_debts", params={"id": f"eq.{debt_id}"})
            if debts:
                debt = debts[0]
                if debt['category_id']:
                    tx_desc = f"{debt['name']} (Borç Kapatma)"
                    today_str = datetime.now().strftime('%Y-%m-%d')
                    client._insert("ory_transactions", {
                        "date": today_str,
                        "category_id": debt['category_id'],
                        "amount": debt['amount'],
                        "description": tx_desc,
                        "time_range": '05:00 - 18:15',
                        "debt_id": debt_id
                    })
        else:
            requests.delete(f"{client.url}/rest/v1/ory_transactions?debt_id=eq.{debt_id}", headers=client.headers, timeout=6)
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE debts SET status = ? WHERE id = ?", (status, debt_id))
        
        if status == 'Ödendi':
            cursor.execute("SELECT type, amount, name, category_id, due_date FROM debts WHERE id = ?", (debt_id,))
            debt = cursor.fetchone()
            if debt and debt['category_id']:
                tx_desc = f"{debt['name']} (Borç Kapatma)"
                today_str = datetime.now().strftime('%Y-%m-%d')
                cursor.execute("""
                    INSERT INTO transactions (date, category_id, amount, description, time_range, debt_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (today_str, debt['category_id'], debt['amount'], tx_desc, '05:00 - 18:15', debt_id))
        else:
            cursor.execute("DELETE FROM transactions WHERE debt_id = ?", (debt_id,))
            
        conn.commit()
        conn.close()
    invalidate_cache()

def update_debt(debt_id, type_val, amount, name, due_date, status, category_id=None):
    if is_using_supabase():
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        debts = client._get("ory_debts", params={"id": f"eq.{debt_id}"})
        old_status = debts[0]['status'] if debts else 'Bekliyor'
        
        client._update("ory_debts", {
            "type": type_val,
            "amount": amount,
            "name": name,
            "due_date": due_date,
            "status": status,
            "category_id": category_id
        }, debt_id)
        
        if status == 'Ödendi':
            if old_status == 'Bekliyor':
                tx_desc = f"{name} (Borç Kapatma)"
                today_str = datetime.now().strftime('%Y-%m-%d')
                client._insert("ory_transactions", {
                    "date": today_str,
                    "category_id": category_id,
                    "amount": amount,
                    "description": tx_desc,
                    "time_range": '05:00 - 18:15',
                    "debt_id": debt_id
                })
            else:
                tx_desc = f"{name} (Borç Kapatma)"
                requests.patch(f"{client.url}/rest/v1/ory_transactions?debt_id=eq.{debt_id}", headers=client.headers, json={
                    "category_id": category_id,
                    "amount": amount,
                    "description": tx_desc
                }, timeout=6)
        else:
            requests.delete(f"{client.url}/rest/v1/ory_transactions?debt_id=eq.{debt_id}", headers=client.headers, timeout=6)
    else:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT status FROM debts WHERE id = ?", (debt_id,))
        row = cursor.fetchone()
        old_status = row['status'] if row else 'Bekliyor'
        
        cursor.execute("""
            UPDATE debts
            SET type = ?, amount = ?, name = ?, due_date = ?, status = ?, category_id = ?
            WHERE id = ?
        """, (type_val, amount, name, due_date, status, category_id, debt_id))
        
        if status == 'Ödendi':
            if old_status == 'Bekliyor':
                tx_desc = f"{name} (Borç Kapatma)"
                today_str = datetime.now().strftime('%Y-%m-%d')
                cursor.execute("""
                    INSERT INTO transactions (date, category_id, amount, description, time_range, debt_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (today_str, category_id, amount, tx_desc, '05:00 - 18:15', debt_id))
            else:
                tx_desc = f"{name} (Borç Kapatma)"
                cursor.execute("""
                    UPDATE transactions
                    SET category_id = ?, amount = ?, description = ?
                    WHERE debt_id = ?
                """, (category_id, amount, tx_desc, debt_id))
        else:
            cursor.execute("DELETE FROM transactions WHERE debt_id = ?", (debt_id,))
            
        conn.commit()
        conn.close()
    invalidate_cache()

@st.cache_data(ttl=60, show_spinner=False)
def _cached_get_debts(using_supabase: bool):
    if using_supabase:
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        data = client._get("ory_debts", params={
            "select": "id,type,amount,name,due_date,status,category_id,categories:ory_categories(name,emoji,color)",
            "order": "due_date.asc,id.desc"
        })
        res = []
        for row in data:
            item = {
                "id": row["id"],
                "type": row["type"],
                "amount": row["amount"],
                "name": row["name"],
                "due_date": row["due_date"],
                "status": row["status"],
                "category_id": row["category_id"],
            }
            cat = row.get("categories")
            if cat:
                item["category_name"] = cat["name"]
                item["category_emoji"] = cat["emoji"]
                item["category_color"] = cat["color"]
            else:
                item["category_name"] = None
                item["category_emoji"] = None
                item["category_color"] = None
            res.append(item)
        return res
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT d.id, d.type, d.amount, d.name, d.due_date, d.status, d.category_id,
                   c.name as category_name, c.emoji as category_emoji, c.color as category_color
            FROM debts d
            LEFT JOIN categories c ON d.category_id = c.id
            ORDER BY d.due_date ASC, d.id DESC
        """)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

def get_debts():
    return _cached_get_debts(is_using_supabase())

# Interest Rate CRUD
def add_or_update_rate_log(date_str, rate):
    if is_using_supabase():
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        logs = client._get("ory_interest_rate_logs", params={"date": f"eq.{date_str}"})
        if logs:
            client._update("ory_interest_rate_logs", {"rate": rate}, logs[0]['id'])
        else:
            client._insert("ory_interest_rate_logs", {"date": date_str, "rate": rate})
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO interest_rate_logs (date, rate)
            VALUES (?, ?)
            ON CONFLICT(date) DO UPDATE SET rate=excluded.rate
        """, (date_str, rate))
        conn.commit()
        conn.close()
    invalidate_cache()

def delete_rate_log(log_id):
    if is_using_supabase():
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        logs = client._get("ory_interest_rate_logs", params={"select": "id"})
        if len(logs) > 1:
            client._delete("ory_interest_rate_logs", log_id)
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM interest_rate_logs")
        if cursor.fetchone()['count'] > 1:
            cursor.execute("DELETE FROM interest_rate_logs WHERE id = ?", (log_id,))
        conn.commit()
        conn.close()
    invalidate_cache()

@st.cache_data(ttl=60, show_spinner=False)
def _cached_get_rate_logs(using_supabase: bool):
    if using_supabase:
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        return client._get("ory_interest_rate_logs", params={"select": "id,date,rate", "order": "date.desc"})
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, date, rate FROM interest_rate_logs ORDER BY date DESC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

def get_rate_logs():
    return _cached_get_rate_logs(is_using_supabase())

# --- BİLEŞİK FAİZ VE ZAMAN SİMÜLASYON MOTORU (Ledger Engine) ---

def get_on_account_details(balance, active_rate):
    """
    Burgan Bank ON Hesap faiz kurallarına göre vadesiz bakiye limitini,
    uygulanan faiz oranını ve faiz işleyip işlemeyeceğini döner.
    """
    if balance < 10500.0:
        return balance, 0.0, False
        
    if balance < 50000.0:
        vadesiz = 10000.0
        rate = 0.41 if active_rate == 0.41 else 0.45
    elif balance < 100000.0:
        vadesiz = 12500.0
        rate = 0.41 if active_rate == 0.41 else 0.45
    elif balance < 250000.0:
        vadesiz = 25000.0
        rate = 0.41 if active_rate == 0.41 else 0.45
    elif balance < 500000.0:
        vadesiz = 50000.0
        rate = 0.41 if active_rate == 0.41 else 0.45
    elif balance < 750000.0:
        vadesiz = 90000.0
        rate = 0.41 if active_rate == 0.41 else 0.45
    elif balance < 1000000.0:
        vadesiz = 140000.0
        rate = 0.41 if active_rate == 0.41 else 0.45
    elif balance < 1250000.0:
        vadesiz = 165000.0
        rate = 0.40 if active_rate == 0.41 else 0.45
    else:
        vadesiz = 175000.0
        rate = 0.40 if active_rate == 0.41 else 0.45
        
    return vadesiz, rate, True

@st.cache_data(ttl=60, show_spinner=False)
def _cached_run_simulation(using_supabase: bool, until_date_str: str):
    until_d = datetime.strptime(until_date_str, '%Y-%m-%d').date() if until_date_str else None
    return _raw_run_simulation(until_d)

def run_simulation(until_date=None):
    if until_date is None:
        until_date = datetime.now().date()
    until_str = until_date.strftime('%Y-%m-%d')
    return _cached_run_simulation(is_using_supabase(), until_str)

def _raw_run_simulation(until_date=None):
    if until_date is None:
        until_date = datetime.now().date()
        
    if is_using_supabase():
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        # Fetch categories
        cat_data = client._get("ory_categories", params={"select": "id,name,emoji,color,type"})
        categories = {row['id']: row for row in cat_data}
        
        # Fetch transactions
        tx_data = client._get("ory_transactions", params={"select": "id,date,category_id,amount,description,time_range", "order": "date.asc,id.asc"})
        tx_list = tx_data
        
        # Fetch rate logs
        rate_data = client._get("ory_interest_rate_logs", params={"select": "date,rate", "order": "date.asc"})
        rate_logs = rate_data
    else:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Fetch categories
        cursor.execute("SELECT id, name, emoji, color, type FROM categories")
        categories = {row['id']: dict(row) for row in cursor.fetchall()}
        
        # Fetch transactions
        cursor.execute("""
            SELECT id, date, category_id, amount, description, time_range
            FROM transactions
            ORDER BY date ASC, id ASC
        """)
        tx_list = [dict(row) for row in cursor.fetchall()]
        
        # Fetch rate logs
        cursor.execute("SELECT date, rate FROM interest_rate_logs ORDER BY date ASC")
        rate_logs = [dict(row) for row in cursor.fetchall()]
        conn.close()
    
    # Group transactions by date
    tx_by_date = {}
    for tx in tx_list:
        cat = categories.get(tx['category_id'])
        if not cat:
            continue
        tx['cat_type'] = cat['type']
        tx['cat_name'] = cat['name']
        tx['emoji'] = cat['emoji']
        tx['color'] = cat['color']
        
        dt = datetime.strptime(tx['date'], '%Y-%m-%d').date()
        tx_by_date.setdefault(dt, []).append(tx)
        
    # Map rate logs by date
    rate_by_date = {}
    for log in rate_logs:
        dt = datetime.strptime(log['date'], '%Y-%m-%d').date()
        rate_by_date[dt] = log['rate']
        
    # Determine start date
    dates = []
    if tx_by_date:
        dates.append(min(tx_by_date.keys()))
    if rate_by_date:
        dates.append(min(rate_by_date.keys()))
        
    if not dates:
        return 0.0, 0.0, [], 0.0
        
    start_date = min(dates)
    if start_date > until_date:
        start_date = until_date
        
    current_vault = 0.0
    total_interest_earned = 0.0
    accumulated_gross = 0.0
    daily_log = []
    
    # Initialize active rate
    active_rate = 0.41
    past_rates = [log for log in rate_logs if datetime.strptime(log['date'], '%Y-%m-%d').date() <= start_date]
    if past_rates:
        active_rate = past_rates[-1]['rate']
        
    curr = start_date
    delta = timedelta(days=1)
    
    # Locked Friday/Last Business Day state for weekends
    locked_vadesiz_limit = 0.0
    locked_rate_today = 0.0
    locked_qualifies = False
    locked_gross_raw = 0.0
    
    # Day by day simulation
    while curr <= until_date:
        if curr in rate_by_date:
            active_rate = rate_by_date[curr]
            
        day_income = 0.0
        day_expense = 0.0
        is_weekend = curr.weekday() >= 5  # 5: Saturday, 6: Sunday
        
        # 1. Morning Posting (05:00) on Business Days
        posted_interest = 0.0
        vault_before_posting = current_vault
        if not is_weekend and accumulated_gross > 0.0:
            posted_gross = math.floor((accumulated_gross + 1e-9) * 100) / 100
            posted_stopaj = math.floor((posted_gross * 0.175 + 1e-9) * 100) / 100
            posted_interest = math.floor((posted_gross - posted_stopaj + 1e-9) * 100) / 100
            current_vault += posted_interest
            total_interest_earned += posted_interest
            accumulated_gross = 0.0
            
        # 2. Process DAY transactions (05:00 - 18:15)
        day_txs = []
        night_txs = []
        if curr in tx_by_date:
            for tx in tx_by_date[curr]:
                t_range = tx.get('time_range', '05:00 - 18:15')
                if t_range == '18:15 - 05:00':
                    night_txs.append(tx)
                else:
                    day_txs.append(tx)
                    
        for tx in day_txs:
            if tx['cat_type'] == 'Gelir':
                current_vault += tx['amount']
                day_income += tx['amount']
            else:
                current_vault -= tx['amount']
                day_expense += tx['amount']
                    
        # 3. Evening Evaluation (18:15)
        if not is_weekend:
            # Business day: evaluate current vault and lock today's interest
            vadesiz_limit, rate_today, qualifies = get_on_account_details(current_vault, active_rate)
            if qualifies:
                earning_base = max(0.0, current_vault - vadesiz_limit)
                gross_raw = earning_base * (rate_today / 365.0)
                gross_truncated = math.floor((gross_raw + 1e-9) * 100) / 100
                stopaj_truncated = math.floor((gross_truncated * 0.175 + 1e-9) * 100) / 100
                daily_interest = math.floor((gross_truncated - stopaj_truncated + 1e-9) * 100) / 100
            else:
                vadesiz_limit = current_vault
                earning_base = 0.0
                gross_raw = 0.0
                daily_interest = 0.0
                rate_today = 0.0
                
            # Lock this state for the weekend
            locked_vadesiz_limit = vadesiz_limit
            locked_rate_today = rate_today
            locked_qualifies = qualifies
            locked_gross_raw = gross_raw
        else:
            # Weekend day: Use Friday's locked state to calculate today's interest
            vadesiz_limit = locked_vadesiz_limit
            rate_today = locked_rate_today
            qualifies = locked_qualifies
            gross_raw = locked_gross_raw
            
            if qualifies:
                earning_base = max(0.0, current_vault - vadesiz_limit)
                gross_truncated = math.floor((gross_raw + 1e-9) * 100) / 100
                stopaj_truncated = math.floor((gross_truncated * 0.175 + 1e-9) * 100) / 100
                daily_interest = math.floor((gross_truncated - stopaj_truncated + 1e-9) * 100) / 100
            else:
                earning_base = 0.0
                daily_interest = 0.0
                
        # Accumulate this day's gross interest
        accumulated_gross += gross_raw
        
        # 4. Process NIGHT transactions (18:15 - 05:00)
        for tx in night_txs:
            if tx['cat_type'] == 'Gelir':
                current_vault += tx['amount']
                day_income += tx['amount']
            else:
                current_vault -= tx['amount']
                day_expense += tx['amount']
        
        daily_log.append({
            'date': curr.strftime('%Y-%m-%d'),
            'vault_before_interest': vault_before_posting, # vault at start of day
            'vault_after_interest': current_vault, # vault after morning posting + txs
            'interest_earned': daily_interest, # interest calculated today
            'posted_interest': posted_interest, # interest posted today
            'rate': rate_today,
            'vadesiz_limit': vadesiz_limit,
            'earning_base': earning_base,
            'income': day_income,
            'expense': day_expense
        })
        
        curr += delta
        
    # Pending interest calculation
    pending_net = 0.0
    if accumulated_gross > 0.0:
        pending_gross = math.floor((accumulated_gross + 1e-9) * 100) / 100
        pending_stopaj = math.floor((pending_gross * 0.175 + 1e-9) * 100) / 100
        pending_net = math.floor((pending_gross - pending_stopaj + 1e-9) * 100) / 100
        
    # Ensure values are rounded to 2 decimal places
    current_vault = round(current_vault, 2)
    total_interest_earned = round(total_interest_earned, 2)
    if daily_log:
        daily_log[-1]['vault_after_interest'] = round(daily_log[-1]['vault_after_interest'], 2)
        
    return current_vault, total_interest_earned, daily_log, pending_net


@st.cache_data(ttl=60, show_spinner=False)
def _cached_get_monthly_flow(using_supabase: bool, month_str: str):
    if using_supabase:
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        now = datetime.now()
        current_year = now.year
        current_month_val = now.month
        start_date = f"{current_year:04d}-{current_month_val:02d}-01"
        if current_month_val == 12:
            end_date = f"{current_year+1:04d}-01-01"
        else:
            end_date = f"{current_year:04d}-{current_month_val+1:02d}-01"
            
        data = client._get("ory_transactions", params={
            "date": [f"gte.{start_date}", f"lt.{end_date}"],
            "select": "amount,categories:ory_categories(type)"
        })
        stats = {'Gelir': 0.0, 'Gider': 0.0}
        for row in data:
            cat = row.get("categories")
            if cat and cat["type"] in stats:
                stats[cat["type"]] += row["amount"]
        return stats
    else:
        current_month = month_str
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.type, SUM(t.amount) as total
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.date LIKE ?
            GROUP BY c.type
        """, (f"{current_month}%",))
        rows = cursor.fetchall()
        conn.close()
        
        stats = {'Gelir': 0.0, 'Gider': 0.0}
        for row in rows:
            stats[row['type']] = row['total'] or 0.0
        return stats

def get_monthly_flow():
    m_str = datetime.now().strftime('%Y-%m')
    return _cached_get_monthly_flow(is_using_supabase(), m_str)

@st.cache_data(ttl=60, show_spinner=False)
def _cached_get_pending_debts_totals(using_supabase: bool):
    if using_supabase:
        client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        data = client._get("ory_debts", params={
            "status": "eq.Bekliyor",
            "select": "type,amount"
        })
        totals = {'Alınacak': 0.0, 'Verilecek': 0.0}
        for row in data:
            t_type = row.get("type")
            if t_type in totals:
                totals[t_type] += row["amount"]
        return totals
    else:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT type, SUM(amount) as total
            FROM debts
            WHERE status = 'Bekliyor'
            GROUP BY type
        """)
        rows = cursor.fetchall()
        conn.close()
        
        totals = {'Alınacak': 0.0, 'Verilecek': 0.0}
        for row in rows:
            totals[row['type']] = row['total'] or 0.0
        return totals

def get_pending_debts_totals():
    return _cached_get_pending_debts_totals(is_using_supabase())

# --- UTILS & HELPERS ---

def get_nice_limits(min_val, max_val):
    if min_val is None or max_val is None:
        return 0.0, 10.0, 2.0
    if min_val == max_val:
        if min_val == 0:
            return 0.0, 10.0, 2.0
        padding = abs(min_val) * 0.1
        min_val -= padding
        max_val += padding
        
    diff = max_val - min_val
    
    # Calculate a raw step size targeting around 6-8 levels
    raw_step = diff / 6.0
    power = 10.0 ** math.floor(math.log10(raw_step)) if raw_step > 0 else 1.0
    norm_step = raw_step / power
    
    if norm_step <= 1.0:
        step = 1.0 * power
    elif norm_step <= 2.0:
        step = 2.0 * power
    elif norm_step <= 5.0:
        step = 5.0 * power
    else:
        step = 10.0 * power
        
    # Enforce a smaller minimum step size based on magnitude to keep ticks clean but granular
    mag = max(abs(min_val), abs(max_val))
    if mag >= 100000:
        step = max(step, 1000.0) # at least 1,000 TL steps for vault
    elif mag >= 1000:
        step = max(step, 10.0)
    elif mag >= 100:
        step = max(step, 1.0) # at least 1.0 step for daily interest in hundreds (e.g. 110, 111, 112...)
    elif mag >= 10:
        step = max(step, 1.0)
        
    nice_min = step * math.floor(min_val / step)
    nice_max = step * math.ceil(max_val / step)
    
    if nice_min == nice_max:
        nice_max += step
        
    # If the range is too narrow (less than 4 steps), expand it symmetrically
    num_ticks = (nice_max - nice_min) / step
    while num_ticks < 4:
        nice_min -= step
        nice_max += step
        num_ticks = (nice_max - nice_min) / step
        
    # Double check if there are too many ticks
    if num_ticks > 12:
        step *= 2
        nice_min = step * math.floor(min_val / step)
        nice_max = step * math.ceil(max_val / step)
        
    return float(nice_min), float(nice_max), float(step)

def format_turkish_amount(amount):
    if amount is None:
        return ""
    try:
        s = f"{float(amount):,.2f}"
        return s.replace(",", "TEMP").replace(".", ",").replace("TEMP", ".")
    except (ValueError, TypeError):
        return ""

def parse_turkish_amount(val_str):
    if not val_str:
        return None
    # Remove dots (thousand separators) and replace comma with dot
    clean_str = val_str.replace(".", "").replace(",", ".")
    try:
        return float(clean_str)
    except ValueError:
        return None

# --- UI HELPERS ---

def get_category_badge_html(emoji, name, color):
    import html as _html
    e_safe = _html.escape(str(emoji or "📌"))
    n_safe = _html.escape(str(name or "Diğer"))
    c_safe = color or "#64748B"
    return f"<span class='category-badge' style='background-color: {c_safe}18; color: {c_safe}; padding: 4px 10px; border-radius: 8px; font-weight: 600; font-size: 0.78rem; border: 1px solid {c_safe}35; display: inline-flex; align-items: center; gap: 4px; white-space: nowrap;'><span style='font-size: 0.82rem; line-height: 1;'>{e_safe}</span><span>{n_safe}</span></span>"

def render_tx_row_html(tx, sign, amt_color, extra_style=""):
    import html as _html
    badge = get_category_badge_html(tx.get('emoji'), tx.get('category_name'), tx.get('color'))
    raw_desc = tx.get('description') or tx.get('category_name') or ''
    desc = _html.escape(str(raw_desc))
    amount_str = f"{sign} {float(tx.get('amount') or 0.0):,.2f} TL"
    time_suffix = f" ({_html.escape(str(tx['time_range']))})" if tx.get('time_range') else ""
    date_str = f"{_html.escape(str(tx.get('date') or ''))}{time_suffix}"
    return f'<div class="tx-feed-item" style="{extra_style}"><div style="display: flex; justify-content: space-between; align-items: center; gap: 10px; min-width: 0;"><div style="display: flex; align-items: center; gap: 8px; min-width: 0;">{badge}<div style="font-weight: 500; font-size: 0.85rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="{desc}">{desc}</div></div><div style="text-align: right; flex-shrink: 0;"><div style="color: {amt_color}; font-weight: 700; font-size: 0.92rem;">{amount_str}</div><div style="font-size: 0.7rem; color: #94A3B8; margin-top: 2px;">{date_str}</div></div></div></div>'

def get_debt_badge_html(due_date_str):
    if not due_date_str:
        return ""
    try:
        due = datetime.strptime(due_date_str, '%Y-%m-%d').date()
        today = datetime.now().date()
        diff = (due - today).days
        
        if diff > 7:
            color = "#64748B"
            bg = "rgba(100, 116, 139, 0.08)"
            border = "rgba(100, 116, 139, 0.15)"
            text = f"📅 {diff} gün kaldı"
        elif diff > 0:
            color = "#D97706"
            bg = "rgba(217, 119, 6, 0.08)"
            border = "rgba(217, 119, 6, 0.15)"
            text = f"⏳ {diff} gün kaldı"
        elif diff == 0:
            color = "#EF4444"
            bg = "rgba(239, 68, 68, 0.1)"
            border = "rgba(239, 68, 68, 0.25)"
            text = "🚨 Bugün son gün!"
        else:
            color = "#EF4444"
            bg = "rgba(239, 68, 68, 0.12)"
            border = "rgba(239, 68, 68, 0.3)"
            text = f"🔴 {abs(diff)} gün gecikti ❗️"
            
        return f"<span style='background-color: {bg}; color: {color}; padding: 4px 10px; border-radius: 8px; font-weight: 600; font-size: 0.78rem; border: 1px solid {border}; margin-left: 10px; display: inline-flex; align-items: center;'>{text}</span>"
    except Exception:
        return ""

def inject_custom_css():
    st.markdown("""
    <style>
    /* Google Fonts */
    @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=Outfit:wght@300;400;500;600;700;800&display=swap');
    
    html, body, [class*="css"], .stApp {
        font-family: 'Plus Jakarta Sans', 'Outfit', 'Inter', sans-serif;
        font-size: 0.88rem !important;
        overflow-x: hidden !important;
        max-width: 100vw !important;
    }
    
    /* Typography Overrides */
    h1 {
        font-size: 1.85rem !important;
    }
    h2 {
        font-size: 1.45rem !important;
    }
    h3 {
        font-size: 1.15rem !important;
    }
    
    /* Hide top header bar to maximize clean feeling */
    header[data-testid="stHeader"] {
        background: transparent;
    }
    
    /* Spendee Metric Cards styling (theme-aware) */
    .metric-card {
        background: var(--secondary-background-color, #1E293B);
        border: 1px solid rgba(148, 163, 184, 0.22);
        border-radius: 16px;
        padding: 16px 20px;
        box-shadow: 0 4px 10px -2px rgba(0, 0, 0, 0.08);
        transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        position: relative;
        overflow: hidden;
        text-align: center;
    }
    
    .metric-card:hover {
        transform: translateY(-3px);
        box-shadow: 0 12px 20px -8px rgba(0, 0, 0, 0.16);
    }
    
    .metric-card::before {
        content: '';
        position: absolute;
        top: 0;
        left: 0;
        width: 100%;
        height: 4px;
        background: linear-gradient(90deg, #3B82F6, #10B981);
    }
    
    .metric-card.gider::before {
        background: linear-gradient(90deg, #EF4444, #F59E0B);
    }
    
    .metric-card.faiz::before {
        background: linear-gradient(90deg, #8B5CF6, #EC4899);
    }
 
    .metric-title {
        font-size: 0.72rem;
        font-weight: 700;
        color: var(--text-color, #94A3B8);
        opacity: 0.72;
        text-transform: uppercase;
        letter-spacing: 1.1px;
        margin-bottom: 6px;
    }
    
    .metric-value {
        font-size: 1.45rem;
        font-weight: 800;
        letter-spacing: -0.5px;
        color: var(--text-color, #F8FAFC);
    }
    
    .metric-sub {
        font-size: 0.7rem;
        color: var(--text-color, #94A3B8);
        opacity: 0.65;
        margin-top: 4px;
        font-weight: 500;
    }
    
    /* Transaction feed card layout */
    .tx-feed-item {
        background: var(--secondary-background-color, #1E293B);
        border: 1px solid rgba(148, 163, 184, 0.22);
        border-radius: 12px;
        padding: 10px 14px;
        margin-bottom: 8px;
        transition: all 0.2s ease;
        box-shadow: 0 2px 4px rgba(0, 0, 0, 0.04);
    }
    
    .tx-feed-item:hover {
        border-color: rgba(148, 163, 184, 0.45);
        transform: translateY(-1px);
    }
    
    /* Balance Header Grid layout */
    .balance-header-grid {
        display: flex;
        justify-content: center;
        align-items: center;
        gap: 20px;
        flex-wrap: wrap;
        margin-bottom: 12px;
    }
    .balance-main {
        font-size: 2.6rem;
        font-weight: 850;
        letter-spacing: -1px;
        background: linear-gradient(135deg, #3B82F6, #10B981);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        filter: drop-shadow(0 2px 8px rgba(59, 130, 246, 0.1));
        padding: 0 10px;
    }
    .balance-badge {
        font-size: 1.15rem;
        font-weight: 700;
        padding: 6px 12px;
        border-radius: 12px;
        display: inline-flex;
        align-items: center;
    }
    .badge-payable {
        color: #EF4444;
        background: rgba(239, 68, 68, 0.08);
        border: 1px solid rgba(239, 68, 68, 0.15);
    }
    .badge-receivable {
        color: #10B981;
        background: rgba(16, 185, 129, 0.08);
        border: 1px solid rgba(16, 185, 129, 0.15);
    }
    
    /* Align all horizontal blocks to center by default (for micro-rows like transaction lists) */
    div[data-testid="stHorizontalBlock"] {
        align-items: center !important;
        gap: 8px !important;
    }
    div[data-testid="stHorizontalBlock"] > div {
        padding: 0 !important;
    }
    
    /* Top-align main dashboard grid (containing charts), forms, and layouts */
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stArrowVegaLiteChart"]),
    div[data-testid="stHorizontalBlock"]:has(div[class*="stVegaLiteChart"]),
    div[data-testid="stHorizontalBlock"]:has(h1),
    div[data-testid="stHorizontalBlock"]:has(h2),
    div[data-testid="stHorizontalBlock"]:has(h3),
    div[data-testid="stHorizontalBlock"]:has(label),
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stWidgetLabel"]) {
        align-items: flex-start !important;
        gap: 16px !important;
    }
    
    /* Pretty forms and buttons */
    .stButton>button {
        border-radius: 10px;
        padding: 6px 12px;
        font-weight: 600;
        font-size: 0.8rem !important;
    }
    .stTextInput>div>div>input, .stSelectbox>div>div>div, .stNumberInput>div>div>input, .stDateInput>div>div>input {
        border-radius: 10px !important;
        font-size: 0.82rem !important;
        height: 36px !important;
    }
    label, div[data-testid="stWidgetLabel"] p {
        font-size: 0.78rem !important;
        font-weight: 600 !important;
    }
    
    /* Target only action buttons inside transaction rows, category lists, or debt actions */
    div[data-testid="stHorizontalBlock"]:has(.tx-card-marker) > div[data-testid="stColumn"]:has(.tx-actions-marker) div[data-testid="stButton"] > button,
    div[data-testid="stHorizontalBlock"]:has(.cat-row-marker) > div[data-testid="stColumn"]:has(.cat-actions-marker) div[data-testid="stButton"] > button,
    div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"] > div[data-testid="element-container"] span.debt-btn-row) div[data-testid="stButton"] > button {
        padding: 2px 4px !important;
        min-width: 36px !important;
        min-height: 36px !important;
        width: 36px !important;
        height: 36px !important;
        border-radius: 50% !important; /* Circle buttons */
        font-size: 0.9rem !important;
        line-height: 1 !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        margin: 0 !important;
        background-color: #F8FAFC !important;
        border: 1px solid #E2E8F0 !important;
        box-shadow: none !important;
        color: inherit !important;
        transition: all 0.2s ease-in-out !important;
    }
    
    div[data-testid="stHorizontalBlock"]:has(.tx-card-marker) > div[data-testid="stColumn"]:has(.tx-actions-marker) div[data-testid="stButton"] > button:hover,
    div[data-testid="stHorizontalBlock"]:has(.cat-row-marker) > div[data-testid="stColumn"]:has(.cat-actions-marker) div[data-testid="stButton"] > button:hover,
    div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"] > div[data-testid="element-container"] span.debt-btn-row) div[data-testid="stButton"] > button:hover {
        background-color: #F1F5F9 !important;
        border-color: #CBD5E1 !important;
        transform: scale(1.1) !important;
    }
    
    /* Debt action button row: precisely target the DIRECT parent stHorizontalBlock using deep-path :has() */
    /* This prevents matching ancestor containers that also contain .debt-btn-row deep inside */
    div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"] > div[data-testid="element-container"] span.debt-btn-row) {
        flex-direction: row !important;
        flex-wrap: nowrap !important;
        align-items: center !important;
        justify-content: flex-end !important; /* Compact actions aligned to the right */
        gap: 8px !important;
        width: 100% !important;
        margin-top: 4px !important;
        margin-bottom: 12px !important;
    }
    div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"] > div[data-testid="element-container"] span.debt-btn-row) > div[data-testid="stColumn"] {
        width: 44px !important;
        min-width: 44px !important;
        max-width: 44px !important;
        flex-grow: 0 !important;
        flex-shrink: 0 !important;
        padding: 0 !important;
        margin: 0 !important;
    }

    /* Accordion details card styling */
    details.debt-details {
        display: block !important;
        width: 100% !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    
    /* Spacing between debt cards */
    .debt-info-card {
        margin-bottom: 16px !important;
        transition: all 0.2s ease-in-out !important;
    }
    
    /* ====== st.expander card styling (theme-aware) ====== */
    div[data-testid="stExpander"] {
        border: 1px solid rgba(148, 163, 184, 0.24) !important;
        border-radius: 12px !important;
        margin-bottom: 6px !important;
        overflow: hidden !important;
        transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
        background: transparent !important;
    }
    div[data-testid="stExpander"]:hover {
        border-color: rgba(148, 163, 184, 0.48) !important;
        box-shadow: 0 4px 10px -2px rgba(0, 0, 0, 0.08) !important;
    }
    /* Expander header (summary row) */
    div[data-testid="stExpander"] > details > summary {
        padding: 12px 14px !important;
        cursor: pointer !important;
        user-select: none !important;
        list-style: none !important;
    }
    div[data-testid="stExpander"] > details > summary::-webkit-details-marker {
        display: none !important;
    }
    /* Expander header text */
    div[data-testid="stExpander"] > details > summary p,
    div[data-testid="stExpander"] > details > summary span {
        font-size: 0.88rem !important;
        font-weight: 600 !important;
    }
    /* Expander toggle icon (chevron) */
    div[data-testid="stExpanderToggleIcon"] {
        transition: transform 0.2s ease !important;
    }
    /* Expander body */
    div[data-testid="stExpander"] > details > div[data-testid="stExpanderDetails"] {
        padding: 8px 14px 12px 14px !important;
        border-top: 1px solid rgba(148, 163, 184, 0.16) !important;
    }
    /* Buttons inside expander - reset circle style, keep normal full-width */
    div[data-testid="stExpander"] div[data-testid="stButton"] > button {
        border-radius: 8px !important;
        font-size: 0.8rem !important;
        padding: 6px 10px !important;
        width: 100% !important;
        min-width: 0 !important;
        height: auto !important;
        min-height: 36px !important;
    }
    /* Force action buttons inside expander to be side-by-side on all screens */
    div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] {
        display: flex !important;
        flex-direction: row !important;
        flex-wrap: nowrap !important;
        align-items: center !important;
        gap: 8px !important;
        width: 100% !important;
    }
    div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
        width: 100% !important;
        min-width: 0 !important;
        max-width: none !important;
        flex: 1 1 0% !important;
        padding: 0 !important;
        margin: 0 !important;
    }
    
    @media (prefers-color-scheme: dark) {
        div[data-testid="stHorizontalBlock"]:has(.tx-card-marker) > div[data-testid="stColumn"]:has(.tx-actions-marker) div[data-testid="stButton"] > button,
        div[data-testid="stHorizontalBlock"]:has(.cat-row-marker) > div[data-testid="stColumn"]:has(.cat-actions-marker) div[data-testid="stButton"] > button,
        div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"] > div[data-testid="element-container"] span.debt-btn-row) div[data-testid="stButton"] > button {
            background-color: #1E293B !important;
            border-color: #334155 !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.tx-card-marker) > div[data-testid="stColumn"]:has(.tx-actions-marker) div[data-testid="stButton"] > button:hover,
        div[data-testid="stHorizontalBlock"]:has(.cat-row-marker) > div[data-testid="stColumn"]:has(.cat-actions-marker) div[data-testid="stButton"] > button:hover,
        div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"] > div[data-testid="element-container"] span.debt-btn-row) div[data-testid="stButton"] > button:hover {
            background-color: #334155 !important;
            border-color: #475569 !important;
        }
    }
    
    /* Keep color and emoji columns side-by-side and vertically aligned */
    div[data-testid="stHorizontalBlock"]:has(.cat-color-emoji-marker) {
        display: flex !important;
        flex-direction: row !important;
        flex-wrap: nowrap !important;
        align-items: flex-end !important;
        gap: 12px !important;
        width: 100% !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.cat-color-emoji-marker) > div[data-testid="stColumn"] {
        width: 50% !important;
        min-width: 0 !important;
        flex-grow: 1 !important;
        flex-shrink: 1 !important;
        margin: 0 !important;
    }
    
    /* Style for Emoji Grid Buttons (WhatsApp style) */
    div[data-testid="stHorizontalBlock"]:has(.emoji-marker):not(:has(div[data-testid="stHorizontalBlock"]:has(.emoji-marker))) {
        display: flex !important;
        flex-direction: row !important;
        flex-wrap: wrap !important;
        justify-content: flex-start !important;
        gap: 6px !important;
        width: 100% !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.emoji-marker):not(:has(div[data-testid="stHorizontalBlock"]:has(.emoji-marker))) > div[data-testid="stColumn"] {
        width: 38px !important;
        min-width: 38px !important;
        max-width: 38px !important;
        flex-grow: 0 !important;
        flex-shrink: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.emoji-marker):not(:has(div[data-testid="stHorizontalBlock"]:has(.emoji-marker))) > div[data-testid="stColumn"] button {
        font-size: 1.25rem !important;
        padding: 0 !important;
        width: 38px !important;
        height: 38px !important;
        min-width: 38px !important;
        min-height: 38px !important;
        border-radius: 8px !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        background-color: #F1F5F9 !important;
        border: 1px solid #E2E8F0 !important;
        transition: all 0.2s ease-in-out !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.emoji-marker):not(:has(div[data-testid="stHorizontalBlock"]:has(.emoji-marker))) > div[data-testid="stColumn"] button:hover {
        background-color: #E2E8F0 !important;
        transform: scale(1.1) !important;
    }
    
    @media (prefers-color-scheme: dark) {
        div[data-testid="stHorizontalBlock"]:has(.emoji-marker):not(:has(div[data-testid="stHorizontalBlock"]:has(.emoji-marker))) > div[data-testid="stColumn"] button {
            background-color: #334155 !important;
            border-color: #475569 !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.emoji-marker):not(:has(div[data-testid="stHorizontalBlock"]:has(.emoji-marker))) > div[data-testid="stColumn"] button:hover {
            background-color: #475569 !important;
        }
    }
    
    /* Style for Emoji Radio Grid (WhatsApp style, zero reruns) */
    .emoji-radio-container div[data-testid="stRadio"] > div[role="radiogroup"] {
        display: flex !important;
        flex-direction: row !important;
        flex-wrap: wrap !important;
        justify-content: flex-start !important;
        gap: 6px !important;
        width: 100% !important;
    }
    .emoji-radio-container div[data-testid="stRadio"] > div[role="radiogroup"] label {
        font-size: 1.25rem !important;
        padding: 0 !important;
        width: 38px !important;
        height: 38px !important;
        min-width: 38px !important;
        min-height: 38px !important;
        border-radius: 8px !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        background-color: #F1F5F9 !important;
        border: 1px solid #E2E8F0 !important;
        transition: all 0.2s ease-in-out !important;
        cursor: pointer !important;
        margin: 0 !important;
    }
    .emoji-radio-container div[data-testid="stRadio"] > div[role="radiogroup"] label:hover {
        background-color: #E2E8F0 !important;
        transform: scale(1.1) !important;
    }
    .emoji-radio-container div[data-testid="stRadio"] > div[role="radiogroup"] label:has(input:checked) {
        background-color: #3B82F6 !important;
        border-color: #60A5FA !important;
        color: white !important;
        box-shadow: 0 0 8px rgba(59, 130, 246, 0.4) !important;
    }
    /* Hide the default radio circle/dot */
    .emoji-radio-container div[data-testid="stRadio"] > div[role="radiogroup"] label > div:first-child {
        display: none !important;
    }
    /* Strip margins and center the emoji label text */
    .emoji-radio-container div[data-testid="stRadio"] > div[role="radiogroup"] label > div:last-child {
        margin: 0 !important;
        padding: 0 !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
    }
    
    @media (prefers-color-scheme: dark) {
        .emoji-radio-container div[data-testid="stRadio"] > div[role="radiogroup"] label {
            background-color: #334155 !important;
            border-color: #475569 !important;
        }
        .emoji-radio-container div[data-testid="stRadio"] > div[role="radiogroup"] label:hover {
            background-color: #475569 !important;
        }
    }
    
    /* Mobile-specific adjustments for native-app feel */
    @media (max-width: 768px) {
        /* Reduce side padding on mobile to maximize content width */
        div[data-testid="stAppViewBlockContainer"] {
            padding: 0.75rem 0.5rem !important;
        }
        
        /* Smaller font sizes on mobile */
        h1 {
            font-size: 1.45rem !important;
        }
        h2 {
            font-size: 1.2rem !important;
        }
        h3 {
            font-size: 1.0rem !important;
        }
        .metric-value {
            font-size: 1.25rem !important;
        }
        .metric-title {
            font-size: 0.65rem !important;
            letter-spacing: 0.8px;
        }
        
        /* Reduce spacing between stacked vertical columns */
        div[data-testid="stVerticalBlock"] {
            gap: 0.5rem !important;
        }
        
        /* Make selectbox options and form fields feel more tap-friendly */
        .stTextInput>div>div>input, .stSelectbox>div>div>div, .stNumberInput>div>div>input, .stDateInput>div>div>input {
            height: 40px !important; /* Slightly larger for easier finger tapping */
            font-size: 0.88rem !important;
        }
        
        /* Compact tab menu on mobile */
        button[data-baseweb="tab"] {
            padding: 6px 10px !important;
            font-size: 0.78rem !important;
        }
        
        /* Compact list items padding */
        .tx-feed-item {
            padding: 8px 10px !important;
        }
        
        /* Balance Header grid on mobile */
        .balance-header-grid {
            display: grid !important;
            grid-template-columns: 1fr 1fr !important;
            grid-template-areas: 
                "main main"
                "payable receivable" !important;
            gap: 12px 16px !important;
            justify-items: center !important;
            align-items: center !important;
        }
        .balance-main {
            grid-area: main !important;
            font-size: 1.95rem !important;
            padding: 0 !important;
            line-height: 1.2 !important;
        }
        .balance-badge {
            font-size: 0.95rem !important;
            padding: 5px 10px !important;
            border-radius: 10px !important;
            width: fit-content !important;
        }
        .badge-payable {
            grid-area: payable !important;
            justify-self: end !important;
        }
        .badge-receivable {
            grid-area: receivable !important;
            justify-self: start !important;
        }

        /* Prevent horizontal overflow on mobile viewports */
        html, body, [data-testid="stAppViewBlockContainer"] {
            overflow-x: hidden !important;
            max-width: 100vw !important;
        }

        /* Metric cards vertical gap fix on mobile */
        .metric-card {
            margin-bottom: 16px !important;
        }

        /* Emoji selector grid on mobile: force it to stay in a compact flex grid */
        div[data-testid="stHorizontalBlock"]:has(.emoji-marker):not(:has(div[data-testid="stHorizontalBlock"]:has(.emoji-marker))) {
            display: flex !important;
            flex-direction: row !important;
            flex-wrap: wrap !important;
            justify-content: flex-start !important;
            gap: 6px !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.emoji-marker):not(:has(div[data-testid="stHorizontalBlock"]:has(.emoji-marker))) > div[data-testid="stColumn"] {
            width: 38px !important;
            min-width: 38px !important;
            max-width: 38px !important;
            flex-grow: 0 !important;
            flex-shrink: 0 !important;
            margin: 0 !important;
            padding: 0 !important;
        }

        /* Metric cards row layout on mobile: keep in a single row */
        div[data-testid="stHorizontalBlock"]:has(.metric-marker):not(:has(div[data-testid="stHorizontalBlock"])) {
            flex-direction: row !important;
            flex-wrap: nowrap !important;
            align-items: stretch !important;
            gap: 6px !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.metric-marker):not(:has(div[data-testid="stHorizontalBlock"])) > div[data-testid="stColumn"] {
            width: 33.333% !important;
            min-width: 0 !important;
            flex-grow: 1 !important;
            flex-shrink: 0 !important;
            margin: 0 !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.metric-marker):not(:has(div[data-testid="stHorizontalBlock"])) .metric-card {
            padding: 8px 10px !important;
            min-height: 90px !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.metric-marker):not(:has(div[data-testid="stHorizontalBlock"])) .metric-value {
            font-size: 1.05rem !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.metric-marker):not(:has(div[data-testid="stHorizontalBlock"])) .metric-title {
            font-size: 0.62rem !important;
            letter-spacing: 0px !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.metric-marker):not(:has(div[data-testid="stHorizontalBlock"])) .metric-sub {
            display: block !important;
            font-size: 0.58rem !important;
            line-height: 1.1 !important;
            margin-top: 4px !important;
        }

        /* Transaction list row layout on mobile */
        div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .tx-card-marker):not(:has(div[data-testid="stHorizontalBlock"]:has(.tx-card-marker))) {
            flex-direction: row !important;
            flex-wrap: nowrap !important;
            align-items: center !important;
            gap: 8px !important;
            max-width: 100% !important;
            width: 100% !important;
        }
        div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .tx-card-marker):not(:has(div[data-testid="stHorizontalBlock"]:has(.tx-card-marker))) > div[data-testid="stColumn"]:has(.tx-card-marker) {
            flex-grow: 1 !important;
            flex-shrink: 1 !important;
            min-width: 0 !important;
            width: auto !important;
        }
        div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .tx-card-marker):not(:has(div[data-testid="stHorizontalBlock"]:has(.tx-card-marker))) > div[data-testid="stColumn"]:has(.tx-actions-marker) {
            flex-grow: 0 !important;
            flex-shrink: 0 !important;
            width: auto !important;
        }
        
        /* Transaction list inner columns containing edit/delete buttons side-by-side on mobile */
        div[data-testid="stColumn"]:has(.tx-actions-marker) div[data-testid="stHorizontalBlock"] {
            flex-direction: row !important;
            flex-wrap: nowrap !important;
            align-items: center !important;
            gap: 6px !important;
            width: 100% !important;
        }
        div[data-testid="stColumn"]:has(.tx-actions-marker) div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
            width: auto !important;
            min-width: 38px !important;
            max-width: 38px !important;
            flex-grow: 0 !important;
            flex-shrink: 0 !important;
            margin: 0 !important;
        }

        .tx-feed-item {
            width: 100% !important;
            min-width: 0 !important;
            box-sizing: border-box !important;
        }

        .category-badge > span:last-child {
            max-width: 90px !important;
            overflow: hidden !important;
            text-overflow: ellipsis !important;
            white-space: nowrap !important;
            display: inline-block !important;
        }

        /* Category Management layout on mobile: stack Yeni Kategori Oluştur on top of Mevcut Kategoriler */
        div[data-testid="stHorizontalBlock"]:has(.cat-page-marker) {
            flex-direction: column !important;
            align-items: stretch !important;
            gap: 24px !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.cat-page-marker) > div[data-testid="stColumn"] {
            width: 100% !important;
        }

        /* Category list row layout on mobile */
        div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .cat-row-marker):not(:has(div[data-testid="stHorizontalBlock"]:has(.cat-row-marker))) {
            flex-direction: row !important;
            flex-wrap: nowrap !important;
            align-items: center !important;
            gap: 6px !important;
            max-width: 100% !important;
            width: 100% !important;
        }
        div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .cat-row-marker):not(:has(div[data-testid="stHorizontalBlock"]:has(.cat-row-marker))) > div[data-testid="stColumn"] {
            width: auto !important;
            flex-grow: 0 !important;
            flex-shrink: 0 !important;
            margin: 0 !important;
        }
        div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .cat-row-marker):not(:has(div[data-testid="stHorizontalBlock"]:has(.cat-row-marker))) > div[data-testid="stColumn"]:has(.cat-row-marker) {
            flex-grow: 1 !important;
            flex-shrink: 1 !important;
            min-width: 0 !important;
        }

        /* Debt summary row layout on mobile: keep side-by-side */
        div[data-testid="stHorizontalBlock"]:has(.debt-summary-marker) {
            flex-direction: row !important;
            flex-wrap: nowrap !important;
            align-items: stretch !important;
            gap: 8px !important;
            width: 100% !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.debt-summary-marker) > div[data-testid="stColumn"] {
            width: 50% !important;
            min-width: 0 !important;
            flex-grow: 1 !important;
            flex-shrink: 0 !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.debt-summary-marker) .metric-card {
            padding: 8px 10px !important;
            min-height: 90px !important;
            margin-bottom: 0 !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.debt-summary-marker) .metric-value {
            font-size: 1.05rem !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.debt-summary-marker) .metric-title {
            font-size: 0.62rem !important;
            letter-spacing: 0px !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.debt-summary-marker) .metric-sub {
            display: block !important;
            font-size: 0.58rem !important;
            line-height: 1.1 !important;
            margin-top: 4px !important;
        }
    }
    </style>
    """, unsafe_allow_html=True)

    # Dynamic JS injection for category coloring inside selectboxes
    try:
        rows = get_categories()
        cat_colors = {f"{row['emoji']} {row['name']}": row['color'] for row in rows}
    except Exception:
        cat_colors = {}
        
    import json
    colors_json = json.dumps(cat_colors)
    
    js_code = """
    <script>
    (function() {
        const categoryColors = """ + colors_json + """;
        
        function styleOptions() {
            let options = [];
            try {
                if (window.parent && window.parent.document) {
                    const parentOpts = window.parent.document.querySelectorAll('div[role="option"], li[role="option"], [data-baseweb="menu"] li, ul[role="listbox"] li, div[data-baseweb="popover"] li');
                    options = Array.from(parentOpts);
                }
            } catch(e) {}
            
            options.forEach(opt => {
                const rawText = opt.innerText || opt.textContent || "";
                const text = rawText.replace(/\\s+/g, ' ').trim();
                
                for (const key in categoryColors) {
                    const normKey = key.replace(/\\s+/g, ' ').trim();
                    if (text === normKey || text.includes(normKey) || (text.indexOf(normKey.split(' ')[1]) !== -1 && text.indexOf(normKey.split(' ')[0]) !== -1)) {
                        const color = categoryColors[key];
                        
                        if (!opt.dataset.colored) {
                            opt.dataset.colored = "true";
                            opt.style.setProperty("border-left", "5px solid " + color, "important");
                            opt.style.setProperty("background-color", color + "15", "important");
                            opt.style.setProperty("font-weight", "600", "important");
                            opt.style.setProperty("padding-left", "10px", "important");
                            opt.style.setProperty("transition", "background-color 0.12s ease, color 0.12s ease", "important");
                            
                            opt.addEventListener("mouseenter", () => {
                                opt.style.setProperty("background-color", color, "important");
                                opt.style.setProperty("color", "#FFFFFF", "important");
                            });
                            opt.addEventListener("mouseleave", () => {
                                opt.style.setProperty("background-color", color + "15", "important");
                                opt.style.setProperty("color", "", "important");
                            });
                        }
                        break;
                    }
                }
            });
        }
        
        styleOptions();
        scanMiktarInputs();
        
        function formatTurkishCurrency(val) {
            if (val.endsWith('.') && !val.includes(',')) {
                val = val.slice(0, -1) + ',';
            }
            let parts = val.split(',');
            let integerPart = parts[0].replace(/\\D/g, '') || '';
            let decimalPart = parts.length > 1 ? parts[1].replace(/\\D/g, '').substring(0, 2) : null;
            
            integerPart = integerPart.replace(/\\B(?=(\\d{3})+(?!\\d))/g, ".");
            
            if (decimalPart !== null) {
                return integerPart + ',' + decimalPart;
            } else {
                return integerPart;
            }
        }

        function setupMiktarInput(input) {
            if (input.dataset.amountFormatted) return;
            input.dataset.amountFormatted = "true";
            
            input.addEventListener('input', function(e) {
                if (e.isTrusted === false) return;
                
                let cursorPosition = input.selectionStart;
                let originalLength = input.value.length;
                
                let formatted = formatTurkishCurrency(input.value);
                
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                if (nativeInputValueSetter) {
                    nativeInputValueSetter.call(input, formatted);
                } else {
                    input.value = formatted;
                }
                input.dispatchEvent(new Event('input', { bubbles: true }));
                
                let newLength = formatted.length;
                let diff = newLength - originalLength;
                
                input.setSelectionRange(cursorPosition + diff, cursorPosition + diff);
            });
        }

        function scanMiktarInputs() {
            try {
                if (window.parent && window.parent.document) {
                    const labels = Array.from(window.parent.document.querySelectorAll('label'));
                    const miktarLabels = labels.filter(el => el.textContent.includes('Miktar (TL)'));
                    miktarLabels.forEach(label => {
                        const parent = label.closest('[data-testid="stTextInput"]');
                        if (parent) {
                            const input = parent.querySelector('input');
                            if (input) {
                                setupMiktarInput(input);
                            }
                        }
                    });
                }
            } catch(e) {}
        }

        if (!window.parentCatObserver && window.parent && window.parent.document) {
            try {
                window.parentCatObserver = new MutationObserver((mutations) => {
                    styleOptions();
                    scanMiktarInputs();
                });
                window.parentCatObserver.observe(window.parent.document.body, { childList: true, subtree: true });
            } catch(e) {}
        }
    })();
    </script>
    """
    st.components.v1.html(js_code, height=0, width=0)


# --- AUTH (en fazla birkaç kişilik, secrets tabanlı) ---
def _auth_users():
    """Streamlit Secrets'taki [APP_USERS] -> {isim: şifre}. Yoksa boş (giriş kapalı)."""
    try:
        raw = st.secrets.get("APP_USERS")
        users = {str(k): str(v) for k, v in dict(raw).items()} if raw else {}
    except Exception:
        users = {}
    return {k: v for k, v in users.items() if v}


def _auth_token(name, password):
    import hmac, hashlib
    try:
        key = str(st.secrets.get("AUTH_SECRET") or "")
    except Exception:
        key = ""
    mac = hmac.new(("ory:" + key).encode(), f"{name}:{password}".encode(), hashlib.sha256).hexdigest()[:40]
    return f"{name}.{mac}"


def _verify_token(token):
    import hmac
    if not token or "." not in str(token):
        return None
    for name, pwd in _auth_users().items():
        if hmac.compare_digest(str(token), _auth_token(name, pwd)):
            return name
    return None


def _match_password(candidate):
    import hmac
    for name, pwd in _auth_users().items():
        if hmac.compare_digest(candidate.encode(), pwd.encode()):
            return name
    return None


def check_password():
    """Returns `True` if the user has entered the correct password."""
    if st.session_state.get("authenticated", False):
        return True

    if not _auth_users():
        st.error("🔒 Giriş kapalı: Secrets içinde **APP_USERS** tanımlı değil.")
        st.info(
            "Streamlit Cloud → uygulama → Settings → Secrets bölümüne şunu ekleyin:\n\n"
            "```toml\nAUTH_SECRET = \"uzun-rastgele-bir-metin\"\n\n[APP_USERS]\nben = \"sifreniz\"\narkadas = \"arkadasin-sifresi\"\n```"
        )
        return False

    _tok_user = _verify_token(st.query_params.get("auth"))
    if _tok_user:
        st.session_state["authenticated"] = True
        st.session_state["auth_user"] = _tok_user
        return True

    st.markdown("""
        <style>
        /* Hide sidebar on login screen */
        [data-testid="stSidebar"] {
            display: none !important;
        }
        .login-card-container {
            display: flex;
            justify-content: center;
            align-items: center;
            margin-top: 40px;
            margin-bottom: 20px;
        }
        .login-card {
            background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 20px;
            padding: 35px 30px 25px 30px;
            width: 100%;
            max-width: 420px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
            text-align: center;
        }
        .login-badge {
            display: inline-block;
            background: rgba(59, 130, 246, 0.15);
            color: #60a5fa;
            font-size: 12px;
            font-weight: 700;
            padding: 5px 14px;
            border-radius: 20px;
            letter-spacing: 1px;
            text-transform: uppercase;
            margin-bottom: 15px;
        }
        .login-title {
            color: #ffffff;
            font-size: 24px;
            font-weight: 800;
            margin-bottom: 8px;
            letter-spacing: -0.5px;
        }
        .login-subtitle {
            color: #94a3b8;
            font-size: 14px;
            margin-bottom: 20px;
            line-height: 1.5;
        }
        </style>
    """, unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("""
            <div class="login-card-container">
                <div class="login-card">
                    <div class="login-badge">🧭 ODTÜ Oryantiring</div>
                    <div class="login-title">Finans Takip Portalı</div>
                    <div class="login-subtitle">Devam etmek için lütfen giriş şifresini girin.</div>
                </div>
            </div>
        """, unsafe_allow_html=True)

        with st.form("login_form", clear_on_submit=False):
            password = st.text_input("🔑 Giriş Şifresi", type="password", placeholder="Şifrenizi girin...", key="login_pwd_input")
            remember_me = st.checkbox("📌 Beni Hatırla", value=True, help="Bu cihazda tekrar şifre sormasın.")
            submit_btn = st.form_submit_button("🔒 Giriş Yap", use_container_width=True, type="primary")

            if submit_btn:
                import time as _time
                locked_until = st.session_state.get("login_locked_until", 0)
                if _time.time() < locked_until:
                    st.error(f"⏳ Çok fazla hatalı deneme. {int(locked_until - _time.time())} sn sonra tekrar deneyin.")
                else:
                    _who = _match_password(password.strip())
                    if _who:
                        st.session_state["authenticated"] = True
                        st.session_state["auth_user"] = _who
                        st.session_state["login_fails"] = 0
                        if remember_me:
                            st.query_params["auth"] = _auth_token(_who, _auth_users()[_who])
                        elif "auth" in st.query_params:
                            del st.query_params["auth"]
                        st.rerun()
                    else:
                        fails = st.session_state.get("login_fails", 0) + 1
                        st.session_state["login_fails"] = fails
                        _time.sleep(1)
                        if fails >= 5:
                            st.session_state["login_locked_until"] = _time.time() + 60
                            st.session_state["login_fails"] = 0
                            st.error("❌ 5 hatalı deneme. 60 saniye beklemeniz gerekiyor.")
                        else:
                            st.error("❌ Hatalı şifre! Lütfen tekrar deneyin.")

    return False


# --- INITIALIZE APP ---
st.set_page_config(
    page_title="ODTÜ Oryantiring Finans Takip",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Apply global styles
inject_custom_css()

# Authentication Check
if not check_password():
    st.stop()

# Today's date logic
today_date = datetime.now().date()
today_str = today_date.strftime('%Y-%m-%d')

db_error = None
try:
    init_db()
except Exception as e:
    db_error = e

current_active_rate = 0.41
if not db_error:
    try:
        if is_using_supabase():
            rate_logs = get_rate_logs()
            row = next((r for r in rate_logs if r['date'] <= today_str), None)
            current_active_rate = row['rate'] if row else 0.41
        else:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT rate FROM interest_rate_logs WHERE date <= ? ORDER BY date DESC LIMIT 1", (today_str,))
            row = cursor.fetchone()
            current_active_rate = row['rate'] if row else 0.41
            conn.close()
    except Exception as e:
        db_error = e

if db_error and is_using_supabase():
    st.markdown("""
    <div style="background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); border: 1px solid rgba(239, 68, 68, 0.4); border-radius: 16px; padding: 24px; margin-bottom: 25px; box-shadow: 0 10px 25px rgba(0,0,0,0.3);">
        <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 12px;">
            <span style="font-size: 28px;">🚨</span>
            <div>
                <h3 style="color: #F87171; margin: 0; font-size: 1.25rem;">Bulut Veritabanına (Supabase) Bağlanılamadı!</h3>
                <span style="color: #94A3B8; font-size: 0.82rem;">Orkestrasyon & Otomatik Kurtarma Modu Devrede</span>
            </div>
        </div>
        <p style="color: #CBD5E1; font-size: 0.9rem; line-height: 1.6; margin-bottom: 15px;">
            Supabase ücretsiz planında 7 gün aktif işlem yapılmadığında sunucular otomatik olarak <b>uyku moduna (Paused)</b> alınır.
            Aşağıdaki seçeneklerden birini kullanarak kesintisiz devam edebilirsiniz:
        </p>
    </div>
    """, unsafe_allow_html=True)
    
    col_c1, col_c2 = st.columns(2)
    with col_c1:
        st.markdown("""
        #### 1. 📂 Yerel Yedek Veritabanı (Anında Devam Et)
        Veritabanı uyanana kadar yerel SQLite veritabanınızla sistemi hemen kullanabilirsiniz.
        """)
        if st.button("⚡ Yerel Mod ile Devam Et (SQLite)", type="primary", use_container_width=True):
            st.session_state["force_offline_mode"] = True
            invalidate_cache()
            st.rerun()
            
    with col_c2:
        st.markdown("""
        #### 2. ☁️ Supabase Projesini Uyandır
        Supabase paneline gidip **"Restore project"** butonuna tıklayın (1-2 dk sürer).
        """)
        st.markdown('[🔗 Supabase Dashboard (cwuwhldshxeewcxrsysl)](https://supabase.com/dashboard/project/cwuwhldshxeewcxrsysl)', unsafe_allow_html=True)
        if st.button("🔄 Bağlantıyı Yeniden Test Et", use_container_width=True):
            invalidate_cache()
            st.rerun()
            
    st.caption(f"Hata Detayı: {str(db_error)}")
    st.stop()


# Navigation & Programmatic Redirect
menu_options = [
    "🏠 Özet",
    "🏕️ Kademe & Ödemeler",
    "💸 İşlemler",
    "🤝 Borç & Alacak",
    "⚙️ Ayarlar & Raporlar",
]
if "_nav_target" in st.session_state:
    _target = st.session_state.pop("_nav_target")
    if _target in menu_options:
        st.session_state["menu_selection"] = _target
if st.session_state.get("menu_selection") not in menu_options:
    st.session_state["menu_selection"] = "🏠 Özet"

_db_pill_html = (
    "<span style='display:inline-block; margin-top:6px; padding:3px 10px; border-radius:999px; font-size:0.72rem; font-weight:700; background:rgba(16,185,129,0.12); color:#10B981; border:1px solid rgba(16,185,129,0.28);'>🟢 Bulut (Supabase)</span>"
    if is_using_supabase()
    else "<span style='display:inline-block; margin-top:6px; padding:3px 10px; border-radius:999px; font-size:0.72rem; font-weight:700; background:rgba(245,158,11,0.12); color:#F59E0B; border:1px solid rgba(245,158,11,0.28);'>🟡 Yerel (SQLite)</span>"
)

st.sidebar.markdown(
    f"""
    <div style='text-align: center; margin-bottom: 14px;'>
        <h2 style='margin-bottom: 2px; color: #3B82F6;'>🧭 ODTÜ Oryantiring</h2>
        <div style='font-size: 0.78rem; color: #94A3B8; font-weight: 600; letter-spacing: 1px; text-transform: uppercase;'>Mali Yönetim</div>
        {_db_pill_html}
    </div>
    """,
    unsafe_allow_html=True,
)
st.sidebar.markdown("---")
menu_selection = st.sidebar.radio(
    "Menü",
    menu_options,
    key="menu_selection",
    label_visibility="collapsed",
)

st.sidebar.markdown("---")
if st.sidebar.button("🚪 Çıkış Yap", use_container_width=True):
    st.session_state["authenticated"] = False
    if "auth" in st.query_params:
        del st.query_params["auth"]
    st.rerun()


# --- MAIN PAGE RENDERER ---

# Run simulation only on pages that use it for instant speed on other pages
if menu_selection in ("🏠 Özet", "⚙️ Ayarlar & Raporlar"):
    book_vault, total_interest_earned, daily_log, accumulated_interest = run_simulation()
    current_vault = book_vault
else:
    book_vault, total_interest_earned, daily_log, accumulated_interest = 0.0, 0.0, [], 0.0
    current_vault = 0.0


if menu_selection == "🏠 Özet":
    debt_totals = get_pending_debts_totals()
    total_payable = debt_totals['Verilecek']
    total_receivable = debt_totals['Alınacak']

    _sb_client_dash = SupabaseClient(SUPABASE_URL, SUPABASE_KEY) if is_using_supabase() else None
    _fee_store_dash = FeeStore(is_using_supabase(), _sb_client_dash, DB_PATH)
    pending_fees_total = 0.0
    try:
        _ok_fees, _ = _fee_store_dash.ensure()
        if _ok_fees:
            pending_fees_total = _fee_store_dash.get_pending_fees_total() if hasattr(_fee_store_dash, "get_pending_fees_total") else 0.0
    except Exception:
        pending_fees_total = 0.0

    # 1. Centered Header Display (Main Vault)
    _extra_notes = []
    if accumulated_interest > 0:
        _extra_notes.append(f"⏳ Hesaba Geçecek Birikmiş Net Faiz: +{accumulated_interest:,.2f} TL")
    if pending_fees_total > 0:
        _extra_notes.append(f"💳 Bekleyen Sporcu Tahsilatı: {pending_fees_total:,.2f} TL")
    _notes_html = (
        "<div style='font-size: 0.78rem; color: #8B5CF6; font-weight: 600; margin-top: 6px; text-align: center;'>"
        + " &nbsp;|&nbsp; ".join(_extra_notes)
        + "</div>"
        if _extra_notes
        else ""
    )

    st.markdown(f"""
    <div style="text-align: center; margin: 1.2rem 0 1.4rem 0; font-family: 'Plus Jakarta Sans', sans-serif;">
        <div class="balance-header-grid">
            <div class="balance-badge badge-payable">( - {total_payable:,.2f} TL )</div>
            <div class="balance-main">{current_vault:,.2f} TL</div>
            <div class="balance-badge badge-receivable">( + {total_receivable:,.2f} TL )</div>
        </div>
        <div style="font-size: 0.78rem; color: #64748B; margin-top: 6px; text-transform: uppercase; letter-spacing: 2px; font-weight: 700;">
            🧭 GÜNCEL KASA DENGESİ VE BORÇLAR
        </div>
        {_notes_html}
    </div>
    """, unsafe_allow_html=True)

    # 2. Hızlı İşlem Çubuğu
    qa1, qa2, qa3 = st.columns(3)
    if qa1.button("➕ Gelir / Gider Ekle", type="primary", use_container_width=True, key="qa_add_tx"):
        st.session_state["_nav_target"] = "💸 İşlemler"
        st.session_state["show_new_tx_form"] = True
        st.rerun()
    if qa2.button("✅ Sporcu Ödemesi İşaretle", use_container_width=True, key="qa_mark_fee"):
        st.session_state["_nav_target"] = "🏕️ Kademe & Ödemeler"
        st.rerun()
    if qa3.button("🤝 Borç / Alacak Ekle", use_container_width=True, key="qa_add_debt"):
        st.session_state["_nav_target"] = "🤝 Borç & Alacak"
        st.session_state["show_new_debt_form"] = True
        st.rerun()

    # 3. Compact Overdue Alert
    all_debts_db = get_debts()
    pending_debts_db = [d for d in all_debts_db if d['status'] == 'Bekliyor']
    overdue_count = 0
    today_check = datetime.now().date()
    for d in pending_debts_db:
        if d['due_date']:
            try:
                due = datetime.strptime(d['due_date'], '%Y-%m-%d').date()
                if due <= today_check:
                    overdue_count += 1
            except Exception:
                pass
    if overdue_count > 0:
        ov_c1, ov_c2 = st.columns([4, 1])
        ov_c1.warning(f"⚠️ Vadesi gelmiş veya geçmiş **{overdue_count} adet** borç/alacak kaydı bulunuyor.")
        if ov_c2.button("Borçlara Git", use_container_width=True, key="qa_goto_debts"):
            st.session_state["_nav_target"] = "🤝 Borç & Alacak"
            st.rerun()

    # 4. Modern Metric Cards
    col1, col2, col3 = st.columns(3)
    vadesiz_limit, rate_today, qualifies = get_on_account_details(current_vault, current_active_rate)
    earning_base = max(0.0, current_vault - vadesiz_limit) if qualifies else 0.0

    with col1:
        st.markdown(f"""
        <div class="metric-card">
            <span class="metric-marker"></span>
            <div class="metric-title">🛡️ Faiz İşleyen Tutar</div>
            <div class="metric-value">{earning_base:,.2f} TL</div>
            <div class="metric-sub">Vadesiz Kalan: {vadesiz_limit:,.0f} TL | Faiz: %{rate_today*100:.1f}</div>
        </div>
        """, unsafe_allow_html=True)

    with col2:
        st.markdown(f"""
        <div class="metric-card faiz">
            <span class="metric-marker"></span>
            <div class="metric-title">📈 Toplam Net Faiz</div>
            <div class="metric-value" style="color: #8B5CF6;">{total_interest_earned:,.2f} TL</div>
            <div class="metric-sub">Kazanılan Net Getiri (%17.5 Stopaj Kesintili)</div>
        </div>
        """, unsafe_allow_html=True)

    with col3:
        m_flow = get_monthly_flow()
        net_flow = m_flow['Gelir'] - m_flow['Gider']
        net_color = "#10B981" if net_flow >= 0 else "#EF4444"
        net_sign = "+" if net_flow >= 0 else ""
        st.markdown(f"""
        <div class="metric-card gider">
            <span class="metric-marker"></span>
            <div class="metric-title">🗓️ Bu Ay Net Akış</div>
            <div class="metric-value" style="color: {net_color};">{net_sign}{net_flow:,.2f} TL</div>
            <div class="metric-sub">Gelir: +{m_flow['Gelir']:,.1f} TL | Gider: -{m_flow['Gider']:,.1f} TL</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # 5. Clean 3-Tab Dashboard Body
    recent_txs = get_transactions()
    tab_recent, tab_cat_dist, tab_charts = st.tabs([
        "🕒 Son İşlemler",
        "📊 Kategori Dağılımı",
        "📈 Kasa & Faiz Grafikleri",
    ])

    with tab_recent:
        if not recent_txs:
            st.info("Henüz kaydedilmiş bir işlem bulunmuyor.")
        else:
            if 'dash_tx_page' not in st.session_state:
                st.session_state.dash_tx_page = 0

            page_size = 5
            total_txs = len(recent_txs)
            total_pages = (total_txs + page_size - 1) // page_size
            if st.session_state.dash_tx_page >= total_pages:
                st.session_state.dash_tx_page = total_pages - 1
            if st.session_state.dash_tx_page < 0:
                st.session_state.dash_tx_page = 0

            current_page = st.session_state.dash_tx_page
            start_idx = current_page * page_size
            end_idx = start_idx + page_size

            for tx in recent_txs[start_idx:end_idx]:
                sign = "+" if tx['type'] == 'Gelir' else "-"
                amt_color = "#10B981" if tx['type'] == 'Gelir' else "#EF4444"
                st.markdown(render_tx_row_html(tx, sign, amt_color), unsafe_allow_html=True)

            if total_pages > 1:
                p_col1, p_col2, p_col3, p_col4, p_col5 = st.columns([4, 1, 2, 1, 4])
                with p_col2:
                    if st.button("◀️", key="prev_dash_tx", disabled=(current_page == 0)):
                        st.session_state.dash_tx_page -= 1
                        st.rerun()
                with p_col3:
                    st.markdown(
                        f"<div style='text-align: center; font-weight: 600; font-size: 0.9rem; padding-top: 6px; white-space: nowrap;'>Sayfa {current_page + 1} / {total_pages}</div>",
                        unsafe_allow_html=True,
                    )
                with p_col4:
                    if st.button("▶️", key="next_dash_tx", disabled=(current_page == total_pages - 1)):
                        st.session_state.dash_tx_page += 1
                        st.rerun()

    with tab_cat_dist:
        render_category_analytics(recent_txs, get_categories())

    with tab_charts:
        col_chart1, col_chart2 = st.columns(2)
        with col_chart1:
            st.markdown("#### 📈 Kasa Bakiyesi Zaman Serisi")
            if daily_log:
                df_log = pd.DataFrame(daily_log)
                df_log['Tarih'] = pd.to_datetime(df_log['date'])
                df_log = df_log.rename(columns={'vault_after_interest': 'vault_balance', 'interest_earned': 'daily_interest'})

                min_val = df_log['vault_balance'].min()
                max_val = df_log['vault_balance'].max()
                nice_min, nice_max, step = get_nice_limits(min_val, max_val)
                y_ticks = []
                curr = nice_min
                while curr <= nice_max + (step * 0.01):
                    y_ticks.append(curr)
                    curr += step

                start_date = df_log['Tarih'].min()
                end_date = df_log['Tarih'].max()
                delta_days = (end_date - start_date).days
                if delta_days >= 3:
                    x_ticks = [start_date, start_date + timedelta(days=delta_days // 3), start_date + timedelta(days=(2 * delta_days) // 3), end_date]
                elif delta_days == 2:
                    x_ticks = [start_date, start_date + timedelta(days=1), end_date]
                else:
                    x_ticks = [start_date, end_date]

                line_chart = alt.Chart(df_log).mark_line(
                    color='#3B82F6',
                    strokeWidth=3,
                    point=alt.OverlayMarkDef(color='#3B82F6', size=40),
                ).encode(
                    x=alt.X('Tarih:T', title=None, axis=alt.Axis(values=x_ticks, format='%Y-%m-%d', labelAngle=0, grid=True)),
                    y=alt.Y('vault_balance:Q', title=None, scale=alt.Scale(domain=[nice_min, nice_max]), axis=alt.Axis(values=y_ticks, format=',.0f')),
                ).properties(height=300).configure_view(strokeWidth=0)
                st.altair_chart(line_chart, use_container_width=True)
            else:
                st.info("Kasa zaman serisini çizmek için yeterli veri bulunamadı.")

        with col_chart2:
            st.markdown("#### 💸 Günlük Faiz Getirisi")
            if daily_log:
                df_log = pd.DataFrame(daily_log)
                df_log['Tarih'] = pd.to_datetime(df_log['date'])
                df_log = df_log.rename(columns={'vault_after_interest': 'vault_balance', 'interest_earned': 'daily_interest'})

                min_val = df_log['daily_interest'].min()
                max_val = df_log['daily_interest'].max()
                nice_min, nice_max, step = get_nice_limits(min_val, max_val)
                y_ticks = []
                curr = nice_min
                while curr <= nice_max + (step * 0.01):
                    y_ticks.append(curr)
                    curr += step

                start_date = df_log['Tarih'].min()
                end_date = df_log['Tarih'].max()
                delta_days = (end_date - start_date).days
                if delta_days >= 3:
                    x_ticks = [start_date, start_date + timedelta(days=delta_days // 3), start_date + timedelta(days=(2 * delta_days) // 3), end_date]
                elif delta_days == 2:
                    x_ticks = [start_date, start_date + timedelta(days=1), end_date]
                else:
                    x_ticks = [start_date, end_date]

                interest_chart = alt.Chart(df_log).mark_line(
                    color='#8B5CF6',
                    strokeWidth=3,
                    point=alt.OverlayMarkDef(color='#8B5CF6', size=40),
                ).encode(
                    x=alt.X('Tarih:T', title=None, axis=alt.Axis(values=x_ticks, format='%Y-%m-%d', labelAngle=0, grid=True)),
                    y=alt.Y('daily_interest:Q', title=None, scale=alt.Scale(domain=[nice_min, nice_max]), axis=alt.Axis(values=y_ticks, format=',.2f')),
                ).properties(height=300).configure_view(strokeWidth=0)
                st.altair_chart(interest_chart, use_container_width=True)
            else:
                st.info("Günlük faiz grafiği için yeterli veri bulunamadı.")


elif menu_selection == "🏕️ Kademe & Ödemeler":
    st.title("🏕️ Kademe & Ödemeler")
    _fee_store = FeeStore(
        is_using_supabase(),
        SupabaseClient(SUPABASE_URL, SUPABASE_KEY) if is_using_supabase() else None,
        DB_PATH,
    )
    render_fees_page(_fee_store, get_categories(), invalidate_cache, today_date, default_tab="odeme", transactions=get_transactions())


elif menu_selection == "💸 İşlemler":
    st.title("💸 İşlemler & Kategoriler")
    categories = get_categories()
    tab_tx_hist, tab_cats = st.tabs(["📋 İşlem Geçmişi", f"🏷️ Kategoriler ({len(categories)})"])

    with tab_tx_hist:
        edit_tx_id = st.session_state.get('edit_tx_id')
        show_tx_form = st.session_state.get('show_new_tx_form', False) or bool(edit_tx_id)

        tx_h1, tx_h2 = st.columns([3, 1])
        tx_h1.markdown("### 📋 İşlem Geçmişi")
        if not edit_tx_id:
            if tx_h2.button("✖️ Formu Kapat" if show_tx_form else "➕ Yeni İşlem Ekle", type="secondary" if show_tx_form else "primary", use_container_width=True, key="toggle_tx_form_btn"):
                st.session_state['show_new_tx_form'] = not show_tx_form
                st.rerun()

        if not categories:
            st.warning("⚠️ İşlem eklemeden önce **🏷️ Kategoriler** sekmesinden en az bir kategori oluşturmalısınız.")
        elif show_tx_form:
            edit_tx = None
            if edit_tx_id:
                if is_using_supabase():
                    client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
                    data = client._get("ory_transactions", params={
                        "select": "id,date,category_id,amount,description,time_range,categories:ory_categories(type)",
                        "id": f"eq.{edit_tx_id}"
                    })
                    if data:
                        row = data[0]
                        edit_tx = {
                            "id": row["id"],
                            "date": row["date"],
                            "category_id": row["category_id"],
                            "amount": row["amount"],
                            "description": row["description"],
                            "time_range": row["time_range"],
                            "type": row["categories"]["type"] if row.get("categories") else None,
                        }
                else:
                    conn = get_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT t.id, t.date, t.category_id, t.amount, t.description, t.time_range, c.type
                        FROM transactions t
                        JOIN categories c ON t.category_id = c.id
                        WHERE t.id = ?
                    """, (edit_tx_id,))
                    row = cursor.fetchone()
                    conn.close()
                    if row:
                        edit_tx = dict(row)

            if edit_tx:
                st.markdown(f"#### ✏️ İşlemi Düzenle (ID: #{edit_tx['id']})")
                try:
                    parsed_date = datetime.strptime(edit_tx['date'], '%Y-%m-%d').date()
                except Exception:
                    parsed_date = today_date

                radio_options = ["Gider", "Gelir"]
                default_radio_idx = radio_options.index(edit_tx['type']) if edit_tx['type'] in radio_options else 0
                tx_type = st.radio("İşlem Türü", radio_options, index=default_radio_idx, horizontal=True, key="edit_tx_type")
                filtered_cats = [c for c in categories if c['type'] == tx_type]

                with st.form("edit_transaction_form", clear_on_submit=False):
                    col_date, col_time, col_amount = st.columns([3, 4, 3])
                    with col_date:
                        tx_date = st.date_input("Tarih", parsed_date, max_value=today_date, key="edit_tx_date")
                    with col_time:
                        time_options = {
                            '05:00 - 18:15': '05:00 - 18:15 (Gün İçi - Bugün)',
                            '18:15 - 23:59': '18:15 - 23:59 (Akşam - Yarın)',
                            '00:00 - 05:00': '00:00 - 05:00 (Gece - Bugün)',
                        }
                        opt_keys = list(time_options.keys())
                        default_time_idx = opt_keys.index(edit_tx['time_range']) if edit_tx['time_range'] in opt_keys else 0
                        tx_time = st.selectbox("Saat Aralığı", options=opt_keys, format_func=lambda x: time_options[x], index=default_time_idx, key="edit_tx_time")
                    with col_amount:
                        tx_amount_str = st.text_input("Miktar (TL)", value=format_turkish_amount(edit_tx['amount']), key="edit_tx_amount")

                    col_cat, col_desc = st.columns([1, 2])
                    with col_cat:
                        if filtered_cats:
                            cat_options = {c['id']: f"{c['emoji']} {c['name']}" for c in filtered_cats}
                            cat_ids = list(cat_options.keys())
                            try:
                                default_cat_idx = cat_ids.index(edit_tx['category_id'])
                            except ValueError:
                                default_cat_idx = 0
                            selected_cat_id = st.selectbox("Kategori", options=cat_ids, format_func=lambda x: cat_options[x], index=default_cat_idx, key="edit_tx_category")
                        else:
                            st.error(f"Seçilen türde ({tx_type}) kategori bulunamadı.")
                            selected_cat_id = None
                    with col_desc:
                        tx_desc = st.text_input("Açıklama / Detay", value=edit_tx['description'] or "", key="edit_tx_desc")

                    submitted = st.form_submit_button("Değişiklikleri Kaydet", type="primary")
                canceled = st.button("İptal Et / Kapat", key="cancel_edit_btn")

                if submitted:
                    tx_amount = parse_turkish_amount(tx_amount_str)
                    if tx_amount is None or tx_amount <= 0:
                        st.error("Lütfen geçerli bir miktar girin (Örn: 1.000,50)")
                    elif selected_cat_id is None:
                        st.error("Kategori seçimi zorunludur.")
                    else:
                        date_str = tx_date.strftime('%Y-%m-%d')
                        update_transaction(edit_tx['id'], date_str, selected_cat_id, tx_amount, tx_desc, tx_time)
                        st.session_state.edit_tx_id = None
                        st.session_state.show_new_tx_form = False
                        st.toast("İşlem başarıyla güncellendi!", icon="✏️")
                        st.rerun()
                if canceled:
                    st.session_state.edit_tx_id = None
                    st.session_state.show_new_tx_form = False
                    st.rerun()
            else:
                st.markdown("#### ➕ Yeni İşlem Kaydet")
                tx_type = st.radio("İşlem Türü", ["Gider", "Gelir"], index=0, horizontal=True, key="new_tx_type_radio")
                filtered_cats = [c for c in categories if c['type'] == tx_type] if tx_type else []

                _kademe_choices = {0: "Yok (Genel İşlem)"}
                _kademe_clean_names = {}
                try:
                    _fs_tx = FeeStore(
                        is_using_supabase(),
                        SupabaseClient(SUPABASE_URL, SUPABASE_KEY) if is_using_supabase() else None,
                        DB_PATH,
                    )
                    _ok_fs, _ = _fs_tx.ensure()
                    if _ok_fs:
                        for _k in _fs_tx.kademeler():
                            _kademe_choices[_k["id"]] = f"🏕️ {_k['name']}"
                            _kademe_clean_names[_k["id"]] = _k["name"]
                except Exception:
                    pass

                with st.form("new_transaction_form", clear_on_submit=True):
                    col_date, col_time, col_amount = st.columns([3, 4, 3])
                    with col_date:
                        tx_date = st.date_input("Tarih", today_date, max_value=today_date)
                    with col_time:
                        time_options = {
                            '05:00 - 18:15': '05:00 - 18:15 (Gün İçi - Bugün)',
                            '18:15 - 23:59': '18:15 - 23:59 (Akşam - Yarın)',
                            '00:00 - 05:00': '00:00 - 05:00 (Gece - Bugün)',
                        }
                        tx_time = st.selectbox("Saat Aralığı", options=list(time_options.keys()), format_func=lambda x: time_options[x], index=0)
                    with col_amount:
                        tx_amount_str = st.text_input("Miktar (TL)", value="", key="new_tx_amount")

                    col_cat, col_kad, col_desc = st.columns([1.2, 1.2, 2])
                    with col_cat:
                        if filtered_cats:
                            cat_options = {c['id']: f"{c['emoji']} {c['name']}" for c in filtered_cats}
                            selected_cat_id = st.selectbox("Kategori", options=list(cat_options.keys()), format_func=lambda x: cat_options[x])
                        else:
                            st.error(f"Seçilen türde ({tx_type}) kategori bulunamadı.")
                            selected_cat_id = None
                    with col_kad:
                        sel_tx_kademe = st.selectbox(
                            "🏕️ İlgili Kademe (İsteğe Bağlı)",
                            options=list(_kademe_choices.keys()),
                            format_func=lambda kid: _kademe_choices[kid],
                            key="new_tx_kademe",
                        )
                    with col_desc:
                        tx_desc = st.text_input("Açıklama / Detay")

                    submitted = st.form_submit_button("✅ İşlemi Kaydet", type="primary")

                if submitted:
                    tx_amount = parse_turkish_amount(tx_amount_str)
                    if tx_amount is None or tx_amount <= 0:
                        st.error("Lütfen geçerli bir miktar girin (Örn: 1.000,50)")
                    elif selected_cat_id is None:
                        st.error("Kategori seçimi zorunludur.")
                    else:
                        final_desc = (tx_desc or "").strip()
                        if sel_tx_kademe and sel_tx_kademe in _kademe_clean_names:
                            k_prefix = f"[{_kademe_clean_names[sel_tx_kademe]}]"
                            if not final_desc.startswith(k_prefix):
                                final_desc = f"{k_prefix} {final_desc}".strip()
                        date_str = tx_date.strftime('%Y-%m-%d')
                        add_transaction(date_str, selected_cat_id, tx_amount, final_desc, time_range=tx_time)
                        st.session_state.show_new_tx_form = False
                        st.toast("İşlem başarıyla eklendi!", icon="✅")
                        st.rerun()
            st.markdown("---")

        all_txs = get_transactions()
        if not all_txs:
            st.info("İşlem geçmişiniz henüz boş. Sağ üstteki **➕ Yeni İşlem Ekle** butonuna basarak ilk işleminizi ekleyebilirsiniz.")
        else:
            f_c1, f_c2 = st.columns([2, 1])
            search_query = f_c1.text_input("🔍 Açıklama, kategori veya tarih ara...", "", label_visibility="collapsed", placeholder="🔍 Açıklama, kategori veya tarih ara...").lower()
            tx_type_flt = f_c2.radio("Tür Filtresi", ["Tümü", "🟢 Gelir", "🔴 Gider"], horizontal=True, label_visibility="collapsed", key="tx_hist_type_flt")

            if st.session_state.get('confirm_delete_id'):
                del_id = st.session_state.confirm_delete_id
                del_tx = next((t for t in all_txs if t['id'] == del_id), None)
                if del_tx:
                    st.warning(f"⚠️ **Seçilen işlemi silmek istediğinize emin misiniz?**\n\n**{del_tx['date']} | {del_tx['emoji']} {del_tx['category_name']} | {del_tx['description'] or ''} | {del_tx['amount']:,.2f} TL**")
                    col_yes, col_no = st.columns([1, 1])
                    with col_yes:
                        if st.button("Evet, Sil", key="btn_confirm_del_yes", type="primary"):
                            delete_transaction(del_id)
                            if st.session_state.get('edit_tx_id') == del_id:
                                st.session_state.edit_tx_id = None
                            st.session_state.confirm_delete_id = None
                            st.toast("İşlem silindi.", icon="🗑️")
                            st.rerun()
                    with col_no:
                        if st.button("İptal Et", key="btn_confirm_del_no"):
                            st.session_state.confirm_delete_id = None
                            st.rerun()
                else:
                    st.session_state.confirm_delete_id = None

            filtered_txs = [
                tx for tx in all_txs
                if (
                    search_query in (tx['description'] or '').lower()
                    or search_query in (tx['category_name'] or '').lower()
                    or search_query in tx['date']
                )
                and (
                    tx_type_flt == "Tümü"
                    or (tx_type_flt == "🟢 Gelir" and tx['type'] == "Gelir")
                    or (tx_type_flt == "🔴 Gider" and tx['type'] == "Gider")
                )
            ]

            if not filtered_txs:
                st.info("Aramanıza uygun işlem bulunamadı.")
            else:
                if 'tx_hist_page' not in st.session_state:
                    st.session_state.tx_hist_page = 0
                tx_page_size = 15
                tx_total_pages = max(1, (len(filtered_txs) + tx_page_size - 1) // tx_page_size)
                if st.session_state.tx_hist_page >= tx_total_pages:
                    st.session_state.tx_hist_page = tx_total_pages - 1
                if st.session_state.tx_hist_page < 0:
                    st.session_state.tx_hist_page = 0
                tx_curr_page = st.session_state.tx_hist_page
                tx_slice = filtered_txs[tx_curr_page * tx_page_size : (tx_curr_page + 1) * tx_page_size]

                # Single shared CSS block for transaction expanders
                st.markdown("""
                <style>
                div[data-testid="stExpander"]:has(.tx-exp-marker) > details > summary {
                    padding-right: 135px !important;
                    position: relative !important;
                }
                div[data-testid="stExpander"]:has(.tx-exp-marker) > details > summary em {
                    background-color: rgba(59, 130, 246, 0.12) !important;
                    color: #60A5FA !important;
                    border: 1px solid rgba(59, 130, 246, 0.28) !important;
                    font-style: normal !important;
                    padding: 2px 8px !important;
                    border-radius: 8px !important;
                    font-size: 0.72rem !important;
                    font-weight: 600 !important;
                    display: inline-flex !important;
                    align-items: center !important;
                    gap: 4px !important;
                    white-space: nowrap !important;
                    margin-right: 6px !important;
                }
                div[data-testid="stExpander"]:has(.tx-exp-marker) > details > summary del {
                    text-decoration: none !important;
                    white-space: nowrap !important;
                    font-size: 0.82rem !important;
                    font-weight: 600 !important;
                    color: inherit !important;
                }
                div[data-testid="stExpander"]:has(.tx-exp-marker) > details > summary strong {
                    position: absolute !important;
                    right: 14px !important;
                    top: 8px !important;
                    font-size: 0.88rem !important;
                    font-weight: 700 !important;
                }
                div[data-testid="stExpander"]:has(.tx-exp-marker) > details > summary code {
                    position: absolute !important;
                    right: 14px !important;
                    bottom: 8px !important;
                    background: transparent !important;
                    border: none !important;
                    padding: 0 !important;
                    margin: 0 !important;
                    color: #94A3B8 !important;
                    font-size: 0.7rem !important;
                    font-weight: 400 !important;
                    font-family: inherit !important;
                }
                </style>
                """, unsafe_allow_html=True)

                for tx in tx_slice:
                    sign = "+" if tx['type'] == 'Gelir' else "-"
                    dot = "🟢" if tx['type'] == 'Gelir' else "🔴"
                    raw_desc = tx['description'] if tx['description'] else tx['category_name']
                    clean_desc = (raw_desc or "").replace("~", "")
                    amt_formatted = f"{dot} {sign} {tx['amount']:,.2f} TL"
                    cat_prefix = f"_{tx['emoji']} {tx['category_name']}_ · " if tx['category_name'] else ""
                    exp_title = f"{cat_prefix}~~{clean_desc}~~ **{amt_formatted}** `{tx['date']}`"

                    with st.expander(exp_title, expanded=False):
                        st.markdown(f'<span class="tx-exp-marker" id="tx-id-{tx["id"]}"></span>', unsafe_allow_html=True)
                        tx_type_lbl = "🟢 Gelir" if tx['type'] == 'Gelir' else "🔴 Gider"
                        time_lbl = f" | 🕒 Saat: {tx['time_range']}" if tx.get('time_range') else " | 🕒 Saat: Belirsiz"
                        st.markdown(f"<div style='font-size: 0.82rem; color: #94A3B8; margin-bottom: 10px;'>{tx_type_lbl}{time_lbl}</div>", unsafe_allow_html=True)
                        c_edit, c_del = st.columns(2)
                        with c_edit:
                            if st.button("✏️ Düzenle", key=f"edit_tx_btn_{tx['id']}", use_container_width=True):
                                st.session_state.edit_tx_id = tx['id']
                                st.rerun()
                        with c_del:
                            if st.button("🗑️ Sil", key=f"del_tx_btn_{tx['id']}", use_container_width=True):
                                st.session_state.confirm_delete_id = tx['id']
                                st.rerun()

                if tx_total_pages > 1:
                    tp1, tp2, tp3, tp4, tp5 = st.columns([4, 1, 2, 1, 4])
                    with tp2:
                        if st.button("◀️", key="prev_tx_hist", disabled=(tx_curr_page == 0)):
                            st.session_state.tx_hist_page -= 1
                            st.rerun()
                    with tp3:
                        st.markdown(f"<div style='text-align: center; font-weight: 600; font-size: 0.88rem; padding-top: 6px;'>Sayfa {tx_curr_page + 1} / {tx_total_pages} ({len(filtered_txs)} işlem)</div>", unsafe_allow_html=True)
                    with tp4:
                        if st.button("▶️", key="next_tx_hist", disabled=(tx_curr_page == tx_total_pages - 1)):
                            st.session_state.tx_hist_page += 1
                            st.rerun()

    with tab_cats:
        edit_cat_id = st.session_state.get('edit_cat_id')
        show_cat_form = st.session_state.get('show_new_cat_form', False) or bool(edit_cat_id)

        cat_h1, cat_h2 = st.columns([3, 1])
        cat_h1.markdown(f"### 🏷️ Kategoriler ({len(categories)})")
        if not edit_cat_id:
            if cat_h2.button("✖️ Formu Kapat" if show_cat_form else "➕ Yeni Kategori Ekle", type="secondary" if show_cat_form else "primary", use_container_width=True, key="toggle_cat_form_btn"):
                st.session_state['show_new_cat_form'] = not show_cat_form
                st.rerun()

        if show_cat_form:
            edit_cat = next((c for c in categories if c['id'] == edit_cat_id), None) if edit_cat_id else None
            emoji_options = [
                "🧭", "🏃", "🗺️", "👟", "🏆", "🎓",
                "🌲", "🏕️", "🪵", "🎒", "🩹", "🏥",
                "🍕", "🥤", "🍽️", "👕", "🏠", "🚗",
                "🪙", "💰", "💵", "💳", "🤝", "🛒",
                "✈️", "🍿", "🎮", "🐾", "🎁", "🛠️",
            ]
            if edit_cat:
                st.markdown(f"#### ✏️ Kategoriyi Düzenle: {edit_cat['emoji']} {edit_cat['name']}")
                with st.form("edit_category_form", clear_on_submit=False):
                    cat_name = st.text_input("Kategori Adı", value=edit_cat['name'], key="edit_cat_name")
                    ec1, ec2, ec3 = st.columns([2, 1, 1])
                    cat_type = ec1.radio("Kategori Türü", ["Gider", "Gelir"], index=0 if edit_cat['type'] == 'Gider' else 1, horizontal=True, key="edit_cat_type")
                    cat_color = ec2.color_picker("Kategori Rengi", value=edit_cat['color'], key="edit_cat_color_picker")
                    default_emoji_idx = emoji_options.index(edit_cat['emoji']) if edit_cat['emoji'] in emoji_options else 0
                    selected_emoji = ec3.selectbox("Kategori Simgesi", options=emoji_options, index=default_emoji_idx, key="edit_cat_emoji_select")
                    submitted = st.form_submit_button("Değişiklikleri Kaydet", type="primary")
                canceled = st.button("İptal Et", key="cancel_edit_cat_btn")

                if submitted:
                    if not cat_name.strip():
                        st.error("Kategori adı boş olamaz!")
                    else:
                        update_category(edit_cat['id'], cat_name.strip(), selected_emoji, cat_color, cat_type)
                        st.session_state.edit_cat_id = None
                        st.session_state.show_new_cat_form = False
                        st.toast("Kategori güncellendi!", icon="✏️")
                        st.rerun()
                if canceled:
                    st.session_state.edit_cat_id = None
                    st.session_state.show_new_cat_form = False
                    st.rerun()
            else:
                st.markdown("#### ➕ Yeni Kategori Oluştur")
                with st.form("new_category_form", clear_on_submit=True):
                    cat_name = st.text_input("Kategori Adı", placeholder="Örn: Aidat, Ulaşım, Kamp", key="new_cat_name")
                    nc1, nc2, nc3 = st.columns([2, 1, 1])
                    cat_type = nc1.radio("Kategori Türü", ["Gider", "Gelir"], index=0, horizontal=True, key="new_cat_type")
                    cat_color = nc2.color_picker("Kategori Rengi", value="#EF4444", key="new_cat_color_picker")
                    selected_emoji = nc3.selectbox("Kategori Simgesi", options=emoji_options, index=0, key="new_cat_emoji_select")
                    submitted = st.form_submit_button("✅ Kategoriyi Ekle", type="primary", key="add_cat_btn")

                if submitted:
                    if not cat_name.strip():
                        st.error("Kategori adı boş olamaz!")
                    else:
                        add_category(cat_name.strip(), selected_emoji, cat_color, cat_type)
                        st.session_state.show_new_cat_form = False
                        st.toast(f"'{selected_emoji} {cat_name}' kategorisi oluşturuldu!", icon="✅")
                        st.rerun()
            st.markdown("---")

        if not categories:
            st.info("Kayıtlı kategori bulunamadı. Sağ üstteki **➕ Yeni Kategori Ekle** butonuna basarak oluşturabilirsiniz.")
        else:
            if st.session_state.get('confirm_delete_cat_id'):
                del_id = st.session_state.confirm_delete_cat_id
                del_c = next((c for c in categories if c['id'] == del_id), None)
                if del_c:
                    st.warning(f"⚠️ **Seçilen kategoriyi silmek istediğinize emin misiniz?**\n\n**Bu kategori silinirse bağlı TÜM işlemler de silinecektir!**\n\nKategori: **{del_c['emoji']} {del_c['name']}**")
                    col_yes, col_no = st.columns([1, 1])
                    with col_yes:
                        if st.button("Evet, Sil", key="btn_confirm_cat_del_yes", type="primary"):
                            delete_category(del_id)
                            if st.session_state.get('edit_cat_id') == del_id:
                                st.session_state.edit_cat_id = None
                            st.session_state.confirm_delete_cat_id = None
                            st.toast("Kategori silindi.", icon="🗑️")
                            st.rerun()
                    with col_no:
                        if st.button("İptal Et", key="btn_confirm_cat_del_no"):
                            st.session_state.confirm_delete_cat_id = None
                            st.rerun()
                else:
                    st.session_state.confirm_delete_cat_id = None

            cat_filter = st.radio("Tür Filtresi", ["Tümü", "🟢 Gelir", "🔴 Gider"], horizontal=True, label_visibility="collapsed", key="cat_list_filter")
            shown_cats = [
                c for c in categories
                if cat_filter == "Tümü"
                or (cat_filter == "🟢 Gelir" and c['type'] == "Gelir")
                or (cat_filter == "🔴 Gider" and c['type'] == "Gider")
            ]

            for c in shown_cats:
                lbl_color = "#10B981" if c['type'] == "Gelir" else "#EF4444"
                st.markdown(f"""
                <style>
                div[data-testid="stExpander"]:has(#cat-id-{c['id']}) {{
                    border-left: 5px solid {c['color']} !important;
                }}
                </style>
                """, unsafe_allow_html=True)

                exp_title = f"{c['emoji']} {c['name']} · **{c['type']}**"
                with st.expander(exp_title, expanded=False):
                    st.markdown(f'<div id="cat-id-{c["id"]}"></div>', unsafe_allow_html=True)
                    st.markdown(f"<div style='font-size: 0.82rem; color: #94A3B8; margin-bottom: 10px;'>Tür: <span style='color: {lbl_color}; font-weight: 700;'>{c['type']}</span> | Renk Kodu: <code>{c['color']}</code></div>", unsafe_allow_html=True)
                    c_edit, c_del = st.columns(2)
                    with c_edit:
                        if st.button("✏️ Düzenle", key=f"edit_cat_btn_{c['id']}", use_container_width=True):
                            st.session_state.edit_cat_id = c['id']
                            st.rerun()
                    with c_del:
                        if st.button("🗑️ Sil", key=f"del_cat_{c['id']}", use_container_width=True):
                            st.session_state.confirm_delete_cat_id = c['id']
                            st.rerun()


elif menu_selection == "🤝 Borç & Alacak":
    all_debts = get_debts()
    pending_debts = [d for d in all_debts if d['status'] == 'Bekliyor']
    paid_debts = [d for d in all_debts if d['status'] == 'Ödendi']
    edit_debt_id = st.session_state.get('edit_debt_id')
    show_debt_form = st.session_state.get('show_new_debt_form', False) or bool(edit_debt_id)

    hdr_c1, hdr_c2 = st.columns([3, 1])
    hdr_c1.title("🤝 Borç & Alacak")
    if not edit_debt_id:
        if hdr_c2.button("✖️ Formu Kapat" if show_debt_form else "➕ Yeni Borç/Alacak Ekle", type="secondary" if show_debt_form else "primary", use_container_width=True, key="toggle_debt_form_btn"):
            st.session_state['show_new_debt_form'] = not show_debt_form
            st.rerun()

    overdue_receivables = []
    overdue_payables = []
    today = today_date
    for d in pending_debts:
        if d['due_date']:
            try:
                due = datetime.strptime(d['due_date'], '%Y-%m-%d').date()
                if due <= today:
                    d['days_overdue'] = (today - due).days
                    if d['type'] == 'Alınacak':
                        overdue_receivables.append(d)
                    else:
                        overdue_payables.append(d)
            except Exception:
                pass

    total_overdue = len(overdue_payables) + len(overdue_receivables)
    if total_overdue > 0:
        with st.expander(f"🔔 Vadesi Gelen / Geçen {total_overdue} Kayıt", expanded=(total_overdue <= 2)):
            for d in overdue_payables:
                diff = d['days_overdue']
                time_str = "bugün!" if diff == 0 else f"{diff} gün gecikti!"
                st.error(f"🚨 **Ödeme Gecikmesi (Bizim Ödeyeceğimiz):** **{d['name']}** — **{d['amount']:,.2f} TL** ({time_str})")
            for d in overdue_receivables:
                diff = d['days_overdue']
                time_str = "bugün!" if diff == 0 else f"{diff} gün gecikti!"
                st.warning(f"⚠️ **Alacak Gecikmesi (Bize Ödenecek):** **{d['name']}** — **{d['amount']:,.2f} TL** ({time_str})")

    debt_totals = get_pending_debts_totals()
    total_payable = debt_totals['Verilecek']
    total_receivable = debt_totals['Alınacak']

    col_tot1, col_tot2 = st.columns(2)
    with col_tot1:
        st.markdown(f"""
        <div class="metric-card gider" style="text-align: center;">
            <span class="debt-summary-marker"></span>
            <div class="metric-title">🔴 Toplam Verilecek Borçlar</div>
            <div class="metric-value" style="color: #EF4444;">{total_payable:,.2f} TL</div>
            <div class="metric-sub">Dışarıya Gidecek Toplam Tutar</div>
        </div>
        """, unsafe_allow_html=True)
    with col_tot2:
        st.markdown(f"""
        <div class="metric-card" style="text-align: center;">
            <span class="debt-summary-marker"></span>
            <div class="metric-title">🟢 Toplam Alınacak Borçlar</div>
            <div class="metric-value" style="color: #10B981;">{total_receivable:,.2f} TL</div>
            <div class="metric-sub">Kasamıza Gelecek Toplam Tutar</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    categories = get_categories()

    if show_debt_form:
        edit_debt = next((d for d in all_debts if d['id'] == edit_debt_id), None) if edit_debt_id else None
        if edit_debt:
            st.markdown(f"### ✏️ Borç Kaydını Düzenle: {edit_debt['name']}")
            debt_type = st.radio("Borç Türü", ["Alınacak", "Verilecek"], index=0 if edit_debt['type'] == 'Alınacak' else 1, horizontal=True, key="edit_debt_type")
            cat_type_needed = "Gelir" if debt_type == "Alınacak" else "Gider"
            filtered_cats = [c for c in categories if c['type'] == cat_type_needed]
            has_due_date = st.checkbox("Vade Tarihi Var", value=edit_debt['due_date'] is not None, key="edit_debt_has_due")
            if has_due_date:
                try:
                    parsed_due = datetime.strptime(edit_debt['due_date'], '%Y-%m-%d').date()
                except Exception:
                    parsed_due = today_date
                debt_due_date = st.date_input("Vade Tarihi", parsed_due, key="edit_debt_due")
            else:
                debt_due_date = None

            with st.form("edit_debt_form", clear_on_submit=False):
                debt_name = st.text_input("Kişi / Kurum Adı", value=edit_debt['name'], key="edit_debt_name")
                debt_amount_str = st.text_input("Miktar (TL)", value=format_turkish_amount(edit_debt['amount']), key="edit_debt_amount")
                if filtered_cats:
                    cat_options = {c['id']: f"{c['emoji']} {c['name']}" for c in filtered_cats}
                    cat_ids = list(cat_options.keys())
                    default_cat_idx = cat_ids.index(edit_debt['category_id']) if edit_debt['category_id'] in cat_ids else 0
                    selected_cat_id = st.selectbox("Kategori", options=cat_ids, format_func=lambda x: cat_options[x], index=default_cat_idx, key="edit_debt_category")
                else:
                    st.error(f"Seçilen tür için ({cat_type_needed}) uygun bir kategori bulunamadı.")
                    selected_cat_id = None
                status_options = ["Bekliyor", "Ödendi"]
                default_status_idx = status_options.index(edit_debt['status']) if edit_debt['status'] in status_options else 0
                debt_status = st.selectbox("Durum", status_options, index=default_status_idx, key="edit_debt_status")
                submitted = st.form_submit_button("Değişiklikleri Kaydet", type="primary")
            canceled = st.button("İptal Et", key="cancel_edit_debt_btn")

            if submitted:
                debt_amount = parse_turkish_amount(debt_amount_str)
                if debt_amount is None or debt_amount <= 0:
                    st.error("Lütfen geçerli bir miktar girin (Örn: 1.000,50)")
                elif not debt_name.strip():
                    st.error("Kişi/Kurum adı boş bırakılamaz!")
                elif selected_cat_id is None:
                    st.error("Kategori seçimi zorunludur.")
                else:
                    due_date_str = debt_due_date.strftime('%Y-%m-%d') if has_due_date else None
                    update_debt(edit_debt['id'], debt_type, debt_amount, debt_name.strip(), due_date_str, debt_status, selected_cat_id)
                    st.session_state.edit_debt_id = None
                    st.session_state.show_new_debt_form = False
                    st.toast("Borç kaydı güncellendi!", icon="✏️")
                    st.rerun()
            if canceled:
                st.session_state.edit_debt_id = None
                st.session_state.show_new_debt_form = False
                st.rerun()
        else:
            st.markdown("### ➕ Yeni Borç / Alacak Kaydı")
            debt_type = st.radio("Borç Türü", ["Alınacak", "Verilecek"], horizontal=True, key="new_debt_type")
            cat_type_needed = "Gelir" if debt_type == "Alınacak" else "Gider"
            filtered_cats = [c for c in categories if c['type'] == cat_type_needed]
            has_due_date = st.checkbox("Vade Tarihi Var", key="new_debt_has_due")
            debt_due_date = st.date_input("Vade Tarihi", today_date, key="new_debt_due") if has_due_date else None

            known_members_list = get_known_members(all_debts)
            selected_quick_member = ""
            if known_members_list:
                quick_choice = st.selectbox(
                    "👥 Kayıtlı Kişi Havuzundan Seç (İsteğe Bağlı)",
                    options=["➕ Yeni Kişi / Kurum Yaz..."] + known_members_list,
                    key="quick_member_select_box",
                )
                if quick_choice != "➕ Yeni Kişi / Kurum Yaz...":
                    selected_quick_member = quick_choice

            with st.form("new_debt_form", clear_on_submit=True):
                debt_name = st.text_input("Kişi / Kurum Adı", value=selected_quick_member, placeholder="Örn: Ahmet Yılmaz, Spor Genel Md.", key="new_debt_name")
                debt_amount_str = st.text_input("Miktar (TL)", value="", key="new_debt_amount")
                if filtered_cats:
                    cat_options = {c['id']: f"{c['emoji']} {c['name']}" for c in filtered_cats}
                    selected_cat_id = st.selectbox("Kategori", options=list(cat_options.keys()), format_func=lambda x: cat_options[x], key="new_debt_category")
                else:
                    st.error(f"Seçilen tür için ({cat_type_needed}) uygun bir kategori bulunamadı.")
                    selected_cat_id = None
                submitted = st.form_submit_button("✅ Borç Kaydını Ekle", type="primary")

            if submitted:
                debt_amount = parse_turkish_amount(debt_amount_str)
                if debt_amount is None or debt_amount <= 0:
                    st.error("Lütfen geçerli bir miktar girin (Örn: 1.000,50)")
                elif not debt_name.strip():
                    st.error("Kişi/Kurum adı boş bırakılamaz!")
                elif selected_cat_id is None:
                    st.error("Kategori seçimi zorunludur.")
                else:
                    due_date_str = debt_due_date.strftime('%Y-%m-%d') if has_due_date else None
                    add_debt(debt_type, debt_amount, debt_name.strip(), due_date_str, "Bekliyor", selected_cat_id)
                    st.session_state.show_new_debt_form = False
                    st.toast("Borç kaydı eklendi!", icon="✅")
                    st.rerun()
        st.markdown("---")

    if not all_debts:
        st.info("Kayıtlı herhangi bir borç bulunamadı. Sağ üstteki **➕ Yeni Borç/Alacak Ekle** butonuna basarak ekleyebilirsiniz.")
    else:
        if st.session_state.get('confirm_delete_debt_id'):
            del_id = st.session_state.confirm_delete_debt_id
            del_d = next((d for d in all_debts if d['id'] == del_id), None)
            if del_d:
                st.warning(f"⚠️ **Seçilen borç kaydını silmek istediğinize emin misiniz?**\n\n**{del_d['name']} | {del_d['amount']:,.2f} TL | {del_d['type']}**")
                col_yes, col_no = st.columns([1, 1])
                with col_yes:
                    if st.button("Evet, Sil", key="btn_confirm_debt_del_yes", type="primary"):
                        delete_debt(del_id)
                        if st.session_state.get('edit_debt_id') == del_id:
                            st.session_state.edit_debt_id = None
                        st.session_state.confirm_delete_debt_id = None
                        st.toast("Borç kaydı silindi.", icon="🗑️")
                        st.rerun()
                with col_no:
                    if st.button("İptal Et", key="btn_confirm_debt_del_no"):
                        st.session_state.confirm_delete_debt_id = None
                        st.rerun()
            else:
                st.session_state.confirm_delete_debt_id = None

        tab_pending, tab_paid, tab_ledger = st.tabs([
            f"⏳ Bekleyenler ({len(pending_debts)})",
            f"✅ Ödenenler ({len(paid_debts)})",
            "👤 Kişi Bazlı Özet",
        ])

        with tab_pending:
            if not pending_debts:
                st.success("Bekleyen borcunuz bulunmuyor! Her şey dengede.")
            else:
                for d in pending_debts:
                    type_lbl = "🔴 Verilecek" if d['type'] == 'Verilecek' else "🟢 Alınacak"
                    vade_lbl = f" | 📅 Vade: {d['due_date']}" if d['due_date'] else " | 📅 Vade: Belirsiz"
                    badge_html = get_debt_badge_html(d['due_date'])
                    if d['category_name']:
                        st.markdown(f"""
                        <style>
                        div[data-testid="stExpander"]:has(#debt-id-{d['id']}) > details > summary em {{
                            background-color: {d['category_color']}15 !important;
                            color: {d['category_color']} !important;
                            border: 1px solid {d['category_color']}30 !important;
                            font-style: normal !important;
                            padding: 2px 8px !important;
                            border-radius: 8px !important;
                            font-size: 0.72rem !important;
                            font-weight: 600 !important;
                            display: inline-flex !important;
                            align-items: center !important;
                            gap: 4px !important;
                            white-space: nowrap !important;
                            margin-right: 6px !important;
                        }}
                        </style>
                        """, unsafe_allow_html=True)
                        cat_prefix = f"_{d['category_emoji']} {d['category_name']}_ · "
                    else:
                        cat_prefix = ""

                    with st.expander(f"{cat_prefix}{d['name']}  —  {d['amount']:,.2f} TL  {type_lbl.split()[0]}", expanded=False):
                        st.markdown(f'<div id="debt-id-{d["id"]}"></div>', unsafe_allow_html=True)
                        st.markdown(f"<div style='font-size: 0.82rem; color: #94A3B8; margin-bottom: 10px; display: flex; align-items: center; flex-wrap: wrap; gap: 6px;'><span>{type_lbl}{vade_lbl}</span>{badge_html}</div>", unsafe_allow_html=True)
                        c_paid, c_edit, c_del = st.columns(3)
                        with c_paid:
                            if st.button("✔️ Ödendi", key=f"pay_debt_{d['id']}", use_container_width=True):
                                if not d['category_id']:
                                    st.toast("⚠️ Önce bir kategori seçmelisiniz! (✏️ Düzenle butonunu kullanın)", icon="⚠️")
                                else:
                                    update_debt_status(d['id'], "Ödendi")
                                    st.toast("Borç ödendi olarak güncellendi.", icon="✅")
                                    st.rerun()
                        with c_edit:
                            if st.button("✏️ Düzenle", key=f"edit_debt_btn_{d['id']}", use_container_width=True):
                                st.session_state.edit_debt_id = d['id']
                                st.rerun()
                        with c_del:
                            if st.button("🗑️ Sil", key=f"del_debt_{d['id']}", use_container_width=True):
                                st.session_state.confirm_delete_debt_id = d['id']
                                st.rerun()

        with tab_paid:
            if not paid_debts:
                st.info("Ödenmiş borç geçmişi bulunmuyor.")
            else:
                for d in paid_debts:
                    type_lbl = "Verilecek" if d['type'] == 'Verilecek' else "Alınacak"
                    cat_prefix = f"_{d['category_emoji']} {d['category_name']}_ · " if d['category_name'] else ""
                    with st.expander(f"✅ {cat_prefix}{d['name']}  —  {d['amount']:,.2f} TL  ({type_lbl})", expanded=False):
                        st.markdown(f'<div id="debt-id-{d["id"]}"></div>', unsafe_allow_html=True)
                        c_unpay, c_edit, c_del = st.columns(3)
                        with c_unpay:
                            if st.button("⏪ Geri Al", key=f"unpay_debt_{d['id']}", use_container_width=True):
                                update_debt_status(d['id'], "Bekliyor")
                                st.toast("Borç tekrar bekliyor statüsüne alındı.", icon="⏪")
                                st.rerun()
                        with c_edit:
                            if st.button("✏️ Düzenle", key=f"edit_paid_debt_btn_{d['id']}", use_container_width=True):
                                st.session_state.edit_debt_id = d['id']
                                st.rerun()
                        with c_del:
                            if st.button("🗑️ Sil", key=f"del_paid_debt_{d['id']}", use_container_width=True):
                                st.session_state.confirm_delete_debt_id = d['id']
                                st.rerun()

        with tab_ledger:
            render_member_ledger(all_debts)


elif menu_selection == "⚙️ Ayarlar & Raporlar":
    st.title("⚙️ Ayarlar & Raporlar")
    tab_reports, tab_faiz_sim, tab_backup = st.tabs([
        "📊 Raporlar & Excel",
        "📈 Faiz & Simülasyon",
        "💾 Yedek & Sistem",
    ])

    with tab_reports:
        render_export_and_reports(
            transactions=get_transactions(),
            debts=get_debts(),
            current_vault=current_vault,
            total_interest_earned=total_interest_earned,
            accumulated_interest=accumulated_interest,
            monthly_flow=get_monthly_flow(),
        )

    with tab_faiz_sim:
        st.info(
            "💡 **ON Hesap Faiz Kuralı:** "
            "Kasadaki paranın Burgan ON limitlerine göre belirlenen 'vadesiz bakiye' kısmı faizden muaftır. "
            "Aşan tutar günlük bileşik faiz ile nemalandırılır. Faiz kazançlarından %17.5 stopaj vergisi kesilmektedir."
        )
        logs = get_rate_logs()
        show_rate_form = st.session_state.get('show_new_rate_form', False)
        r_col1, r_col2 = st.columns([3, 1])
        r_col1.markdown(f"### 📋 Kayıtlı Faiz Oranları ({len(logs)})")
        if r_col2.button("✖️ Kapat" if show_rate_form else "➕ Yeni Faiz Oranı Ekle", type="secondary" if show_rate_form else "primary", use_container_width=True, key="toggle_rate_form_btn"):
            st.session_state['show_new_rate_form'] = not show_rate_form
            st.rerun()

        if show_rate_form:
            with st.form("new_rate_log_form", clear_on_submit=True):
                log_date = st.date_input("Geçerlilik Başlangıç Tarihi", today_date, max_value=today_date)
                log_rate = st.selectbox("Yıllık Faiz Oranı", [0.41, 0.45], format_func=lambda x: f"%{int(x*100)}")
                if st.form_submit_button("✅ Faiz Değişikliğini Kaydet", type="primary"):
                    date_str = log_date.strftime('%Y-%m-%d')
                    add_or_update_rate_log(date_str, log_rate)
                    st.session_state['show_new_rate_form'] = False
                    st.toast(f"{date_str} itibarıyla faiz oranı %{int(log_rate*100)} kaydedildi!", icon="✅")
                    st.rerun()
            st.markdown("---")

        if not logs:
            st.info("Kayıtlı faiz logu bulunamadı.")
        else:
            for l in logs:
                col_info, col_action = st.columns([6, 1])
                with col_info:
                    st.markdown(f"""
                    <div style="padding: 10px 14px; border: 1px solid rgba(148,163,184,0.25); border-radius: 12px; margin-bottom: 8px;">
                        <strong>📅 Başlangıç Tarihi:</strong> {l['date']} &nbsp;|&nbsp;
                        <strong>📈 Faiz Oranı (Yıllık):</strong> %{int(l['rate']*100)}
                    </div>
                    """, unsafe_allow_html=True)
                with col_action:
                    if len(logs) > 1:
                        if st.button("🗑️ Sil", key=f"del_rate_{l['id']}", use_container_width=True):
                            delete_rate_log(l['id'])
                            st.toast("Faiz kaydı silindi.", icon="🗑️")
                            st.rerun()

        st.markdown("---")
        st.markdown("### 🔍 Simülasyon Günlük Detayları (Son 30 Gün)")
        if not daily_log:
            st.info("Henüz simüle edilecek veri bulunmuyor.")
        else:
            df_full = pd.DataFrame(daily_log).rename(columns={
                'date': 'Tarih',
                'vault_before_interest': 'Gün Başı Kasa (TL)',
                'income': 'Giren Para (TL)',
                'expense': 'Çıkan Para (TL)',
                'vadesiz_limit': 'Vadesiz Bakiye (TL)',
                'earning_base': 'Faiz İşleyen Matrah (TL)',
                'interest_earned': 'Net Faiz Getirisi (TL)',
                'vault_after_interest': 'Gün Sonu Kasa (TL)',
                'rate': 'Uygulanan Faiz Oranı',
            })
            df_full['Uygulanan Faiz Oranı'] = df_full['Uygulanan Faiz Oranı'].apply(lambda x: f"%{x*100:.1f}" if x > 0 else "%0.0")
            df_last_30 = df_full.tail(30).iloc[::-1]
            st.dataframe(
                df_last_30,
                column_config={
                    "Gün Başı Kasa (TL)": st.column_config.NumberColumn(format="%.2f TL"),
                    "Giren Para (TL)": st.column_config.NumberColumn(format="%.2f TL"),
                    "Çıkan Para (TL)": st.column_config.NumberColumn(format="%.2f TL"),
                    "Vadesiz Bakiye (TL)": st.column_config.NumberColumn(format="%.2f TL"),
                    "Faiz İşleyen Matrah (TL)": st.column_config.NumberColumn(format="%.2f TL"),
                    "Net Faiz Getirisi (TL)": st.column_config.NumberColumn(format="%.4f TL"),
                    "Gün Sonu Kasa (TL)": st.column_config.NumberColumn(format="%.2f TL"),
                },
                hide_index=True,
                use_container_width=True,
            )

    with tab_backup:
        st.markdown("### ⚡ Sistem Sağlığı & Bağlantı")
        if is_using_supabase():
            st.markdown("""
            <div style="background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 12px; padding: 14px; margin-bottom: 12px;">
                <div style="color: #10B981; font-weight: 700; font-size: 0.95rem;">🟢 Bulut Veritabanı: Supabase Aktif</div>
                <div style="font-size: 0.8rem; color: #94A3B8; margin-top: 2px;">Canlı veritabanı senkronize ve Turbo Önbellek devrede.</div>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.markdown("""
            <div style="background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.3); border-radius: 12px; padding: 14px; margin-bottom: 12px;">
                <div style="color: #F59E0B; font-weight: 700; font-size: 0.95rem;">🟡 Yerel Veritabanı: SQLite Aktif</div>
                <div style="font-size: 0.8rem; color: #94A3B8; margin-top: 2px;">Çevrimdışı / Yedek çalışma modu devrede.</div>
            </div>
            """, unsafe_allow_html=True)

        sys_c1, sys_c2, sys_c3 = st.columns(3)
        with sys_c1:
            if is_using_supabase():
                if st.button("📂 Yerel Moda Geç", key="switch_to_offline_btn", use_container_width=True):
                    st.session_state["force_offline_mode"] = True
                    invalidate_cache()
                    st.toast("Yerel SQLite moduna geçildi.", icon="📂")
                    st.rerun()
            elif USE_SUPABASE:
                if st.button("☁️ Bulut (Supabase) Modunu Dene", key="switch_to_cloud_btn", use_container_width=True):
                    st.session_state["force_offline_mode"] = False
                    invalidate_cache()
                    st.toast("Bulut modu kontrol ediliyor...", icon="☁️")
                    st.rerun()
        with sys_c2:
            if st.button("🧹 Önbelleği Sıfırla", key="btn_clear_cache", use_container_width=True):
                invalidate_cache()
                st.toast("Önbellek temizlendi!", icon="⚡")
                st.rerun()
        with sys_c3:
            if st.button("🩺 Bağlantı Testi (Ping)", key="btn_test_db", use_container_width=True):
                is_ok, lat, msg = test_supabase_connection()
                if is_ok:
                    st.toast(f"Supabase Aktif: {lat}ms", icon="🟢")
                else:
                    st.toast(f"{msg}", icon="🔴")

        st.markdown("---")
        try:
            _sb_client = SupabaseClient(SUPABASE_URL, SUPABASE_KEY) if is_using_supabase() else None
            render_backup_sync_ui(
                categories=get_categories(),
                transactions=get_transactions(),
                debts=get_debts(),
                rate_logs=get_rate_logs(),
                is_using_supabase_Flag=is_using_supabase(),
                supabase_client=_sb_client,
            )
        except Exception as _backup_err:
            st.error(f"Yedekleme paneli yüklenemedi: {_backup_err}")
