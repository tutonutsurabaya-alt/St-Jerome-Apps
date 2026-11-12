"""Build cross-platform offline portable package for Perpustakaan STPD.

Creates an isolated copy of St. Jerome Apps in ``/root/st_jerome_releases``
scoped strictly to STPD data without mutating or locking production IMAVI DB.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
from io import BytesIO
import os
import random
import shutil
import sqlite3
import stat
import tarfile
import threading
import time
import zipfile

from PIL import Image
import requests

SRC_ROOT = "/root/st_jerome_web"
DEFAULT_RELEASE_DIR = "/root/st_jerome_releases"
BUNDLE_DIRNAME = "st_jerome_stpd_offline"

ROOT_CODE_FILES = (
    "app.py",
    "database.py",
    "pos_routes.py",
    "template_stiker.py",
    "template_kartu.py",
    "slims_bridge.py",
    "ddc_kamus.json",
    "1.png",
    "2.png",
)

CODE_DIRS = ("core", "modules", "templates", "ops")

CLEAR_TABLES = (
    "audit_rak",
    "evaluasi_semester",
    "kurikulum_matakuliah",
    "kurikulum_pustaka",
    "kurikulum_usulan_pengadaan",
    "pencarian_gagal",
    "pos_antrean_print",
    "pos_buku_kas",
    "pos_mutasi_saldo",
    "pos_tabungan",
    "pos_transaksi",
    "pos_transaksi_detail",
    "reservasi",
    "sjla_visitor_logs",
    "stock_opname_scan",
    "stock_opname_session",
    "usulan_buku",
)

SECRET_SETTING_KEYS = (
    "CF_TURNSTILE_SITE_KEY",
    "CF_TURNSTILE_SECRET_KEY",
    "GEMINI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "FLASK_SECRET_KEY",
)

STPD_OFFLINE_SETTINGS = (
    ("APP_MODE", "offline_portable"),
    ("LIBRARY_NAME", "Perpustakaan STPD"),
    ("DEFAULT_LOKASI", "STPD"),
    ("ENABLE_WA_BOT", "0"),
    ("ENABLE_TELEGRAM_OPS", "0"),
    ("ENABLE_POS_FOTOCOPY", "0"),
    ("ENABLE_AI_ASSISTANT", "0"),
    ("ENABLE_AUTOCOVER_ONLINE", "0"),
    ("ENABLE_SYNC_JADWAL_PROXY", "0"),
)

REQUIREMENTS_OFFLINE = """\
Flask>=3.0.0
Werkzeug>=3.0.0
waitress>=3.0.0
reportlab>=4.0.0
Pillow>=10.0.0
pandas>=2.0.0
openpyxl>=3.1.0
requests>=2.28.0
pypdf>=3.0.0
python-dotenv>=1.0.0
"""

RUN_OFFLINE_PY = """\
#!/usr/bin/env python3
\"\"\"Cross-platform offline runner for St. Jerome Apps (Perpustakaan STPD).\"\"\"

import argparse
import os
import socket
import sys
import threading
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _configure_env():
    os.chdir(BASE_DIR)
    os.environ.setdefault("STJEROME_APP_MODE", "offline_portable")
    os.environ.setdefault("STJEROME_LIBRARY_NAME", "Perpustakaan STPD")
    os.environ.setdefault("STJEROME_DEFAULT_LOKASI", "STPD")
    os.environ.setdefault("STJEROME_DISABLE_ALERTS", "1")
    os.environ.setdefault("STJEROME_SESSION_COOKIE_SECURE", "0")
    os.environ.setdefault("DATABASE_PATH", os.path.join(BASE_DIR, "katalog.db"))


