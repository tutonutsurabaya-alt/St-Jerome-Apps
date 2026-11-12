"""Centralized Feature Flags and library branding ("1 Core Banyak Wajah")."""

import os
import database

FEATURE_FLAGS = (
    'ENABLE_WA_BOT',
    'ENABLE_TELEGRAM_OPS',
    'ENABLE_POS_FOTOCOPY',
    'ENABLE_AI_ASSISTANT',
    'ENABLE_AUTOCOVER_ONLINE',
    'ENABLE_SYNC_JADWAL_PROXY',
)

SETTINGS_KEYS = (
    'APP_MODE',
    'LIBRARY_NAME',
    'DEFAULT_LOKASI',
) + FEATURE_FLAGS

_ONLINE_ONLY_FLAGS = {
    'ENABLE_AI_ASSISTANT',
    'ENABLE_AUTOCOVER_ONLINE',
    'ENABLE_SYNC_JADWAL_PROXY',
}


def parse_bool(value, default=False):
    """Parse boolean flag string from env vars or SQLite settings."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ('1', 'true', 'yes', 'on'):
        return True
    if s in ('0', 'false', 'no', 'off'):
        return False
    return default


def _default_for_mode(app_mode):
    is_offline = (app_mode == 'offline_portable')
    return {
        'ENABLE_WA_BOT': False,  # Disabled by default; ops use Telegram.
        'ENABLE_TELEGRAM_OPS': not is_offline,
        'ENABLE_POS_FOTOCOPY': not is_offline,
        'ENABLE_AI_ASSISTANT': not is_offline,
        'ENABLE_AUTOCOVER_ONLINE': not is_offline,
        'ENABLE_SYNC_JADWAL_PROXY': not is_offline,
    }


def _read_db_settings():
    if not database.DB_NAME or not os.path.exists(database.DB_NAME):
        return {}
    conn = None
    try:
        conn = database.get_db_connection()
        placeholders = ','.join('?' for _ in SETTINGS_KEYS)
        rows = conn.execute(
            f"SELECT kunci, nilai FROM pengaturan_sistem WHERE kunci IN ({placeholders})",
            SETTINGS_KEYS,
        ).fetchall()
        return {row['kunci']: row['nilai'] for row in rows}
    except Exception:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _compute_feature_config():
    db_cfg = _read_db_settings()

    app_mode = (
        os.environ.get('STJEROME_APP_MODE')
        or os.environ.get('APP_MODE')
        or db_cfg.get('APP_MODE')
        or ('demo' if os.environ.get('APP_ENV') == 'demo' else 'online')
    ).strip().lower()

    if app_mode not in ('online', 'offline_portable', 'demo'):
        app_mode = 'online'

    defaults = _default_for_mode(app_mode)
    library_name = (
        os.environ.get('STJEROME_LIBRARY_NAME')
        or db_cfg.get('LIBRARY_NAME')
        or 'Perpustakaan IMAVI'
    ).strip() or 'Perpustakaan IMAVI'

    default_lokasi = (
        os.environ.get('STJEROME_DEFAULT_LOKASI')
        or db_cfg.get('DEFAULT_LOKASI')
        or ('STPD' if 'STPD' in library_name.upper() else 'IMAVI')
    ).strip() or 'IMAVI'

    resolved = {
        'APP_MODE': app_mode,
        'LIBRARY_NAME': library_name,
        'DEFAULT_LOKASI': default_lokasi,
    }

    for flag in FEATURE_FLAGS:
        env_override = os.environ.get(f'STJEROME_{flag}')
        if env_override is not None:
            resolved[flag] = parse_bool(env_override, defaults[flag])
        elif flag in db_cfg and db_cfg[flag] is not None and str(db_cfg[flag]).strip() != '':
            val = parse_bool(db_cfg[flag], defaults[flag])
            if app_mode == 'offline_portable' and flag in _ONLINE_ONLY_FLAGS:
                val = False
            resolved[flag] = val
        else:
            resolved[flag] = defaults[flag]

    return resolved


def get_feature_config():
    """Return resolved feature flags, caching per-request on ``flask.g`` when in request scope."""
    try:
        from flask import has_request_context, g
        if has_request_context():
            cached = getattr(g, '_stj_feature_config', None)
            if cached is not None:
                return cached
            cfg = _compute_feature_config()
            g._stj_feature_config = cfg
            return cfg
    except Exception:
        pass
    return _compute_feature_config()


def is_enabled(flag_name):
    """Return True if a named feature flag is currently enabled."""
    return bool(get_feature_config().get(flag_name, False))


def get_branding():
    """Return UI branding values derived from current feature config."""
    cfg = get_feature_config()
    return {
        'app_name': 'St. Jerome',
        'library_name': cfg['LIBRARY_NAME'],
        'default_lokasi': cfg['DEFAULT_LOKASI'],
        'app_mode': cfg['APP_MODE'],
    }
