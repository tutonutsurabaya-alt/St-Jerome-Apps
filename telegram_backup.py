"""Daily & manual database backup to Telegram (WAL-safe, integrity-verified, <50 MB cap)."""

import datetime
import html
import os
import tempfile
import zipfile

from ops.sqlite_utils import create_verified_snapshot, get_db_path
from ops.telegram_ops import get_telegram_config

TELEGRAM_MAX_BYTES = 49 * 1024 * 1024  # Bot API sendDocument limit is 50 MB


def get_config():
    """Return (bot_token, chat_id) configured for Telegram backups."""
    return get_telegram_config()


def run_backup(manual=False):
    """Create integrity-checked WAL snapshot, compress katalog.db only, and upload to Telegram."""
    token, chat_id = get_config()
    if not token or not chat_id:
        return False, "Telegram Token atau Chat ID belum dikonfigurasi."

    db_path = get_db_path()
    if not os.path.exists(db_path):
        return False, "Database tidak ditemukan."

    now = datetime.datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    zip_filename = f"backup_stjerome_{timestamp}.zip"

    try:
        import requests

        with tempfile.TemporaryDirectory(prefix="stjerome_tg_bak_") as tmpdir:
            snapshot_db = os.path.join(tmpdir, "katalog.db")
            zip_path = os.path.join(tmpdir, zip_filename)

            raw_bytes, _counts = create_verified_snapshot(db_path, snapshot_db)
            db_size_mib = raw_bytes / (1024 * 1024)

            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                zipf.write(snapshot_db, arcname='katalog.db')

            zip_bytes = os.path.getsize(zip_path)
            zip_size_mib = zip_bytes / (1024 * 1024)
            if zip_bytes > TELEGRAM_MAX_BYTES:
                return (
                    False,
                    f"Ukuran file backup ({zip_size_mib:.2f} MiB) melebihi batas upload Bot Telegram (50 MB).",
                )

            url = f"https://api.telegram.org/bot{token}/sendDocument"
            status_text = 'Manual Backup' if manual else 'Auto Backup (Harian)'
            caption = (
                f"<b>Backup Database St. Jerome Library</b>\n\n"
                f"Tanggal: {html.escape(now.strftime('%d %b %Y %H:%M WIB'))}\n"
                f"Status: {html.escape(status_text)}\n"
                f"Ukuran: {db_size_mib:.2f} MiB -&gt; {zip_size_mib:.2f} MiB (ZIP)\n"
                f"Isi: <code>katalog.db</code> (terverifikasi <code>integrity_check=ok</code>; "
                f"folder <code>static/uploads</code> disinkronkan terpisah agar di bawah batas 50 MB)"
            )

            with open(zip_path, 'rb') as f:
                response = requests.post(
                    url,
                    data={
                        'chat_id': chat_id,
                        'caption': caption,
                        'parse_mode': 'HTML',
                    },
                    files={'document': (zip_filename, f, 'application/zip')},
                    timeout=120,
                )

            if response.status_code == 200:
                return True, f"Backup berhasil dikirim ke Telegram ({zip_size_mib:.2f} MiB)."
            return False, f"Gagal mengirim. Kode: {response.status_code}, Response: {response.text}"

    except ImportError:
        return False, "Library requests tidak ditemukan. Jalankan 'pip install requests'"
    except Exception as e:
        return False, str(e)


if __name__ == "__main__":
    print("Memulai proses backup...", flush=True)
    success, msg = run_backup()
    print(f"[{'SUCCESS' if success else 'FAILED'}] {msg}", flush=True)
