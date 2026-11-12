#!/bin/bash
set -euo pipefail

PROJECT_ROOT="/root/st_jerome_web"
BACKUP_DAILY="/root/st_jerome_backups/daily"
REMOTE_NAME="${STJEROME_RCLONE_REMOTE:-gdrive}"
LOG_FILE="/var/log/st_jerome_ops/rclone_gdrive.log"
LOCK_FILE="/var/lock/st_jerome_gdrive_sync.lock"
PYTHON_BIN="${PROJECT_ROOT}/venv/bin/python"

mkdir -p "$(dirname "$LOG_FILE")"

if ! command -v rclone >/dev/null 2>&1; then
    echo "[$(date '+%F %T')] rclone not installed; skipping Google Drive sync." >> "$LOG_FILE"
    exit 0
fi

if ! rclone listremotes 2>/dev/null | grep -qx "${REMOTE_NAME}:"; then
    echo "[$(date '+%F %T')] Remote '${REMOTE_NAME}:' not configured yet. Run 'rclone config' to link Google Drive." >> "$LOG_FILE"
    exit 0
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date '+%F %T')] Another Google Drive sync is already running; skipping." >> "$LOG_FILE"
    exit 0
fi

echo "[$(date '+%F %T')] Starting incremental rclone sync to ${REMOTE_NAME}:st_jerome_backups/" >> "$LOG_FILE"

if ! rclone sync "${PROJECT_ROOT}/static/uploads" "${REMOTE_NAME}:st_jerome_backups/static_uploads" \
    --transfers 4 --checkers 8 --log-file="$LOG_FILE" --log-level INFO; then
    "$PYTHON_BIN" -c "import sys; sys.path.insert(0, '${PROJECT_ROOT}'); from ops.telegram_ops import send_alert; send_alert('Google Drive Sync Failed (uploads)', 'Check ${LOG_FILE}', level='CRITICAL', dedup_key='gdrive_uploads_fail', cooldown_sec=300)"
    exit 1
fi

if [ -d "$BACKUP_DAILY" ]; then
    if ! rclone sync "${BACKUP_DAILY}" "${REMOTE_NAME}:st_jerome_backups/db_daily" \
        --transfers 2 --log-file="$LOG_FILE" --log-level INFO; then
        "$PYTHON_BIN" -c "import sys; sys.path.insert(0, '${PROJECT_ROOT}'); from ops.telegram_ops import send_alert; send_alert('Google Drive Sync Failed (db_daily)', 'Check ${LOG_FILE}', level='CRITICAL', dedup_key='gdrive_db_fail', cooldown_sec=300)"
        exit 1
    fi
fi

echo "[$(date '+%F %T')] Google Drive sync completed successfully." >> "$LOG_FILE"
