#!/bin/bash
set -euo pipefail
cd /root/st_jerome_web
exec /root/st_jerome_web/venv/bin/gunicorn \
    --preload \
    --worker-class gthread \
    --workers 2 \
    --threads 4 \
    --bind 0.0.0.0:5005 \
    --timeout 180 \
    --graceful-timeout 30 \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --access-logfile - \
    --error-logfile - \
    app:app
