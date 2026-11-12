"""Authentication, RBAC permissions, security config, and error hooks."""

import datetime
import os
import re
import secrets
import traceback
from functools import wraps
from flask import flash, g, jsonify, redirect, request, session, url_for
import database
from core.features import get_branding, get_feature_config, is_enabled

_OFFLINE_CDN_URL_MAP = {
    'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css': '/static/vendor/fontawesome/css/all.min.css',
    'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css': '/static/vendor/fontawesome/css/all.min.css',
    'https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js': '/static/vendor/qrcode.min.js',
    'https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js': '/static/vendor/alpine.min.js',
    'https://cdn.jsdelivr.net/npm/sweetalert2@11': '/static/vendor/sweetalert2.all.min.js',
    'https://cdn.jsdelivr.net/npm/chart.js': '/static/vendor/chart.umd.min.js',
    'https://cdn.jsdelivr.net/npm/apexcharts': '/static/vendor/apexcharts.min.js',
    'https://cdn.tailwindcss.com': '/static/vendor/tailwindcss.js',
    'https://unpkg.com/html5-qrcode': '/static/js/html5-qr.min.js',
}
_OFFLINE_CDN_RE = re.compile(
    '(' + '|'.join(re.escape(k) for k in sorted(_OFFLINE_CDN_URL_MAP, key=len, reverse=True)) + r')(?=["\'\s])'
)
_GOOGLE_FONTS_LINK_RE = re.compile(
    r'<link[^>]+href=["\']https://fonts\.(?:googleapis|gstatic)\.com[^>]*>\s*',
    re.IGNORECASE,
)
_GOOGLE_FONTS_IMPORT_RE = re.compile(
    r'@import\s+url\(["\']?https://fonts\.googleapis\.com[^"\')]+["\']?\);\s*',
    re.IGNORECASE,
)
_TURNSTILE_SCRIPT_RE = re.compile(
    r'<script[^>]+challenges\.cloudflare\.com/turnstile[^>]*>\s*</script>\s*',
    re.IGNORECASE,
)


def rewrite_offline_html(html_text):
    """Rewrite external CDN asset URLs to local static vendor files for offline mode."""
    cleaned = _GOOGLE_FONTS_LINK_RE.sub('', html_text)
    cleaned = _GOOGLE_FONTS_IMPORT_RE.sub('', cleaned)
    cleaned = _TURNSTILE_SCRIPT_RE.sub('', cleaned)
    return _OFFLINE_CDN_RE.sub(lambda m: _OFFLINE_CDN_URL_MAP[m.group(1)], cleaned)

PERMISSION_MAP = [
    ('/sirkulasi', 'sirkulasi'),
    ('/api/sirkulasi', 'sirkulasi'),
    ('/cek_pinjaman', 'sirkulasi'),
    ('/self_checkout', 'sirkulasi'),
    ('/pos', 'pos'),
    ('/api/pos', 'pos'),
    ('/api/antrean', 'pos'),
    ('/pengadaan_kurikulum', 'koleksi'),
    ('/api/pengadaan_kurikulum', 'koleksi'),
    ('/koleksi', 'koleksi'),
    ('/api/koleksi', 'koleksi'),
    ('/input_buku', 'koleksi'),
    ('/input_majalah', 'koleksi'),
    ('/kelola_sampul', 'koleksi'),
    ('/api/kelola_sampul', 'koleksi'),
    ('/cetak_khusus', 'koleksi'),
    ('/cetak_sirkulasi', 'koleksi'),
    ('/cetak_kartu', 'koleksi'),
    ('/cetak_pdf', 'koleksi'),
    ('/cetak_stiker_tas', 'koleksi'),
    ('/audit_rak', 'koleksi'),
    ('/api/audit_rak', 'koleksi'),
    ('/api/buku', 'koleksi'),
    ('/api/majalah', 'koleksi'),
    ('/api/eksemplar', 'koleksi'),
    ('/api/bibliografi', 'koleksi'),
    ('/api/sampul', 'koleksi'),
    ('/api/sampul_kosong', 'koleksi'),
    ('/export_biblio', 'koleksi'),
    ('/export_item', 'koleksi'),
    ('/import_slims', 'koleksi'),
    ('/import_slims_katalog', 'koleksi'),
    ('/import_slims_loan', 'koleksi'),
    ('/scan', 'baca'),
    ('/scan_baca', 'baca'),
    ('/api/scan', 'baca'),
    ('/api/buku_dibaca', 'baca'),
    ('/laporan_buku_dibaca', 'baca'),
    ('/analisis_kecenderungan_dampak', 'baca'),
    ('/export_baca', 'baca'),
    ('/anggota', 'anggota'),
    ('/api/anggota', 'anggota'),
    ('/analitik_kunjungan', 'anggota'),
    ('/edit_anggota', 'anggota'),
    ('/tambah_anggota', 'anggota'),
    ('/import_anggota', 'anggota'),
    ('/api/kunjungan', 'anggota'),
    ('/api/visitors', 'anggota'),
    ('/export_kunjungan', 'anggota'),
    ('/dashboard', 'laporan'),
    ('/analisis_strategis_terpadu', 'laporan'),
    ('/analisis_peminjaman_dampak', 'laporan'),
    ('/analisis_lanjutan', 'laporan'),
    ('/laporan_ddc', 'laporan'),
    ('/laporan_pertumbuhan', 'laporan'),
    ('/anomali', 'laporan'),
    ('/usulan_buku', 'laporan'),
    ('/api/usulan_buku', 'laporan'),
    ('/laporan_koleksi', 'laporan'),
    ('/laporan_opname', 'laporan'),
    ('/export_akreditasi', 'laporan'),
    ('/settings', 'pengaturan'),
    ('/wa_broadcast', 'pengaturan'),
    ('/api/wa_broadcast', 'pengaturan'),
    ('/api/wa_groups', 'pengaturan'),
    ('/backup', 'pengaturan'),
    ('/api/test_backup', 'pengaturan'),
    ('/pengguna', 'users'),
    ('/api/pengguna', 'users'),
]

