"""Proactive production health monitor with state-transition Telegram alerts."""

import json
import os
import shutil
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from ops.db_backup import get_backup_root, get_latest_backup_info
from ops.telegram_ops import send_alert

HEALTH_URL = os.environ.get('STJEROME_HEALTH_URL', 'http://127.0.0.1:5005/health')
WA_STATUS_URL = os.environ.get('STJEROME_WA_STATUS_URL', 'http://127.0.0.1:3005/api/status')
DISK_WARN_PERCENT = float(os.environ.get('STJEROME_DISK_WARN_PCT', '85.0'))
DISK_MIN_FREE_GIB = float(os.environ.get('STJEROME_DISK_MIN_GIB', '3.0'))
MAX_BACKUP_AGE_HOURS = float(os.environ.get('STJEROME_MAX_BACKUP_AGE_HOURS', '3.5'))
REMINDER_INTERVAL_SEC = 6 * 3600  # Remind at most every 6h while issue persists


def check_web_health():
    import requests
    try:
        resp = requests.get(HEALTH_URL, timeout=5)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
        if data.get('status') != 'ok' or data.get('db') != 'ok':
            return False, f"Unhealthy payload: {data}"
        return True, f"HTTP 200 (tables={data.get('tables')})"
    except Exception as e:
        return False, f"Web health unreachable: {e}"


def check_wa_bot():
    import requests
    try:
        resp = requests.get(WA_STATUS_URL, timeout=4)
        if resp.status_code != 200:
            return False, f"wa_bot HTTP {resp.status_code}"
        data = resp.json()
        status = data.get('status', 'unknown')
        if status == 'ready':
            return True, f"ready (phone={data.get('phone')})"
        if status == 'qr_ready':
            return False, "Bot belum terhubung (status: qr_ready / menunggu scan QR). Silakan buka menu WA Broadcast atau log pm2 wa_bot untuk scan ulang."
        return False, f"Bot status: {status}"
    except Exception as e:
        return False, f"wa_bot unreachable: {e}"


def check_disk():
    usage = shutil.disk_usage('/')
    used_pct = (usage.used / usage.total) * 100.0
    free_gib = usage.free / (1024 ** 3)
    if used_pct >= DISK_WARN_PERCENT or free_gib < DISK_MIN_FREE_GIB:
        return False, f"Disk usage high: {used_pct:.1f}% used, {free_gib:.2f} GiB free"
    return True, f"{used_pct:.1f}% used ({free_gib:.2f} GiB free)"


def check_backup_freshness():
    info = get_latest_backup_info()
    if info is None:
        return False, "No local .db.gz backup found in /root/st_jerome_backups/"
    age_h = info['age_seconds'] / 3600.0
    if age_h > MAX_BACKUP_AGE_HOURS:
        return False, f"Latest local backup is stale ({age_h:.1f}h old): {info['path']}"
    return True, f"Latest backup {age_h:.2f}h old ({info['size_bytes']} bytes)"


def _load_state(state_path):
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state_path, state):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    tmp_path = f"{state_path}.tmp.{os.getpid()}"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, state_path)


def _wa_bot_enabled():
    env_val = os.environ.get('STJEROME_ENABLE_WA_BOT')
    if env_val is not None:
        return env_val.strip().lower() in ('1', 'true', 'yes', 'on')
    try:
        from core.features import is_enabled
        return is_enabled('ENABLE_WA_BOT')
    except Exception:
        return False


def run_checks(notify=True, state_path=None):
    if state_path is None:
        state_path = os.path.join(get_backup_root(), 'monitor_state.json')

    checks = {
        'web_app': check_web_health(),
        'disk': check_disk(),
        'local_backup': check_backup_freshness(),
    }
    if _wa_bot_enabled():
        checks['wa_bot'] = check_wa_bot()

    prev = _load_state(state_path)
    now = time.time()
    new_state = {}

    for name, (ok, detail) in checks.items():
        old_entry = prev.get(name, {'status': 'ok', 'last_notified_at': 0.0})
        old_status = old_entry.get('status', 'ok')
        last_notified = float(old_entry.get('last_notified_at', 0.0))

        if not ok:
            should_notify = (
                old_status == 'ok'
                or (now - last_notified) >= REMINDER_INTERVAL_SEC
            )
            if notify and should_notify:
                send_alert(
                    f"Health Alert: {name}",
                    detail,
                    level='CRITICAL' if name in ('web_app', 'local_backup') else 'WARNING',
                    dedup_key=None,
                    cooldown_sec=0,
                )
                last_notified = now
            new_state[name] = {
                'status': 'fail',
                'detail': detail,
                'last_notified_at': last_notified,
                'updated_at': now,
            }
        else:
            if notify and old_status == 'fail':
                send_alert(
                    f"Recovered: {name}",
                    detail,
                    level='INFO',
                    dedup_key=None,
                    cooldown_sec=0,
                )
                last_notified = now
            new_state[name] = {
                'status': 'ok',
                'detail': detail,
                'last_notified_at': last_notified,
                'updated_at': now,
            }

    _save_state(state_path, new_state)
    return checks


def main():
    dry = '--no-notify' in sys.argv
    checks = run_checks(notify=not dry)
    all_ok = True
    for name, (ok, detail) in checks.items():
        if not ok:
            all_ok = False
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
