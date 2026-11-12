#!/bin/bash
set -e

echo "=========================================================="
echo "ST. JEROME ILS - DOCKER INSTALLATION & DEPLOYMENT SCRIPT"
echo "=========================================================="

# 1. Cek & Install Docker jika belum terpasang
if ! command -v docker &> /dev/null; then
    echo "[1/4] Docker belum terpasang. Memasang Docker & Docker Compose..."
    apt-get update
    apt-get install -y docker.io docker-compose-v2
    systemctl enable --now docker
    usermod -aG docker ubuntu
    echo "[✓] Docker berhasil dipasang!"
else
    echo "[1/4] Docker sudah terpasang: $(docker --version)"
fi

cd /root/st_jerome_web

# 2. Build Docker Image
echo "[2/4] Melakukan build image stjerome-ils:latest..."
docker compose build

# 3. Hentikan proses manual lama agar port 5005 & 5006 tidak bentrok
echo "[3/4] Memeriksa proses lama pada port 5005 & 5006..."
# Mencari PID yang berjalan di port 5005 (Flask lama)
OLD_PID_5005=$(lsof -ti :5005 2>/dev/null || true)
if [ -n "$OLD_PID_5005" ]; then
    echo "Menghentikan proses lama di port 5005 (PID: $OLD_PID_5005)..."
    kill -15 $OLD_PID_5005 || true
    sleep 2
fi

# Mencari PID yang berjalan di port 5006
OLD_PID_5006=$(lsof -ti :5006 2>/dev/null || true)
if [ -n "$OLD_PID_5006" ]; then
    echo "Menghentikan proses lama di port 5006 (PID: $OLD_PID_5006)..."
    kill -15 $OLD_PID_5006 || true
    sleep 2
fi

# 4. Jalankan Container Produksi & Demo
echo "[4/4] Menjalankan stjerome-prod dan stjerome-demo via Docker Compose..."
docker compose up -d

echo ""
echo "=========================================================="
echo "DEPLOYMENT BERHASIL!"
echo "=========================================================="
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo ""
echo "Aplikasi Produksi (Live) : http://127.0.0.1:5005 -> https://yosefhomeserver.my.id"
echo "Aplikasi Demo (Sandbox)  : http://127.0.0.1:5006 -> https://yosefhomeserver.my.id/demo"
echo "=========================================================="