def _detect_lan_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect(("10.255.255.255", 1))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="St. Jerome Offline Portable (Perpustakaan STPD)")
    parser.add_argument("--host", default=os.environ.get("STJEROME_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5050")))
    parser.add_argument("--no-browser", action="store_true", help="Jangan buka browser otomatis")
    args = parser.parse_args()

    _configure_env()
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)

    from app import create_app

    flask_app = create_app()
    local_url = f"http://127.0.0.1:{args.port}"
    lan_ip = _detect_lan_ip()

    print("=" * 64)
    print("  St. Jerome Apps — Mode Offline Portable")
    print("  Perpustakaan : Perpustakaan STPD")
    print(f"  Database     : {os.environ['DATABASE_PATH']}")
    print(f"  Buka Browser : {local_url}")
    if lan_ip and lan_ip != "127.0.0.1":
        print(f"  Akses Wi-Fi  : http://{lan_ip}:{args.port}")
    print("  Tekan Ctrl+C untuk menghentikan aplikasi dengan aman.")
    print("=" * 64)

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(local_url)).start()

    try:
        from waitress import serve

        serve(flask_app, host=args.host, port=args.port, threads=8)
    except ImportError:
        flask_app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
"""

START_WINDOWS_BAT = """\
@echo off
setlocal
cd /d "%~dp0"
title St. Jerome Apps - Perpustakaan STPD (Offline)

echo ============================================================
echo   St. Jerome Apps - Perpustakaan STPD (Mode Offline)
echo   Zero XAMPP • Berjalan langsung dengan Python
echo ============================================================
echo.

set "PY_CMD="
where py >nul 2>nul && (py -3 --version >nul 2>nul && set "PY_CMD=py -3")
if "%PY_CMD%"=="" (
    where python >nul 2>nul && set "PY_CMD=python"
)

if "%PY_CMD%"=="" (
    echo [ERROR] Python 3 belum terpasang di komputer ini.
    echo Silakan instal Python 3 dari https://www.python.org/downloads/
    echo ^(Centang "Add Python to PATH" saat instalasi^).
    pause
    exit /b 1
)

if not exist ".venv\\Scripts\\python.exe" (
    echo [*] Menyiapkan environment Python lokal ^(.venv^)...
    %PY_CMD% -m venv .venv
    if errorlevel 1 (
        echo [INFO] Fallback menggunakan Python global...
        goto :run_global
    )
    echo [*] Menginstal dependensi offline pertama kali...
    ".venv\\Scripts\\python.exe" -m pip install --upgrade pip >nul 2>nul
    ".venv\\Scripts\\python.exe" -m pip install -r requirements_offline.txt
    if errorlevel 1 (
        echo [PERINGATAN] Gagal install ke .venv, mencoba Python global...
        goto :run_global
    )
)

".venv\\Scripts\\python.exe" run_offline.py %*
goto :end

:run_global
%PY_CMD% run_offline.py %*

:end
echo.
echo Server St. Jerome dihentikan.
pause
"""

START_LINUX_UBUNTU_SH = """\
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "============================================================"
echo "  St. Jerome Apps - Perpustakaan STPD (Ubuntu / Linux)"
echo "  Zero XAMPP • Mode Offline Portable"
echo "============================================================"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] python3 belum terpasang. Jalankan: sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "[*] Menyiapkan virtual environment lokal (.venv)..."
    if ! python3 -m venv .venv 2>/dev/null; then
        echo "[INFO] python3-venv tidak tersedia. Mencoba menginstal atau menggunakan python3 aktif..."
        python3 -m pip install --user -r requirements_offline.txt || {
            echo "[ERROR] Silakan jalankan sekali: sudo apt update && sudo apt install -y python3-venv python3-pip"
            exit 1
        }
        exec python3 run_offline.py "$@"
    fi
    echo "[*] Menginstal dependensi (Waitress, Flask, Pandas, Pillow, ReportLab)..."
    .venv/bin/pip install --upgrade pip >/dev/null 2>&1 || true
    .venv/bin/pip install -r requirements_offline.txt
fi

exec .venv/bin/python run_offline.py "$@"
"""

README_OFFLINE_MD = """\
# St. Jerome Apps — Edisi Offline Portable (Perpustakaan STPD)

