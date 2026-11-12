import sqlite3
import os
import threading

DB_NAME = os.environ.get('DATABASE_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'katalog.db'))
_wal_lock = threading.Lock()
_wal_initialized = False


def _ensure_wal(conn):
    global _wal_initialized
    if _wal_initialized:
        return
    with _wal_lock:
        if not _wal_initialized:
            row = conn.execute('PRAGMA journal_mode;').fetchone()
            mode = (row[0] if row else '').lower()
            if mode != 'wal':
                conn.execute('PRAGMA journal_mode = WAL;')
            _wal_initialized = True


def get_db_connection():
    conn = sqlite3.connect(DB_NAME, timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout = 30000;')
        _ensure_wal(conn)
        conn.execute('PRAGMA synchronous = NORMAL;')
        try:
            from flask import has_app_context, g
            if has_app_context():
                conns = getattr(g, '_managed_db_conns', None)
                if conns is None:
                    conns = []
                    g._managed_db_conns = conns
                conns.append(conn)
        except Exception:
            pass
        return conn
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise


def init_db():
    conn = get_db_connection()
    try:
        conn.execute('PRAGMA journal_mode=WAL;')
        cursor = conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS buku (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            no_induk TEXT UNIQUE NOT NULL,
            judul TEXT NOT NULL,
            gmd TEXT DEFAULT 'Text',
            edisi TEXT,
            isbn TEXT,
            penerbit TEXT,
            tahun_terbit TEXT,
            deskripsi_fisik TEXT,
            judul_seri TEXT,
            klasifikasi TEXT,
            cutter TEXT,
            huruf_judul TEXT,
            bahasa TEXT DEFAULT 'Indonesia',
            tempat_terbit TEXT,
            copy_ke INTEGER DEFAULT 1,
            catatan TEXT,
            pengarang TEXT,
            subjek TEXT,
            status_buku TEXT DEFAULT 'BELI',
            lokasi TEXT DEFAULT 'IMAVI',
            tgl_terima TEXT
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS buku_dibaca (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            no_induk TEXT NOT NULL,
            waktu_baca DATETIME DEFAULT CURRENT_TIMESTAMP,
            tanggal DATE DEFAULT (DATE('now', 'localtime')),
            FOREIGN KEY (no_induk) REFERENCES buku(no_induk)
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS evaluasi_semester (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            periode_label TEXT NOT NULL,
            tgl_evaluasi DATE NOT NULL,
            total_koleksi_imavi INTEGER,
            total_baca_inhouse INTEGER,
            subjek_terpopuler TEXT,
            subjek_defisit TEXT,
            laporan_narasi_ai TEXT
        )
        """)

        cursor.execute("""CREATE TABLE IF NOT EXISTS pengaturan_sistem (
            kunci TEXT PRIMARY KEY,
            nilai TEXT
        )""")

        cursor.execute("""CREATE TABLE IF NOT EXISTS stock_opname_session (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nama_sesi TEXT NOT NULL,
            start_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            end_date DATETIME,
            status TEXT DEFAULT 'AKTIF'
        )""")

        cursor.execute("""CREATE TABLE IF NOT EXISTS stock_opname_scan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            no_induk TEXT,
            scan_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'DITEMUKAN',
            FOREIGN KEY (session_id) REFERENCES stock_opname_session(id)
        )""")

        conn.commit()
    finally:
        conn.close()


if __name__ == '__main__':
    print("Membaca dan menginisialisasi database...")
    init_db()
    print("Database berhasil diinisialisasi.")
