"""Scheduled daily backup runner: creates verified local archive + sends Telegram backup."""

import datetime
import os
import sys

# Ensure project root is on sys.path when invoked from cron
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from ops.db_backup import run_backup as run_local_backup
from ops.telegram_ops import send_alert
import telegram_backup


def main():
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    exit_code = 0

    try:
        local_res = run_local_backup(mode='daily', verify_restore=True)
        print(
            f"[{ts}] Local daily DB backup OK: {local_res['path']} "
            f"({local_res['gz_bytes']} bytes)",
            flush=True,
        )
    except Exception as e:
        print(f"[{ts}] Local daily DB backup FAILED: {e}", file=sys.stderr, flush=True)
        exit_code = 1

    try:
        ok, msg = telegram_backup.run_backup(manual=False)
        label = 'SUCCESS' if ok else 'FAILED'
        print(f"[{ts}] Auto-backup Telegram [{label}]: {msg}", flush=True)
        if not ok:
            send_alert(
                "Telegram Daily Backup Failed",
                msg,
                level='CRITICAL',
                dedup_key='tg_daily_backup_fail',
                cooldown_sec=300,
            )
            exit_code = 1
    except Exception as e:
        print(f"[{ts}] Auto-backup Telegram exception: {e}", file=sys.stderr, flush=True)
        send_alert(
            "Telegram Daily Backup Exception",
            str(e),
            level='CRITICAL',
            dedup_key='tg_daily_backup_exc',
            cooldown_sec=300,
        )
        exit_code = 1

    return exit_code


if __name__ == '__main__':
    sys.exit(main())
