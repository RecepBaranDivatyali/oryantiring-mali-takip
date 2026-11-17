# -*- coding: utf-8 -*-
"""
Yedekleme ve Senkronizasyon Modülü (utils/backup_sync.py)
---------------------------------------------------------
- Tam kapsamlı JSON yedekleme oluşturma (tek tıkla indirme için)
- Supabase bulut veritabanından yerel SQLite (ekonomi.db) veritabanına atomik senkronizasyon
- Streamlit arayüz bileşeni (render_backup_sync_ui)
"""

import json
import sqlite3
from datetime import datetime
import streamlit as st


def create_json_backup(categories, transactions, debts, rate_logs) -> str:
    """
    Tüm veritabanı verilerini zaman damgalı, tam kapsamlı bir JSON string olarak döndürür.
    Kullanıcı tek tıkla bilgisayarında .json yedek dosyası indirebilir.
    """
    categories_list = list(categories) if categories else []
    transactions_list = list(transactions) if transactions else []
    debts_list = list(debts) if debts else []
    rate_logs_list = list(rate_logs) if rate_logs else []

    payload = {
        "metadata": {
            "application": "Oryantiring Takımı Mali Takip Sistemi",
            "schema_version": "1.0",
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "counts": {
                "categories": len(categories_list),
                "transactions": len(transactions_list),
                "debts": len(debts_list),
                "interest_rate_logs": len(rate_logs_list),
            },
        },
        "data": {
            "categories": categories_list,
            "transactions": transactions_list,
            "debts": debts_list,
            "interest_rate_logs": rate_logs_list,
        },
    }

    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _ensure_sqlite_schema(cursor: sqlite3.Cursor) -> None:
    """Yerel SQLite veritabanında gerekli tabloların ve sütunların varlığını garanti eder."""
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        emoji TEXT NOT NULL,
        color TEXT NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('Gelir', 'Gider'))
    )
    """)

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

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS interest_rate_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL UNIQUE,
        rate REAL NOT NULL
    )
    """)

    # Mevcut eski veritabanları için sütun kontrolü
    cursor.execute("PRAGMA table_info(transactions)")
    cols_tx = [row[1] for row in cursor.fetchall()]
    if "time_range" not in cols_tx:
        cursor.execute(
            "ALTER TABLE transactions ADD COLUMN time_range TEXT NOT NULL DEFAULT '05:00 - 18:15'"
        )
    if "debt_id" not in cols_tx:
        cursor.execute(
            "ALTER TABLE transactions ADD COLUMN debt_id INTEGER REFERENCES debts(id) ON DELETE CASCADE"
        )

    cursor.execute("PRAGMA table_info(debts)")
    cols_debt = [row[1] for row in cursor.fetchall()]
    if "category_id" not in cols_debt:
        cursor.execute(
            "ALTER TABLE debts ADD COLUMN category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL"
        )