Paket ini adalah edisi mandiri **St. Jerome Apps** untuk **Perpustakaan STPD**:
- Berjalan **100% offline tanpa internet dan tanpa XAMPP**.
- Database SQLite `katalog.db` sudah disaring khusus koleksi **STPD** + sampul lokal tersimpan di `static/uploads/`.
- Siap **Stock Opname / Scan Ulang Barcode Fisik** (`0003/21`, dll.), laporan buku hilang (termasuk **Export CSV Lengkap**), deteksi barcode **Belum Tercatat**, dan input buku baru dengan mempertahankan barcode fisik asli.

## 1. Cara Menjalankan di Windows
1. Ekstrak folder `st_jerome_stpd_offline` (misalnya ke `D:\\Perpustakaan_STPD` atau Desktop).
2. Pastikan Python 3 sudah terpasang (saat install Python centang *"Add Python to PATH"*).
3. Klik dua kali **`START_WINDOWS.bat`**.
   - Pada pembukaan pertama, skrip akan otomatis membuat `.venv` dan memasang dependensi Python (`Flask`, `waitress`, `pandas`, `openpyxl`, `Pillow`, `reportlab`, dll.).
   - Selanjutnya aplikasi langsung terbuka di browser pada `http://127.0.0.1:5050`.

## 2. Cara Menjalankan di Linux Ubuntu
1. Ekstrak folder `st_jerome_stpd_offline`.
2. Buka Terminal di folder tersebut lalu jalankan:
   ```bash
   chmod +x START_LINUX_UBUNTU.sh
   ./START_LINUX_UBUNTU.sh
   ```
   *(Jika Ubuntu baru dan `python3-venv` belum terpasang, instal sekali dengan `sudo apt update && sudo apt install -y python3-venv python3-pip`.)*
3. Browser akan otomatis membuka `http://127.0.0.1:5050` (atau akses dari HP/laptop lain di Wi-Fi lokal menggunakan alamat `Akses Wi-Fi` yang tampil di terminal).

## 3. Catatan Pemakaian Harian & Stock Opname
- Login pustakawan menggunakan akun admin (`yosef`) — tanpa CAPTCHA/Turnstile di mode offline.
- Untuk **Scan Ulang / Stock Opname**:
  1. Buka menu **Stock Opname / Audit Rak** (`/audit_rak`) dan klik **Mulai Sesi**.
  2. Buka **Scanner** (`/audit_rak/scanner`), gunakan preset **"Semua Rak (Scan Cepat / Mode Bebas)"** bila ingin men-scan cepat dengan USB barcode scanner atau kamera HP tanpa gonta-ganti nomor rak.
  3. Bila ada buku fisik yang barcodenya belum ada di katalog, scanner otomatis menyimpannya sebagai **Belum Tercatat** dan menyediakan tombol langsung **Input ke Katalog** sambil membawa nomor barcodenya.
  4. Setelah selesai menutup sesi, buka halaman laporan dan klik **Export CSV Lengkap** untuk mengunduh daftar lengkap buku hilang & barcode belum tercatat.
