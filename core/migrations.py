"""Idempotent database bootstrap and startup migrations."""

import os
import database


def _add_column_if_missing(conn, table, column, sql_type, msg=None):
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
        if msg:
            print(msg)
        return True
    return False


def run_startup_migrations():
    """Ensure SQLite schema tables and legacy columns exist without altering existing data."""
    if not os.path.exists(database.DB_NAME):
        database.init_db()

    conn = database.get_db_connection()
    try:
        added_tgl = _add_column_if_missing(
            conn, 'anggota', 'tanggal_input', 'TEXT',
            "Migrating database: adding tanggal_input to anggota...",
        )
        if added_tgl:
            conn.execute(
                "UPDATE anggota SET tanggal_input = date(masa_berlaku, '-4 years') "
                "WHERE masa_berlaku IS NOT NULL AND tanggal_input IS NULL"
            )

        _add_column_if_missing(
            conn, 'anggota', 'is_tesis', 'INTEGER DEFAULT 0',
            "Migrasi: Kolom is_tesis berhasil ditambahkan ke tabel anggota.",
        )

        conn.execute('''CREATE TABLE IF NOT EXISTS pengaturan_sistem (
            kunci TEXT PRIMARY KEY,
            nilai TEXT
        )''')

        conn.execute('''CREATE TABLE IF NOT EXISTS stock_opname_session (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nama_sesi TEXT NOT NULL,
            start_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            end_date DATETIME,
            status TEXT DEFAULT 'AKTIF'
        )''')

        conn.execute('''CREATE TABLE IF NOT EXISTS stock_opname_scan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            no_induk TEXT,
            scan_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'DITEMUKAN',
            FOREIGN KEY (session_id) REFERENCES stock_opname_session(id)
        )''')

        conn.execute('''CREATE TABLE IF NOT EXISTS reservasi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id TEXT NOT NULL,
            biblio_id INTEGER NOT NULL,
            tanggal_reservasi DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'MENUNGGU'
        )''')

        conn.commit()
    except Exception as e:
        print("Migration error:", e)
    finally:
        conn.close()