_PUBLIC_EXACT_PATHS = {'/login', '/logout', '/opac', '/kiosk', '/kiosk_sirkulasi', '/landing', '/health'}


def get_or_create_secret_key():
    """Fetch persistent Flask secret key from DB or generate and store a new one."""
    conn = database.get_db_connection()
    try:
        row = conn.execute("SELECT nilai FROM pengaturan_sistem WHERE kunci = 'FLASK_SECRET_KEY'").fetchone()
        if row and row['nilai']:
            key = row['nilai']
        else:
            key = secrets.token_hex(32)
            conn.execute("INSERT OR REPLACE INTO pengaturan_sistem (kunci, nilai) VALUES (?, ?)", ('FLASK_SECRET_KEY', key))
            conn.commit()
    except Exception:
        key = secrets.token_hex(32)
    finally:
        conn.close()
    return key


def login_required(f):
    """Redirect unauthenticated browser requests to /login."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


def api_login_required(f):
    """Reject unauthenticated JSON API requests with 401."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return jsonify({'status': 'error', 'message': 'Sesi tidak valid, silakan refresh halaman.'}), 401
        return f(*args, **kwargs)
    return decorated_function


def admin_api_required(f):
    """Require both authenticated session and pengaturan admin permission."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return jsonify({'status': 'error', 'message': 'Sesi tidak valid, silakan login.'}), 401
        if not user_has_permission('pengaturan'):
            return jsonify({'status': 'error', 'message': 'Akses khusus admin.'}), 403
        return f(*args, **kwargs)
    return decorated_function


def feature_required(flag_name, message="Fitur ini dinonaktifkan pada konfigurasi saat ini.", api_status=200):
    """Gate a route on a centralized feature flag without touching endpoint names."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not is_enabled(flag_name):
                if request.path.startswith('/api/') or request.is_json:
                    return jsonify({'status': 'disabled', 'message': message, 'qr': None}), api_status
                if api_status >= 400:
                    return message, api_status
                flash(message, 'error')
                return redirect(url_for('settings') if user_has_permission('pengaturan') else '/')
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def user_has_permission(module_name):
    """Check whether the logged-in session has permission for ``module_name``."""
    if 'logged_in' not in session:
        return False
    username = (session.get('username') or '').lower()
    role = session.get('role') or ''
    if username == 'yosef' or role == 'Master Admin':
        return True
    hak = session.get('hak_akses') or ''
    if hak == 'all':
        return True
    allowed = [m.strip().lower() for m in hak.split(',') if m.strip()]
    return module_name.lower() in allowed