- Jangan lupa **Backup Database** secara berkala lewat menu **Pengaturan Sistem -> Download Backup** atau cukup salin file `katalog.db` dan folder `static/uploads/`.
"""


def _copy_code_and_static_base(bundle_dir):
    for rel in ROOT_CODE_FILES:
        src = os.path.join(SRC_ROOT, rel)
        if not os.path.exists(src):
            raise FileNotFoundError(f"Missing root file: {src}")
        shutil.copy2(os.path.realpath(src), os.path.join(bundle_dir, rel))

    for d in CODE_DIRS:
        src_dir = os.path.join(SRC_ROOT, d)
        dst_dir = os.path.join(bundle_dir, d)
        shutil.copytree(
            src_dir,
            dst_dir,
            symlinks=False,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.bak*", "*.db*", "health_monitor_state.json"),
        )

    # Static UI assets: vendor/, js/, css/, and top-level files in static/img/
    dst_static = os.path.join(bundle_dir, "static")
    os.makedirs(os.path.join(dst_static, "uploads"), exist_ok=True)

    for sub in ("vendor", "js", "css"):
        s_sub = os.path.join(SRC_ROOT, "static", sub)
        if os.path.exists(s_sub):
            shutil.copytree(s_sub, os.path.join(dst_static, sub), symlinks=False)

    src_img_dir = os.path.join(SRC_ROOT, "static", "img")
    dst_img_dir = os.path.join(dst_static, "img")
    os.makedirs(dst_img_dir, exist_ok=True)
    for fname in os.listdir(src_img_dir):
        fpath = os.path.join(src_img_dir, fname)
        if os.path.isfile(fpath):
            shutil.copy2(os.path.realpath(fpath), os.path.join(dst_img_dir, fname))

    for fname in os.listdir(os.path.join(SRC_ROOT, "static")):
        fpath = os.path.join(SRC_ROOT, "static", fname)
        if os.path.isfile(fpath):
            shutil.copy2(os.path.realpath(fpath), os.path.join(dst_static, fname))


def _snapshot_and_prune_db(dst_db_path):
    src_uri = f"file:{os.path.join(SRC_ROOT, 'katalog.db')}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True)
    dst_conn = sqlite3.connect(dst_db_path)
    try:
        src_conn.backup(dst_conn)
    finally:
        src_conn.close()

    try:
        # Filter collections strictly to STPD
        dst_conn.execute("DELETE FROM eksemplar WHERE lokasi != 'STPD' OR lokasi IS NULL")
        dst_conn.execute("DELETE FROM buku WHERE lokasi != 'STPD' OR lokasi IS NULL")
        dst_conn.execute("DELETE FROM bibliografi WHERE id NOT IN (SELECT DISTINCT biblio_id FROM eksemplar WHERE biblio_id IS NOT NULL)")

        # Prune circulation, in-house reading logs, and loans linked only to removed items
        dst_conn.execute("DELETE FROM sirkulasi WHERE no_induk NOT IN (SELECT no_induk FROM eksemplar)")
        dst_conn.execute("DELETE FROM peminjaman WHERE no_induk NOT IN (SELECT no_induk FROM eksemplar)")
        dst_conn.execute("DELETE FROM buku_dibaca WHERE no_induk NOT IN (SELECT no_induk FROM eksemplar)")

        # Keep STPD members + any member linked to remaining STPD sirkulasi
        dst_conn.execute("""
            DELETE FROM anggota
            WHERE (LOWER(COALESCE(institusi, '')) NOT LIKE '%providentia%'
                   AND LOWER(COALESCE(institusi, '')) NOT LIKE '%stpd%')
              AND member_id NOT IN (SELECT DISTINCT member_id FROM sirkulasi WHERE member_id IS NOT NULL)
        """)

        # Keep admin account(s); drop POS-only cashier user
        dst_conn.execute("DELETE FROM users WHERE LOWER(COALESCE(role, '')) != 'master admin' AND LOWER(username) != 'yosef'")

        existing_tables = {
            r[0] for r in dst_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for tbl in CLEAR_TABLES:
            if tbl in existing_tables:
                dst_conn.execute(f"DELETE FROM [{tbl}]")

        placeholders = ",".join("?" for _ in SECRET_SETTING_KEYS)
        dst_conn.execute(f"DELETE FROM pengaturan_sistem WHERE kunci IN ({placeholders})", SECRET_SETTING_KEYS)
        dst_conn.executemany(
            "INSERT OR REPLACE INTO pengaturan_sistem (kunci, nilai) VALUES (?, ?)",
            STPD_OFFLINE_SETTINGS,
        )
        dst_conn.commit()
    finally:
        dst_conn.close()


def _resolve_local_cover_source(img_val):
    val = (img_val or "").strip()
    if not val or val.startswith("http") or val in ("tidak_ada", "NOT_FOUND", "RATE_LIMIT"):
        return None, None
    rel = val.lstrip("/")
    if rel.startswith("static/"):
        rel = rel[len("static/"):]
    candidates = [
        (os.path.join(SRC_ROOT, "static", "uploads", rel), os.path.join("static", "uploads", rel)),
        (os.path.join(SRC_ROOT, "static", rel), os.path.join("static", rel)),
    ]
    for src_path, dst_rel in candidates:
        if os.path.exists(src_path):
            return os.path.realpath(src_path), dst_rel
    return None, None


def _normalize_cover_url(url):
    u = (url or "").strip()
    if "covers.openlibrary.org" in u and "default=false" not in u:
        sep = "&" if "?" in u else "?"
        u = f"{u}{sep}default=false"
    return u


_RATE_LOCK = threading.Lock()
_GLOBAL_COOLDOWN_UNTIL = 0.0


def _respect_cooldown():
    with _RATE_LOCK:
        wait = _GLOBAL_COOLDOWN_UNTIL - time.time()
    if wait > 0:
        time.sleep(wait)


def _trigger_cooldown(seconds):
    global _GLOBAL_COOLDOWN_UNTIL
    with _RATE_LOCK:
        target = time.time() + seconds
        if target > _GLOBAL_COOLDOWN_UNTIL:
            _GLOBAL_COOLDOWN_UNTIL = target


def _find_cached_cover(cache_dir, digest):
    if not cache_dir or not os.path.isdir(cache_dir):
        return None
    for ext in (".jpg", ".png", ".webp", ".gif"):
        cand = os.path.join(cache_dir, f"stpd_dl_{digest}{ext}")
        if os.path.exists(cand) and os.path.getsize(cand) > 0:
            return cand
    return None


def _download_one_cover(session, url, uploads_dir, cache_dir=None):
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    cached = _find_cached_cover(cache_dir, digest)
    if cached:
        fname = os.path.basename(cached)
        shutil.copy2(cached, os.path.join(uploads_dir, fname))
        return url, fname, "cached"

    req_url = _normalize_cover_url(url)
    last_status = "error"
    for attempt in range(5):
        _respect_cooldown()
        time.sleep(0.18 + random.uniform(0.02, 0.12))
        try:
            resp = session.get(req_url, timeout=18)
            if resp.status_code in (429, 503):
                retry_after = resp.headers.get("Retry-After")
                try:
                    backoff = max(float(retry_after), 3.0) if retry_after else (3.0 * (2 ** attempt))
                except ValueError:
                    backoff = 3.0 * (2 ** attempt)
                backoff = min(backoff, 30.0)
                _trigger_cooldown(backoff)
                last_status = f"http_{resp.status_code}"
                continue
            if resp.status_code != 200 or not resp.content:
                return url, None, f"http_{resp.status_code}"
            raw = resp.content
            with Image.open(BytesIO(raw)) as im:
                im.verify()
            with Image.open(BytesIO(raw)) as im2:
                w, h = im2.size
                fmt = (im2.format or "JPEG").upper()
            if w <= 5 or h <= 5:
                return url, None, "tiny_placeholder"
            ext_map = {"JPEG": ".jpg", "JPG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}
            ext = ext_map.get(fmt, ".jpg")
            fname = f"stpd_dl_{digest}{ext}"
            dest = os.path.join(uploads_dir, fname)
            with open(dest, "wb") as f:
                f.write(raw)
            if cache_dir:
                os.makedirs(cache_dir, exist_ok=True)
                shutil.copy2(dest, os.path.join(cache_dir, fname))
            return url, fname, "ok"
        except Exception as exc:
            last_status = type(exc).__name__
            time.sleep(1.5 * (attempt + 1))
    return url, None, last_status


def _stage_and_download_covers(bundle_dir, db_path, cache_dir=None, skip_http_download=False):
    uploads_dir = os.path.join(bundle_dir, "static", "uploads")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    bib_rows = conn.execute("SELECT id, image FROM bibliografi").fetchall()
    buku_cols = {r[1] for r in conn.execute("PRAGMA table_info(buku)").fetchall()}
    has_gambar_sampul = "gambar_sampul" in buku_cols

    # 1. Dereference & copy existing local STPD covers
    copied_local = 0
    missing_local = []
    seen_dests = set()
    http_urls = {}

    for r in bib_rows:
        img_val = (r["image"] or "").strip()
        if not img_val or img_val in ("tidak_ada", "NOT_FOUND", "RATE_LIMIT"):
            continue
        if img_val.startswith("http"):
            http_urls.setdefault(img_val, []).append(r["id"])
            continue
        real_src, dst_rel = _resolve_local_cover_source(img_val)
        if real_src:
            dest_path = os.path.join(bundle_dir, dst_rel)
            if dest_path not in seen_dests:
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                shutil.copy2(real_src, dest_path)
                seen_dests.add(dest_path)
                copied_local += 1
        else:
            missing_local.append(img_val)

    # Also copy any extra local gambar_sampul on buku table
    if has_gambar_sampul:
        for r in conn.execute("SELECT DISTINCT gambar_sampul FROM buku WHERE gambar_sampul IS NOT NULL AND gambar_sampul != ''").fetchall():
            val = (r[0] or "").strip()
            if val.startswith("http"):
                http_urls.setdefault(val, [])
            elif val not in ("tidak_ada", "NOT_FOUND", "RATE_LIMIT"):
                real_src, dst_rel = _resolve_local_cover_source(val)
                if real_src:
                    dest_path = os.path.join(bundle_dir, dst_rel)
                    if dest_path not in seen_dests:
                        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                        shutil.copy2(real_src, dest_path)
                        seen_dests.add(dest_path)
                        copied_local += 1

    # 2. Download HTTP covers into portable bundle and rewrite ONLY portable DB rows
    downloaded_ok = 0
    download_failed = {}
    url_to_local = {}

    unique_urls = list(http_urls.keys())
    if unique_urls and not skip_http_download:
        sess = requests.Session()
        sess.headers.update({"User-Agent": "StJeromeOfflineBuilder/1.0 (Perpustakaan STPD)"})
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_download_one_cover, sess, u, uploads_dir, cache_dir) for u in unique_urls]
            done_cnt = 0
            for fut in as_completed(futures):
                u, local_name, status = fut.result()
                done_cnt += 1
                if local_name:
                    downloaded_ok += 1
                    url_to_local[u] = local_name
                else:
                    download_failed[status] = download_failed.get(status, 0) + 1
                    url_to_local[u] = ""
                if done_cnt % 200 == 0 or done_cnt == len(unique_urls):
                    print(
                        f"Cover download progress: {done_cnt}/{len(unique_urls)} "
                        f"(ok={downloaded_ok}, failed={sum(download_failed.values())}, elapsed={time.time() - t0:.1f}s)",
                        flush=True,
                    )

        for orig_url, new_val in url_to_local.items():
            conn.execute("UPDATE bibliografi SET image = ? WHERE image = ?", (new_val, orig_url))
            if has_gambar_sampul:
                conn.execute("UPDATE buku SET gambar_sampul = ? WHERE gambar_sampul = ?", (new_val, orig_url))

    # Normalize DELETE journal + VACUUM so zip holds a single clean SQLite file
    conn.execute("PRAGMA journal_mode = DELETE;")
    conn.commit()
    conn.execute("VACUUM;")
    integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
    summary = {
        "stpd_biblio": conn.execute("SELECT COUNT(*) FROM bibliografi").fetchone()[0],
        "stpd_eksemplar": conn.execute("SELECT COUNT(*) FROM eksemplar").fetchone()[0],
        "stpd_buku": conn.execute("SELECT COUNT(*) FROM buku").fetchone()[0],
        "stpd_anggota": conn.execute("SELECT COUNT(*) FROM anggota").fetchone()[0],
        "stpd_sirkulasi": conn.execute("SELECT COUNT(*) FROM sirkulasi").fetchone()[0],
        "stpd_local_covers_copied": copied_local,
        "stpd_missing_local": len(missing_local),
        "stpd_unique_http_urls": len(unique_urls),
        "stpd_http_downloaded_ok": downloaded_ok,
        "stpd_http_failed_breakdown": download_failed,
        "sqlite_integrity": integrity,
    }
    conn.close()
    return summary


def _write_launchers(bundle_dir):
    specs = (
        ("requirements_offline.txt", REQUIREMENTS_OFFLINE, 0o644, "\n"),
        ("run_offline.py", RUN_OFFLINE_PY, 0o755, "\n"),
        ("START_WINDOWS.bat", START_WINDOWS_BAT, 0o644, "\r\n"),
        ("START_LINUX_UBUNTU.sh", START_LINUX_UBUNTU_SH, 0o755, "\n"),
        ("README_OFFLINE_STPD.md", README_OFFLINE_MD, 0o644, "\n"),
    )
    for fname, content, mode, nl in specs:
        dest = os.path.join(bundle_dir, fname)
        normalized = content.replace("\r\n", "\n")
        with open(dest, "w", encoding="utf-8", newline=nl) as f:
            f.write(normalized)
        os.chmod(dest, mode)


def _package_archives(release_dir, bundle_dir):
    zip_path = os.path.join(release_dir, f"{BUNDLE_DIRNAME}_portable.zip")
    tgz_path = os.path.join(release_dir, f"{BUNDLE_DIRNAME}_portable.tar.gz")

    for p in (zip_path, tgz_path):
        if os.path.exists(p):
            os.remove(p)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, _, files in os.walk(bundle_dir):
            for fname in sorted(files):
                abs_p = os.path.join(root, fname)
                arcname = os.path.join(BUNDLE_DIRNAME, os.path.relpath(abs_p, bundle_dir)).replace("\\", "/")
                zinfo = zipfile.ZipInfo.from_file(abs_p, arcname=arcname)
                if fname.endswith(".sh") or fname == "run_offline.py":
                    zinfo.external_attr = (0o100755 << 16)
                with open(abs_p, "rb") as src_f:
                    zf.writestr(zinfo, src_f.read(), compress_type=zipfile.ZIP_DEFLATED)

    with tarfile.open(tgz_path, "w:gz") as tf:
        tf.add(bundle_dir, arcname=BUNDLE_DIRNAME)

    return zip_path, tgz_path


def main():
    parser = argparse.ArgumentParser(description="Build STPD offline portable ZIP/tar.gz")
    parser.add_argument("--release-dir", default=DEFAULT_RELEASE_DIR)
    parser.add_argument("--skip-http-download", action="store_true")
    args = parser.parse_args()

    os.makedirs(args.release_dir, exist_ok=True)
    cache_dir = os.path.join(args.release_dir, "stpd_cover_cache")
    os.makedirs(cache_dir, exist_ok=True)

    bundle_dir = os.path.join(args.release_dir, BUNDLE_DIRNAME)
    prev_uploads = os.path.join(bundle_dir, "static", "uploads")
    if os.path.isdir(prev_uploads):
        for fname in os.listdir(prev_uploads):
            if fname.startswith("stpd_dl_"):
                src_p = os.path.join(prev_uploads, fname)
                dst_p = os.path.join(cache_dir, fname)
                if not os.path.exists(dst_p):
                    shutil.copy2(src_p, dst_p)

    if os.path.exists(bundle_dir):
        shutil.rmtree(bundle_dir)
    os.makedirs(bundle_dir, exist_ok=True)

    print("[1/5] Copying core code & offline static assets...")
    _copy_code_and_static_base(bundle_dir)

    print("[2/5] Creating read-only snapshot & pruning DB strictly to STPD...")
    db_path = os.path.join(bundle_dir, "katalog.db")
    _snapshot_and_prune_db(db_path)

    print("[3/5] Dereferencing local STPD covers & downloading remote cover URLs...")
    summary = _stage_and_download_covers(bundle_dir, db_path, cache_dir=cache_dir, skip_http_download=args.skip_http_download)

    print("[4/5] Writing Windows & Ubuntu launchers...")
    _write_launchers(bundle_dir)

    print("[5/5] Creating .zip and .tar.gz portable archives...")
    zip_path, tgz_path = _package_archives(args.release_dir, bundle_dir)

    print("\n=== STPD OFFLINE PORTABLE BUILD SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"  zip_path: {zip_path} ({os.path.getsize(zip_path) / (1024 * 1024):.2f} MB)")
    print(f"  tgz_path: {tgz_path} ({os.path.getsize(tgz_path) / (1024 * 1024):.2f} MB)")


if __name__ == "__main__":
    main()
