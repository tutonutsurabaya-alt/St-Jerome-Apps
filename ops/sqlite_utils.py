"""Shared SQLite snapshot and integrity verification helpers for St. Jerome Apps."""

import gzip
import os
import shutil
import sqlite3
import tempfile
from urllib.parse import quote


CORE_TABLES = (
    'buku',
    'bibliografi',
    'eksemplar',
    'sirkulasi',
    'anggota',
    'pos_transaksi',
    'users',
)


def get_db_path():
    """Return active SQLite database path, respecting DATABASE_PATH env override."""
    env_path = os.environ.get('DATABASE_PATH', '').strip()
    if env_path:
        return os.path.abspath(env_path)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, 'katalog.db')


def connect_readonly(db_path, timeout=10.0):
    """Open a read-only SQLite connection without creating WAL/SHM writes when unnecessary."""
    abs_path = os.path.abspath(db_path)
    uri = f"file:{quote(abs_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)};")
    return conn


def inspect_snapshot_db(db_path):
    """Verify PRAGMA integrity_check on a snapshot file and return core table row counts."""
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=30.0)
        row = conn.execute("PRAGMA integrity_check;").fetchone()
        status = (row[0] if row else '').strip().lower()
        if status != 'ok':
            raise RuntimeError(f"PRAGMA integrity_check failed: {row[0] if row else 'empty'}")

        existing = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        counts = {}
        for table in CORE_TABLES:
            if table in existing:
                counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        return counts
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def create_verified_snapshot(src_path, dst_path):
    """Create a WAL-consistent SQLite backup snapshot and verify integrity_check."""
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"Source database not found: {src_path}")

    parent = os.path.dirname(os.path.abspath(dst_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    src_conn = None
    dst_conn = None
    try:
        src_conn = sqlite3.connect(src_path, timeout=30.0)
        dst_conn = sqlite3.connect(dst_path, timeout=30.0)
        src_conn.backup(dst_conn)
        dst_conn.commit()
    finally:
        if dst_conn is not None:
            try:
                dst_conn.close()
            except Exception:
                pass
        if src_conn is not None:
            try:
                src_conn.close()
            except Exception:
                pass

    counts = inspect_snapshot_db(dst_path)
    return os.path.getsize(dst_path), counts


def verify_gzip_restore(gz_path):
    """Decompress a .db.gz backup to a temporary file and run PRAGMA integrity_check."""
    with tempfile.TemporaryDirectory(prefix="stjerome_restore_chk_") as tmpdir:
        tmp_db = os.path.join(tmpdir, "restore_check.db")
        with gzip.open(gz_path, 'rb') as src_f, open(tmp_db, 'wb') as dst_f:
            shutil.copyfileobj(src_f, dst_f)
        return inspect_snapshot_db(tmp_db)
