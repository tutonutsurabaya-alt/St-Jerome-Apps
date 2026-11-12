"""Non-blocking, rate-limited Telegram operational alert helpers."""

import hashlib
import html
import os
import sys
import threading
import time

from ops.sqlite_utils import connect_readonly, get_db_path

COOLDOWN_DIR = os.environ.get('STJEROME_ALERT_COOLDOWN_DIR', '/tmp/stjerome_alerts')


def get_telegram_config(db_path=None):
    """Load TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from env or read-only database settings."""
    env_token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    env_chat = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if env_token and env_chat:
        return env_token, env_chat

    path = db_path or get_db_path()
    if not os.path.exists(path):
        return None, None

    conn = None
    try:
        conn = connect_readonly(path, timeout=10.0)
        rows = conn.execute(
            "SELECT kunci, nilai FROM pengaturan_sistem "
            "WHERE kunci IN ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID')"
        ).fetchall()
        vals = {r['kunci']: (str(r['nilai']).strip() if r['nilai'] is not None else '') for r in rows}
        token = vals.get('TELEGRAM_BOT_TOKEN') or None
        chat_id = vals.get('TELEGRAM_CHAT_ID') or None
        return token, chat_id
    except Exception as e:
        print(f"[telegram_ops] Error reading config: {e}", file=sys.stderr, flush=True)
        return None, None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _acquire_cooldown(dedup_key, cooldown_sec):
    """Cross-process rate limit via timestamp marker file; returns True if alert may send."""
    if not dedup_key or cooldown_sec <= 0:
        return True
    try:
        os.makedirs(COOLDOWN_DIR, exist_ok=True)
        digest = hashlib.sha1(dedup_key.encode('utf-8')).hexdigest()
        marker = os.path.join(COOLDOWN_DIR, f"{digest}.ts")
        now = time.time()
        if os.path.exists(marker):
            try:
                with open(marker, 'r', encoding='utf-8') as f:
                    last_ts = float(f.read().strip() or '0')
                if (now - last_ts) < cooldown_sec:
                    return False
            except ValueError:
                pass
        tmp_marker = f"{marker}.{os.getpid()}"
        with open(tmp_marker, 'w', encoding='utf-8') as f:
            f.write(f"{now:.3f}")
        os.replace(tmp_marker, marker)
        return True
    except Exception:
        return True


def _post_message(token, chat_id, text, timeout=6):
    try:
        import requests
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(
            url,
            data={
                'chat_id': chat_id,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': 'true',
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            print(
                f"[telegram_ops] Telegram sendMessage HTTP {resp.status_code}: {resp.text[:200]}",
                file=sys.stderr,
                flush=True,
            )
            return False
        return True
    except Exception as e:
        print(f"[telegram_ops] Failed sending Telegram alert: {e}", file=sys.stderr, flush=True)
        return False


def send_alert(title, message, level='WARNING', dedup_key=None, cooldown_sec=300, async_send=False):
    """Send formatted HTML operational alert to admin Telegram with optional cooldown & async delivery."""
    if os.environ.get('STJEROME_DISABLE_ALERTS') == '1':
        return False

    if not _acquire_cooldown(dedup_key, cooldown_sec):
        return False

    token, chat_id = get_telegram_config()
    if not token or not chat_id:
        return False

    safe_level = html.escape(str(level).upper())
    safe_title = html.escape(str(title))
    safe_msg = html.escape(str(message))
    if len(safe_msg) > 3200:
        safe_msg = safe_msg[:3200] + "\n...(truncated)"

    formatted = f"<b>[{safe_level}] St. Jerome Ops — {safe_title}</b>\n\n<pre>{safe_msg}</pre>"

    if async_send:
        t = threading.Thread(
            target=_post_message,
            args=(token, chat_id, formatted),
            daemon=True,
        )
        t.start()
        return True

    return _post_message(token, chat_id, formatted)