def sync_supabase_to_sqlite(supabase_client, sqlite_db_path: str = "ekonomi.db") -> tuple[bool, str]:
    """
    Supabase'deki `ory_categories`, `ory_transactions`, `ory_debts`, `ory_interest_rate_logs`
    verilerini çekip yerel `ekonomi.db` SQLite tablolarına (`categories`, `transactions`,
    `debts`, `interest_rate_logs`) güvenli ve atomik bir şekilde senkronize eder.

    Dönüş:
        (başarılı_mı: bool, mesaj: str)
    """
    if supabase_client is None:
        return False, "Supabase istemcisi (supabase_client) sağlanmadı veya bağlantı kurulamadı."

    try:
        # 1. Supabase'den tüm tabloları çek (hata olursa yerel veritabanına hiç dokunulmaz)
        supa_categories = supabase_client._get(
            "ory_categories", params={"select": "*", "order": "id.asc"}
        ) or []
        supa_debts = supabase_client._get(
            "ory_debts", params={"select": "*", "order": "id.asc"}
        ) or []
        supa_transactions = supabase_client._get(
            "ory_transactions", params={"select": "*", "order": "id.asc"}
        ) or []
        supa_rate_logs = supabase_client._get(
            "ory_interest_rate_logs", params={"select": "*", "order": "date.asc"}
        ) or []
    except Exception as exc:
        return False, f"Supabase verileri çekilirken hata oluştu: {exc}"

    conn = None
    try:
        conn = sqlite3.connect(sqlite_db_path)
        cursor = conn.cursor()

        # Tablo şemalarını hazırla
        _ensure_sqlite_schema(cursor)

        # Senkronizasyon esnasında sıralı silme/ekleme için foreign_keys geçici kapatılır
        cursor.execute("PRAGMA foreign_keys = OFF;")
        cursor.execute("BEGIN TRANSACTION;")

        # Mevcut yerel kayıtları temizle
        cursor.execute("DELETE FROM transactions;")
        cursor.execute("DELETE FROM debts;")
        cursor.execute("DELETE FROM categories;")
        cursor.execute("DELETE FROM interest_rate_logs;")

        # 1. Kategorileri aktar
        for cat in supa_categories:
            cursor.execute(
                """
                INSERT INTO categories (id, name, emoji, color, type)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    cat.get("id"),
                    cat.get("name", "Kategori"),
                    cat.get("emoji", "📁"),
                    cat.get("color", "#3b82f6"),
                    cat.get("type", "Gider"),
                ),
            )

        # 2. Borçları aktar
        for debt in supa_debts:
            cursor.execute(
                """
                INSERT INTO debts (id, type, amount, name, due_date, status, category_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    debt.get("id"),
                    debt.get("type", "Verilecek"),
                    float(debt.get("amount", 0.0)),
                    debt.get("name", ""),
                    debt.get("due_date"),
                    debt.get("status", "Bekliyor"),
                    debt.get("category_id"),
                ),
            )

        # 3. İşlemleri aktar
        for tx in supa_transactions:
            cursor.execute(
                """
                INSERT INTO transactions (id, date, category_id, amount, description, time_range, debt_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx.get("id"),
                    tx.get("date"),
                    tx.get("category_id"),
                    float(tx.get("amount", 0.0)),
                    tx.get("description", ""),
                    tx.get("time_range") or "05:00 - 18:15",
                    tx.get("debt_id"),
                ),
            )

        # 4. Faiz oranı geçmişini aktar
        for log in supa_rate_logs:
            cursor.execute(
                """
                INSERT OR REPLACE INTO interest_rate_logs (id, date, rate)
                VALUES (?, ?, ?)
                """,
                (
                    log.get("id"),
                    log.get("date"),
                    float(log.get("rate", 0.41)),
                ),
            )

        # Eğer faiz tablosu boşsa varsayılan değeri koru
        if not supa_rate_logs:
            cursor.execute(
                "INSERT OR IGNORE INTO interest_rate_logs (date, rate) VALUES (?, ?)",
                ("2026-01-01", 0.41),
            )

        conn.commit()
        cursor.execute("PRAGMA foreign_keys = ON;")

        summary_msg = (
            f"Senkronizasyon tamamlandı ({sqlite_db_path}): "
            f"{len(supa_categories)} kategori, "
            f"{len(supa_transactions)} işlem, "
            f"{len(supa_debts)} borç/alacak, "
            f"{len(supa_rate_logs)} faiz kaydı yerel veritabanına aktarıldı."
        )
        return True, summary_msg

    except Exception as exc:
        if conn is not None:
            conn.rollback()
        return False, f"Yerel SQLite senkronizasyon hatası: {exc}"
    finally:
        if conn is not None:
            conn.close()


def render_backup_sync_ui(
    categories,
    transactions,
    debts,
    rate_logs,
    is_using_supabase_Flag: bool,
    supabase_client=None,
) -> None:
    """
    Streamlit sidebar veya ayarlar sayfasında gösterilebilecek şık bir
    '💾 Veri Yedekleme & Senkronizasyon' bileşeni.
    """
    with st.expander("💾 Veri Yedekleme & Senkronizasyon", expanded=False):
        st.caption(
            "Verilerinizi yerel JSON dosyası olarak yedekleyebilir veya Supabase bulut "
            "verilerinizi çevrimdışı kullanım için yerel `ekonomi.db` dosyasına eşitleyebilirsiniz."
        )

        # JSON yedek verisini oluştur
        backup_json_str = create_json_backup(categories, transactions, debts, rate_logs)
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"oryantiring_mali_yedek_{timestamp_str}.json"

        st.download_button(
            label="📥 JSON Yedek İndir (.json)",
            data=backup_json_str.encode("utf-8"),
            file_name=backup_filename,
            mime="application/json",
            use_container_width=True,
            help="Tüm kategori, işlem, borç/alacak ve faiz geçmişini zaman damgalı JSON dosyası olarak indirir.",
        )

        # Buluttan Yerel SQLite'a Senkronize Et butonu
        sync_disabled = not (is_using_supabase_Flag and supabase_client is not None)
        help_text = (
            "Supabase bulut veritabanındaki tüm güncel verileri yerel 'ekonomi.db' SQLite dosyasına kopyalar."
            if not sync_disabled
            else "Bu özellik yalnızca Supabase bulut bağlantısı aktifken kullanılabilir."
        )

        if st.button(
            "🔄 Buluttan Yerel SQLite'a Senkronize Et",
            use_container_width=True,
            disabled=sync_disabled,
            help=help_text,
        ):
            with st.spinner("Supabase verileri yerel SQLite veritabanına aktarılıyor..."):
                ok, msg = sync_supabase_to_sqlite(supabase_client, sqlite_db_path="ekonomi.db")
            if ok:
                st.success(f"✅ {msg}")
            else:
                st.error(f"❌ {msg}")