def register_security_hooks(app):
    """Register DB cleanup, context variables, RBAC guard, and 500 error telemetry."""

    @app.teardown_appcontext
    def close_managed_db_connections(exception=None):
        conns = g.pop('_managed_db_conns', None)
        if conns:
            for conn in conns:
                try:
                    conn.close()
                except Exception:
                    pass

    @app.context_processor
    def inject_permissions():
        return {
            'has_perm': user_has_permission,
            'is_demo': (os.environ.get('APP_ENV') == 'demo' or is_enabled('APP_MODE') == 'demo'),
            'features': get_feature_config(),
            'is_feature': is_enabled,
            'branding': get_branding(),
        }

    @app.before_request
    def enforce_rbac():
        path = request.path
        if path.startswith('/static') or path in _PUBLIC_EXACT_PATHS:
            return None
        if path.startswith('/api/opac') or path.startswith('/api/kiosk') or path.startswith('/demo'):
            return None

        if 'logged_in' not in session:
            return None

        curr_username = (session.get('username') or '').lower()
        if curr_username:
            conn = None
            try:
                conn = database.get_db_connection()
                user_row = conn.execute(
                    "SELECT role, hak_akses, nama_lengkap, is_active FROM users WHERE LOWER(username) = ?",
                    (curr_username,),
                ).fetchone()
                if user_row:
                    if user_row['is_active'] == 0:
                        session.clear()
                        flash("Akun Anda telah dinonaktifkan oleh Master Admin.", "error")
                        return redirect(url_for('login'))
                    session['role'] = user_row['role']
                    session['hak_akses'] = user_row['hak_akses']
                    session['nama_lengkap'] = user_row['nama_lengkap']
                else:
                    session['hak_akses'] = 'all' if curr_username == 'yosef' else ''
            except Exception:
                session['hak_akses'] = 'all' if curr_username == 'yosef' else ''
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        if curr_username == 'yosef' or session.get('role') == 'Master Admin' or session.get('hak_akses') == 'all':
            return None

        user_perms = [p.strip().lower() for p in (session.get('hak_akses') or '').split(',') if p.strip()]
        if path == '/' and user_perms == ['pos']:
            return redirect('/pos')

        for prefix, required_perm in PERMISSION_MAP:
            if path == prefix or path.startswith(prefix + '/') or path.startswith(prefix + '?'):
                if required_perm not in user_perms:
                    if path.startswith('/api/'):
                        return jsonify({'status': 'error', 'message': f'Akses ditolak: Anda tidak memiliki izin untuk modul {required_perm.upper()}.'}), 403
                    flash("Akses ditolak: Akun Anda tidak memiliki hak akses untuk membuka modul tersebut.", "error")
                    for perm, target in (
                        ('pos', '/pos'), ('sirkulasi', '/sirkulasi'), ('koleksi', '/koleksi'),
                        ('baca', '/scan'), ('anggota', '/anggota'), ('laporan', '/dashboard'),
                    ):
                        if perm in user_perms:
                            return redirect(target)
                    return redirect('/')

    @app.errorhandler(500)
    def internal_error(error):
        trace = traceback.format_exc()
        req_path = getattr(request, 'path', 'unknown')
        req_method = getattr(request, 'method', 'GET')
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            with open('error_debug.log', 'a', encoding='utf-8') as lf:
                lf.write(f"\n=== 500 ERROR [{ts}] {req_method} {req_path} ===\n")
                lf.write(trace)
        except Exception:
            pass
        try:
            if is_enabled('ENABLE_TELEGRAM_OPS'):
                from ops.telegram_ops import send_alert
                send_alert(
                    f"HTTP 500 on {req_method} {req_path}",
                    f"Time: {ts}\nError: {error}\n\n{trace[-1500:]}",
                    level='CRITICAL',
                    dedup_key=f"http500:{req_path}:{type(error).__name__}",
                    cooldown_sec=300,
                    async_send=True,
                )
        except Exception:
            pass
        payload = {
            'status': 'error',
            'message': 'Internal Server Error: ' + str(error) + " | See error_debug.log",
        }
        if session.get('logged_in'):
            payload['trace'] = trace
        return jsonify(payload), 500

    @app.after_request
    def rewrite_offline_vendor_assets(response):
        """Serve local static vendor assets instead of CDNs when running offline."""
        if get_feature_config().get('APP_MODE') != 'offline_portable':
            return response
        if response.status_code != 200 or response.mimetype != 'text/html' or response.direct_passthrough:
            return response
        try:
            body = response.get_data(as_text=True)
            rewritten = rewrite_offline_html(body)
            if rewritten != body:
                response.set_data(rewritten)
        except Exception:
            pass
        return response
