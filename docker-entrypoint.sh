#!/bin/bash
set -e

echo "=== Memulai St. Jerome Container [ENV: ${APP_ENV:-production}] ==="

# Pastikan folder database ada
mkdir -p "$(dirname "$DATABASE_PATH")"

# Inisialisasi schema jika file database belum terbentuk
if [ ! -f "$DATABASE_PATH" ]; then
    echo "[Init] Database belum ada. Menginisialisasi schema di $DATABASE_PATH..."
    python3 -c "import database; database.init_db()"
    
    # Jika mode demo, isi dengan sampel data buku dan anggota
    if [ "$APP_ENV" = "demo" ]; then
        echo "[Seed] Mode Demo terdeteksi. Mengisi data sampel perpustakaan..."
        python3 seed_demo_data.py
    fi
fi

# Optimasi SQLite WAL Mode
sqlite3 "$DATABASE_PATH" "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;" 2>/dev/null || true

echo "=== Siap menjalankan service ==="
exec "$@"
